"""
Webhook de Mercado Pago.

POST /mp/notification → recibe notificaciones de pago de MP.

Cuando el pago es aprobado:
  1. Consulta MP API para verificar estado y obtener datos
  2. Extrae el teléfono del cliente de external_reference
  3. Envía mensaje de confirmación por WhatsApp
  4. Actualiza la sesión a "pedido_confirmado"
"""

import hashlib
import hmac
import logging
from fastapi import APIRouter, Request, HTTPException

from app.config import get_settings
from app.services.payment_service import get_payment_service
from app.services.whatsapp_service import get_whatsapp_service
from app.services.session_service import get_session_service
from app.services.order_service import get_order_service
from app.services.config_service import get_config_service

logger = logging.getLogger(__name__)
router = APIRouter()


def _validate_mp_signature(request: Request, body_bytes: bytes, secret: str) -> bool:
    """
    Valida el header x-signature de MP.
    Formato: ts=1;v1=hash
    String a firmar: id:{query_id};request-id:{x-request-id};ts:{ts};
    """
    signature_header = request.headers.get("x-signature", "")
    request_id = request.headers.get("x-request-id", "")
    data_id = request.query_params.get("id", "")

    if not signature_header:
        return False

    parts = dict(p.split("=", 1) for p in signature_header.split(";") if "=" in p)
    ts = parts.get("ts", "")
    v1 = parts.get("v1", "")

    manifest = f"id:{data_id};request-id:{request_id};ts:{ts};"
    expected = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)


@router.post("/mp/notification")
async def mp_notification(request: Request):
    """
    MP envía: { "type": "payment", "data": { "id": "123456789" } }
    También puede enviar como query param: ?id=xxx&topic=payment
    """
    settings = get_settings()
    body_bytes = await request.body()

    # Validar firma si está configurado el secret y MP envió el header.
    # Logueamos si falla pero NO rechazamos: la seguridad real viene de
    # consultar el estado del pago directamente a la API de MP.
    if settings.mp_webhook_secret and request.headers.get("x-signature"):
        if not _validate_mp_signature(request, body_bytes, settings.mp_webhook_secret):
            logger.warning("MP webhook: firma no válida (se procesa igual, se verifica con MP API)")

    # MP puede mandar el ID como query param o en el body
    params = dict(request.query_params)
    body = {}
    try:
        import json
        body = json.loads(body_bytes) if body_bytes else {}
    except Exception:
        pass

    topic = params.get("topic") or body.get("type", "")
    payment_id = (
        params.get("id")
        or str(body.get("data", {}).get("id", ""))
        or str(body.get("id", ""))
    )

    logger.info(f"MP notification → topic={topic} payment_id={payment_id}")

    # Solo procesamos notificaciones de pagos
    if topic not in ("payment", "") or not payment_id or payment_id == "0":
        return {"status": "ignored"}

    payment_svc = get_payment_service(settings.mp_access_token, settings.mp_notification_url)
    payment = await payment_svc.get_payment_info(payment_id)

    if not payment:
        logger.warning(f"No se pudo obtener info del pago {payment_id}")
        return {"status": "error", "detail": "no se pudo consultar el pago"}

    status = payment.get("status")
    external_ref = payment.get("external_reference", "")
    logger.info(f"Pago {payment_id} → status={status} external_ref={external_ref}")

    if status != "approved":
        return {"status": "ignored", "payment_status": status}

    # external_reference = "{phone}_{sku_id}"
    phone = external_ref.split("_")[0] if "_" in external_ref else external_ref
    sku_id = external_ref.split("_", 1)[1] if "_" in external_ref else ""
    if not phone:
        logger.warning(f"No se pudo extraer phone de external_reference={external_ref}")
        return {"status": "error", "detail": "sin phone"}

    # Obtener nombre del producto y cantidad del pago
    items = payment.get("additional_info", {}).get("items") or []
    nombre_producto = items[0].get("title", "") if items else ""
    cantidad = int(items[0].get("quantity", 1)) if items else 1
    total = float(payment.get("transaction_amount", 0))

    # Leer sesión para el modo de entrega (y nombre/total si faltan)
    session_svc = get_session_service(settings.redis_url)
    session = await session_svc.get(phone)
    if not nombre_producto:
        nombre_producto = session.get("pending_sku_nombre") or "tu pedido"
        if not total:
            precio = session.get("pending_precio") or 0
            cantidad = session.get("pending_cantidad") or 1
            total = precio * cantidad

    tipo_entrega = session.get("tipo_entrega") or "retiro"
    direccion_envio = session.get("direccion_envio")

    # ── Crear pedido en la consola de operaciones ────────────────────────────
    order_svc = get_order_service(settings.redis_url)
    order = await order_svc.create(
        phone=phone,
        sku_id=sku_id,
        sku_nombre=nombre_producto,
        cantidad=cantidad,
        total=total,
        mp_payment_id=payment_id,
        tipo_entrega=tipo_entrega,
        direccion_envio=direccion_envio,
    )
    logger.info(f"Pedido registrado: {order['order_id']} entrega={tipo_entrega}")

    # Enviar confirmación por WhatsApp con el código de retiro
    wa_svc = get_whatsapp_service(settings.whatsapp_token, settings.whatsapp_phone_number_id)
    cfg_svc = get_config_service(settings.redis_url)
    cfg = await cfg_svc.get_all()
    hours = await cfg_svc.get_hours()
    pickup_minutes = int(cfg.get("pickup_minutes") or settings.pickup_minutes)
    pickup_text = cfg_svc.get_pickup_text(hours, pickup_minutes)

    pickup_code = order.get("pickup_code", "")
    pickup_line = f"\n{pickup_text}" if pickup_text else ""

    if tipo_entrega == "envio":
        dir_txt = f" a *{direccion_envio}*" if direccion_envio else ""
        mensaje = (
            f"✅ *¡Pago confirmado!*\n\n"
            f"Recibimos tu pago de *{nombre_producto}*. 🙌\n"
            f"🚚 Te lo enviamos a domicilio{dir_txt}. Nos comunicamos para coordinar la entrega.\n"
            f"📋 Código de pedido: *{pickup_code}*\n\n"
            f"¡Muchas gracias! 💊"
        )
    else:
        mensaje = (
            f"✅ *¡Pago confirmado!*\n\n"
            f"Recibimos tu pago de *{nombre_producto}*. 🙌\n"
            f"🔑 *Tu código de retiro es: {pickup_code}*{pickup_line}\n\n"
            f"Guardalo para presentarlo al retirar. ¡Muchas gracias! 💊"
        )

    sent = await wa_svc.send_text(phone, mensaje)
    logger.info(f"Confirmación enviada a {phone}: {sent}")

    # Actualizar sesión
    session_svc = get_session_service(settings.redis_url)
    await session_svc.set_estado(phone, "pedido_confirmado")
    await session_svc.add_message(phone, "assistant", mensaje)

    return {"status": "ok", "phone": phone, "product": nombre_producto, "order_id": order["order_id"]}
