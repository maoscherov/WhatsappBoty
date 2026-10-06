"""
Persistencia durable de pedidos (tabla `orders`, migración 0017).

Los pedidos viven en Redis con TTL de 7 días (order_service) y eso es lo que
lee el backoffice. Esta tabla es la copia que no se pierde: un pedido COBRADO
que desaparece por un reinicio o una evicción de Redis es plata. También es
la cola de reintentos del alta en el ERP (F5, Mercurio):

  erp_estado: NULL      → no aplica (deploy sin alta de pedidos habilitada)
              pendiente → hay que (re)intentar el POST /pedidos
              enviado   → registrado en el ERP (erp_id_comprobante / erp_numero)
              rechazado → 422: lo mira un humano, no se reintenta

La escritura es best-effort desde order_service (si no hay Postgres, el bot
sigue solo con Redis, como el resto del sistema).
"""

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class OrderStore:
    def __init__(self, db):
        self._db = db

    async def upsert(self, order: dict) -> None:
        """Vuelca el pedido completo (JSON en `data` + columnas de consulta)."""
        await self._db.execute(
            """
            INSERT INTO orders (order_id, phone, estado, total, pago, payment_id, data)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (order_id) DO UPDATE SET
                estado = EXCLUDED.estado, total = EXCLUDED.total,
                pago = EXCLUDED.pago, payment_id = EXCLUDED.payment_id,
                data = EXCLUDED.data, updated_at = now()
            """,
            str(order.get("order_id")), str(order.get("phone") or ""),
            str(order.get("estado") or "pendiente"),
            round(float(order.get("total") or 0), 2), order.get("pago"),
            str(order.get("mp_payment_id") or "") or None,
            json.dumps(order, ensure_ascii=False),
        )

    async def marcar_erp(self, order_id: str, estado: str, *,
                         id_comprobante: Optional[str] = None,
                         numero: Optional[str] = None,
                         error: Optional[str] = None,
                         incrementar_intento: bool = False) -> None:
        await self._db.execute(
            """
            UPDATE orders SET
                erp_estado = $2,
                erp_id_comprobante = COALESCE($3, erp_id_comprobante),
                erp_numero = COALESCE($4, erp_numero),
                erp_ultimo_error = $5,
                erp_intentos = erp_intentos + $6,
                updated_at = now()
            WHERE order_id = $1
            """,
            order_id, estado, id_comprobante, numero, error,
            1 if incrementar_intento else 0,
        )

    async def pendientes_erp(self, limit: int = 20) -> list[dict]:
        """Pedidos esperando el alta en el ERP, los más viejos primero."""
        rows = await self._db.fetch(
            "SELECT order_id, data, erp_intentos FROM orders "
            "WHERE erp_estado = 'pendiente' ORDER BY created_at LIMIT $1", limit)
        out = []
        for r in rows:
            try:
                data = json.loads(r["data"])
            except (TypeError, ValueError):
                data = {}
            out.append({"order_id": r["order_id"], "erp_intentos": r["erp_intentos"],
                        **data})
        return out


_instance: Optional[OrderStore] = None


def get_order_store(db) -> OrderStore:
    global _instance
    if _instance is None or _instance._db is not db:
        _instance = OrderStore(db)
    return _instance
