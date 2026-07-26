"""
Conexión a PostgreSQL (asyncpg) + esquema con pgvector.

Degradación elegante: si DATABASE_URL no está configurada o Postgres no
responde, `available()` devuelve False y el resto del sistema sigue con Redis.

Tablas:
  messages        → historial permanente de conversaciones
  sku_embeddings  → embeddings del catálogo (pgvector) para búsqueda semántica
  kb_documents    → base de conocimiento (FAQ / info de la farmacia) con embeddings
"""

import logging
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

EMBED_DIM = 1536  # text-embedding-3-small

_SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS messages (
    id          BIGSERIAL PRIMARY KEY,
    phone       TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_messages_phone ON messages (phone, created_at);

CREATE TABLE IF NOT EXISTS sku_embeddings (
    sku_id          TEXT PRIMARY KEY,
    nombre          TEXT,
    requiere_receta TEXT,
    precio          DOUBLE PRECISION,
    embedding       vector({EMBED_DIM})
);
CREATE INDEX IF NOT EXISTS idx_sku_emb ON sku_embeddings
    USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS kb_documents (
    id          BIGSERIAL PRIMARY KEY,
    titulo      TEXT,
    contenido   TEXT NOT NULL,
    embedding   vector({EMBED_DIM}),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kb_emb ON kb_documents
    USING hnsw (embedding vector_cosine_ops);
"""


def to_vector(values: list[float]) -> str:
    """Formatea un embedding como literal de pgvector: '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{v:.7f}" for v in values) + "]"


class Database:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool: Optional[asyncpg.Pool] = None
        self._ok: Optional[bool] = None

    async def connect(self) -> bool:
        """Crea el pool y el esquema. Devuelve False si no hay DSN o falla."""
        if not self._dsn:
            self._ok = False
            return False
        try:
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5, timeout=10)
            async with self._pool.acquire() as con:
                await con.execute(_SCHEMA)
            self._ok = True
            logger.info("PostgreSQL conectado y esquema listo")
            return True
        except Exception as e:
            logger.warning(f"PostgreSQL no disponible ({type(e).__name__}: {e}) — se usa solo Redis")
            self._ok = False
            self._pool = None
            return False

    def available(self) -> bool:
        return bool(self._ok and self._pool)

    async def execute(self, query: str, *args):
        if not self.available():
            return None
        try:
            async with self._pool.acquire() as con:
                return await con.execute(query, *args)
        except Exception as e:
            logger.error(f"DB execute error: {e}")
            return None

    async def fetch(self, query: str, *args) -> list:
        if not self.available():
            return []
        try:
            async with self._pool.acquire() as con:
                return await con.fetch(query, *args)
        except Exception as e:
            logger.error(f"DB fetch error: {e}")
            return []

    async def close(self):
        if self._pool:
            await self._pool.close()


_instance: Optional[Database] = None


def get_db(dsn: str = "") -> Database:
    global _instance
    if _instance is None:
        _instance = Database(dsn)
    return _instance
