"""
Gestión de pedidos confirmados por pago.

Estado: pendiente → preparado → retirado

Trazabilidad de operador: agente/tomado_at (quién lo tomó), preparado_por/
preparado_at y retirado_por/retirado_at. Todos nullables — los pedidos viejos
(creados antes de esta versión) los devuelven en null.

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

# Campos de trazabilidad de operador. Las fechas van en epoch segundos (int),
# igual que derivada_at/ultimo_mensaje_at en las sesiones.
TRACE_FIELDS = (
    "agente", "tomado_at",
    "preparado_por", "preparado_at",
    "retirado_por", "retirado_at",
)


def _now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


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
        tipo_entrega: str = "retiro",
        direccion_envio: Optional[str] = None,
    ) -> dict:
        order_id = self._gen_order_id()
        now = datetime.now(timezone.utc).isoformat()
        order = {
            "order_id":       order_id,
            "phone":          phone,
            "sku_id":         sku_id,
            "sku_nombre":     sku_nombre,
            "cantidad":       int(cantidad),
            "total":          round(float(total), 2),
            "mp_payment_id":  mp_payment_id,
            "estado":         "pendiente",
            "tipo_entrega":   tipo_entrega,       # "retiro" | "envio"
            "direccion_envio": direccion_envio,
            "pickup_code":    self._gen_pickup_code(),  # generado al confirmar el pago
            "created_at":     now,
            "updated_at":     now,
            # Trazabilidad de operador — se completan desde el backoffice
            "agente":         None,   # quién tomó el pedido (None = sin dueño)
            "tomado_at":      None,
            "preparado_por":  None,
            "preparado_at":   None,
            "retirado_por":   None,
            "retirado_at":    None,
        }
        ts = datetime.now(timezone.utc).timestamp()
        try:
            await self._redis.setex(self._key(order_id), ORDER_TTL, json.dumps(order))
            await self._redis.zadd(ORDERS_IDX, {order_id: ts})
        except Exception as e:
            logger.error(f"OrderService.create error: {e}")
        logger.info(f"Pedido creado: {order_id} phone={phone} producto={sku_nombre}")
        return order

    @staticmethod
    def _with_trace_defaults(order: dict) -> dict:
        """Completa en null los campos de trazabilidad en pedidos ya guardados
        antes de esta versión, para que la API siempre los devuelva."""
        for f in TRACE_FIELDS:
            order.setdefault(f, None)
        return order

    async def _save(self, order: dict) -> None:
        order["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            await self._redis.setex(self._key(order["order_id"]), ORDER_TTL, json.dumps(order))
        except Exception as e:
            logger.error(f"OrderService._save error: {e}")

    async def get(self, order_id: str) -> Optional[dict]:
        try:
            raw = await self._redis.get(self._key(order_id))
            return self._with_trace_defaults(json.loads(raw)) if raw else None
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

    @staticmethod
    def _asignar_si_libre(order: dict, agente: Optional[str]) -> None:
        """Si el pedido no tenía dueño, el operador que ejecuta la acción lo toma."""
        if agente and not order.get("agente"):
            order["agente"] = agente
            order["tomado_at"] = order.get("tomado_at") or _now_epoch()

    async def takeover(
        self, order_id: str, agente: str, force: bool = False
    ) -> tuple[Optional[dict], Optional[str]]:
        """
        Asigna el pedido a un operador.

        Devuelve (pedido, None)        → asignado
                 (None, None)          → el pedido no existe
                 (None, agente_actual) → ya lo tomó otro y force=False
        """
        order = await self.get(order_id)
        if not order:
            return None, None

        agente = (agente or "").strip()
        actual = (order.get("agente") or "").strip()
        if actual and actual != agente and not force:
            return None, actual

        # Reasignación (o primera toma) refresca tomado_at; re-tomar el propio
        # pedido no pisa la marca original.
        if actual != agente or not order.get("tomado_at"):
            order["tomado_at"] = _now_epoch()
        order["agente"] = agente or None
        await self._save(order)
        logger.info(f"Pedido {order_id} tomado por {agente}{' (force)' if force else ''}")
        return order, None

    async def mark_preparado(self, order_id: str, agente: Optional[str] = None) -> Optional[dict]:
        order = await self.get(order_id)
        if not order:
            return None
        order["estado"]        = "preparado"
        # El pickup_code ya fue generado al crear el pedido — no se regenera
        order["preparado_por"] = agente or order.get("preparado_por")
        order["preparado_at"]  = _now_epoch()
        self._asignar_si_libre(order, agente)
        await self._save(order)
        return order

    async def mark_retirado(self, order_id: str, agente: Optional[str] = None) -> Optional[dict]:
        order = await self.get(order_id)
        if not order:
            return None
        order["estado"]       = "retirado"
        order["retirado_por"] = agente or order.get("retirado_por")
        order["retirado_at"]  = _now_epoch()
        self._asignar_si_libre(order, agente)
        await self._save(order)
        return order

    async def pendientes_por_phone(self, limit: int = 300) -> dict[str, int]:
        """
        {phone: cantidad de pedidos en estado pendiente} — para el chip
        "N pedidos" en la cola unificada del backoffice.

        Usa MGET (dos round-trips) en vez de un GET por pedido: /bo/derivadas
        se pollea cada pocos segundos.
        """
        counts: dict[str, int] = {}
        try:
            ids = await self._redis.zrevrange(ORDERS_IDX, 0, limit - 1)
            if not ids:
                return counts
            raws = await self._redis.mget([self._key(oid) for oid in ids])
            for raw in raws:
                if not raw:
                    continue   # expirado; list_all() limpia el índice
                o = json.loads(raw)
                phone = o.get("phone")
                if o.get("estado") == "pendiente" and phone:
                    counts[phone] = counts.get(phone, 0) + 1
        except Exception as e:
            logger.error(f"OrderService.pendientes_por_phone error: {e}")
        return counts


_instance: Optional[OrderService] = None


def get_order_service(redis_url: str) -> OrderService:
    global _instance
    if _instance is None:
        _instance = OrderService(redis_url)
    return _instance
