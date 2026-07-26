"""
RAG con pgvector: búsqueda semántica de productos + base de conocimiento.

Todo best-effort: si Postgres o los embeddings no están disponibles, las
funciones devuelven [] y el sistema cae a la búsqueda fuzzy / respuesta normal.
"""

import logging
from typing import Optional

from app.services.db import to_vector

logger = logging.getLogger(__name__)


class RagService:
    def __init__(self, db, emb):
        self._db = db
        self._emb = emb

    def enabled(self) -> bool:
        return self._db.available() and self._emb.enabled

    # ── Catálogo: indexación y búsqueda semántica ────────────────────────────

    async def reindex_catalogo(self, skus: list, batch: int = 256) -> int:
        """
        Embebe el catálogo y lo guarda en sku_embeddings (upsert).
        `skus` es la lista de dicts de sku_service._to_response o los SKU crudos.
        Devuelve la cantidad indexada.
        """
        if not self.enabled():
            return 0
        # Texto a embeber: nombre + marca + laboratorio + categoría
        items = []
        for s in skus:
            nombre = s.get("nombre") or s.get("sku_nombre") or ""
            extra = " ".join(filter(None, [s.get("marca", ""), s.get("laboratorio", ""), s.get("categoria", "")]))
            texto = f"{nombre} {extra}".strip()
            if texto:
                items.append((str(s.get("sku_id")), nombre, s.get("requiere_receta", "no"),
                              float(s.get("precio") or s.get("precio_venta") or 0), texto))

        total = 0
        for i in range(0, len(items), batch):
            chunk = items[i:i + batch]
            vectors = await self._emb.embed([c[4] for c in chunk])
            if not vectors:
                break
            for (sku_id, nombre, receta, precio, _texto), vec in zip(chunk, vectors):
                await self._db.execute(
                    """INSERT INTO sku_embeddings (sku_id, nombre, requiere_receta, precio, embedding)
                       VALUES ($1, $2, $3, $4, $5::vector)
                       ON CONFLICT (sku_id) DO UPDATE SET
                         nombre=$2, requiere_receta=$3, precio=$4, embedding=$5::vector""",
                    sku_id, nombre, receta, precio, to_vector(vec),
                )
                total += 1
        logger.info(f"RAG: catálogo indexado, {total} productos")
        return total

    async def count_indexed(self) -> int:
        rows = await self._db.fetch("SELECT count(*) AS n FROM sku_embeddings")
        return rows[0]["n"] if rows else 0

    async def buscar_semantico(self, query: str, n: int = 3) -> list[dict]:
        """Productos más parecidos semánticamente a la consulta."""
        if not self.enabled() or not query.strip():
            return []
        vec = await self._emb.embed_one(query)
        if not vec:
            return []
        rows = await self._db.fetch(
            """SELECT sku_id, nombre, requiere_receta, precio,
                      1 - (embedding <=> $1::vector) AS score
               FROM sku_embeddings
               ORDER BY embedding <=> $1::vector
               LIMIT $2""",
            to_vector(vec), n,
        )
        return [
            {"sku_id": r["sku_id"], "nombre": r["nombre"],
             "requiere_receta": r["requiere_receta"], "precio": float(r["precio"] or 0),
             "score": float(r["score"])}
            for r in rows
        ]

    # ── Base de conocimiento (FAQ / info) ────────────────────────────────────

    async def kb_add(self, titulo: str, contenido: str) -> bool:
        if not self.enabled() or not contenido.strip():
            return False
        vec = await self._emb.embed_one(f"{titulo}\n{contenido}")
        if not vec:
            return False
        await self._db.execute(
            "INSERT INTO kb_documents (titulo, contenido, embedding) VALUES ($1, $2, $3::vector)",
            titulo, contenido, to_vector(vec),
        )
        return True

    async def kb_list(self) -> list[dict]:
        rows = await self._db.fetch(
            "SELECT id, titulo, contenido, created_at FROM kb_documents ORDER BY created_at DESC"
        )
        return [{"id": r["id"], "titulo": r["titulo"], "contenido": r["contenido"],
                 "ts": r["created_at"].isoformat()} for r in rows]

    async def kb_delete(self, doc_id: int) -> bool:
        r = await self._db.execute("DELETE FROM kb_documents WHERE id = $1", doc_id)
        return r is not None

    async def kb_search(self, query: str, n: int = 3, min_score: float = 0.3) -> list[dict]:
        """Fragmentos de la base de conocimiento relevantes a la consulta."""
        if not self.enabled() or not query.strip():
            return []
        vec = await self._emb.embed_one(query)
        if not vec:
            return []
        rows = await self._db.fetch(
            """SELECT titulo, contenido, 1 - (embedding <=> $1::vector) AS score
               FROM kb_documents
               ORDER BY embedding <=> $1::vector
               LIMIT $2""",
            to_vector(vec), n,
        )
        return [
            {"titulo": r["titulo"], "contenido": r["contenido"], "score": float(r["score"])}
            for r in rows if float(r["score"]) >= min_score
        ]


_instance: Optional[RagService] = None


def get_rag_service(db, emb) -> RagService:
    global _instance
    if _instance is None:
        _instance = RagService(db, emb)
    return _instance
