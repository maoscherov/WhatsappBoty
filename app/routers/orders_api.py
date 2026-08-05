"""
API REST para la consola de pedidos.

GET   /orders/api/list                  → listar pedidos (filtrable por estado)
GET   /orders/api/{order_id}            → detalle de un pedido
POST  /orders/api/{order_id}/takeover   → un operador toma el pedido
PATCH /orders/api/{order_id}/preparado  → marcar preparado + enviar código WA
PATCH /orders/api/{order_id}/retirado   → marcar retirado

Las tres últimas aceptan {"agente": "Sofía G."} en el body para dejar trazado
quién hizo la acción. El body es opcional: el backoffice viejo llama sin body.
"""

import json
import logging
from fastapi import APIRouter, HTTPException, Query, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import get_settings
from app.services.order_service import get_order_service
from app.services.whatsapp_service import get_whatsapp_service
from app.services.config_service import get_config_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/orders/api")


def _auth(key: str = Query(None, alias="key")):
    settings = get_settings()
    if settings.bo_key and key != settings.bo_key:
        raise HTTPException(status_code=403, detail="Acceso denegado")


async def _agente_del_body(request: Request) -> str | None:
    """
    Lee {"agente": "..."} de forma tolerante: sin body, body vacío o JSON
    inválido devuelven None en vez de 422, para no romper a los clientes que
    hoy llaman a estos PATCH sin cuerpo.
    """
    try:
        raw = await request.body()
        if not raw:
            return None
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    agente = data.get("agente")
    if isinstance(agente, str) and agente.strip():
        return agente.strip()
    return None


class TakeoverIn(BaseModel):
    agente: str
    force: bool = False


@router.get("/list")
async def list_orders(_=Depends(_auth), estado: str = Query(None)):
    settings = get_settings()
    svc = get_order_service(settings.redis_url)
    orders = await svc.list_all()
    if estado:
        orders = [o for o in orders if o.get("estado") == estado]
    return orders


@router.get("/{order_id}")
async def get_order(order_id: str, _=Depends(_auth)):
    settings = get_settings()
    svc = get_order_service(settings.redis_url)
    order = await svc.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Pedido no encontrado")
    return order


@router.post("/{order_id}/takeover")
async def takeover_order(order_id: str, body: TakeoverIn, _=Depends(_auth)):
    """
    Un operador toma el pedido. Si ya lo tomó otro y force es false devuelve
    409 {"error": "ya_tomado", "agente": "<actual>"}.
    """
    agente = (body.agente or "").strip()
    if not agente:
        raise HTTPException(status_code=400, detail="Falta el agente")

    settings = get_settings()
    svc = get_order_service(settings.redis_url)
    order, ocupado_por = await svc.takeover(order_id, agente, force=body.force)

    if ocupado_por:
        return JSONResponse(status_code=409, content={"error": "ya_tomado", "agente": ocupado_por})
    if not order:
        raise HTTPException(status_code=404, detail="Pedido no encontrado")
    return order


@router.patch("/{order_id}/preparado")
async def mark_preparado(order_id: str, request: Request, _=Depends(_auth)):
    settings = get_settings()
    svc = get_order_service(settings.redis_url)

    agente = await _agente_del_body(request)
    order = await svc.mark_preparado(order_id, agente=agente)
    if not order:
        raise HTTPException(status_code=404, detail="Pedido no encontrado")

    # Enviar confirmación de pedido listo con código y horario de retiro
    wa = get_whatsapp_service(settings.whatsapp_token, settings.whatsapp_phone_number_id)
    cfg_svc = get_config_service(settings.redis_url)
    cfg = await cfg_svc.get_all()
    hours = await cfg_svc.get_hours()
    pickup_minutes = int(cfg.get("pickup_minutes") or settings.pickup_minutes)
    pickup_text = cfg_svc.get_pickup_text(hours, pickup_minutes)

    code = order["pickup_code"]
    nombre = order["sku_nombre"]
    cantidad = order["cantidad"]
    total = order["total"]
    nombre_con_cant = nombre + (f" x{cantidad}" if cantidad > 1 else "")
    pickup_line = f"\n{pickup_text}" if pickup_text else ""

    msg = (
        f"🎉 *¡Tu pedido está listo para retirar!*\n\n"
        f"*{nombre_con_cant}* — ${total:,.2f}\n"
        f"🔑 *Código de retiro: {code}*{pickup_line}\n\n"
        f"Presentá este código y te lo entregamos. ¡Te esperamos! 💊"
    )
    sent = await wa.send_text(order["phone"], msg, simulate_typing=False)
    logger.info(f"Código {code} enviado a {order['phone']}: {sent}")

    return order


@router.patch("/{order_id}/retirado")
async def mark_retirado(order_id: str, request: Request, _=Depends(_auth)):
    settings = get_settings()
    svc = get_order_service(settings.redis_url)

    agente = await _agente_del_body(request)
    order = await svc.mark_retirado(order_id, agente=agente)
    if not order:
        raise HTTPException(status_code=404, detail="Pedido no encontrado")

    return order
