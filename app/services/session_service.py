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
    "pending_opciones": [],
    "estado": "idle",
}


class SessionService:
    def __init__(self, redis_url: str):
        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._memory: dict[str, dict] = {}       # fallback in-memory sesiones
        self._redis_ok: Optional[bool] = None    # None = no testeado aún
        self._processed_ids: set[str] = set()    # fallback in-memory dedup

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

    async def set_pending(self, phone: str, sku_id: str, sku_nombre: str, precio: float,
                          cantidad: int = 1, opciones: list | None = None):
        session = await self.get(phone)
        session.update({
            "pending_sku_id": sku_id,
            "pending_sku_nombre": sku_nombre,
            "pending_precio": precio,
            "pending_cantidad": max(1, int(cantidad)),
            "pending_opciones": opciones if opciones is not None else session.get("pending_opciones", []),
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
            "pending_opciones": [],
            "estado": "idle",
        })
        await self.save(phone, session)

    async def set_estado(self, phone: str, estado: str):
        session = await self.get(phone)
        session["estado"] = estado
        await self.save(phone, session)

    async def is_processed(self, msg_id: str) -> bool:
        """
        Retorna True si el mensaje ya fue procesado (deduplicación de webhooks).
        Usa Redis como primera línea; si falla cae a un set in-memory para que
        los retries de WhatsApp no se cuelen aunque Redis tenga un micro-corte.
        """
        key = f"processed:{msg_id}"
        if await self._use_redis():
            try:
                result = await self._redis.set(key, "1", ex=300, nx=True)
                if result is None:
                    return True   # ya existía en Redis → duplicado
                # Guardado en Redis OK → también marcamos en memoria por si acaso
                self._processed_ids.add(msg_id)
                return False
            except Exception:
                pass  # Redis falló → fallback a memoria

        # Fallback in-memory (instancia única: Railway, Render, etc.)
        if msg_id in self._processed_ids:
            return True
        self._processed_ids.add(msg_id)
        # Evitar leak: si crece demasiado, descartar la mitad más antigua
        if len(self._processed_ids) > 2000:
            self._processed_ids = set(list(self._processed_ids)[1000:])
        return False

    async def list_all(self) -> list[tuple[str, dict]]:
        """Devuelve todas las sesiones activas como lista de (phone, session)."""
        if await self._use_redis():
            try:
                keys = await self._redis.keys("session:*")
                result = []
                for key in keys:
                    raw = await self._redis.get(key)
                    if raw:
                        phone = key.removeprefix("session:")
                        result.append((phone, json.loads(raw)))
                return sorted(result, key=lambda x: x[0])
            except Exception:
                pass
        return list(self._memory.items())

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
