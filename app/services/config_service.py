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
    "send_images":    "always",
    "pickup_minutes": "30",    # tiempo estimado de preparación/retiro
    "receta_mode":    "conservador",  # "conservador" (ambiguo deriva) | "estricto" (solo "si")
    "envio_enabled":  "true",  # ofrecer envío a domicilio además de retiro
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

    def get_pickup_text(self, hours: dict, pickup_minutes: int = 30) -> str:
        """
        Devuelve un texto de horario de retiro para incluir en mensajes al cliente.
        Funciona siempre, independientemente de si enabled=True/False.
        Ejemplos:
          "Podés retirarlo hoy de 9:00 a 18:00 hs 🕐"
          "Retiros mañana de 9:00 a 13:00 hs (hoy estamos cerrados) 🕐"
          "Consultá nuestro horario de atención 🕐"
        """
        schedule = hours.get("schedule", {})
        mins_txt = f"⏱ Tiempo estimado: *{pickup_minutes} min*" if pickup_minutes else ""

        DAY_ES = {
            "mon": "lunes", "tue": "martes", "wed": "miércoles",
            "thu": "jueves", "fri": "viernes", "sat": "sábado", "sun": "domingo",
        }

        now = datetime.now(TZ_ARG)

        # Buscar el próximo día activo (hoy y los 6 siguientes)
        for offset in range(7):
            idx = (now.weekday() + offset) % 7
            day = DAY_MAP[idx]
            cfg = schedule.get(day, {})
            if not cfg.get("active"):
                continue

            open_t  = cfg.get("open", "")
            close_t = cfg.get("close", "")
            if not open_t or not close_t:
                continue

            # Formatear horas sin segundos
            def fmt(t: str) -> str:
                return t[:5].lstrip("0") or "0:00"

            prefix = f"{mins_txt} · " if mins_txt else ""

            if offset == 0:
                if now.strftime("%H:%M") >= close_t:
                    continue
                return f"{prefix}Podés retirarlo hoy de {fmt(open_t)} a {fmt(close_t)} hs 🕐"
            elif offset == 1:
                return f"{prefix}Retiros mañana de {fmt(open_t)} a {fmt(close_t)} hs 🕐"
            else:
                return f"{prefix}Próximos retiros el {DAY_ES[day]} de {fmt(open_t)} a {fmt(close_t)} hs 🕐"

        # Sin schedule: mostrar solo el tiempo estimado si existe
        return mins_txt


_instance: Optional[ConfigService] = None


def get_config_service(redis_url: str) -> ConfigService:
    global _instance
    if _instance is None:
        _instance = ConfigService(redis_url)
    return _instance
