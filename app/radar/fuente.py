"""
Almacén de fuente (§6.4): el Postgres separado donde vivirán el texto, los ids
de proveedor y la identidad de contactos (tramo 3). En este tramo solo se
cablea el almacén PERMANENTE: conexión, marcador de esquema y health check.
El purgable es una política opcional del tramo 6.
"""

from typing import Optional

import asyncpg

from app.radar.db import _normalizar_dsn

ALMACENES_DISPONIBLES = frozenset({"permanente"})


class FuenteStore:
    def __init__(self, dsn: str, max_size: int = 3):
        self._dsn = _normalizar_dsn(dsn)
        self._max_size = max_size
        self._pool: Optional[asyncpg.Pool] = None
        self._almacen: str = ""

    async def connect(self) -> None:
        if not self._dsn:
            raise RuntimeError("RADAR_FUENTE_DATABASE_URL vacía")
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=self._max_size, timeout=10)
        async with self._pool.acquire() as con:
            almacen = await con.fetchval("SELECT valor FROM fuente_meta WHERE clave = 'almacen'")
        if almacen not in ALMACENES_DISPONIBLES:
            await self._pool.close()
            self._pool = None
            raise RuntimeError(f"RADAR_FUENTE_DATABASE_URL no apunta a un almacén conocido: {almacen!r}")
        self._almacen = almacen

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    @property
    def almacen(self) -> str:
        return self._almacen

    async def salud(self) -> dict:
        try:
            if self._pool is None:
                raise RuntimeError("sin pool")
            async with self._pool.acquire() as con:
                almacen = await con.fetchval("SELECT valor FROM fuente_meta WHERE clave = 'almacen'")
            return {"ok": True, "almacen": almacen}
        except Exception as e:
            return {"ok": False, "error": type(e).__name__}
