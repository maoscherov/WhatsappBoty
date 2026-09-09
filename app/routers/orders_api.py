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


@router.get("/export.csv")
async def export_orders(_=Depends(_auth), pago: str = Query(None),
                        estado: str = Query(None)):
    """
    Export CSV de pedidos para conciliación (minuta 79: pedidos con cuenta
    corriente). Filtros opcionales: ?pago=cuenta_corriente y/o ?estado=.
    Declarado ANTES de /{order_id} para que la ruta no lo capture.
    """
    import csv as _csv
    import io
    from fastapi.responses import PlainTextResponse

    settings = get_settings()
    orders = await get_order_service(settings.redis_url).list_all()
    if pago:
        orders = [o for o in orders if (o.get("pago") or "online") == pago]
    if estado:
        orders = [o for o in orders if o.get("estado") == estado]

    buf = io.StringIO()
    w = _csv.writer(buf, lineterminator="\n")
    w.writerow(["fecha", "pedido", "telefono", "producto", "cantidad", "total",
                "pago", "entrega", "direccion", "estado", "codigo",
                "cc_cargado", "cc_cargado_por", "agente"])
    for o in orders:
        w.writerow([
            o.get("created_at", ""), o.get("order_id", ""), o.get("phone", ""),
            o.get("sku_nombre", ""), o.get("cantidad", 1), o.get("total", 0),
            o.get("pago") or "online", o.get("tipo_entrega", ""),
            o.get("direccion_envio") or "", o.get("estado", ""),
            o.get("pickup_code", ""),
            "si" if o.get("cc_cargado_at") else "no",
            o.get("cc_cargado_por") or "", o.get("agente") or "",
        ])
    return PlainTextResponse(
        buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=pedidos.csv"})


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


def armar_mensaje_pedido_listo(order: dict, cfg: dict, pickup_text: str = "") -> str:
    """
    Mensaje de "pedido preparado" según el TIPO DE ENTREGA (minuta 79, acción 1):
    hasta ahora siempre decía "pasá a retirarlo", aunque fuera envío a domicilio.

    Plantillas configurables (pedido_listo_retiro_message /
    pedido_listo_envio_message) con placeholders {producto}, {total}, {codigo},
    {direccion} y {horario}.
    """
    nombre = order.get("sku_nombre") or "tu pedido"
    cantidad = int(order.get("cantidad") or 1)
    producto = nombre + (f" x{cantidad}" if cantidad > 1 else "")
    horario = f"\n{pickup_text}" if pickup_text else ""

    if (order.get("tipo_entrega") or "retiro") == "envio":
        plantilla = cfg.get("pedido_listo_envio_message") or (
            "🎉 *¡Tu pedido está listo!*\n\n"
            "*{producto}* — ${total}\n"
            "🚚 Sale para *{direccion}*. Te avisamos cuando esté en camino. 💊"
        )
    else:
        plantilla = cfg.get("pedido_listo_retiro_message") or (
            "🎉 *¡Tu pedido está listo para retirar!*\n\n"
            "*{producto}* — ${total}\n"
            "🔑 *Código de retiro: {codigo}*{horario}\n\n"
            "Presentá este código y te lo entregamos. ¡Te esperamos! 💊"
        )

    return (plantilla
            .replace("{producto}", producto)
            .replace("{total}", f"{float(order.get('total') or 0):,.2f}")
            .replace("{codigo}", str(order.get("pickup_code") or ""))
            .replace("{direccion}", order.get("direccion_envio") or "tu domicilio")
            .replace("{horario}", horario))


@router.patch("/{order_id}/preparado")
async def mark_preparado(order_id: str, request: Request, _=Depends(_auth)):
    settings = get_settings()
    svc = get_order_service(settings.redis_url)

    agente = await _agente_del_body(request)
    order = await svc.mark_preparado(order_id, agente=agente)
    if not order:
        raise HTTPException(status_code=404, detail="Pedido no encontrado")

    # Aviso de pedido listo: con código y horario si es retiro, con la
    # dirección si es envío a domicilio.
    wa = get_whatsapp_service(settings.whatsapp_token, settings.whatsapp_phone_number_id)
    cfg_svc = get_config_service(settings.redis_url)
    cfg = await cfg_svc.get_all()
    hours = await cfg_svc.get_hours()
    pickup_minutes = int(cfg.get("pickup_minutes") or settings.pickup_minutes)
    pickup_text = cfg_svc.get_pickup_text(hours, pickup_minutes)

    msg = armar_mensaje_pedido_listo(order, cfg, pickup_text)
    sent = await wa.send_text(order["phone"], msg, simulate_typing=False)
    logger.info(f"Aviso de pedido listo ({order.get('tipo_entrega') or 'retiro'}) "
                f"enviado a {order['phone']}: {sent}")

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


@router.patch("/{order_id}/cc-cargado")
async def mark_cc_cargado(order_id: str, request: Request, _=Depends(_auth)):
    """La farmacia registró el saldo en su sistema contable (minuta 79)."""
    settings = get_settings()
    svc = get_order_service(settings.redis_url)

    agente = await _agente_del_body(request)
    order = await svc.mark_cc_cargado(order_id, agente=agente)
    if not order:
        raise HTTPException(status_code=404, detail="Pedido no encontrado")
    logger.info(f"Pedido {order_id} marcado como cargado en cta. cte. por {agente or '?'}")
    return order

