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
import time as _time
from datetime import datetime, timezone as _tz
from fastapi import APIRouter, Request, Query, HTTPException

from app.config import get_settings
from app.models.whatsapp import WhatsAppMessage
from app.services.sku_service import get_sku_service
from app.services.session_service import get_session_service
from app.services.intent_service import get_intent_service
from app.services.payment_service import get_payment_service
from app.services.whatsapp_service import get_whatsapp_service
from app.services.audio_service import get_audio_service
from app.services.image_service import get_image_service
from app.services.perf_service import get_perf_service
from app.services.config_service import get_config_service
from app.services.socio_service import get_socio_service
from app.services.db import get_db
from app.services.embeddings import get_embedding_service
from app.services.rag_service import get_rag_service
from app.services.message_store import get_message_store
from app.services.checkout_helper import (
    confirmar_pedido, resolver_entrega, capturar_direccion,
    match_retiro, match_envio, pide_humano, derivar_si_receta, afirma_envio,
    quiere_cambiar_direccion, extraer_direccion_de,
)

logger = logging.getLogger(__name__)
router = APIRouter()

import asyncio as _asyncio

# Lock por teléfono: serializa los mensajes de un mismo usuario para que
# lleguen en orden (ej.: una foto que tarda en procesarse no se "adelanta"
# por un texto posterior más rápido). Instancia única (Railway).
_phone_locks: dict[str, "_asyncio.Lock"] = {}

def _lock_for(phone: str) -> "_asyncio.Lock":
    lk = _phone_locks.get(phone)
    if lk is None:
        lk = _asyncio.Lock()
        _phone_locks[phone] = lk
    return lk

INTENCIONES_CON_SKU = {"consulta_precio", "consulta_stock", "pedido", "consulta_abierta"}
import re as _re

_PALABRAS_SI = [r"\bsi\b", r"\bsí\b", r"\bdale\b", r"\bok\b", r"\blisto\b",
                r"\bperfecto\b", r"\bconfirmo\b", r"\bvamos\b", r"\bva\b",
                r"\bconfirma\b", r"\bmanda\b", r"\bmandame\b", r"\bprocede\b"]

_NO_EXACTO = [r"^no$", r"^nope$", r"^cancel$", r"^cancela$"]
_NO_FRASE  = [r"\bno quiero\b", r"\bno gracias\b", r"\bmejor no\b",
              r"\bcancela(r|me)?\b", r"\bnope\b"]

def _match_si(t: str) -> bool:
    return any(_re.search(p, t, _re.IGNORECASE) for p in _PALABRAS_SI)

def _match_no(t: str) -> bool:
    t = t.strip()
    if any(_re.fullmatch(p, t, _re.IGNORECASE) for p in _NO_EXACTO):
        return True
    return any(_re.search(p, t, _re.IGNORECASE) for p in _NO_FRASE)


def _deps(settings=None):
    s = settings or get_settings()
    audio_key = s.groq_api_key if s.audio_provider == "groq" else s.openai_api_key
    return {
        "wa":      get_whatsapp_service(s.whatsapp_token, s.whatsapp_phone_number_id),
        "sku":     get_sku_service(s.sku_csv_path),
        "session": get_session_service(s.redis_url),
        "intent":  get_intent_service(s.anthropic_api_key, s.openai_api_key, s.llm_provider),
        "payment": get_payment_service(s.mp_access_token, s.mp_notification_url, s.mp_sandbox),
        "audio":   get_audio_service(audio_key, s.audio_provider),
        "image":   get_image_service(s.anthropic_api_key),
        "perf":    get_perf_service(s.redis_url),
        "config":  get_config_service(s.redis_url),
        "socios":  get_socio_service(s.socios_path),
        "msgs":    get_message_store(get_db(s.database_url)),
        "rag":     get_rag_service(get_db(s.database_url), get_embedding_service(s.openai_api_key)),
    }


async def _maybe_send_image(
    wa,
    phone: str,
    resultados: list[dict],
    producto_elegido: dict,
    solicita_imagen: bool,
    send_images_cfg: str,
):
    """Envía la imagen del producto si corresponde según configuración."""
    imagen_url = producto_elegido.get("imagen_url") or (
        next((r["imagen_url"] for r in resultados if r.get("imagen_url")), None)
    )
    if not imagen_url:
        return
    debe_enviar = (
        send_images_cfg == "always" or
        (send_images_cfg == "on_request" and solicita_imagen)
    )
    if debe_enviar:
        await wa.send_image(phone, imagen_url)


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

    _s = get_settings()
    deps = _deps(_s)

    for msg in messages:
        phone   = msg["from"]
        msg_id  = msg["id"]
        msg_type = msg["type"]

        # ── Timing init ──────────────────────────────────────────────────────
        _t0 = _time.perf_counter()
        _steps: dict = {}
        _tipo = msg_type
        _intencion = "desconocido"
        _skip_record = False  # True para mensajes descartados antes de procesar
        _audio_prov_used: str | None = None   # "groq" | "openai" si se transcribió
        texto = ""            # texto del usuario (para historial en finally)
        respuesta = None      # respuesta del bot (para historial en finally)

        # Serializar por teléfono: los mensajes del mismo usuario se procesan
        # en orden de llegada (evita respuestas a destiempo con foto + texto).
        _lock = _lock_for(phone)
        await _lock.acquire()

        try:
            # Deduplicación: ignorar si ya procesamos este mensaje
            if await deps["session"].is_processed(msg_id):
                logger.info(f"Mensaje duplicado ignorado: {msg_id}")
                _skip_record = True
                continue

            await deps["wa"].mark_read(msg_id)

            texto = msg["text"]

            # Audio → transcripción
            if msg_type == "audio" and msg["audio_id"]:
                _ta = _time.perf_counter()
                audio_bytes = await deps["wa"].download_audio(msg["audio_id"])
                if audio_bytes:
                    texto = await deps["audio"].transcribir(audio_bytes) or ""
                    _steps["transcripcion_ms"] = int((_time.perf_counter() - _ta) * 1000)
                    _audio_prov_used = _s.audio_provider or "groq"
                    if not texto:
                        await deps["wa"].send_text(phone, "No pude escuchar bien el audio. ¿Me lo mandás por texto?")
                        continue
                else:
                    await deps["wa"].send_text(phone, "No pude procesar el audio. ¿Me lo mandás por texto?")
                    continue

            # Imagen → clasificar (receta/credencial derivan; producto sigue el flujo)
            if msg_type == "image" and msg.get("image_id"):
                _ti = _time.perf_counter()
                image_bytes = await deps["wa"].download_image(msg["image_id"])
                if not image_bytes:
                    await deps["wa"].send_text(phone, "No pude procesar la imagen. ¿Me lo escribís?")
                    continue
                mime = msg.get("image_mime_type", "image/jpeg")

                # Guardar la imagen en Redis (7 días) para que el operador la vea
                # en el backoffice. La referencia va al historial como /media/chat/{id}.
                _img_id = _re.sub(r"[^\w]", "", msg_id)[-32:] or msg_id[-32:]
                _img_ref = None
                try:
                    _ext = ".png" if "png" in mime else ".webp" if "webp" in mime else ".jpg"
                    from app.services.blob_store import get_blob_store as _gbs
                    ok = await _gbs(_s.redis_url).save(f"chat:{_img_id}", image_bytes, _ext, ttl=7 * 24 * 3600)
                    if ok:
                        _img_ref = f"📷 /media/chat/{_img_id}"
                except Exception:
                    pass
                if _img_ref:
                    await deps["session"].add_message(phone, "user", _img_ref)

                img = await deps["image"].analizar(image_bytes, mime)
                _steps["vision_ms"] = int((_time.perf_counter() - _ti) * 1000)

                # Receta o credencial → derivar a una persona (nunca vender automático)
                if img["tipo"] in ("receta", "credencial"):
                    _intencion = f"imagen_{img['tipo']}"
                    await deps["session"].set_estado(phone, "operador")
                    que = "la receta" if img["tipo"] == "receta" else "la credencial"
                    respuesta = (
                        f"Recibí {que} 🙌. Para gestionarla te paso con alguien del equipo, "
                        "que la revisa y te ayuda. ¡En un momento te contactamos!"
                    )
                    _ts = _time.perf_counter()
                    await deps["wa"].send_text(phone, respuesta)
                    _steps["send_ms"] = int((_time.perf_counter() - _ts) * 1000)
                    if not _img_ref:
                        await deps["session"].add_message(phone, "user", "[imagen recibida]")
                    await deps["session"].add_message(phone, "assistant", respuesta)
                    continue

                texto = img["items"]
                if not texto.strip():
                    await deps["wa"].send_text(phone, "No pude identificar el producto en la imagen. ¿Me lo escribís?")
                    continue

            if not texto.strip():
                _skip_record = True
                continue

            session = await deps["session"].get(phone)

            # Personalización: si el número está en el padrón de socios,
            # Claude recibe nombre y N° de socio para saludar por nombre.
            _ctx_socio = deps["socios"].contexto_para_prompt(phone)
            _socio_data = deps["socios"].find_by_phone(phone)
            _nombre_socio = (_socio_data.get("nombre", "").split() or [""])[0] if _socio_data else ""

            # ── Control de horario de atención ──────────────────────────────
            hours = await deps["config"].get_hours()
            if not deps["config"].is_open_now(hours):
                # Solo avisar una vez cada 10 mins para no spamear
                last_closed = session.get("_last_closed_msg", "")
                now_str = _time.strftime("%Y-%m-%dT%H:%M", _time.gmtime())[:15]  # cada 15min
                if last_closed != now_str:
                    await deps["wa"].send_text(phone, hours.get("closed_message", "Estamos fuera de horario 🙏"))
                    session["_last_closed_msg"] = now_str
                    await deps["session"].save(phone, session)
                _skip_record = True
                continue

            # ── Modo operador: bot silencioso, solo guarda el mensaje ────────
            if session.get("estado") == "operador":
                _intencion = "operador"
                await deps["session"].add_message(phone, "user", texto)
                logger.info(f"Modo operador activo para {phone} — bot silencioso")
                continue

            # ── Pide hablar con una persona → derivar (sin generar link) ─────
            if pide_humano(texto):
                _intencion = "derivado_humano"
                await deps["session"].set_estado(phone, "operador")
                _saludo = f"Dale {_nombre_socio}, " if _nombre_socio else "Dale, "
                respuesta = f"{_saludo}te paso con alguien del equipo. En un momento te contactamos 🙌"
                _ts = _time.perf_counter()
                await deps["wa"].send_text(phone, respuesta)
                _steps["send_ms"] = int((_time.perf_counter() - _ts) * 1000)
                await deps["session"].add_message(phone, "user", texto)
                await deps["session"].add_message(phone, "assistant", respuesta)
                continue

            # ── Estado: eligiendo modo de entrega (retiro / envío) ───────────
            if session.get("estado") == "esperando_entrega" and session.get("pending_sku_id"):
                texto_lower = texto.lower().strip()
                _dir_expl = extraer_direccion_de(texto)
                if _match_no(texto_lower):
                    _intencion = "pedido_cancelado"
                    await deps["session"].clear_pending(phone)
                    respuesta = "Dale, sin problema. ¿En qué más te puedo ayudar?"
                elif _dir_expl:
                    # Escribió una dirección directamente → envío a esa dirección
                    respuesta, _intencion = await capturar_direccion(
                        deps["payment"], deps["session"], phone, session, _dir_expl,
                    )
                else:
                    respuesta, _intencion = await resolver_entrega(
                        deps["payment"], deps["session"], deps["socios"],
                        phone, session,
                        es_retiro=match_retiro(texto_lower),
                        es_envio=match_envio(texto_lower) or afirma_envio(texto_lower),
                    )
                _ts = _time.perf_counter()
                await deps["wa"].send_text(phone, respuesta)
                _steps["send_ms"] = int((_time.perf_counter() - _ts) * 1000)
                await deps["session"].add_message(phone, "user", texto)
                await deps["session"].add_message(phone, "assistant", respuesta)
                continue

            # ── Estado: link enviado — permitir cambiar la dirección ─────────
            if session.get("estado") == "esperando_pago" and session.get("pending_sku_id"):
                _dir_expl = extraer_direccion_de(texto)
                if _dir_expl or quiere_cambiar_direccion(texto):
                    if _dir_expl:
                        respuesta, _intencion = await capturar_direccion(
                            deps["payment"], deps["session"], phone, session, _dir_expl,
                        )
                    else:
                        await deps["session"].set_estado(phone, "esperando_direccion")
                        respuesta, _intencion = (
                            "Dale! Pasame la dirección nueva y te regenero el link 🚚",
                            "esperando_direccion",
                        )
                    _ts = _time.perf_counter()
                    await deps["wa"].send_text(phone, respuesta)
                    _steps["send_ms"] = int((_time.perf_counter() - _ts) * 1000)
                    await deps["session"].add_message(phone, "user", texto)
                    await deps["session"].add_message(phone, "assistant", respuesta)
                    continue
                # Otros mensajes en esperando_pago → siguen al flujo normal

            # ── Estado: esperando dirección de envío ─────────────────────────
            if session.get("estado") == "esperando_direccion" and session.get("pending_sku_id"):
                if _match_no(texto.lower().strip()):
                    _intencion = "pedido_cancelado"
                    await deps["session"].clear_pending(phone)
                    respuesta = "Dale, sin problema. ¿En qué más te puedo ayudar?"
                else:
                    respuesta, _intencion = await capturar_direccion(
                        deps["payment"], deps["session"], phone, session, texto,
                    )
                _ts = _time.perf_counter()
                await deps["wa"].send_text(phone, respuesta)
                _steps["send_ms"] = int((_time.perf_counter() - _ts) * 1000)
                await deps["session"].add_message(phone, "user", texto)
                await deps["session"].add_message(phone, "assistant", respuesta)
                continue

            # ── Caso especial: hay producto pendiente de confirmar ───────────
            if session.get("estado") == "esperando_confirmacion" and session.get("pending_sku_id"):
                texto_lower = texto.lower().strip()
                if _match_no(texto_lower):
                    _intencion = "pedido_cancelado"
                    await deps["session"].clear_pending(phone)
                    respuesta = "Dale, sin problema. ¿En qué más te puedo ayudar?"
                    _ts = _time.perf_counter()
                    await deps["wa"].send_text(phone, respuesta)
                    _steps["send_ms"] = int((_time.perf_counter() - _ts) * 1000)
                    await deps["session"].add_message(phone, "user", texto)
                    await deps["session"].add_message(phone, "assistant", respuesta)
                    continue

                elif _match_si(texto_lower) or match_envio(texto_lower) or match_retiro(texto_lower):
                    # Confirma. Si además ya indicó cómo recibirlo, se resuelve sin re-preguntar.
                    _entrega = ("envio" if match_envio(texto_lower)
                                else "retiro" if match_retiro(texto_lower) else None)
                    cfg_all = await deps["config"].get_all()
                    respuesta, _intencion = await confirmar_pedido(
                        deps["sku"], deps["payment"], deps["session"], deps["socios"],
                        cfg_all, phone, session, entrega=_entrega, nombre=_nombre_socio,
                    )
                    _ts = _time.perf_counter()
                    await deps["wa"].send_text(phone, respuesta)
                    _steps["send_ms"] = int((_time.perf_counter() - _ts) * 1000)
                    await deps["session"].add_message(phone, "user", texto)
                    await deps["session"].add_message(phone, "assistant", respuesta)
                    continue

                else:
                    # Ambiguo: Claude decide con contexto completo de las opciones
                    # SIEMPRE pasamos pending_opciones para que Claude pueda interpretar
                    # "el 1 puede ser?" como selección de la opción 1, no como búsqueda nueva.
                    pending_opciones = session.get("pending_opciones", [])
                    _tc = _time.perf_counter()
                    intent_result = await deps["intent"].procesar(
                        mensaje=texto,
                        history=session.get("history", []),
                        resultados_sku=pending_opciones if pending_opciones else None,
                        label_sku="OPCIONES MOSTRADAS",
                        contexto_cliente=_ctx_socio,
                    )
                    _steps["claude1_ms"] = int((_time.perf_counter() - _tc) * 1000)

                    confirmacion  = intent_result.get("confirmacion")
                    _intencion    = intent_result.get("intencion", "desconocido")
                    respuesta     = intent_result.get("respuesta", "")
                    _entidad_nueva = intent_result.get("entidad_producto")
                    sku_index     = intent_result.get("sku_seleccionado_index")

                    # ── Paso 1: Si el usuario seleccionó una opción de la lista existente,
                    #    actualizar pending ANTES de evaluar confirmacion/cambio.
                    #    Esto resuelve "el 1 puede ser?" → seleccionar opción 1 sin buscar de nuevo.
                    if sku_index is not None and pending_opciones:
                        try:
                            idx = int(sku_index) - 1
                            if 0 <= idx < len(pending_opciones):
                                elegido = pending_opciones[idx]
                                nueva_cantidad = max(1, int(intent_result.get("cantidad") or session.get("pending_cantidad", 1)))
                                await deps["session"].set_pending(
                                    phone=phone,
                                    sku_id=elegido["sku_id"],
                                    sku_nombre=elegido["nombre"],
                                    precio=elegido["precio"],
                                    cantidad=nueva_cantidad,
                                    opciones=pending_opciones,
                                )
                                session = await deps["session"].get(phone)
                                logger.info(f"Opción {sku_index} seleccionada: {elegido['nombre']}")
                        except (ValueError, TypeError):
                            pass

                    # ── Paso 1b: si la opción elegida requiere receta, derivar YA
                    #    (no ofrecer link). Anula el resto del flujo de confirmación.
                    if sku_index is not None and session.get("pending_sku_id"):
                        cfg_all = await deps["config"].get_all()
                        _deriv = await derivar_si_receta(
                            deps["sku"], deps["session"], cfg_all, phone, session["pending_sku_id"],
                            nombre=_nombre_socio,
                        )
                        if _deriv:
                            respuesta = _deriv
                            _intencion = "derivado_receta"
                            confirmacion = None       # saltar confirmación/cambio
                            _entidad_nueva = None

                    # ── Paso 2: _es_cambio solo aplica cuando NO hay selección de opción existente
                    #    y el usuario menciona un producto genuinamente diferente.
                    _es_cambio = (
                        sku_index is None and (
                            confirmacion is False or
                            (confirmacion is None
                             and _entidad_nueva
                             and _intencion in INTENCIONES_CON_SKU)
                        )
                    )

                    if confirmacion is True:
                        cfg_all = await deps["config"].get_all()
                        respuesta, _intencion = await confirmar_pedido(
                            deps["sku"], deps["payment"], deps["session"], deps["socios"],
                            cfg_all, phone, session, nombre=_nombre_socio,
                        )
                    elif _es_cambio:
                        # Cambio genuino de producto (ej: "mejor bayer", "no, un lotrial")
                        # Capturar la intención de Claude ANTES de sobrescribirla,
                        # si no nunca se busca el producto nuevo.
                        nueva_entidad = _entidad_nueva
                        nueva_intencion = _intencion
                        _intencion = "pedido_cancelado"
                        await deps["session"].clear_pending(phone)
                        if nueva_entidad and nueva_intencion in INTENCIONES_CON_SKU:
                            _tsku = _time.perf_counter()
                            resultados_nuevos = deps["sku"].buscar(nueva_entidad)
                            _steps["sku_ms"] = int((_time.perf_counter() - _tsku) * 1000)
                            if resultados_nuevos:
                                _tc2 = _time.perf_counter()
                                ir2 = await deps["intent"].procesar(
                                    mensaje=texto,
                                    history=session.get("history", []),
                                    resultados_sku=resultados_nuevos,
                                    contexto_cliente=_ctx_socio,
                                )
                                _steps["claude2_ms"] = int((_time.perf_counter() - _tc2) * 1000)
                                _intencion = ir2.get("intencion", nueva_intencion)
                                respuesta = ir2.get("respuesta", "")
                                cantidad = max(1, int(ir2.get("cantidad") or 1))
                                idx2 = ir2.get("sku_seleccionado_index")
                                prod2 = None
                                if idx2 is not None:
                                    try:
                                        i2 = int(idx2) - 1
                                        if 0 <= i2 < len(resultados_nuevos):
                                            prod2 = resultados_nuevos[i2]
                                    except (ValueError, TypeError):
                                        pass
                                if not prod2:
                                    prod2 = (
                                        next((r for r in resultados_nuevos if r["estado"] == "disponible"), None)
                                        or resultados_nuevos[0]
                                    )
                                await deps["session"].set_pending(
                                    phone=phone,
                                    sku_id=prod2["sku_id"],
                                    sku_nombre=prod2["nombre"],
                                    precio=prod2["precio"],
                                    cantidad=cantidad,
                                    opciones=resultados_nuevos,
                                )
                            else:
                                respuesta = f"No encontramos {nueva_entidad} en el catálogo. ¿Buscás algo más?"
                        else:
                            respuesta = "Dale, sin problema. ¿En qué más te puedo ayudar?"

                    _ts = _time.perf_counter()
                    await deps["wa"].send_text(phone, respuesta)
                    _steps["send_ms"] = int((_time.perf_counter() - _ts) * 1000)
                    await deps["session"].add_message(phone, "user", texto)
                    await deps["session"].add_message(phone, "assistant", respuesta)
                    continue

            # ── Flujo normal ─────────────────────────────────────────────────
            resultados_sku = None
            _sku_pendiente_nuevo = None   # sku elegido este turno (para chequeo de receta)

            # Claude 1 — Haiku (rápido): clasifica intención + extrae entidad.
            # Para intenciones simples (saludo, social, agradecimiento) su
            # respuesta se usa directamente sin un segundo llamado a Claude.
            _tc = _time.perf_counter()
            intent_result = await deps["intent"].procesar_rapido(
                mensaje=texto,
                history=session.get("history", []),
                contexto_cliente=_ctx_socio,
            )
            _steps["claude1_ms"] = int((_time.perf_counter() - _tc) * 1000)

            intencion = intent_result.get("intencion", "desconocido")
            _intencion = intencion
            entidad = intent_result.get("entidad_producto")
            respuesta = intent_result.get("respuesta", "")

            # Base de conocimiento (RAG): preguntas generales sin producto →
            # responder con la info de la farmacia si hay algo relevante.
            _general = intencion == "desconocido" or (intencion == "consulta_abierta" and not entidad)
            if _general and deps["rag"].enabled():
                _kb = await deps["rag"].kb_search(texto, n=3)
                if _kb:
                    _kb_txt = "\n\n".join(f"{d['titulo']}: {d['contenido']}".strip(": ") for d in _kb)
                    _ir_kb = await deps["intent"].procesar(
                        mensaje=texto,
                        history=session.get("history", []),
                        contexto_cliente=_ctx_socio,
                        contexto_kb=_kb_txt,
                    )
                    respuesta = _ir_kb.get("respuesta") or respuesta

            # Derivar postventa a humano
            if intencion == "cambio_postventa":
                respuesta = (
                    "Para cambios y devoluciones te paso con alguien del equipo. "
                    "En un momento te contactamos. Gracias por tu paciencia!"
                )
                _ts = _time.perf_counter()
                await deps["wa"].send_text(phone, respuesta)
                _steps["send_ms"] = int((_time.perf_counter() - _ts) * 1000)
                await deps["session"].add_message(phone, "user", texto)
                await deps["session"].add_message(phone, "assistant", respuesta)
                continue

            ya_tiene_pending = session.get("estado") == "esperando_confirmacion"

            # Buscar SIEMPRE que Claude haya detectado un producto (entidad),
            # aunque el mensaje también salude (ej: "hola, tenés Dexopral?").
            # Antes solo se buscaba si la intención era de SKU, y un mensaje que
            # arrancaba con saludo quedaba sin búsqueda ("un segundito" al vacío).
            if entidad and not ya_tiene_pending and intencion != "cambio_postventa":
                _tsku = _time.perf_counter()
                resultados_sku = deps["sku"].buscar(entidad)
                # Fallback semántico (pgvector): si el fuzzy no encontró nada,
                # buscar por significado ("algo para la tos", nombres coloquiales).
                if not resultados_sku and deps["rag"].enabled():
                    _sem = await deps["rag"].buscar_semantico(entidad, n=3)
                    for m in _sem:
                        _sku = deps["sku"].get_by_id(m["sku_id"])
                        if _sku:
                            resultados_sku.append(deps["sku"]._to_response(_sku))
                _steps["sku_ms"] = int((_time.perf_counter() - _tsku) * 1000)

                _tc2 = _time.perf_counter()
                intent_result = await deps["intent"].procesar(
                    mensaje=texto,
                    history=session.get("history", []),
                    resultados_sku=resultados_sku,
                    contexto_cliente=_ctx_socio,
                )
                _steps["claude2_ms"] = int((_time.perf_counter() - _tc2) * 1000)

                intencion = intent_result.get("intencion", "desconocido")
                _intencion = intencion
                entidad = intent_result.get("entidad_producto")
                cantidad = max(1, int(intent_result.get("cantidad") or 1))
                respuesta = intent_result.get("respuesta", "")

                if resultados_sku:
                    sku_index = intent_result.get("sku_seleccionado_index")
                    solicita_imagen = bool(intent_result.get("solicita_imagen"))
                    producto_elegido = None

                    # 1. Intentar con el índice que devolvió Claude
                    if sku_index is not None:
                        try:
                            idx = int(sku_index) - 1  # 1-based → 0-based
                            if 0 <= idx < len(resultados_sku):
                                producto_elegido = resultados_sku[idx]
                        except (ValueError, TypeError):
                            pass

                    # 2. Validación por precio: si la respuesta menciona un precio que no
                    #    coincide con el producto elegido, buscar el que sí coincide.
                    #    Evita que index=null caiga al primer resultado incorrecto.
                    if producto_elegido and respuesta:
                        precio_str = f"{producto_elegido['precio']:.2f}".replace(".", ",")
                        precio_str2 = f"{producto_elegido['precio']:,.2f}".replace(".", ",")
                        if precio_str not in respuesta and precio_str2 not in respuesta:
                            # El precio del elegido no aparece en la respuesta → buscar el correcto
                            import re as _re2
                            precios_mencionados = _re2.findall(r'\$[\d.,]+', respuesta)
                            for r_sku in resultados_sku:
                                p_str = f"${r_sku['precio']:,.2f}".replace(".", ",")
                                p_str2 = f"${r_sku['precio']:.2f}".replace(".", ",")
                                if any(p in respuesta for p in [p_str, p_str2]):
                                    producto_elegido = r_sku
                                    logger.info(f"SKU corregido por precio: {r_sku['nombre']}")
                                    break

                    # 3. Fallback: primer vendible (disponible + con precio)
                    if not producto_elegido:
                        producto_elegido = (
                            next((r for r in resultados_sku if r.get("vendible")), None)
                            or resultados_sku[0]
                        )
                    # Solo se marca como pendiente (comprable) si es VENDIBLE.
                    # Si es sin stock / sin precio, no entra al flujo de compra —
                    # Claude ya respondió ofreciendo las alternativas disponibles.
                    if producto_elegido.get("vendible", True):
                        await deps["session"].set_pending(
                            phone=phone,
                            sku_id=producto_elegido["sku_id"],
                            sku_nombre=producto_elegido["nombre"],
                            precio=producto_elegido["precio"],
                            cantidad=cantidad,
                            opciones=resultados_sku,
                        )
                        _sku_pendiente_nuevo = producto_elegido["sku_id"]
                        send_images_cfg = await deps["config"].get("send_images")
                        await _maybe_send_image(
                            deps["wa"], phone, resultados_sku,
                            producto_elegido, solicita_imagen, send_images_cfg,
                        )

            elif ya_tiene_pending:
                pending_opciones = session.get("pending_opciones", [])
                _tc2 = _time.perf_counter()
                intent_result = await deps["intent"].procesar(
                    mensaje=texto,
                    history=session.get("history", []),
                    resultados_sku=pending_opciones if pending_opciones else None,
                    label_sku="OPCIONES MOSTRADAS",
                    contexto_cliente=_ctx_socio,
                )
                _steps["claude2_ms"] = int((_time.perf_counter() - _tc2) * 1000)

                sku_index = intent_result.get("sku_seleccionado_index")
                cantidad_nueva = intent_result.get("cantidad")
                respuesta = intent_result.get("respuesta", "")

                if sku_index is not None and pending_opciones:
                    try:
                        idx = int(sku_index) - 1  # 1-based → 0-based
                        if 0 <= idx < len(pending_opciones):
                            elegido = pending_opciones[idx]
                            nueva_cantidad = max(1, int(cantidad_nueva or session.get("pending_cantidad", 1)))
                            await deps["session"].set_pending(
                                phone=phone,
                                sku_id=elegido["sku_id"],
                                sku_nombre=elegido["nombre"],
                                precio=elegido["precio"],
                                cantidad=nueva_cantidad,
                                opciones=pending_opciones,
                            )
                            _sku_pendiente_nuevo = elegido["sku_id"]
                    except (ValueError, TypeError):
                        pass
                elif cantidad_nueva and int(cantidad_nueva) > 0 and int(cantidad_nueva) != session.get("pending_cantidad", 1):
                    await deps["session"].set_pending(
                        phone=phone,
                        sku_id=session["pending_sku_id"],
                        sku_nombre=session["pending_sku_nombre"],
                        precio=session["pending_precio"],
                        cantidad=int(cantidad_nueva),
                    )

            # Si el producto recién elegido requiere receta, derivar ahora
            # (no ofrecer link). Reemplaza el mensaje de confirmación de Claude.
            if _sku_pendiente_nuevo:
                cfg_all = await deps["config"].get_all()
                _deriv = await derivar_si_receta(
                    deps["sku"], deps["session"], cfg_all, phone, _sku_pendiente_nuevo,
                    nombre=_nombre_socio,
                )
                if _deriv:
                    respuesta = _deriv
                    _intencion = "derivado_receta"

            _ts = _time.perf_counter()
            await deps["wa"].send_text(phone, respuesta)
            _steps["send_ms"] = int((_time.perf_counter() - _ts) * 1000)

            await deps["session"].add_message(phone, "user", texto)
            await deps["session"].add_message(phone, "assistant", respuesta)

        finally:
            _lock.release()
            if not _skip_record:
                _total = int((_time.perf_counter() - _t0) * 1000)
                step_str = " | ".join(f"{k}={v}ms" for k, v in _steps.items()) if _steps else "—"
                logger.info(
                    f"⏱ PERF …{phone[-4:]} tipo={_tipo} intent={_intencion} "
                    f"total={_total}ms | {step_str}"
                )
                # Construir lista de APIs externas usadas en esta llamada
                _apis: list[str] = []
                if "claude1_ms" in _steps or "claude2_ms" in _steps:
                    _apis.append("claude")
                if _audio_prov_used:
                    _apis.append(_audio_prov_used)   # "groq" o "openai"
                if "vision_ms" in _steps:
                    _apis.append("vision")
                await deps["perf"].record({
                    "ts": datetime.now(_tz.utc).isoformat(),
                    "phone_suffix": phone[-4:],
                    "tipo": _tipo,
                    "intencion": _intencion,
                    "total_ms": _total,
                    "steps": dict(_steps),
                    "apis": _apis,
                })
                # Historial permanente en Postgres (best-effort, no-op sin DB)
                try:
                    if texto:
                        await deps["msgs"].save(phone, "user", texto)
                    if respuesta:
                        await deps["msgs"].save(phone, "assistant", respuesta)
                except Exception:
                    pass

    return {"status": "ok"}
