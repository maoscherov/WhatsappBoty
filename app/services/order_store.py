"""
Persistencia durable de pedidos (tabla `orders`, migración 0019).

Los pedidos viven en Redis con TTL de 7 días (order_service) y eso es lo que
lee el backoffice. Esta tabla es la copia que no se pierde: un pedido COBRADO
que desaparece por un reinicio o una evicción de Redis es plata. También es
la cola de reintentos del alta en el ERP (F5, Mercurio):

  erp_estado: NULL      → no aplica (deploy sin alta de pedidos habilitada)
              pendiente → hay que (re)intentar el POST /pedidos
              enviado   → registrado en el ERP (erp_id_comprobante / erp_numero)
              rechazado → 422: lo mira un humano, no se reintenta

La escritura es best-effort desde order_service (si no hay Postgres, el bot
sigue solo con Redis, como el resto del sistema). Pero acá los errores de
Postgres se PROPAGAN (`raise_errors=True`): el que llama decide si los traga
(el write-through) o los hace visibles (la cola del ERP). Un "no pude
guardar" nunca se confunde con "no había nada".
"""

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _columnas(order: dict) -> tuple:
    """order_id, phone, estado, total, pago, payment_id y data de la fila."""
    return (
        str(order.get("order_id")), str(order.get("phone") or ""),
        str(order.get("estado") or "pendiente"),
        round(float(order.get("total") or 0), 2), order.get("pago"),
        str(order.get("mp_payment_id") or "") or None,
        json.dumps(order, ensure_ascii=False),
    )


class OrderStore:
    def __init__(self, db):
        self._db = db

    async def upsert(self, order: dict, erp_estado: Optional[str] = None) -> None:
        """
        Vuelca el pedido completo (JSON en `data` + columnas de consulta).

        `erp_estado`: estado del alta en el ERP con el que NACE la fila (una
        orden cobrada online con el alta habilitada nace 'pendiente': entra a
        la cola del job en el mismo INSERT que la crea, antes del WhatsApp).
        Nunca pisa un estado que ya tenga (COALESCE): un 'enviado' no vuelve a
        'pendiente' y un _save posterior (erp_estado=None) no lo toca.
        """
        await self._db.execute(
            """
            INSERT INTO orders (order_id, phone, estado, total, pago, payment_id, data,
                                erp_estado)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (order_id) DO UPDATE SET
                estado = EXCLUDED.estado, total = EXCLUDED.total,
                pago = EXCLUDED.pago, payment_id = EXCLUDED.payment_id,
                data = EXCLUDED.data, updated_at = now(),
                erp_estado = COALESCE(orders.erp_estado, EXCLUDED.erp_estado)
            """,
            *_columnas(order), erp_estado, raise_errors=True,
        )

    async def marcar_erp(self, order_id: str, estado: str, *,
                         id_comprobante: Optional[str] = None,
                         numero: Optional[str] = None,
                         error: Optional[str] = None,
                         incrementar_intento: bool = False,
                         order: Optional[dict] = None) -> None:
        """
        Estado del alta en el ERP. Si la fila no existe (el write-through falló
        al crear la orden) nunca actualiza 0 filas en silencio: con `order`
        inserta la orden completa con ese estado; sin `order`, LookupError.
        """
        intento = 1 if incrementar_intento else 0
        r = await self._db.execute(
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
            order_id, estado, id_comprobante, numero, error, intento, raise_errors=True,
        )
        if str(r).split()[-1:] != ["0"]:
            return
        if order is None:
            raise LookupError(f"orden {order_id} sin fila en orders: no se pudo marcar "
                              f"'{estado}' en el ERP")
        logger.warning(f"Pedido {order_id}: no estaba en orders (falló el write-through); "
                       f"se inserta completo con erp_estado='{estado}'")
        await self._db.execute(
            """
            INSERT INTO orders (order_id, phone, estado, total, pago, payment_id, data,
                                erp_estado, erp_id_comprobante, erp_numero,
                                erp_ultimo_error, erp_intentos)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            ON CONFLICT (order_id) DO UPDATE SET
                erp_estado = EXCLUDED.erp_estado,
                erp_id_comprobante = COALESCE(EXCLUDED.erp_id_comprobante,
                                              orders.erp_id_comprobante),
                erp_numero = COALESCE(EXCLUDED.erp_numero, orders.erp_numero),
                erp_ultimo_error = EXCLUDED.erp_ultimo_error,
                erp_intentos = orders.erp_intentos + EXCLUDED.erp_intentos,
                updated_at = now()
            """,
            *_columnas({**order, "order_id": order_id}), estado, id_comprobante, numero,
            error, intento, raise_errors=True,
        )

    async def pendientes_erp(self, limit: int = 20) -> list[dict]:
        """Pedidos esperando el alta en el ERP, los más viejos primero."""
        rows = await self._db.fetch(
            "SELECT order_id, data, erp_intentos FROM orders "
            "WHERE erp_estado = 'pendiente' ORDER BY created_at LIMIT $1", limit,
            raise_errors=True)
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
