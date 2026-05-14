"""
Webhook principal de WhatsApp.

GET  /webhook  → verificación de Meta
POST /webhook  → recibe mensajes, procesa y responde

Flujo por mensaje:
  1. Si es audio → transcribir con Whisper
  2. Cargar sesión de Redis
  3. Si hay producto pendiente de confirmar → detectar confirmación/rechazo
  4. Clasificar intención con Claude
  5. Si intención = consulta_precio | consulta_stock | pedido → buscar SKU
  6. Si intención = pedido y producto confirmado → crear link MP
  7. Enviar respuesta por WhatsApp
  8. Guardar historial en Redis
"""

import logging
from fastapi import APIRouter, Request, Query, HTTPException

from app.config import get_settings
from app.models.whatsapp import WhatsAppMessage
from app.services.sku_service import get_sku_service
from app.services.session_service import get_session_service
from app.services.intent_service import get_intent_service
from app.services.payment_service import get_payment_service
from app.services.whatsapp_service import get_whatsapp_service
from app.services.audio_service import get_audio_service

logger = logging.getLogger(__name__)
router = APIRouter()

INTENCIONES_CON_SKU = {"consulta_precio", "consulta_stock", "pedido", "consulta_abierta"}
PALABRAS_SI = {"si", "sí", "dale", "ok", "listo", "perfecto", "confirmo", "quiero", "sí quiero"}
PALABRAS_NO = {"no", "cancel", "cancela", "nope", "no quiero", "mejor no"}


def _deps(settings=None):
    s = settings or get_settings()
    return {
        "wa": get_whatsapp_service(s.whatsapp_token, s.whatsapp_phone_number_id),
        "sku": get_sku_service(s.sku_csv_path),
        "session": get_session_service(s.redis_url),
        "intent": get_intent_service(s.anthropic_api_key),
        "payment": get_payment_service(s.mp_access_token, s.mp_notification_url),
        "audio": get_audio_service(s.anthropic_api_key),  # reutiliza la misma key si usás OpenAI
    }


@router.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    settings = get_settings()
    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_verify_token:
        return int(hub_challenge)
    raise HTTPException(status_code=403, detail="Verify token mismatch")


@router.post("/webhook")
async def receive_message(request: Request):
    body = await request.json()

    try:
        payload = WhatsAppMessage(**body)
    except Exception:
        return {"status": "ignored"}

    messages = payload.get_messages()
    if not messages:
        return {"status": "no_messages"}

    deps = _deps()

    for msg in messages:
        phone = msg["from"]
        msg_id = msg["id"]
        msg_type = msg["type"]

        await deps["wa"].mark_read(msg_id)

        texto = msg["text"]

        # Audio → transcripción
        if msg_type == "audio" and msg["audio_id"]:
            audio_bytes = await deps["wa"].download_audio(msg["audio_id"])
            if audio_bytes:
                texto = await deps["audio"].transcribir(audio_bytes) or ""
                if not texto:
                    await deps["wa"].send_text(phone, "No pude escuchar bien el audio. ¿Me lo mandás por texto?")
                    continue
            else:
                await deps["wa"].send_text(phone, "No pude procesar el audio. ¿Me lo mandás por texto?")
                continue

        if not texto.strip():
            continue

        session = await deps["session"].get(phone)

        # ── Caso especial: hay producto pendiente de confirmar ───────────────
        if session.get("estado") == "esperando_confirmacion" and session.get("pending_sku_id"):
            texto_lower = texto.lower().strip()
            if any(p in texto_lower for p in PALABRAS_SI):
                cantidad = session.get("pending_cantidad", 1)
                precio_unitario = session["pending_precio"]
                total = precio_unitario * cantidad
                link, _ = await deps["payment"].crear_link(
                    sku_id=session["pending_sku_id"],
                    nombre=session["pending_sku_nombre"],
                    precio=precio_unitario,
                    phone=phone,
                    cantidad=cantidad,
                )
                if link:
                    nombre_con_cant = session["pending_sku_nombre"] + (f" x{cantidad}" if cantidad > 1 else "")
                    respuesta = (
                        f"Perfecto! Acá te mando el link de pago para "
                        f"{nombre_con_cant} (${total:,.2f}):\n\n{link}\n\n"
                        "Tiene vigencia de 24hs. ¡Cualquier cosa me avisás!"
                    )
                    await deps["session"].set_estado(phone, "esperando_pago")
                else:
                    respuesta = "Tuve un problema generando el link de pago. Te paso con alguien del equipo."
                    await deps["session"].clear_pending(phone)
                await deps["wa"].send_text(phone, respuesta)
                await deps["session"].add_message(phone, "user", texto)
                await deps["session"].add_message(phone, "assistant", respuesta)
                continue

            elif any(p in texto_lower for p in PALABRAS_NO):
                await deps["session"].clear_pending(phone)
                respuesta = "Dale, sin problema. ¿En qué más te puedo ayudar?"
                await deps["wa"].send_text(phone, respuesta)
                await deps["session"].add_message(phone, "user", texto)
                await deps["session"].add_message(phone, "assistant", respuesta)
                continue

        # ── Flujo normal ─────────────────────────────────────────────────────
        resultados_sku = None

        # Pre-búsqueda rápida para intenciones con producto
        # (Claude recibe los resultados como contexto)
        intent_result = await deps["intent"].procesar(
            mensaje=texto,
            history=session.get("history", []),
        )

        intencion = intent_result.get("intencion", "desconocido")
        entidad = intent_result.get("entidad_producto")
        respuesta = intent_result.get("respuesta", "")

        # Derivar postventa a humano
        if intencion == "cambio_postventa":
            respuesta = (
                "Para cambios y devoluciones te paso con alguien del equipo. "
                "En un momento te contactamos. Gracias por tu paciencia!"
            )
            await deps["wa"].send_text(phone, respuesta)
            await deps["session"].add_message(phone, "user", texto)
            await deps["session"].add_message(phone, "assistant", respuesta)
            continue

        ya_tiene_pending = session.get("estado") == "esperando_confirmacion"

        # Si menciona un producto y NO hay confirmación pendiente → buscar y guardar
        if intencion in INTENCIONES_CON_SKU and entidad and not ya_tiene_pending:
            resultados_sku = deps["sku"].buscar(entidad)
            intent_result = await deps["intent"].procesar(
                mensaje=texto,
                history=session.get("history", []),
                resultados_sku=resultados_sku,
            )
            intencion = intent_result.get("intencion", "desconocido")
            entidad = intent_result.get("entidad_producto")
            cantidad = max(1, int(intent_result.get("cantidad") or 1))
            respuesta = intent_result.get("respuesta", "")

            if resultados_sku:
                primer_producto = (
                    next((r for r in resultados_sku if r["estado"] == "disponible"), None)
                    or resultados_sku[0]
                )
                await deps["session"].set_pending(
                    phone=phone,
                    sku_id=primer_producto["sku_id"],
                    sku_nombre=primer_producto["nombre"],
                    precio=primer_producto["precio"],
                    cantidad=cantidad,
                )

        await deps["wa"].send_text(phone, respuesta)
        await deps["session"].add_message(phone, "user", texto)
        await deps["session"].add_message(phone, "assistant", respuesta)

    return {"status": "ok"}
