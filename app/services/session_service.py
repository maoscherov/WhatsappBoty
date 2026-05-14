"""
Persistencia de sesión de conversación en Redis.
TTL: 30 minutos de inactividad.

Si Redis no está disponible, opera en modo in-memory (sin persistencia entre
reinicios del servidor). El bot funciona igual pero pierde el historial si
la instancia se reinicia.
"""

import json
import logging
import redis.asyncio as aioredis
from typing import Optional

logger = logging.getLogger(__name__)

SESSION_TTL = 60 * 30
MAX_HISTORY = 10

_EMPTY_SESSION = lambda: {
    "history": [],
    "pending_sku_id": None,
    "pending_sku_nombre": None,
    "pending_precio": None,
    "pending_cantidad": 1,
    "estado": "idle",
}


class SessionService:
    def __init__(self, redis_url: str):
        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._memory: dict[str, dict] = {}   # fallback in-memory
        self._redis_ok: Optional[bool] = None  # None = no testeado aún

    def _key(self, phone: str) -> str:
        return f"session:{phone}"

    async def _use_redis(self) -> bool:
        if self._redis_ok is None:
            self._redis_ok = await self.ping()
            if not self._redis_ok:
                logger.warning("Redis no disponible — usando sesiones en memoria")
        return self._redis_ok

    async def get(self, phone: str) -> dict:
        if await self._use_redis():
            try:
                raw = await self._redis.get(self._key(phone))
                if raw:
                    return json.loads(raw)
            except Exception:
                self._redis_ok = False
        return self._memory.get(phone, _EMPTY_SESSION())

    async def save(self, phone: str, session: dict):
        session["history"] = session["history"][-MAX_HISTORY:]
        if await self._use_redis():
            try:
                await self._redis.setex(self._key(phone), SESSION_TTL, json.dumps(session))
                return
            except Exception:
                self._redis_ok = False
        self._memory[phone] = session

    async def add_message(self, phone: str, role: str, content: str):
        session = await self.get(phone)
        session["history"].append({"role": role, "content": content})
        await self.save(phone, session)

    async def set_pending(self, phone: str, sku_id: str, sku_nombre: str, precio: float, cantidad: int = 1):
        session = await self.get(phone)
        session.update({
            "pending_sku_id": sku_id,
            "pending_sku_nombre": sku_nombre,
            "pending_precio": precio,
            "pending_cantidad": max(1, int(cantidad)),
            "estado": "esperando_confirmacion",
        })
        await self.save(phone, session)

    async def clear_pending(self, phone: str):
        session = await self.get(phone)
        session.update({
            "pending_sku_id": None,
            "pending_sku_nombre": None,
            "pending_precio": None,
            "pending_cantidad": 1,
            "estado": "idle",
        })
        await self.save(phone, session)

    async def set_estado(self, phone: str, estado: str):
        session = await self.get(phone)
        session["estado"] = estado
        await self.save(phone, session)

    async def ping(self) -> bool:
        try:
            return await self._redis.ping()
        except Exception:
            return False


_instance: Optional[SessionService] = None


def get_session_service(redis_url: str = "redis://localhost:6379") -> SessionService:
    global _instance
    if _instance is None:
        _instance = SessionService(redis_url)
    return _instance
