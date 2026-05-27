"""
Registro de tiempos de respuesta para monitoreo de performance.
Guarda los últimos MAX_ENTRIES registros en Redis (LPUSH perf:log).
Si Redis no está disponible, los datos se pierden (no crítico).
"""

import json
import logging
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

MAX_ENTRIES = 300
PERF_KEY = "perf:log"


class PerfService:
    def __init__(self, redis_url: str):
        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._ok: Optional[bool] = None

    async def _usable(self) -> bool:
        if self._ok is None:
            try:
                self._ok = await self._redis.ping()
            except Exception:
                self._ok = False
                logger.debug("PerfService: Redis no disponible, métricas desactivadas")
        return bool(self._ok)

    async def record(self, entry: dict):
        """Guarda un registro de performance. No falla si Redis no está."""
        if not await self._usable():
            return
        try:
            await self._redis.lpush(PERF_KEY, json.dumps(entry))
            await self._redis.ltrim(PERF_KEY, 0, MAX_ENTRIES - 1)
        except Exception as e:
            logger.debug(f"perf.record error: {e}")

    async def get_recent(self, n: int = 100) -> list[dict]:
        """Devuelve los últimos n registros, del más reciente al más antiguo."""
        if not await self._usable():
            return []
        try:
            items = await self._redis.lrange(PERF_KEY, 0, min(n, MAX_ENTRIES) - 1)
            return [json.loads(x) for x in items]
        except Exception:
            return []

    async def clear(self):
        """Borra el historial de performance."""
        if not await self._usable():
            return
        try:
            await self._redis.delete(PERF_KEY)
        except Exception:
            pass


_instance: Optional[PerfService] = None


def get_perf_service(redis_url: str) -> PerfService:
    global _instance
    if _instance is None:
        _instance = PerfService(redis_url)
    return _instance
