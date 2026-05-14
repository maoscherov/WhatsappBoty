"""
Endpoint de simulación para testing sin WhatsApp real.
POST /simulate  →  procesa un mensaje y devuelve la respuesta del bot.
"""

import logging
import re
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from app.config import get_settings
from app.services.sku_service import get_sku_service
from app.services.session_service import get_session_service
from app.services.intent_service import get_intent_service
from app.services.payment_service import get_payment_service
from app.services.image_service import get_image_service

router = APIRouter()

INTENCIONES_CON_SKU = {"consulta_precio", "consulta_stock", "pedido", "consulta_abierta"}

# Usar palabra completa (word boundary) para evitar que "ibuprofeno" matchee "no"
_PALABRAS_SI = [r"\bsi\b", r"\bsí\b", r"\bdale\b", r"\bok\b", r"\blisto\b",
                r"\bperfecto\b", r"\bconfirmo\b", r"\bvamos\b", r"\bbuenisimo\b"]
_PALABRAS_NO = [r"\bno\b", r"\bcancel\b", r"\bcancela\b", r"\bnope\b",
                r"\bmejor no\b", r"\bno quiero\b"]

def _match_si(texto: str) -> bool:
    return any(re.search(p, texto, re.IGNORECASE) for p in _PALABRAS_SI)

def _match_no(texto: str) -> bool:
    return any(re.search(p, texto, re.IGNORECASE) for p in _PALABRAS_NO)


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
    mp_error: str | None = None
    mp_token_ok: bool | None = None
    texto_extraido: str | None = None   # texto extraído de imagen o audio


@router.post("/simulate", response_model=SimulateResponse)
async def simulate(req: SimulateRequest):
    settings = get_settings()
    try:
        sku_svc = get_sku_service(settings.sku_csv_path)
    except FileNotFoundError:
        logger.error(f"Catálogo no encontrado: {settings.sku_csv_path} — revisá SKU_CSV_PATH")
        sku_svc = None
    session_svc = get_session_service(settings.redis_url)
    intent_svc = get_intent_service(settings.anthropic_api_key)
    payment_svc = get_payment_service(settings.mp_access_token, settings.mp_notification_url)

    session = await session_svc.get(req.phone)
    texto = req.message.strip()
    productos_encontrados: list[dict] = []
    link_pago = None
    mp_error = None
    mp_token_ok = not settings.mp_access_token.startswith("placeholder")

    # ── Confirmación de pedido pendiente ─────────────────────────────────────
    if session.get("estado") == "esperando_confirmacion" and session.get("pending_sku_id"):
        if _match_si(texto):
            cantidad = session.get("pending_cantidad", 1)
            precio_unitario = session["pending_precio"]
            total = precio_unitario * cantidad
            link, mp_error = await payment_svc.crear_link(
                sku_id=session["pending_sku_id"],
                nombre=session["pending_sku_nombre"],
                precio=precio_unitario,
                phone=req.phone,
                cantidad=cantidad,
            )
            link_pago = link
            if link:
                nombre_con_cant = f"{session['pending_sku_nombre']}" + (f" x{cantidad}" if cantidad > 1 else "")
                respuesta = (
                    f"Perfecto! Acá te mando el link de pago para "
                    f"{nombre_con_cant} (${total:,.2f}):\n\n{link}\n\n"
                    "Tiene vigencia de 24hs. ¡Cualquier cosa me avisás!"
                )
                await session_svc.set_estado(req.phone, "esperando_pago")
            else:
                logger.error(f"MP error para {req.phone}: {mp_error}")
                respuesta = "Tuve un problema generando el link de pago. Te paso con alguien del equipo."
                await session_svc.clear_pending(req.phone)
            await session_svc.add_message(req.phone, "user", texto)
            await session_svc.add_message(req.phone, "assistant", respuesta)
            return SimulateResponse(
                respuesta=respuesta, intencion="pedido",
                entidad_producto=session.get("pending_sku_nombre"),
                productos_encontrados=[], estado_sesion=session.get("estado", "idle"),
                link_pago=link_pago, mp_error=mp_error, mp_token_ok=mp_token_ok,
            )

        elif _match_no(texto):
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

    ya_tiene_pending = session.get("estado") == "esperando_confirmacion"

    if intencion in INTENCIONES_CON_SKU and entidad and sku_svc and not ya_tiene_pending:
        # Solo buscar productos si NO hay una confirmación pendiente.
        # Si hay pending, el usuario está refinando la selección (ej: "el de x20"),
        # Claude lo maneja con el historial sin pisar el producto guardado.
        productos_encontrados = sku_svc.buscar(entidad)
        intent_result = await intent_svc.procesar(
            mensaje=texto,
            history=session.get("history", []),
            resultados_sku=productos_encontrados,
        )
        intencion = intent_result.get("intencion", "desconocido")
        entidad = intent_result.get("entidad_producto")
        cantidad = max(1, int(intent_result.get("cantidad") or 1))
        respuesta = intent_result.get("respuesta", "")

        if productos_encontrados:
            primer_producto = (
                next((r for r in productos_encontrados if r["estado"] == "disponible"), None)
                or productos_encontrados[0]
            )
            await session_svc.set_pending(
                phone=req.phone,
                sku_id=primer_producto["sku_id"],
                sku_nombre=primer_producto["nombre"],
                precio=primer_producto["precio"],
                cantidad=cantidad,
                opciones=productos_encontrados,
            )
    elif ya_tiene_pending:
        # Hay pending activo: el usuario puede refinar la selección o la cantidad.
        # Pasamos las opciones guardadas para que Claude identifique cuál eligió.
        pending_opciones = session.get("pending_opciones", [])
        intent_result = await intent_svc.procesar(
            mensaje=texto,
            history=session.get("history", []),
            resultados_sku=pending_opciones if pending_opciones else None,
            label_sku="OPCIONES MOSTRADAS",
        )
        cantidad_nueva = intent_result.get("cantidad")
        sku_index = intent_result.get("sku_seleccionado_index")
        respuesta = intent_result.get("respuesta", "")

        # Si Claude identificó un producto específico de las opciones, actualizar pending
        if sku_index is not None and pending_opciones:
            try:
                idx = int(sku_index)
                if 0 <= idx < len(pending_opciones):
                    elegido = pending_opciones[idx]
                    nueva_cantidad = max(1, int(cantidad_nueva or session.get("pending_cantidad", 1)))
                    await session_svc.set_pending(
                        phone=req.phone,
                        sku_id=elegido["sku_id"],
                        sku_nombre=elegido["nombre"],
                        precio=elegido["precio"],
                        cantidad=nueva_cantidad,
                        opciones=pending_opciones,
                    )
            except (ValueError, TypeError):
                pass
        elif cantidad_nueva and int(cantidad_nueva) > 0 and int(cantidad_nueva) != session.get("pending_cantidad", 1):
            await session_svc.set_pending(
                phone=req.phone,
                sku_id=session["pending_sku_id"],
                sku_nombre=session["pending_sku_nombre"],
                precio=session["pending_precio"],
                cantidad=int(cantidad_nueva),
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
        mp_error=mp_error,
        mp_token_ok=mp_token_ok,
    )


@router.post("/simulate/image", response_model=SimulateResponse)
async def simulate_image(
    phone: str = Form("5491100000000"),
    image: UploadFile = File(...),
):
    """Procesa una imagen (receta, foto de producto) y responde igual que /simulate."""
    settings = get_settings()
    image_svc = get_image_service(settings.anthropic_api_key)

    image_bytes = await image.read()
    media_type = image.content_type or "image/jpeg"

    texto_extraido = await image_svc.extraer_medicamentos(image_bytes, media_type)
    if not texto_extraido:
        return SimulateResponse(
            respuesta="No pude identificar medicamentos en la imagen. ¿Me lo escribís?",
            intencion="desconocido",
            entidad_producto=None,
            productos_encontrados=[],
            estado_sesion="idle",
            texto_extraido=None,
        )

    # Procesamos el texto extraído igual que un mensaje normal
    req = SimulateRequest(phone=phone, message=texto_extraido)
    result = await simulate(req)
    result.texto_extraido = texto_extraido
    return result


@router.delete("/simulate/session/{phone}")
async def reset_session(phone: str):
    settings = get_settings()
    session_svc = get_session_service(settings.redis_url)
    await session_svc.save(phone, {
        "history": [], "pending_sku_id": None,
        "pending_sku_nombre": None, "pending_precio": None, "estado": "idle",
    })
    return {"status": "ok", "message": f"Sesión {phone} reseteada"}
