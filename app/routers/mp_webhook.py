"""
Webhook de Mercado Pago.

POST /mp/notification → recibe notificaciones de pago de MP.

Cuando el pago es aprobado:
  1. Consulta MP API para verificar estado y obtener datos
  2. Extrae el teléfono del cliente de external_reference
  3. Envía mensaje de confirmación por WhatsApp
  4. Actualiza la sesión a "pedido_confirmado"
"""

import logging
from fastapi import APIRouter, Request

from app.config import get_settings
from app.services.payment_service import get_payment_service
from app.services.whatsapp_service import get_whatsapp_service
from app.services.session_service import get_session_service

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/mp/notification")
async def mp_notification(request: Request):
    """
    MP envía: { "type": "payment", "data": { "id": "123456789" } }
    También puede enviar como query param: ?id=xxx&topic=payment
    """
    settings = get_settings()

    # MP puede mandar el ID como query param o en el body
    params = dict(request.query_params)
    body = {}
    try:
        body = await request.json()
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
    if not phone:
        logger.warning(f"No se pudo extraer phone de external_reference={external_ref}")
        return {"status": "error", "detail": "sin phone"}

    # Obtener nombre del producto del pago
    items = payment.get("additional_info", {}).get("items") or []
    nombre_producto = items[0].get("title") if items else ""
    if not nombre_producto:
        # Fallback: leer de la sesión
        session_svc = get_session_service(settings.redis_url)
        session = await session_svc.get(phone)
        nombre_producto = session.get("pending_sku_nombre") or "tu pedido"

    # Enviar confirmación por WhatsApp
    wa_svc = get_whatsapp_service(settings.whatsapp_token, settings.whatsapp_phone_number_id)
    mins = settings.pickup_minutes
    mensaje = (
        f"✅ *¡Pago confirmado!*\n\n"
        f"Recibimos tu pago de *{nombre_producto}*. "
        f"Tu pedido está siendo preparado y va a estar listo para retirar "
        f"en aproximadamente *{mins} minutos*. 🙌\n\n"
        f"📍 Farmacia Mutual Independencia\n"
        f"¡Muchas gracias! 💊"
    )

    sent = await wa_svc.send_text(phone, mensaje)
    logger.info(f"Confirmación enviada a {phone}: {sent}")

    # Actualizar sesión
    session_svc = get_session_service(settings.redis_url)
    await session_svc.set_estado(phone, "pedido_confirmado")
    await session_svc.add_message(phone, "assistant", mensaje)

    return {"status": "ok", "phone": phone, "product": nombre_producto}
