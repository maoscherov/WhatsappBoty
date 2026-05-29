"""
Configuración dinámica del bot guardada en Redis.
Permite cambiar comportamientos sin redeploy.

Clave: bot:config (hash Redis)

Campos actuales:
  send_images   → "always" | "on_request"   (default: "always")
"""

import logging
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

CONFIG_KEY = "bot:config"

DEFAULTS: dict[str, str] = {
    "send_images": "always",
}


class ConfigService:
    def __init__(self, redis_url: str):
        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._ok: Optional[bool] = None
        self._cache: dict[str, str] = {}   # fallback in-memory

    async def _usable(self) -> bool:
        if self._ok is None:
            try:
                self._ok = await self._redis.ping()
            except Exception:
                self._ok = False
        return bool(self._ok)

    async def get_all(self) -> dict[str, str]:
        if await self._usable():
            try:
                data = await self._redis.hgetall(CONFIG_KEY)
                return {**DEFAULTS, **data}
            except Exception:
                pass
        return {**DEFAULTS, **self._cache}

    async def get(self, key: str) -> str:
        config = await self.get_all()
        return config.get(key, DEFAULTS.get(key, ""))

    async def set(self, key: str, value: str):
        self._cache[key] = value
        if await self._usable():
            try:
                await self._redis.hset(CONFIG_KEY, key, value)
                return
            except Exception:
                pass

    async def set_many(self, updates: dict[str, str]):
        self._cache.update(updates)
        if await self._usable():
            try:
                await self._redis.hset(CONFIG_KEY, mapping=updates)
            except Exception:
                pass


_instance: Optional[ConfigService] = None


def get_config_service(redis_url: str) -> ConfigService:
    global _instance
    if _instance is None:
        _instance = ConfigService(redis_url)
    return _instance
