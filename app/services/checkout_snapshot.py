"""
Snapshot del checkout: los renglones del pedido tal como se cobran.

El link de pago sale por un total (un carrito es "MULTI" x1 y el envío se suma
al precio), así que ni MP ni Payway devuelven el detalle de lo vendido. El
alta en el ERP (F5) lo necesita: qué artículo, cuántos y a qué precio. Por eso
al crear el link se guarda este snapshot JUNTO con el link, donde sobrevive a
que el cliente siga chateando y cambie la sesión:

  - Mercado Pago: en `metadata` de la preferencia (vuelve en payment["metadata"]
    al consultar el pago);
  - Payway: dentro del pago pendiente de Redis (payway:pending:{pid}).

Forma (claves snake_case, como las devuelve MP):

  {"items": [{"sku_id", "nombre", "cantidad", "precio_unitario", "total"}],
   "costo_envio": 0.0,      # 0 si es retiro
   "total": 2000.0}         # lo que se cobra (productos + envío)

`precio_unitario` es el que se le dijo al cliente (con el descuento de socio
ya aplicado, igual que el link). Al cerrar la venta la orden guarda `items` y
`costo_envio` (order_service.create, extra).
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def armar_snapshot(renglones: list[dict], costo_envio: float, total: float) -> dict:
    """renglones: [{sku_id, nombre, cantidad, precio}] con el precio UNITARIO cobrado."""
    items = []
    for r in renglones:
        cant = max(1, int(r.get("cantidad") or 1))
        pu = round(float(r.get("precio") or 0), 2)
        items.append({"sku_id": str(r.get("sku_id") or ""), "nombre": str(r.get("nombre") or ""),
                      "cantidad": cant, "precio_unitario": pu, "total": round(pu * cant, 2)})
    return {"items": items, "costo_envio": round(float(costo_envio or 0), 2),
            "total": round(float(total or 0), 2)}


def snapshot_de_sesion(session: dict, costo_envio: float, total: float) -> dict:
    """El pedido pendiente de la sesión: el carrito (pending_items, más de uno)
    o el producto único (pending_*), igual que arma el link."""
    items = session.get("pending_items") or []
    if len(items) > 1:
        fuente = [{"sku_id": i.get("sku_id"), "nombre": i.get("nombre"),
                   "cantidad": i.get("cantidad", 1), "precio": i.get("precio")} for i in items]
    else:
        fuente = [{"sku_id": session.get("pending_sku_id"),
                   "nombre": session.get("pending_sku_nombre"),
                   "cantidad": session.get("pending_cantidad", 1),
                   "precio": session.get("pending_precio")}]
    return armar_snapshot(fuente, costo_envio, total)


def snapshot_valido(d) -> Optional[dict]:
    """El snapshot guardado (metadata de MP, pending de Payway) normalizado, o
    None si no está o no tiene la forma esperada (links de antes de esto)."""
    if not isinstance(d, dict):
        return None
    items = d.get("items")
    if not isinstance(items, list) or not items:
        return None
    try:
        renglones = []
        for i in items:
            if not isinstance(i, dict) or not str(i.get("sku_id") or "").strip():
                return None
            if int(i.get("cantidad") or 0) < 1 or i.get("precio_unitario") is None:
                return None
            renglones.append({"sku_id": i["sku_id"], "nombre": i.get("nombre"),
                              "cantidad": int(i["cantidad"]),
                              "precio": float(i["precio_unitario"])})
        return armar_snapshot(renglones, float(d.get("costo_envio") or 0),
                              float(d.get("total") or 0))
    except (TypeError, ValueError):
        return None


def snapshot_del_cobro(guardado, session: dict, sku_cobrado: str,
                       costo_envio_sesion: float, total: float) -> Optional[dict]:
    """
    Renglones de un cobro aprobado: el snapshot guardado con el link; si no
    está (link creado antes de este cambio), el pedido de la sesión como hoy,
    pero solo si sigue siendo el del cobro (carrito para "MULTI", el mismo
    SKU si no). Si no hay forma de saberlo, None: la orden queda sin `items`.
    """
    snap = snapshot_valido(guardado)
    if snap is not None:
        return snap
    items = session.get("pending_items") or []
    if str(sku_cobrado) == "MULTI":
        if len(items) <= 1:
            return None
    elif len(items) > 1 or str(session.get("pending_sku_id") or "") != str(sku_cobrado or ""):
        return None
    if session.get("pending_precio") is None and len(items) <= 1:
        return None
    logger.info(f"Cobro sin snapshot guardado: renglones tomados de la sesión ({sku_cobrado})")
    return snapshot_valido(snapshot_de_sesion(session, costo_envio_sesion, total))


async def costo_envio_vigente(cfg_svc, tipo_entrega: str) -> float:
    """Costo de envío de la config (para armar los renglones desde la sesión
    cuando el cobro no trae snapshot). 0 si es retiro o si no se puede leer."""
    if tipo_entrega != "envio":
        return 0.0
    try:
        from app.services.checkout_helper import costo_envio_de
        return costo_envio_de(await cfg_svc.get_all())
    except Exception as e:
        logger.warning(f"No se pudo leer el costo de envío: {e}")
        return 0.0


def extra_de_la_orden(snap: Optional[dict]) -> Optional[dict]:
    """Lo que la orden guarda del snapshot (order_service.create, extra)."""
    if not snap:
        return None
    return {"items": snap["items"], "costo_envio": snap["costo_envio"]}
