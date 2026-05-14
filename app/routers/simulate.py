"""
Endpoint de simulación para testing sin WhatsApp real.
POST /simulate  →  procesa un mensaje y devuelve la respuesta del bot.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import get_settings
from app.services.sku_service import get_sku_service
from app.services.session_service import get_session_service
from app.services.intent_service import get_intent_service
from app.services.payment_service import get_payment_service

router = APIRouter()

INTENCIONES_CON_SKU = {"consulta_precio", "consulta_stock", "pedido", "consulta_abierta"}
PALABRAS_SI = {"si", "sí", "dale", "ok", "listo", "perfecto", "confirmo", "quiero", "sí quiero"}
PALABRAS_NO = {"no", "cancel", "cancela", "nope", "no quiero", "mejor no"}


class SimulateRequest(BaseModel):
    phone: str = "5491100000000"
    message: str


class SimulateResponse(BaseModel):
    respuesta: str
    intencion: str
    entidad_producto: str | None
    productos_encontrados: list[dict]
    estado_sesion: str
    link_pago: str | None = None


@router.post("/simulate", response_model=SimulateResponse)
async def simulate(req: SimulateRequest):
    settings = get_settings()
    sku_svc = get_sku_service(settings.sku_csv_path)
    session_svc = get_session_service(settings.redis_url)
    intent_svc = get_intent_service(settings.anthropic_api_key)
    payment_svc = get_payment_service(settings.mp_access_token, settings.mp_notification_url)

    session = await session_svc.get(req.phone)
    texto = req.message.strip()
    productos_encontrados: list[dict] = []
    link_pago = None

    # ── Confirmación de pedido pendiente ─────────────────────────────────────
    if session.get("estado") == "esperando_confirmacion" and session.get("pending_sku_id"):
        texto_lower = texto.lower()
        if any(p in texto_lower for p in PALABRAS_SI):
            link = await payment_svc.crear_link(
                sku_id=session["pending_sku_id"],
                nombre=session["pending_sku_nombre"],
                precio=session["pending_precio"],
                phone=req.phone,
            )
            link_pago = link
            if link:
                respuesta = (
                    f"Perfecto! Acá te mando el link de pago para "
                    f"{session['pending_sku_nombre']} "
                    f"(${session['pending_precio']:.2f}):\n\n{link}\n\n"
                    "Tiene vigencia de 24hs. ¡Cualquier cosa me avisás!"
                )
                await session_svc.set_estado(req.phone, "esperando_pago")
            else:
                respuesta = "Tuve un problema generando el link de pago. Te paso con alguien del equipo."
                await session_svc.clear_pending(req.phone)
            await session_svc.add_message(req.phone, "user", texto)
            await session_svc.add_message(req.phone, "assistant", respuesta)
            return SimulateResponse(
                respuesta=respuesta, intencion="pedido",
                entidad_producto=session.get("pending_sku_nombre"),
                productos_encontrados=[], estado_sesion=session.get("estado", "idle"),
                link_pago=link_pago,
            )

        elif any(p in texto_lower for p in PALABRAS_NO):
            await session_svc.clear_pending(req.phone)
            respuesta = "Dale, sin problema. ¿En qué más te puedo ayudar?"
            await session_svc.add_message(req.phone, "user", texto)
            await session_svc.add_message(req.phone, "assistant", respuesta)
            return SimulateResponse(
                respuesta=respuesta, intencion="social",
                entidad_producto=None, productos_encontrados=[],
                estado_sesion="idle",
            )

    # ── Flujo normal ─────────────────────────────────────────────────────────
    intent_result = await intent_svc.procesar(
        mensaje=texto,
        history=session.get("history", []),
    )
    intencion = intent_result.get("intencion", "desconocido")
    entidad = intent_result.get("entidad_producto")
    respuesta = intent_result.get("respuesta", "")

    if intencion == "cambio_postventa":
        respuesta = (
            "Para cambios y devoluciones te paso con alguien del equipo. "
            "En un momento te contactamos. Gracias por tu paciencia!"
        )
        await session_svc.add_message(req.phone, "user", texto)
        await session_svc.add_message(req.phone, "assistant", respuesta)
        return SimulateResponse(
            respuesta=respuesta, intencion=intencion,
            entidad_producto=entidad, productos_encontrados=[],
            estado_sesion=session.get("estado", "idle"),
        )

    if intencion in INTENCIONES_CON_SKU and entidad:
        productos_encontrados = sku_svc.buscar(entidad)
        intent_result = await intent_svc.procesar(
            mensaje=texto,
            history=session.get("history", []),
            resultados_sku=productos_encontrados,
        )
        intencion = intent_result.get("intencion", "desconocido")
        entidad = intent_result.get("entidad_producto")
        respuesta = intent_result.get("respuesta", "")

        if intencion == "pedido" and productos_encontrados:
            primer_disponible = next(
                (r for r in productos_encontrados if r["estado"] == "disponible"), None
            )
            if primer_disponible:
                await session_svc.set_pending(
                    phone=req.phone,
                    sku_id=primer_disponible["sku_id"],
                    sku_nombre=primer_disponible["nombre"],
                    precio=primer_disponible["precio"],
                )

    await session_svc.add_message(req.phone, "user", texto)
    await session_svc.add_message(req.phone, "assistant", respuesta)
    session = await session_svc.get(req.phone)

    return SimulateResponse(
        respuesta=respuesta,
        intencion=intencion,
        entidad_producto=entidad,
        productos_encontrados=productos_encontrados,
        estado_sesion=session.get("estado", "idle"),
        link_pago=link_pago,
    )


@router.delete("/simulate/session/{phone}")
async def reset_session(phone: str):
    settings = get_settings()
    session_svc = get_session_service(settings.redis_url)
    await session_svc.save(phone, {
        "history": [], "pending_sku_id": None,
        "pending_sku_nombre": None, "pending_precio": None, "estado": "idle",
    })
    return {"status": "ok", "message": f"Sesión {phone} reseteada"}
