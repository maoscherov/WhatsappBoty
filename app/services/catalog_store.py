"""
Persistencia del catálogo ERP en Postgres (catalog_items / catalog_extras).

A diferencia del resto de los stores (best-effort), acá los errores de DB se
PROPAGAN: el agente reintenta ante un no-2xx, y un 200 sobre un write fallido
desincroniza la sucursal hasta el próximo manifiesto.
"""

import logging
from typing import Optional

from app.models.sync import CatalogItemIn, ManifestEntryIn
from app.services.catalog_rules import derivar_requiere_receta

logger = logging.getLogger(__name__)

_UPSERT_SQL = """
INSERT INTO catalog_items (branch_id, external_id, hash, barcodes, troquel, name,
    brand, drug, form, category, rubro, subrubro, therapeutic_actions, price,
    stock, visible, active, requiere_receta, source, updated_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
        $17, $18, $19, now())
ON CONFLICT (branch_id, external_id) DO UPDATE SET
    hash = EXCLUDED.hash,
    barcodes = EXCLUDED.barcodes,
    troquel = EXCLUDED.troquel,
    name = EXCLUDED.name,
    brand = EXCLUDED.brand,
    drug = EXCLUDED.drug,
    form = EXCLUDED.form,
    category = EXCLUDED.category,
    rubro = EXCLUDED.rubro,
    subrubro = EXCLUDED.subrubro,
    therapeutic_actions = EXCLUDED.therapeutic_actions,
    price = EXCLUDED.price,
    stock = EXCLUDED.stock,
    visible = EXCLUDED.visible,
    active = EXCLUDED.active,
    requiere_receta = EXCLUDED.requiere_receta,
    source = EXCLUDED.source,
    updated_at = now()
WHERE catalog_items.hash IS DISTINCT FROM EXCLUDED.hash
   OR catalog_items.active IS DISTINCT FROM EXCLUDED.active
"""
# La segunda condición del WHERE reactiva productos que el manifiesto había
# desactivado aunque su hash no haya cambiado (el agente los manda de nuevo
# como si fueran nuevos cuando reaparecen en el ERP).


def _fila(branch_id: str, item: CatalogItemIn, source: str) -> tuple:
    requiere = derivar_requiere_receta(item.category, item.rubro, item.subrubro, item.name)
    return (
        branch_id, item.external_id, item.hash, item.barcodes, item.troquel,
        item.name, item.brand, item.drug, item.form, item.category, item.rubro,
        item.subrubro, item.therapeutic_actions, item.price, item.stock,
        item.visible, item.active, requiere, source,
    )


class CatalogStore:
    def __init__(self, db):
        self._db = db

    async def upsert_items(self, branch_id: str, items: list[CatalogItemIn],
                           source: str = "observer-gestion") -> tuple[int, int]:
        """
        Upsert de un lote en una transacción. Devuelve (received, upserted).
        Idempotente: reenviar el mismo lote da upserted = 0 (el WHERE del
        ON CONFLICT no matchea). Lanza ante error de DB.
        """
        upserted = 0
        async with self._db.transaction() as con:
            for item in items:
                tag = await con.execute(_UPSERT_SQL, *_fila(branch_id, item, source))
                # command tag: "INSERT 0 1" (afectó) | "INSERT 0 0" (sin cambios)
                if tag and tag.rsplit(" ", 1)[-1] == "1":
                    upserted += 1
            await con.execute(
                "UPDATE branches SET last_catalog_push_at = now() WHERE branch_id = $1",
                branch_id)
        return len(items), upserted

    async def full_manifest(self, branch_id: str,
                            entries: list[ManifestEntryIn]) -> tuple[list[str], int]:
        """
        Reconciliación total: (resend, deactivated).
        resend = external_id cuyo hash difiere o que el servidor no tiene;
        deactivated = filas activas que ya no están en el manifiesto.
        Lanza ante error de DB.
        """
        ids_manifiesto = [e.external_id for e in entries]
        async with self._db.transaction() as con:
            rows = await con.fetch(
                "SELECT external_id, hash FROM catalog_items WHERE branch_id = $1",
                branch_id)
            guardados = {r["external_id"]: r["hash"] for r in rows}
            resend = [e.external_id for e in entries
                      if guardados.get(e.external_id) != e.hash]
            tag = await con.execute(
                """
                UPDATE catalog_items SET active = false, updated_at = now()
                WHERE branch_id = $1 AND active AND NOT (external_id = ANY($2))
                """,
                branch_id, ids_manifiesto)
            deactivated = int(tag.rsplit(" ", 1)[-1]) if tag else 0
            await con.execute(
                "UPDATE branches SET last_manifest_at = now() WHERE branch_id = $1",
                branch_id)
        return resend, deactivated

    # ── Lectura para el bot (best-effort: usa fetch, que traga errores) ──────

    async def load_rows(self, branch_id: str) -> tuple[list[dict], dict[str, dict]]:
        """
        (filas de catalog_items, extras por external_id) de una sucursal.
        Trae TODAS las filas (activas o no): las inactivas se cargan como
        pausadas para que get_by_id siga encontrando un pendiente viejo.
        """
        rows = await self._db.fetch(
            "SELECT * FROM catalog_items WHERE branch_id = $1", branch_id)
        extras_rows = await self._db.fetch(
            "SELECT * FROM catalog_extras WHERE branch_id = $1", branch_id)
        extras = {r["external_id"]: dict(r) for r in extras_rows}
        return [dict(r) for r in rows], extras

    async def count_items(self, branch_id: str) -> int:
        row = await self._db.fetchrow(
            "SELECT COUNT(*) AS n FROM catalog_items WHERE branch_id = $1", branch_id)
        return int(row["n"]) if row else 0

    async def update_stock(self, branch_id: str, external_id: str,
                           stock: int, price=None) -> None:
        """Actualiza stock (y precio si vino) tras un lookup en vivo. Best-effort."""
        await self._db.execute(
            """
            UPDATE catalog_items SET stock = $3,
                   price = COALESCE($4, price), updated_at = now()
            WHERE branch_id = $1 AND external_id = $2
            """,
            branch_id, external_id, stock, price)

    async def set_extras(self, branch_id: str, external_id: str, **campos) -> None:
        """
        Upsert de catalog_extras (lo manual que sobrevive a los syncs).
        Campos aceptados: requiere_receta_override, pausado_manual, imagen_url,
        ventas_mes, prom_semanal, clasificacion, tipo_producto.
        """
        permitidos = ("requiere_receta_override", "pausado_manual", "imagen_url",
                      "ventas_mes", "prom_semanal", "clasificacion", "tipo_producto")
        datos = {k: v for k, v in campos.items() if k in permitidos}
        if not datos:
            return
        cols = ", ".join(datos)
        marcas = ", ".join(f"${i + 3}" for i in range(len(datos)))
        sets = ", ".join(f"{k} = EXCLUDED.{k}" for k in datos)
        await self._db.execute(
            f"""
            INSERT INTO catalog_extras (branch_id, external_id, {cols}, updated_at)
            VALUES ($1, $2, {marcas}, now())
            ON CONFLICT (branch_id, external_id) DO UPDATE SET {sets}, updated_at = now()
            """,
            branch_id, external_id, *datos.values())


_instance: Optional[CatalogStore] = None


def get_catalog_store(db) -> CatalogStore:
    global _instance
    if _instance is None:
        _instance = CatalogStore(db)
    return _instance
