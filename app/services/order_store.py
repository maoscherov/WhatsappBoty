"""
Persistencia durable de pedidos (tabla `orders`, migración 0019).

Los pedidos viven en Redis con TTL de 7 días (order_service) y eso es lo que
lee el backoffice. Esta tabla es la copia que no se pierde: un pedido COBRADO
que desaparece por un reinicio o una evicción de Redis es plata. También es
la cola de reintentos del alta en el ERP (F5, Mercurio):

  erp_estado: NULL      → no aplica (deploy sin alta de pedidos habilitada,
                          o una orden sin cobro online)
              pendiente → hay que (re)intentar el POST /pedidos (la orden
                          cobrada nace así; también si falta un código)
              enviado   → registrado en el ERP (erp_id_comprobante / erp_numero)
              rechazado → 422, o renglones que no cuadran con el total: lo
                          mira un humano, no se reintenta

Un pago, una orden: índice único de payment_id (PedidoDuplicado).

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


class PedidoDuplicado(Exception):
    """El pago ya tiene OTRA orden (índice único ux_orders_payment, migración
    0019): dos cierres del mismo pago, o una renotificación después de perder
    Redis. No es un error: el que llama lo trata como reintento duplicado y
    no crea la orden, no confirma de nuevo ni la manda al ERP."""

    def __init__(self, payment_id: str, order_id: str):
        super().__init__(f"el pago {payment_id} ya tiene una orden (no se crea {order_id})")
        self.payment_id = payment_id
        self.order_id = order_id


def _es_pago_repetido(e: Exception) -> bool:
    try:
        import asyncpg
    except ImportError:                                   # pragma: no cover
        return False
    return (isinstance(e, asyncpg.exceptions.UniqueViolationError)
            and getattr(e, "constraint_name", "") == "ux_orders_payment")


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

    async def _insertar(self, sql: str, order: dict, *args) -> None:
        """INSERT/upsert de una orden. Si choca con el índice único del pago,
        PedidoDuplicado (WARNING, no ERROR): el pago ya tiene su orden."""
        cols = _columnas(order)
        try:
            await self._db.execute(sql, *cols, *args, raise_errors=True)
        except Exception as e:
            if not _es_pago_repetido(e):
                raise
            logger.warning(f"Pedido {cols[0]}: el pago {cols[5]} ya tiene otra orden "
                           "(índice único de payment_id) — se trata como duplicado")
            raise PedidoDuplicado(cols[5], cols[0]) from e

    async def upsert(self, order: dict, erp_estado: Optional[str] = None) -> None:
        """
        Vuelca el pedido completo (JSON en `data` + columnas de consulta).

        `erp_estado`: estado del alta en el ERP con el que NACE la fila (una
        orden cobrada online con el alta habilitada nace 'pendiente': entra a
        la cola del job en el mismo INSERT que la crea, antes del WhatsApp).
        Nunca pisa un estado que ya tenga (COALESCE): un 'enviado' no vuelve a
        'pendiente' y un _save posterior (erp_estado=None) no lo toca.

        Si el pago ya tiene OTRA orden, PedidoDuplicado (índice único).
        """
        await self._insertar(
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
            order, erp_estado,
        )

    async def por_pago(self, payment_id: str) -> Optional[dict]:
        """La orden de ese pago (JSON completo), o None. Es la defensa durable
        de idempotencia cuando Redis ya no la tiene (OrderService.find_by_payment)."""
        row = await self._db.fetchrow(
            "SELECT order_id, data FROM orders WHERE payment_id = $1 "
            "ORDER BY created_at LIMIT 1", str(payment_id), raise_errors=True)
        if not row:
            return None
        try:
            data = json.loads(row["data"]) if isinstance(row["data"], str) else dict(row["data"])
        except (TypeError, ValueError):
            data = {}
        data.setdefault("order_id", row["order_id"])
        return data

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
        await self._insertar(
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
            {**order, "order_id": order_id}, estado, id_comprobante, numero, error, intento,
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
