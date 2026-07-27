"""
Conexión a PostgreSQL (asyncpg).

El esquema lo manejan las migraciones de Alembic (migrations/), no este módulo.

Degradación elegante: si DATABASE_URL no está configurada o Postgres no
responde, `available()` devuelve False y el resto del sistema sigue con Redis.

Tablas (ver migración 0001):
  messages        → historial permanente de conversaciones
  sku_embeddings  → embeddings del catálogo (pgvector) para búsqueda semántica
  kb_documents    → base de conocimiento (FAQ / info de la farmacia) con embeddings
"""

import logging
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

EMBED_DIM = 1536  # text-embedding-3-small (debe coincidir con la migración)


def _normalize_dsn(dsn: str) -> str:
    """asyncpg no acepta el prefijo +driver de SQLAlchemy."""
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
    if dsn.startswith("postgres://"):
        return dsn.replace("postgres://", "postgresql://", 1)
    return dsn


def to_vector(values: list[float]) -> str:
    """Formatea un embedding como literal de pgvector: '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{v:.7f}" for v in values) + "]"


class Database:
    def __init__(self, dsn: str):
        self._dsn = _normalize_dsn(dsn)
        self._pool: Optional[asyncpg.Pool] = None
        self._ok: Optional[bool] = None

    async def connect(self) -> bool:
        """Crea el pool. Devuelve False si no hay DSN o falla la conexión."""
        if not self._dsn:
            self._ok = False
            return False
        try:
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5, timeout=10)
            async with self._pool.acquire() as con:
                await con.execute("SELECT 1")
            self._ok = True
            logger.info("PostgreSQL conectado")
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
