"""
Acceso a la base de resultados de Radar con RLS efectiva (§6.4).

- Un solo pool, con el rol radar_app (NOSUPERUSER, NOBYPASSRLS, no dueño).
  connect() lo verifica y aborta si no es así: con un superusuario RLS no
  protegería nada.
- Todo acceso a tablas con tenant pasa por `tenant_tx(tenant_id)`: abre una
  transacción y fija app.tenant_id con set_config(..., is_local=true). El valor
  muere con la transacción, así que una conexión devuelta al pool no conserva
  el tenant.
- `sin_tenant()` es para las funciones SECURITY DEFINER de administración
  (crear/listar tenants, buscar usuarios por email); sobre cualquier tabla con
  RLS devuelve cero filas.
- Nada acá traga errores. app/services/db.py (execute()/fetch() best-effort)
  no se usa en Radar.
"""

import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import asyncpg


def _normalizar_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
    if dsn.startswith("postgres://"):
        return dsn.replace("postgres://", "postgresql://", 1)
    return dsn


class RadarDB:
    def __init__(self, dsn: str, max_size: int = 5):
        self._dsn = _normalizar_dsn(dsn)
        self._max_size = max_size
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        if not self._dsn:
            raise RuntimeError("RADAR_DATABASE_URL vacía")
        pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=self._max_size, timeout=10)
        async with pool.acquire() as con:
            fila = await con.fetchrow(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            )
        if fila is None or fila["rolsuper"] or fila["rolbypassrls"]:
            await pool.close()
            raise RuntimeError(
                "RADAR_DATABASE_URL conecta con un rol superusuario o BYPASSRLS: RLS no tendría efecto"
            )
        self._pool = pool

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("RadarDB sin conectar")
        return self._pool

    @asynccontextmanager
    async def tenant_tx(self, tenant_id: uuid.UUID) -> AsyncIterator[asyncpg.Connection]:
        if not isinstance(tenant_id, uuid.UUID):
            raise TypeError("tenant_tx requiere un uuid.UUID")
        async with self.pool.acquire() as con:
            async with con.transaction():
                await con.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
                yield con

    @asynccontextmanager
    async def sin_tenant(self) -> AsyncIterator[asyncpg.Connection]:
        async with self.pool.acquire() as con:
            async with con.transaction():
                yield con

    async def salud(self) -> dict:
        try:
            async with self.sin_tenant() as con:
                await con.fetchval("SELECT 1")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": type(e).__name__}
