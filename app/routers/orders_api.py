"""
API REST para la consola de pedidos.

GET   /orders/api/list                  → listar pedidos (filtrable por estado)
GET   /orders/api/{order_id}            → detalle de un pedido
POST  /orders/api/{order_id}/takeover   → un operador toma el pedido
PATCH /orders/api/{order_id}/preparado  → marcar preparado + enviar código WA
PATCH /orders/api/{order_id}/retirado   → marcar retirado

Las tres últimas aceptan {"agente": "Sofía G."} en el body para dejar trazado
quién hizo la acción. El body es opcional: el backoffice viejo llama sin body.

La lista y el detalle traen además el alta del pedido en el ERP (F5,
Mercurio): erp_estado, erp_ultimo_error, erp_intentos, erp_numero y
erp_id_comprobante, leídos de la tabla durable `orders` (Redis no los tiene).
Sin fila, o con Postgres caído o lento, van en null (nunca rompen la consola).
"""

import asyncio
import json
import logging
from fastapi import APIRouter, HTTPException, Query, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import get_settings
from app.services.order_service import get_order_service
from app.services.whatsapp_service import get_whatsapp_service
from app.services.config_service import get_config_service
from app.services.perfil import get_perfil

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


def _con_personas(o: dict) -> dict:
    """
    Pedido + quién es el cliente (nombre, socio/empleado/no socio, N° de
    socio) y quién lo hizo / atendió (6/10: en Pedidos solo se veía el
    teléfono). Los nombres de operador salen unificados con la lista.
    """
    from app.routers.backoffice import _datos_cliente
    from app.services.operadores_service import canonico
    d = dict(o)
    cli = _datos_cliente(o.get("phone") or "")
    nro = None
    if cli.get("tipo_cliente") == "socio":
        try:
            from app.services.socio_service import get_socio_service
            nro = (get_socio_service(get_settings().socios_path).find_by_phone(o["phone"]) or {}).get("nro_socio")
        except Exception:
            nro = None
    d["cliente"] = {"nombre": cli.get("nombre"), "tipo": cli.get("tipo_cliente"), "nro_socio": nro}
    d["hecho_por"] = canonico(o.get("armado_por")) if o.get("origen") == "operador" else "Bot"
    d["atendido_por"] = canonico(o.get("atendido_por") or o.get("armado_por")) or None
    for k in ("agente", "preparado_por", "retirado_por", "cobrado_por", "cc_cargado_por"):
        if d.get(k):
            d[k] = canonico(d[k])
    return d


async def _estado_erp(order_ids: list) -> dict:
    """{order_id: campos erp_*} de la tabla orders (el alta en el ERP, F5).
    Con Postgres caído o lento (más de 3 s), {}: los campos van en null y la
    consola de pedidos sigue andando."""
    ids = [str(i) for i in order_ids if i]
    if not ids:
        return {}
    try:
        from app.services.db import get_db
        from app.services.order_store import get_order_store
        db = get_db(get_settings().database_url)
        if not db.available():          # deploy sin Postgres: null, sin ruido en el log
            return {}
        return await asyncio.wait_for(get_order_store(db).estado_erp(ids), timeout=3.0)
    except Exception as e:
        logger.warning(f"Pedidos: no se pudo leer el estado del alta en el ERP: {e}")
        return {}


def _con_erp(d: dict, erp: dict) -> dict:
    from app.services.order_store import CAMPOS_ERP
    fila = erp.get(str(d.get("order_id"))) or {}
    for k in CAMPOS_ERP:
        d[k] = fila.get(k)
    return d


@router.get("/list")
async def list_orders(_=Depends(_auth), estado: str = Query(None)):
    settings = get_settings()
    svc = get_order_service(settings.redis_url)
    orders = await svc.list_all()
    if estado:
        orders = [o for o in orders if o.get("estado") == estado]
    erp = await _estado_erp([o.get("order_id") for o in orders])
    return [_con_erp(_con_personas(o), erp) for o in orders]


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
    orders = [_con_personas(o) for o in orders]
    w.writerow(["fecha", "pedido", "telefono", "cliente", "tipo_cliente", "nro_socio",
                "hecho_por", "atendido_por", "producto", "cantidad", "total",
                "pago", "entrega", "direccion", "estado", "codigo",
                "cc_cargado", "cc_cargado_por", "cobrado", "cobrado_por", "agente",
                "origen", "armado_por", "cobra", "repartidor"])
    for o in orders:
        w.writerow([
            o.get("created_at", ""), o.get("order_id", ""), o.get("phone", ""),
            o["cliente"]["nombre"] or "", o["cliente"]["tipo"] or "", o["cliente"]["nro_socio"] or "",
            o["hecho_por"] or "", o["atendido_por"] or "",
            o.get("sku_nombre", ""), o.get("cantidad", 1), o.get("total", 0),
            o.get("pago") or "online", o.get("tipo_entrega", ""),
            o.get("direccion_envio") or "", o.get("estado", ""),
            o.get("pickup_code", ""),
            "si" if o.get("cc_cargado_at") else "no",
            o.get("cc_cargado_por") or "",
            # Cobrado: online y cta. cte. nacen cobrados; efectivo, al marcarlo.
            "si" if (o.get("cobrado_at") or (o.get("pago") or "online") != "efectivo") else "no",
            o.get("cobrado_por") or "", o.get("agente") or "",
            o.get("origen") or "bot", o.get("armado_por") or "",
            o.get("cobra") or "", o.get("repartidor") or "",
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
    return _con_erp(_con_personas(order), await _estado_erp([order_id]))


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

    # Sin texto guardado, el del perfil de rubro (farmacia: el de DEFAULTS).
    if (order.get("tipo_entrega") or "retiro") == "envio":
        plantilla = (cfg.get("pedido_listo_envio_message")
                     or get_perfil().textos["pedido_listo_envio_message"])
    else:
        plantilla = (cfg.get("pedido_listo_retiro_message")
                     or get_perfil().textos["pedido_listo_retiro_message"])

    if (order.get("pago") or "") == "efectivo" and not order.get("cobrado_at"):
        plantilla += "\n\n💵 Recordá que lo abonás en efectivo."

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
    if sent:
        from app.services.message_store import guardar_historico
        await guardar_historico(order["phone"], "assistant", msg)
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


@router.patch("/{order_id}/cobrado")
async def mark_cobrado(order_id: str, request: Request, _=Depends(_auth)):
    """La farmacia cobró un pedido en efectivo (19/9)."""
    settings = get_settings()
    svc = get_order_service(settings.redis_url)
    agente = await _agente_del_body(request)
    order = await svc.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Pedido no encontrado")
    if (order.get("pago") or "online") != "efectivo":
        raise HTTPException(status_code=409, detail="Solo los pedidos en efectivo se marcan como cobrados")
    order = await svc.mark_cobrado(order_id, agente=agente)
    logger.info(f"Pedido {order_id} marcado como cobrado (efectivo) por {agente or '?'}")
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

