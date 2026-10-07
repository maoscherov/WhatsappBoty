"""
Persistencia durable de pedidos (tabla `orders`, migración 0019).

Los pedidos viven en Redis con TTL de 7 días (order_service) y eso es lo que
lee el backoffice. Esta tabla es la copia que no se pierde: un pedido COBRADO
que desaparece por un reinicio o una evicción de Redis es plata. También es
la cola de reintentos del alta en el ERP (F5, Mercurio):

  erp_estado: NULL      → no aplica (deploy sin alta de pedidos habilitada,
                          o una orden sin cobro online)
              pendiente → hay que (re)intentar el POST /pedidos (la orden
                          cobrada nace así; también si falta un código, el
                          ERP no responde, contesta 429/5xx o un 2xx sin
                          id_comprobante, o falta el customer_id)
              enviado   → registrado en el ERP (erp_id_comprobante / erp_numero)
              rechazado → rechazo definitivo del ERP (422, 400, 404, 409, 413),
                          renglones ilegibles o que no cuadran con el total,
                          o un ítem sin artículo del ERP (MANUAL, LIBREn,
                          TEST): lo mira un humano, no se reintenta
              vencido   → siguió 'pendiente' más de MERCURIO_PEDIDOS_MAX_DIAS
                          (6) desde que se creó: no se reintenta más, porque
                          la Idempotency-Key del ERP dura 7 días y un
                          reintento posterior podría duplicar el pedido. Lo
                          mira un humano (el motivo conserva el último error)

Backoff por pedido: cada falla reintentable suma un intento (`erp_intentos`)
y deja `erp_proximo_intento = now() + min(base * 2^(intentos-1), 6 h)`, con
base = MERCURIO_PEDIDOS_RETRY_SECS (300 s: 5, 10, 20, 40 min... hasta 6 h).
La cola (`pendientes_erp`) toma solo los vencidos (`erp_proximo_intento` NULL
o ya pasado), ordenados por COALESCE(erp_proximo_intento, created_at): un
pedido con un error persistente no tranca la cabeza de la cola. Un error de
credencial (401/403) no suma intento ni aplaza el pedido.

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

# Lo que la consola de pedidos muestra del alta en el ERP (estado_erp).
CAMPOS_ERP = ("erp_estado", "erp_ultimo_error", "erp_intentos", "erp_numero",
              "erp_id_comprobante")

# Tope del backoff por pedido (6 horas) y del exponente (2^30 ya lo supera
# con cualquier base razonable; evita un overflow con erp_intentos enormes).
BACKOFF_TOPE_SECS = 6 * 3600
_BACKOFF_EXPONENTE_MAX = 30


def _sql_proximo_intento(base: str, intentos: str) -> str:
    """now() + min(base * 2^(intentos-1), 6 h), en SQL."""
    return (f"now() + make_interval(secs => LEAST({base}::float8 * power(2::float8, "
            f"LEAST(GREATEST(({intentos}) - 1, 0), {_BACKOFF_EXPONENTE_MAX})), "
            f"{BACKOFF_TOPE_SECS}))")


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
                         backoff_secs: Optional[float] = None,
                         order: Optional[dict] = None) -> None:
        """
        Estado del alta en el ERP. Si la fila no existe (el write-through falló
        al crear la orden) nunca actualiza 0 filas en silencio: con `order`
        inserta la orden completa con ese estado; sin `order`, LookupError.

        `backoff_secs` (base del backoff, con una falla reintentable): deja
        `erp_proximo_intento = now() + min(base * 2^(intentos-1), 6 h)`, con
        los intentos ya incrementados. Sin backoff, un estado distinto de
        'pendiente' limpia `erp_proximo_intento` y 'pendiente' lo conserva.
        """
        intento = 1 if incrementar_intento else 0
        base = float(backoff_secs) if backoff_secs is not None else None
        r = await self._db.execute(
            f"""
            UPDATE orders SET
                erp_estado = $2,
                erp_id_comprobante = COALESCE($3, erp_id_comprobante),
                erp_numero = COALESCE($4, erp_numero),
                erp_ultimo_error = $5,
                erp_intentos = erp_intentos + $6,
                erp_proximo_intento = CASE
                    WHEN $7::float8 IS NOT NULL
                        THEN {_sql_proximo_intento("$7", "erp_intentos + $6")}
                    WHEN $2 <> 'pendiente' THEN NULL
                    ELSE erp_proximo_intento END,
                updated_at = now()
            WHERE order_id = $1
            """,
            order_id, estado, id_comprobante, numero, error, intento, base,
            raise_errors=True,
        )
        if str(r).split()[-1:] != ["0"]:
            return
        if order is None:
            raise LookupError(f"orden {order_id} sin fila en orders: no se pudo marcar "
                              f"'{estado}' en el ERP")
        logger.warning(f"Pedido {order_id}: no estaba en orders (falló el write-through); "
                       f"se inserta completo con erp_estado='{estado}'")
        await self._insertar(
            f"""
            INSERT INTO orders (order_id, phone, estado, total, pago, payment_id, data,
                                erp_estado, erp_id_comprobante, erp_numero,
                                erp_ultimo_error, erp_intentos, erp_proximo_intento)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                    CASE WHEN $13::float8 IS NULL THEN NULL
                         ELSE {_sql_proximo_intento("$13", "$12")} END)
            ON CONFLICT (order_id) DO UPDATE SET
                erp_estado = EXCLUDED.erp_estado,
                erp_id_comprobante = COALESCE(EXCLUDED.erp_id_comprobante,
                                              orders.erp_id_comprobante),
                erp_numero = COALESCE(EXCLUDED.erp_numero, orders.erp_numero),
                erp_ultimo_error = EXCLUDED.erp_ultimo_error,
                erp_intentos = orders.erp_intentos + EXCLUDED.erp_intentos,
                erp_proximo_intento = EXCLUDED.erp_proximo_intento,
                updated_at = now()
            """,
            {**order, "order_id": order_id}, estado, id_comprobante, numero, error, intento,
            base,
        )

    async def encolar_erp(self, order: dict) -> str:
        """
        Deja la orden en la cola del ERP para el hook post-cobro y devuelve el
        `erp_estado` con el que queda. Solo pasa de NULL a 'pendiente' (o deja
        'pendiente' como está): si el job ya la procesó en la ventana entre el
        create y el hook ('enviado', 'rechazado' o 'vencido'), la deja así y
        devuelve ese estado; el que llama no hace el POST (ronda de arreglo 2).
        Sin fila (falló el write-through), inserta la orden completa en
        'pendiente'. Si el pago ya tiene OTRA orden, PedidoDuplicado.
        """
        cols = _columnas(order)
        try:
            row = await self._db.fetchrow(
                """
                INSERT INTO orders (order_id, phone, estado, total, pago, payment_id, data,
                                    erp_estado)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'pendiente')
                ON CONFLICT (order_id) DO UPDATE SET
                    erp_estado = COALESCE(orders.erp_estado, 'pendiente')
                RETURNING erp_estado, (xmax = 0) AS insertada
                """,
                *cols, raise_errors=True)
        except Exception as e:
            if not _es_pago_repetido(e):
                raise
            logger.warning(f"Pedido {cols[0]}: el pago {cols[5]} ya tiene otra orden "
                           "(índice único de payment_id) — se trata como duplicado")
            raise PedidoDuplicado(cols[5], cols[0]) from e
        if row["insertada"]:
            logger.warning(f"Pedido {cols[0]}: no estaba en orders (falló el write-through); "
                           "se inserta completo con erp_estado='pendiente'")
        return row["erp_estado"]

    async def pendientes_erp(self, limit: int = 20) -> list[dict]:
        """Pedidos esperando el alta en el ERP cuyo próximo intento ya llegó
        (`erp_proximo_intento` NULL o pasado), por COALESCE(próximo intento,
        creación): un pedido que viene fallando no tranca la cabeza de la cola."""
        rows = await self._db.fetch(
            "SELECT order_id, data, erp_intentos FROM orders "
            "WHERE erp_estado = 'pendiente' "
            "  AND (erp_proximo_intento IS NULL OR erp_proximo_intento <= now()) "
            "ORDER BY COALESCE(erp_proximo_intento, created_at), created_at LIMIT $1",
            limit, raise_errors=True)
        out = []
        for r in rows:
            try:
                data = json.loads(r["data"])
            except (TypeError, ValueError):
                data = {}
            out.append({"order_id": r["order_id"], "erp_intentos": r["erp_intentos"],
                        **data})
        return out

    async def vencer_pendientes(self, max_dias: int) -> list[dict]:
        """
        Pasa a 'vencido' los pedidos 'pendiente' creados hace más de
        `max_dias` días (la Idempotency-Key del ERP dura 7: reintentarlos
        podría duplicarlos). El motivo conserva el último error. Devuelve
        [{order_id, phone, total, erp_ultimo_error}] de los que vencieron.
        """
        rows = await self._db.fetch(
            """
            UPDATE orders SET
                erp_estado = 'vencido',
                erp_ultimo_error = left(
                    'vencido: más de ' || $1::int || ' días sin alta en el ERP (la '
                    || 'Idempotency-Key dura 7: reintentarlo podría duplicar el pedido)'
                    || COALESCE('; último error: ' || erp_ultimo_error, ''), 500),
                erp_proximo_intento = NULL,
                updated_at = now()
            WHERE erp_estado = 'pendiente'
              AND created_at < now() - make_interval(days => $1::int)
            RETURNING order_id, phone, total, erp_ultimo_error
            """,
            int(max_dias), raise_errors=True)
        return [dict(r) for r in rows]


    async def estado_erp(self, order_ids: list[str]) -> dict[str, dict]:
        """{order_id: {erp_estado, erp_ultimo_error, erp_intentos, erp_numero,
        erp_id_comprobante}} de esos pedidos (los que tienen fila). Es lo que
        muestra la consola de pedidos: en Redis no está."""
        ids = [str(i) for i in order_ids if i]
        if not ids:
            return {}
        rows = await self._db.fetch(
            "SELECT order_id, erp_estado, erp_ultimo_error, erp_intentos, erp_numero, "
            "erp_id_comprobante FROM orders WHERE order_id = ANY($1::text[])",
            ids, raise_errors=True)
        return {r["order_id"]: {k: r[k] for k in CAMPOS_ERP} for r in rows}

    async def resumen_erp(self) -> dict:
        """Contadores de la cola del ERP para /bo/mercurio/estado: pendientes,
        pendientes creados hace más de 1 hora, rechazados, vencidos y el
        último error (pedido pendiente, rechazado o vencido con motivo)."""
        row = await self._db.fetchrow(
            """
            SELECT
                count(*) FILTER (WHERE erp_estado = 'pendiente')               AS pendientes,
                count(*) FILTER (WHERE erp_estado = 'pendiente'
                                   AND created_at < now() - interval '1 hour') AS pendientes_mas_1h,
                count(*) FILTER (WHERE erp_estado = 'rechazado')               AS rechazados,
                count(*) FILTER (WHERE erp_estado = 'vencido')                 AS vencidos
            FROM orders WHERE erp_estado IS NOT NULL
            """, raise_errors=True)
        ultimo = await self._db.fetchrow(
            "SELECT order_id, erp_estado, erp_ultimo_error, updated_at FROM orders "
            "WHERE erp_ultimo_error IS NOT NULL "
            "  AND erp_estado IN ('pendiente', 'rechazado', 'vencido') "
            "ORDER BY updated_at DESC LIMIT 1", raise_errors=True)
        out = {k: int(row[k] or 0) for k in ("pendientes", "pendientes_mas_1h",
                                              "rechazados", "vencidos")}
        out["ultimo_error"] = None if not ultimo else {
            "order_id": ultimo["order_id"], "erp_estado": ultimo["erp_estado"],
            "error": ultimo["erp_ultimo_error"],
            "at": ultimo["updated_at"].isoformat() if ultimo["updated_at"] else None,
        }
        return out


_instance: Optional[OrderStore] = None


def get_order_store(db) -> OrderStore:
    global _instance
    if _instance is None or _instance._db is not db:
        _instance = OrderStore(db)
    return _instance
