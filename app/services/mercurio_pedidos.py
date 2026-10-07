"""
F5 — Alta de pedidos en Mercurio (Mascotas del Oeste).

Nuestra orden (order_service, cobrada por la pasarela) → POST /pedidos del
ERP, con la order_id como Idempotency-Key: un reintento (acá o del job de
fondo) jamás duplica el pedido.

Contrato (spec 14/9): en line_items, `variant_id` = codigo de la variante y
`product_id` = codigo_padre. Esos códigos no están en catalog_items (el
contrato compartido con el agente no los tiene): el sync los deja en la
tabla `mercurio_codigos` y acá se resuelven por external_id (= sku_id). Sin
código (o sin poder leerlos) no hay POST: el pedido queda `pendiente` y se
reintenta después del próximo sync. Nunca se manda el external_id.

`state` y `customer_id` siguen pendientes de confirmación del proveedor
(mail 14/9): salen de Settings (mercurio_pedido_state /
mercurio_customer_id_default) para ajustarlos sin tocar código. Un 422 marca
el pedido `rechazado` (lo mira un humano); un error de red/5xx lo deja
`pendiente` y lo retoma el job de reintentos.

Todo detrás de `mercurio_pedidos_enabled` (False por defecto): en el deploy
de la farmacia este módulo no hace nada.
"""

import hashlib
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def erp_estado_inicial(order: dict) -> Optional[str]:
    """
    Estado del alta en el ERP con el que NACE la fila durable de la orden
    (OrderStore.upsert, en el mismo INSERT): 'pendiente' si el alta está
    habilitada y la orden se cobró online (pago "online" con id de pago);
    None si no aplica (flag apagado, efectivo, cuenta corriente).
    """
    from app.config import get_settings
    if not get_settings().mercurio_pedidos_enabled:
        return None
    if (order.get("pago") or "online") != "online":
        return None
    if not str(order.get("mp_payment_id") or "").strip():
        return None
    return "pendiente"


def _id_estable(order_id: str) -> int:
    """`id` del contrato del ecommerce: entero estable por pedido, derivado
    de la order_id para que el reintento mande exactamente el mismo cuerpo."""
    return int(hashlib.sha256(order_id.encode()).hexdigest()[:8], 16)


class PedidoInconsistente(ValueError):
    """La orden no tiene renglones confiables (carrito viejo sin `items`) o
    sus renglones no cuadran con el total cobrado: NO se manda y queda
    'rechazado' con el motivo (mandarlo registraría otra cosa que lo vendido)."""


def _items_de_la_orden(order: dict) -> list[dict]:
    """
    Renglones de la orden: [{sku_id, cantidad, total}].

    - Con `items` (el snapshot del checkout guardado al crear el link): uno
      por producto, total = precio_unitario * cantidad redondeado.
    - Orden vieja sin `items` de un solo SKU: un renglón con su cantidad y el
      total de la orden (como hasta ahora).
    - Orden vieja de carrito ("MULTI") o sin SKU: PedidoInconsistente.
    """
    if order.get("items"):
        out = []
        for it in order["items"]:
            cant = max(1, int(it.get("cantidad") or 1))
            pu = round(float(it.get("precio_unitario") or 0), 2)
            out.append({"sku_id": str(it.get("sku_id") or ""), "cantidad": cant,
                        "total": round(pu * cant, 2)})
        return out
    sku = str(order.get("sku_id") or "")
    if not sku or sku == "MULTI":
        raise PedidoInconsistente("pedido sin renglones (orden sin items y sin un SKU único)")
    return [{
        "sku_id": sku,
        "cantidad": int(order.get("cantidad") or 1),
        "total": round(float(order.get("total") or 0), 2),
    }]


def _costo_envio(order: dict) -> float:
    return round(float(order.get("costo_envio") or 0), 2) if order.get("items") else 0.0


def _validar_total(order: dict, renglones: list[dict]) -> None:
    """suma(renglones) + envío == total cobrado (tolerancia de un centavo)."""
    suma = round(sum(r["total"] for r in renglones), 2)
    envio = _costo_envio(order)
    total = round(float(order.get("total") or 0), 2)
    if round(abs(suma + envio - total), 2) > 0.01:
        raise PedidoInconsistente(
            f"renglones no cuadran con el total: {suma:.2f} + envío {envio:.2f} "
            f"!= {total:.2f}")


class CodigoMercurioFaltante(LookupError):
    """Algún renglón no tiene código de Mercurio (el artículo todavía no pasó
    por el sync) o no se pudieron leer: el pedido NO se manda y queda
    'pendiente' hasta la pasada siguiente del job."""


def pedido_desde_orden(order: dict, codigos: dict[str, dict], *,
                       state: str, customer_id_default: str) -> dict:
    """
    Arma el JSON del POST /pedidos (campos obligatorios del contrato).

    Un line_item por renglón de la orden (quantity = cantidad, subtotal =
    total = precio_unitario * cantidad). El envío va aparte, en
    `shipping_total` (solo si es > 0), y `total` es lo cobrado. Antes valida
    que suma(line_items) + envío == total: si no cuadra, o la orden es un
    carrito viejo sin renglones, PedidoInconsistente y no hay pedido.

    Cada renglón necesita su `codigo` de variante (de mercurio_codigos). Nunca
    se cae al external_id: es el id_articulo_mercurio, comparte el espacio
    numérico con los códigos y podría registrar OTRO artículo. Si falta alguno,
    CodigoMercurioFaltante y no hay pedido.
    """
    order_id = str(order.get("order_id"))
    renglones = _items_de_la_orden(order)
    _validar_total(order, renglones)
    faltan = []
    for it in renglones:
        sku = str(it.get("sku_id") or "")
        if not (codigos.get(sku) or {}).get("codigo") and sku not in faltan:
            faltan.append(sku)
    if faltan:
        raise CodigoMercurioFaltante(
            f"sin código Mercurio para {', '.join(faltan)}; "
            "se reintenta después del próximo sync")
    line_items = []
    for it in renglones:
        cod = codigos[str(it.get("sku_id") or "")]
        total_item = round(float(it.get("total") or 0), 2)
        line_items.append({
            "variant_id": cod["codigo"],
            "product_id": cod.get("codigo_padre") or cod["codigo"],
            "quantity": int(it.get("cantidad") or 1),
            "subtotal": total_item,
            "total": total_item,
        })
    pedido = {
        "id": _id_estable(order_id),
        "number": order_id,
        "state": state,
        "customer_id": str(order.get("customer_id") or customer_id_default),
        "total": round(float(order.get("total") or 0), 2),
        "line_items": line_items,
    }
    # El contrato no documenta el envío: va en `shipping_total` (pregunta
    # abierta al proveedor, docs/superpowers/specs/2026-09-14-mercurio-api-v1.md).
    envio = _costo_envio(order)
    if envio > 0:
        pedido["shipping_total"] = envio
    return pedido


async def enviar_pedido_erp(order: dict, *, client=None, db=None) -> Optional[dict]:
    """
    Intenta el alta de la orden en Mercurio. Devuelve el resultado del ERP
    ({id_comprobante, numero, replay}) o None (deshabilitado, rechazado o
    pendiente de reintento). Nunca lanza: el cobro ya ocurrió y la
    confirmación al cliente no depende del ERP.
    """
    from app.config import get_settings
    from app.services.mercurio_service import (
        MercurioError, MercurioPedidoRechazado, get_mercurio_client,
        mercurio_configurado,
    )
    from app.services.order_store import get_order_store

    s = get_settings()
    if not s.mercurio_pedidos_enabled:
        return None
    if client is None:
        if not mercurio_configurado():
            return None
        client = get_mercurio_client()
    if db is None:
        from app.services.db import get_db
        db = get_db(s.database_url)

    order_id = str(order.get("order_id"))
    store = get_order_store(db)

    from app.services.order_store import PedidoDuplicado

    async def _marcar(estado: str, error: str) -> None:
        try:
            await store.marcar_erp(order_id, estado, error=error[:500],
                                   incrementar_intento=True, order=order)
        except PedidoDuplicado as e2:
            logger.warning(f"Pedido {order_id}: no se marca {estado}: {e2}")
        except Exception as e2:
            logger.error(f"Pedido {order_id}: no se pudo marcar {estado}: {e2}")

    # Renglones: sin renglones confiables, o si no cuadran con lo cobrado, no
    # se manda nada (registraría otra cosa que lo vendido) y queda rechazado.
    try:
        renglones = _items_de_la_orden(order)
        _validar_total(order, renglones)
    except PedidoInconsistente as e:
        logger.error(f"Pedido {order_id} NO se manda al ERP: {e}")
        await _marcar("rechazado", str(e))
        return None

    # Códigos de variante. Un error de Postgres se propaga (raise_errors): no
    # se confunde con "no hay códigos" ni se manda nada a ciegas.
    try:
        codigos: dict[str, dict] = {}
        skus = [r["sku_id"] for r in renglones]
        rows = await db.fetch(
            "SELECT external_id, codigo, codigo_padre FROM mercurio_codigos "
            "WHERE branch_id = $1 AND external_id = ANY($2::text[])",
            s.mercurio_branch_id, [x for x in skus if x], raise_errors=True)
        for r in rows:
            codigos[r["external_id"]] = {"codigo": r["codigo"],
                                         "codigo_padre": r["codigo_padre"]}
    except Exception as e:
        motivo = f"no se pudieron leer los códigos Mercurio ({e}); se reintenta"
        logger.warning(f"Pedido {order_id}: {motivo}")
        await _marcar("pendiente", motivo)
        return None

    try:
        pedido = pedido_desde_orden(order, codigos, state=s.mercurio_pedido_state,
                                    customer_id_default=s.mercurio_customer_id_default)
    except CodigoMercurioFaltante as e:
        logger.warning(f"Pedido {order_id}: {e}")
        await _marcar("pendiente", str(e))
        return None
    try:
        resultado = await client.crear_pedido(pedido, idempotency_key=order_id)
    except MercurioPedidoRechazado as e:
        logger.error(f"Pedido {order_id} RECHAZADO por Mercurio: {e}")
        await _marcar("rechazado", str(e))
        return None
    except MercurioError as e:
        logger.warning(f"Pedido {order_id}: ERP inalcanzable, queda pendiente: {e}")
        await _marcar("pendiente", str(e))
        return None

    try:
        await store.marcar_erp(order_id, "enviado",
                               id_comprobante=resultado.get("id_comprobante"),
                               numero=resultado.get("numero"), error=None, order=order)
    except Exception as e:
        logger.error(f"Pedido {order_id}: alta OK pero no se pudo registrar: {e}")
    if resultado.get("replay"):
        logger.info(f"Pedido {order_id}: el ERP ya lo tenía (Idempotent-Replay)")
    else:
        logger.info(f"Pedido {order_id} registrado en Mercurio: "
                    f"{resultado.get('id_comprobante')} (nº {resultado.get('numero')})")
    return resultado


async def despachar_alta_erp(order: dict, *, client=None, db=None) -> Optional[dict]:
    """
    Hook post-cobro (mp_webhook / payway): intenta el alta. La orden ya nació
    'pendiente' en la fila durable (OrderService.create, erp_estado_inicial):
    si el post-cobro se corta antes de llegar acá, o el proceso muere en el
    medio, el job de reintentos la retoma con la misma Idempotency-Key. Igual
    la vuelve a marcar 'pendiente' antes del POST, e inserta la orden si el
    write-through había fallado. Con el flag apagado no hace nada (farmacia).
    """
    from app.config import get_settings
    from app.services.order_store import get_order_store

    s = get_settings()
    if not s.mercurio_pedidos_enabled:
        return None
    if db is None:
        from app.services.db import get_db
        db = get_db(s.database_url)
    from app.services.order_store import PedidoDuplicado
    try:
        await get_order_store(db).marcar_erp(str(order.get("order_id")), "pendiente",
                                             order=order)
    except PedidoDuplicado as e:
        # El pago ya tiene OTRA orden en la tabla (esa es la que va al ERP):
        # mandar esta duplicaría el pedido con otra Idempotency-Key.
        logger.warning(f"Pedido {order.get('order_id')}: no se manda al ERP: {e}")
        return None
    except Exception as e:
        logger.error(f"Pedido {order.get('order_id')}: no se pudo encolar para el ERP: {e}")
    return await enviar_pedido_erp(order, client=client, db=db)


async def reintentar_pedidos_pendientes(*, client=None, db=None, limit: int = 20) -> int:
    """
    Una pasada del job de fondo: reintenta el alta de los pedidos que quedaron
    'pendiente' (ERP caído o proceso reiniciado en el momento del cobro).
    Devuelve cuántos se registraron. Idempotente: la clave es la order_id.
    """
    from app.config import get_settings
    from app.services.order_store import get_order_store

    s = get_settings()
    if not s.mercurio_pedidos_enabled:
        return 0
    if db is None:
        from app.services.db import get_db
        db = get_db(s.database_url)
    try:
        pendientes = await get_order_store(db).pendientes_erp(limit)
    except Exception as e:
        logger.error(f"Reintento de pedidos: no se pudo leer la cola: {e}")
        return 0
    enviados = 0
    for order in pendientes:
        if await enviar_pedido_erp(order, client=client, db=db) is not None:
            enviados += 1
    if pendientes:
        logger.info(f"Reintento de pedidos ERP: {enviados}/{len(pendientes)} registrados")
    return enviados
