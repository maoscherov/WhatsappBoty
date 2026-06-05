"""
Configuración dinámica del bot guardada en Redis.
Permite cambiar comportamientos sin redeploy.

Clave: bot:config (hash Redis)

Campos actuales:
  send_images   → "always" | "on_request"   (default: "always")
"""

import json
import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

CONFIG_KEY   = "bot:config"
HOURS_KEY    = "bot:hours"
TZ_ARG       = ZoneInfo("America/Argentina/Buenos_Aires")
DAY_MAP      = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}

DEFAULTS: dict[str, str] = {
    "send_images": "always",
}

DEFAULT_HOURS = {
    "enabled": False,
    "closed_message": "Estamos fuera del horario de atención. Te respondemos en cuanto abramos 🙏",
    "schedule": {
        "mon": {"open": "09:00", "close": "18:00", "active": True},
        "tue": {"open": "09:00", "close": "18:00", "active": True},
        "wed": {"open": "09:00", "close": "18:00", "active": True},
        "thu": {"open": "09:00", "close": "18:00", "active": True},
        "fri": {"open": "09:00", "close": "18:00", "active": True},
        "sat": {"open": "09:00", "close": "13:00", "active": True},
        "sun": {"open": "09:00", "close": "13:00", "active": False},
    },
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

    # ── Horarios ──────────────────────────────────────────────────────────────

    async def get_hours(self) -> dict:
        if await self._usable():
            try:
                raw = await self._redis.get(HOURS_KEY)
                if raw:
                    return json.loads(raw)
            except Exception:
                pass
        return dict(DEFAULT_HOURS)

    async def set_hours(self, hours: dict):
        if await self._usable():
            try:
                await self._redis.set(HOURS_KEY, json.dumps(hours))
                return
            except Exception:
                pass

    def is_open_now(self, hours: dict) -> bool:
        """True si el horario está activo y el momento actual cae dentro del rango."""
        if not hours.get("enabled"):
            return True  # sin control de horario → siempre abierto
        now = datetime.now(TZ_ARG)
        day = DAY_MAP[now.weekday()]
        cfg = hours.get("schedule", {}).get(day, {})
        if not cfg.get("active"):
            return False
        open_t  = cfg.get("open",  "00:00")
        close_t = cfg.get("close", "23:59")
        current = now.strftime("%H:%M")
        return open_t <= current <= close_t


_instance: Optional[ConfigService] = None


def get_config_service(redis_url: str) -> ConfigService:
    global _instance
    if _instance is None:
        _instance = ConfigService(redis_url)
    return _instance
