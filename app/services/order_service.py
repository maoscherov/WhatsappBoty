"""
Gestión de pedidos confirmados por pago.

Estado: pendiente → preparado → retirado

Redis keys:
  order:{order_id}  → JSON del pedido, TTL 7 días
  orders:idx        → sorted set (score=timestamp, member=order_id)
"""

import json
import logging
import random
import string
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

ORDER_TTL = 60 * 60 * 24 * 7   # 7 días
ORDERS_IDX = "orders:idx"


class OrderService:
    def __init__(self, redis_url: str):
        self._redis = aioredis.from_url(redis_url, decode_responses=True)

    @staticmethod
    def _gen_order_id() -> str:
        suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
        return f"ORD-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{suffix}"

    @staticmethod
    def _gen_pickup_code() -> str:
        return "".join(random.choices(string.digits, k=6))

    def _key(self, order_id: str) -> str:
        return f"order:{order_id}"

    async def create(
        self,
        phone: str,
        sku_id: str,
        sku_nombre: str,
        cantidad: int,
        total: float,
        mp_payment_id: str,
    ) -> dict:
        order_id = self._gen_order_id()
        now = datetime.now(timezone.utc).isoformat()
        order = {
            "order_id":      order_id,
            "phone":         phone,
            "sku_id":        sku_id,
            "sku_nombre":    sku_nombre,
            "cantidad":      int(cantidad),
            "total":         round(float(total), 2),
            "mp_payment_id": mp_payment_id,
            "estado":        "pendiente",
            "pickup_code":   self._gen_pickup_code(),  # generado al confirmar el pago
            "created_at":    now,
            "updated_at":    now,
        }
        ts = datetime.now(timezone.utc).timestamp()
        try:
            await self._redis.setex(self._key(order_id), ORDER_TTL, json.dumps(order))
            await self._redis.zadd(ORDERS_IDX, {order_id: ts})
        except Exception as e:
            logger.error(f"OrderService.create error: {e}")
        logger.info(f"Pedido creado: {order_id} phone={phone} producto={sku_nombre}")
        return order

    async def get(self, order_id: str) -> Optional[dict]:
        try:
            raw = await self._redis.get(self._key(order_id))
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def list_all(self, limit: int = 300) -> list[dict]:
        """Devuelve pedidos ordenados del más reciente al más antiguo."""
        try:
            ids = await self._redis.zrevrange(ORDERS_IDX, 0, limit - 1)
            orders = []
            for oid in ids:
                o = await self.get(oid)
                if o:
                    orders.append(o)
                else:
                    # Limpiar índice si el pedido expiró
                    await self._redis.zrem(ORDERS_IDX, oid)
            return orders
        except Exception as e:
            logger.error(f"OrderService.list_all error: {e}")
            return []

    async def mark_preparado(self, order_id: str) -> Optional[dict]:
        order = await self.get(order_id)
        if not order:
            return None
        order["estado"]     = "preparado"
        # El pickup_code ya fue generado al crear el pedido — no se regenera
        order["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            await self._redis.setex(self._key(order_id), ORDER_TTL, json.dumps(order))
        except Exception as e:
            logger.error(f"OrderService.mark_preparado error: {e}")
        return order

    async def mark_retirado(self, order_id: str) -> Optional[dict]:
        order = await self.get(order_id)
        if not order:
            return None
        order["estado"]     = "retirado"
        order["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            await self._redis.setex(self._key(order_id), ORDER_TTL, json.dumps(order))
        except Exception as e:
            logger.error(f"OrderService.mark_retirado error: {e}")
        return order


_instance: Optional[OrderService] = None


def get_order_service(redis_url: str) -> OrderService:
    global _instance
    if _instance is None:
        _instance = OrderService(redis_url)
    return _instance
