"""
API REST para la consola de pedidos.

GET   /orders/api/list                  → listar pedidos (filtrable por estado)
PATCH /orders/api/{order_id}/preparado  → marcar preparado + enviar código WA
PATCH /orders/api/{order_id}/retirado   → marcar retirado
"""

import logging
from fastapi import APIRouter, HTTPException, Query, Depends

from app.config import get_settings
from app.services.order_service import get_order_service
from app.services.whatsapp_service import get_whatsapp_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/orders/api")


def _auth(key: str = Query(None, alias="key")):
    settings = get_settings()
    if settings.bo_key and key != settings.bo_key:
        raise HTTPException(status_code=403, detail="Acceso denegado")


@router.get("/list")
async def list_orders(_=Depends(_auth), estado: str = Query(None)):
    settings = get_settings()
    svc = get_order_service(settings.redis_url)
    orders = await svc.list_all()
    if estado:
        orders = [o for o in orders if o.get("estado") == estado]
    return orders


@router.patch("/{order_id}/preparado")
async def mark_preparado(order_id: str, _=Depends(_auth)):
    settings = get_settings()
    svc = get_order_service(settings.redis_url)

    order = await svc.mark_preparado(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Pedido no encontrado")

    # Enviar código de retiro por WhatsApp
    wa = get_whatsapp_service(settings.whatsapp_token, settings.whatsapp_phone_number_id)
    code = order["pickup_code"]
    nombre = order["sku_nombre"]
    cantidad = order["cantidad"]
    total = order["total"]
    nombre_con_cant = nombre + (f" x{cantidad}" if cantidad > 1 else "")

    msg = (
        f"🎉 *¡Tu pedido está listo para retirar!*\n\n"
        f"*{nombre_con_cant}* — ${total:,.2f}\n\n"
        f"🔑 *Código de retiro: {code}*\n\n"
        f"Presentá este código en la farmacia y te entregamos tu pedido. "
        f"¡Te esperamos! 💊"
    )
    sent = await wa.send_text(order["phone"], msg, simulate_typing=False)
    logger.info(f"Código {code} enviado a {order['phone']}: {sent}")

    return order


@router.patch("/{order_id}/retirado")
async def mark_retirado(order_id: str, _=Depends(_auth)):
    settings = get_settings()
    svc = get_order_service(settings.redis_url)

    order = await svc.mark_retirado(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Pedido no encontrado")

    return order
