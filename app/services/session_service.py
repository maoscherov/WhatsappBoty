"""
Persistencia de sesión de conversación en Redis.
TTL: 30 minutos de inactividad.
Estructura almacenada (JSON):
  {
    "history": [{"role": "user"|"assistant", "content": "..."}],
    "pending_sku_id": "...",   # producto seleccionado pendiente de pago
    "pending_sku_nombre": "...",
    "pending_precio": 0.0,
    "estado": "idle" | "esperando_confirmacion" | "esperando_pago"
  }
"""

import json
import redis.asyncio as aioredis
from typing import Optional

SESSION_TTL = 60 * 30  # 30 minutos
MAX_HISTORY = 10       # mensajes que se pasan al LLM (últimos N)


class SessionService:
    def __init__(self, redis_url: str):
        self._redis = aioredis.from_url(redis_url, decode_responses=True)

    def _key(self, phone: str) -> str:
        return f"session:{phone}"

    async def get(self, phone: str) -> dict:
        raw = await self._redis.get(self._key(phone))
        if not raw:
            return {"history": [], "pending_sku_id": None, "pending_sku_nombre": None,
                    "pending_precio": None, "estado": "idle"}
        return json.loads(raw)

    async def save(self, phone: str, session: dict):
        # Mantener solo los últimos MAX_HISTORY mensajes en Redis
        session["history"] = session["history"][-MAX_HISTORY:]
        await self._redis.setex(self._key(phone), SESSION_TTL, json.dumps(session))

    async def add_message(self, phone: str, role: str, content: str):
        session = await self.get(phone)
        session["history"].append({"role": role, "content": content})
        await self.save(phone, session)

    async def set_pending(self, phone: str, sku_id: str, sku_nombre: str, precio: float):
        session = await self.get(phone)
        session["pending_sku_id"] = sku_id
        session["pending_sku_nombre"] = sku_nombre
        session["pending_precio"] = precio
        session["estado"] = "esperando_confirmacion"
        await self.save(phone, session)

    async def clear_pending(self, phone: str):
        session = await self.get(phone)
        session["pending_sku_id"] = None
        session["pending_sku_nombre"] = None
        session["pending_precio"] = None
        session["estado"] = "idle"
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
