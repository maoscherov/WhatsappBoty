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
from app.services.payway_link import get_payway_link_service
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
from app.services.metrics_store import get_metrics_store
from app.services.checkout_helper import (
    confirmar_pedido, resolver_entrega, capturar_direccion,
    match_retiro, match_envio, pide_humano, derivar_si_receta, afirma_envio,
    quiere_cambiar_direccion, extraer_direccion_de, contiene_link, pide_pago_manual,
    producto_respaldado, parece_direccion,
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
                r"\bconfirma\b", r"\bmanda\b", r"\bmandame\b", r"\bprocede\b",
                r"\bbueno\b", r"\bpor favor\b", r"\bobvio\b", r"\bclaro\b"]

_NO_EXACTO = [r"^no$", r"^nope$", r"^cancel$", r"^cancela$"]
_NO_FRASE  = [r"\bno quiero\b", r"\bno gracias\b", r"\bmejor no\b",
              r"\bcancela(r|me)?\b", r"\bnope\b"]

def _match_si(t: str) -> bool:
    return any(_re.search(p, t, _re.IGNORECASE) for p in _PALABRAS_SI)

_AFIRMACION = (r"\b(si|sí|dale|ok|okey|oka|listo|bueno|claro|obvio|perfecto|genial|va|vale|"
               r"por|favor|porfa|gracias|please|consultalo|consulta|consultá|averigualo|"
               r"averigua|averiguá|preguntalo|pregunta|encargalo|encarga|encargá|lo|la|me|"
               r"te|eso|esa|ese|sería|seria|estaría|estaria|buenísimo|buenisimo|joya|barbaro|bárbaro)\b")


def _es_afirmacion_pura(t: str) -> bool:
    """
    True si el mensaje es SÓLO una aceptación ("sí por favor", "dale, consultalo")
    y no una afirmación que además pide otra cosa ("dale, mandame un Dove").
    Se quitan las palabras de aceptación y cortesía: si no queda nada sustancial,
    era una aceptación pura.
    """
    if not _match_si(t) or _match_no(t):
        return False
    resto = _re.sub(_AFIRMACION, " ", t.lower())
    resto = _re.sub(r"[^\wáéíóúñ]+", " ", resto)
    return len(resto.replace(" ", "")) <= 3


def _match_no(t: str) -> bool:
    t = t.strip()
    if any(_re.fullmatch(p, t, _re.IGNORECASE) for p in _NO_EXACTO):
        return True
    return any(_re.search(p, t, _re.IGNORECASE) for p in _NO_FRASE)


def payment_svc_para(cfg: dict, s=None):
    """
    Proveedor de cobro activo. Se elige desde el backoffice (clave
    payment_provider); si no está configurado, cae a la variable de entorno.
    """
    s = s or get_settings()
    prov = (cfg.get("payment_provider") or s.payment_provider or "mercadopago").strip().lower()
    if prov == "payway":
        return get_payway_link_service()
    return get_payment_service(s.mp_access_token, s.mp_notification_url, s.mp_sandbox)


def _deps(settings=None):
    s = settings or get_settings()
    audio_key = s.groq_api_key if s.audio_provider == "groq" else s.openai_api_key
    return {
        "wa":      get_whatsapp_service(s.whatsapp_token, s.whatsapp_phone_number_id),
        "sku":     get_sku_service(s.sku_csv_path),
        "session": get_session_service(s.redis_url),
        "intent":  get_intent_service(s.anthropic_api_key, s.openai_api_key, s.llm_provider, s.vertical),
        "payment": (get_payway_link_service() if s.payment_provider == "payway"
                    else get_payment_service(s.mp_access_token, s.mp_notification_url, s.mp_sandbox)),
        "audio":   get_audio_service(audio_key, s.audio_provider),
        "image":   get_image_service(s.anthropic_api_key, s.openai_api_key, s.llm_provider),
        "perf":    get_perf_service(s.redis_url),
        "config":  get_config_service(s.redis_url),
        "socios":  get_socio_service(s.socios_path),
        "msgs":    get_message_store(get_db(s.database_url)),
        "metrics": get_metrics_store(get_db(s.database_url)),
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


async def _flujo_mutual(deps, phone: str, session: dict, texto: str,
                        ctx_socio, nombre_socio: str, steps: dict) -> tuple[str, str]:
    """
    Vertical "mutual": responde con la base de conocimiento institucional y
    deriva a una persona todo lo que toque cuentas o dinero.

    Devuelve (respuesta, intencion). No genera links de pago ni usa catálogo.
    """
    from app.services.mutual_helper import requiere_derivacion_financiera, mensaje_derivacion

    cfg = await deps["config"].get_all()

    # 0. Aceptó que lo pasemos con un oficial (tras una simulación).
    if session.get("derivacion_ofrecida") == "prestamo":
        _s0 = await deps["session"].get(phone)
        _s0.pop("derivacion_ofrecida", None)
        await deps["session"].save(phone, _s0)
        if _es_afirmacion_pura(texto):
            await deps["session"].set_estado(phone, "operador", motivo="prestamo")
            return (cfg.get("mutual_derivar_oficial_message") or
                    "Dale, te paso con un oficial de créditos 🙌 En un momento te contactan."
                    ), "derivado_prestamo"

    # 1. Consultas de cuenta: derivan siempre, antes de llegar al modelo.
    motivo = requiere_derivacion_financiera(texto)
    if motivo:
        await deps["session"].set_estado(phone, "operador", motivo=motivo)
        return mensaje_derivacion(motivo, nombre_socio), f"derivado_{motivo}"

    # 2. Corte por conversación larga: evita el efecto bucle (spec 4.2).
    turnos = len([m for m in session.get("history", []) if m.get("role") == "user"]) + 1
    max_turnos = int(cfg.get("mutual_max_turnos") or 30)
    max_minutos = int(cfg.get("mutual_max_minutos") or 90)
    inicio = session.get("_conv_inicio") or _time.time()
    minutos = (_time.time() - float(inicio)) / 60
    if turnos >= max_turnos or minutos >= max_minutos:
        await deps["session"].set_estado(phone, "operador", motivo="conversacion_larga")
        logger.info(f"Derivación por conversación larga: {phone} turnos={turnos} min={minutos:.0f}")
        return (cfg.get("mutual_corte_message") or
                "Para no hacerte perder más tiempo, te paso con alguien del equipo que "
                "sigue con vos desde acá 🙌"), "derivado_conversacion_larga"

    # 3. Base de conocimiento como contexto (es la única fuente de verdad).
    _tkb = _time.perf_counter()
    docs = await deps["rag"].kb_search(texto, n=4)
    steps["kb_ms"] = int((_time.perf_counter() - _tkb) * 1000)
    contexto_kb = "\n\n".join(f"{d['titulo']}: {d['contenido']}".strip(": ") for d in docs) or None

    _tc = _time.perf_counter()
    resultado = await deps["intent"].procesar(
        mensaje=texto,
        history=session.get("history", []),
        contexto_cliente=ctx_socio,
        contexto_kb=contexto_kb,
    )
    steps["claude_ms"] = int((_time.perf_counter() - _tc) * 1000)

    intencion = resultado.get("intencion", "desconocido")
    respuesta = (resultado.get("respuesta") or "").strip() or (
        "Disculpá, no pude procesar tu consulta. ¿Me la repetís?")

    # Simulación de préstamo: los números los calcula el código, nunca el modelo.
    if str(cfg.get("mutual_simulador_activo", "true")).lower() == "true":
        from app.services.mutual_helper import (simular_prestamo, texto_simulacion,
                                                menciona_simulacion)
        try:
            monto = float(resultado.get("simulacion_monto") or 0)
            cuotas = int(resultado.get("simulacion_cuotas") or 0)
        except (TypeError, ValueError):
            monto, cuotas = 0, 0
        # El modelo arrastra los datos del turno anterior: sólo se simula si el
        # mensaje actual efectivamente pide una simulación.
        if (monto > 0 or cuotas > 0) and not menciona_simulacion(texto):
            logger.info(f"Simulación descartada (el mensaje no la pide): {texto[:60]!r}")
            monto = cuotas = 0
        if monto > 0 and cuotas > 0:
            sim = simular_prestamo(monto, cuotas, cfg)
            respuesta = texto_simulacion(sim, cfg)
            intencion = "simulacion_prestamo"
            logger.info(f"Simulación de préstamo: {monto} en {cuotas} cuotas → {sim}")
            # Se ofrece el oficial: si acepta en el próximo mensaje, se deriva.
            if not sim.get("error"):
                _s2 = await deps["session"].get(phone)
                _s2["derivacion_ofrecida"] = "prestamo"
                await deps["session"].save(phone, _s2)

    # 4. Pidió hablar con una persona.
    if intencion == "derivacion" or pide_humano(texto):
        await deps["session"].set_estado(phone, "operador", motivo="pidio_humano")
        return respuesta, "derivado_humano"

    # 5. Frustración sostenida: dos mensajes negativos seguidos → ofrecer escalar.
    sesion = await deps["session"].get(phone)
    negativos = int(sesion.get("_negativos") or 0)
    negativos = negativos + 1 if resultado.get("sentimiento") == "negativo" else 0
    sesion["_negativos"] = negativos
    if not sesion.get("_conv_inicio"):
        sesion["_conv_inicio"] = _time.time()
    await deps["session"].save(phone, sesion)

    umbral = int(cfg.get("mutual_negativos_para_escalar") or 2)
    if umbral and negativos >= umbral:
        await deps["session"].set_estado(phone, "operador", motivo="cliente_molesto")
        logger.info(f"Derivación por emoción negativa sostenida: {phone} ({negativos} seguidos)")
        return (cfg.get("mutual_escalada_message") or
                "Perdón por las vueltas 🙏 Te paso con alguien del equipo para que te ayude "
                "personalmente."), "derivado_cliente_molesto"

    # 6. Conversación larga: recordar que puede hablar con un asesor (spec 4.4).
    turno_aviso = int(cfg.get("mutual_turno_ofrecer_asesor") or 10)
    if turno_aviso and turnos >= turno_aviso and "asesor" not in respuesta.lower():
        respuesta += "\n\nSi preferís, también te puedo pasar con un asesor 🙂"

    return respuesta, intencion


async def _responder_consulta_en_flujo(deps, phone: str, session: dict, texto: str,
                                       ctx_socio, situacion: str, fallback: str) -> str:
    """
    Responde una consulta hecha en medio de un paso del flujo (elegir entrega,
    dar dirección) sin sacar al cliente de ese paso: le pasa a Claude el pedido
    pendiente y la situación, y devuelve su respuesta. Ante cualquier problema
    cae al mensaje de siempre — el flujo nunca se corta por esto.
    """
    try:
        pend = deps["sku"].get_by_id(session.get("pending_sku_id") or "")
        contexto = [deps["sku"]._to_response(pend)] if pend else None
        resultado = await deps["intent"].procesar(
            mensaje=texto,
            history=session.get("history", []),
            resultados_sku=contexto,
            label_sku="PEDIDO PENDIENTE",
            contexto_cliente=ctx_socio,
            situacion=situacion,
        )
        return (resultado.get("respuesta") or "").strip() or fallback
    except Exception as e:
        logger.warning(f"No se pudo responder la consulta en flujo para {phone}: {e}")
        return fallback


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


async def _descargar_url(url: str) -> bytes | None:
    """Descarga un archivo por URL (Kapso entrega los adjuntos así)."""
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, timeout=30, follow_redirects=True)
            return r.content if r.status_code == 200 else None
    except Exception as e:
        logger.warning(f"No se pudo descargar el adjunto: {e}")
        return None


def _kapso_a_mensajes(evento: dict) -> list[dict]:
    """
    Traduce un evento de Kapso al formato interno de mensajes.

    Kapso manda su propio payload (no el de Meta): estructura plana con
    `message` y `conversation`, el audio ya transcripto y los archivos con URL
    directa en vez de un id para descargar.

    Se ignoran los mensajes salientes (`direction: outbound`): son los que
    enviamos nosotros y reprocesarlos haría que el bot se responda solo.
    """
    msg = evento.get("message") or {}
    kapso = msg.get("kapso") or {}
    if kapso.get("direction") == "outbound":
        return []

    telefono = msg.get("from") or (evento.get("conversation") or {}).get("phone_number")
    if not telefono:
        return []

    tipo = msg.get("type") or "text"
    media_url = kapso.get("media_url") or (kapso.get("media_data") or {}).get("url")
    transcripcion = (kapso.get("transcript") or {}).get("text") or ""

    return [{
        "from": str(telefono).lstrip("+"),
        "id": msg.get("id") or "",
        "type": tipo,
        "text": (msg.get("text") or {}).get("body", "") or (msg.get("image") or {}).get("caption", ""),
        "audio_id": None,
        "image_id": None,
        "image_mime_type": (kapso.get("media_data") or {}).get("content_type", "image/jpeg"),
        "phone_number_id": evento.get("phone_number_id"),
        # Propios de Kapso
        "media_url": media_url,
        "texto_transcripto": transcripcion,
    }]


def _firma_kapso_valida(body_bytes: bytes, firma: str, secret: str) -> bool:
    """
    Verifica el header X-Webhook-Signature: HMAC-SHA256 del cuerpo, en hexa.

    Se compara contra dos serializaciones del JSON porque Kapso firma el objeto
    ya serializado (con separadores estilo JavaScript) y no siempre coincide
    byte a byte con lo que llega.
    """
    import hashlib
    import hmac as _hmac
    import json as _json

    if not firma:
        return False
    candidatos = [body_bytes]
    try:
        data = _json.loads(body_bytes)
        candidatos.append(_json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode())
        candidatos.append(_json.dumps(data, ensure_ascii=False).encode())
    except Exception:
        pass
    firma = firma.strip().lower().removeprefix("sha256=")
    for cuerpo in candidatos:
        esperada = _hmac.new(secret.encode(), cuerpo, hashlib.sha256).hexdigest()
        if _hmac.compare_digest(esperada, firma):
            return True
    return False


@router.post("/webhook/kapso")
async def receive_kapso(request: Request):
    """
    Webhook de Kapso (tipo "Kapso events"). Es el único disponible en sandbox.

    Acepta un evento suelto o un lote (`{batch: true, data: [...]}`), y sólo
    procesa los mensajes entrantes. Si hay secret configurado, se exige la
    firma: sin eso, cualquiera que conozca la URL podría inyectar mensajes.
    """
    body_bytes = await request.body()
    _secret = get_settings().kapso_webhook_secret
    if _secret:
        if not _firma_kapso_valida(body_bytes, request.headers.get("x-webhook-signature", ""), _secret):
            logger.warning("Kapso webhook: firma inválida — mensaje descartado")
            raise HTTPException(status_code=401, detail="firma inválida")
    else:
        logger.warning("KAPSO_WEBHOOK_SECRET sin configurar: el webhook acepta "
                       "requests sin verificar su origen")

    body = await request.json()
    tipo_evento = request.headers.get("x-webhook-event", "")
    logger.info(f"Kapso webhook → evento={tipo_evento or '(sin header)'} "
                f"batch={bool(body.get('batch'))}")

    eventos = body.get("data") if body.get("batch") else [body.get("data") or body]
    mensajes: list[dict] = []
    for ev in (eventos or []):
        if not isinstance(ev, dict):
            continue
        # Sólo mensajes recibidos; el resto de los eventos se ignoran.
        if tipo_evento and "message.received" not in tipo_evento and not ev.get("message"):
            continue
        mensajes.extend(_kapso_a_mensajes(ev))

    if not mensajes:
        return {"status": "no_messages"}
    return await procesar_mensajes(mensajes)


@router.post("/webhook")
async def receive_message(request: Request):
    """Webhook de la Cloud API de Meta (formato oficial de WhatsApp)."""
    body = await request.json()

    try:
        payload = WhatsAppMessage(**body)
    except Exception:
        return {"status": "ignored"}

    messages = payload.get_messages()
    if not messages:
        return {"status": "no_messages"}
    return await procesar_mensajes(messages)


async def procesar_mensajes(messages: list[dict]) -> dict:
    """
    Procesa los mensajes ya normalizados, venga el webhook de Meta o de Kapso.

    Cada mensaje es un dict con: from, id, type, text, audio_id, image_id,
    image_mime_type, phone_number_id. Kapso además puede traer `media_url`
    (descarga directa) y `texto_transcripto` (audio ya pasado a texto).
    """
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

            # Audio con transcripción de Kapso: ya viene resuelto, no hace falta
            # descargar ni pasar por Whisper.
            if msg_type == "audio" and msg.get("texto_transcripto"):
                texto = msg["texto_transcripto"]
                _audio_prov_used = "kapso"

            # Audio → transcripción
            elif msg_type == "audio" and msg["audio_id"]:
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
            if msg_type == "image" and (msg.get("image_id") or msg.get("media_url")):
                _ti = _time.perf_counter()
                if msg.get("media_url"):
                    image_bytes = await _descargar_url(msg["media_url"])   # Kapso: URL directa
                else:
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
                    await deps["session"].set_estado(phone, "operador",
                                                     motivo="receta_foto" if img["tipo"] == "receta" else "credencial")
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

            # ── Receta/bono enviado como LINK → derivar (igual que la foto) ──
            if contiene_link(texto):
                _intencion = "receta_link"
                await deps["session"].set_estado(phone, "operador", motivo="receta_link")
                respuesta = (
                    "Recibí tu link 🙌. Para gestionarlo te paso con alguien del equipo, "
                    "que lo revisa y te ayuda. ¡En un momento te contactamos!"
                )
                _ts = _time.perf_counter()
                await deps["wa"].send_text(phone, respuesta)
                _steps["send_ms"] = int((_time.perf_counter() - _ts) * 1000)
                await deps["session"].add_message(phone, "user", texto)
                await deps["session"].add_message(phone, "assistant", respuesta)
                continue

            # ── Vertical "mutual": información + derivación, sin venta ───────
            if _s.vertical == "mutual":
                respuesta, _intencion = await _flujo_mutual(
                    deps, phone, session, texto, _ctx_socio, _nombre_socio, _steps,
                )
                _ts = _time.perf_counter()
                await deps["wa"].send_text(phone, respuesta)
                _steps["send_ms"] = int((_time.perf_counter() - _ts) * 1000)
                await deps["session"].add_message(phone, "user", texto)
                await deps["session"].add_message(phone, "assistant", respuesta)
                continue

            # ── Aceptó que consultemos un producto que no tenemos → derivar ───
            # Sólo si no hay una compra en curso: con un pedido pendiente, un
            # "dale" es la confirmación de esa compra, no de la consulta.
            _ofrecida = session.get("derivacion_ofrecida")
            if _ofrecida and not session.get("pending_sku_id"):
                _cfg_ss = await deps["config"].get_all()
                if _es_afirmacion_pura(texto):
                    _intencion = "sin_stock_derivado"
                    _s = await deps["session"].get(phone)
                    _s.pop("derivacion_ofrecida", None)
                    await deps["session"].save(phone, _s)
                    await deps["session"].set_estado(phone, "operador", motivo="sin_stock")
                    respuesta = _cfg_ss.get("sin_stock_derivar_message") or (
                        "Te paso con alguien del equipo para ver si podemos conseguirlo 🙌"
                    )
                    _ts = _time.perf_counter()
                    await deps["wa"].send_text(phone, respuesta)
                    _steps["send_ms"] = int((_time.perf_counter() - _ts) * 1000)
                    await deps["session"].add_message(phone, "user", texto)
                    await deps["session"].add_message(phone, "assistant", respuesta)
                    continue
                # No aceptó: la oferta vale sólo para el turno siguiente.
                _s = await deps["session"].get(phone)
                _s.pop("derivacion_ofrecida", None)
                await deps["session"].save(phone, _s)

            # ── Pide transferencia/efectivo → según config del backoffice ────
            #   "derivar" (default) → atención humana.
            #   "solo_tarjeta"      → avisa que solo hay tarjeta, sin derivar.
            _cfg_pm = await deps["config"].get_all()
            # El proveedor de cobro se elige desde el backoffice, no por deploy.
            deps["payment"] = payment_svc_para(_cfg_pm, _s)
            # Compat: derivar_pago_manual=false (clave vieja) equivale a solo_tarjeta.
            _pm_mode = _cfg_pm.get("pago_manual_mode") or "derivar"
            if str(_cfg_pm.get("derivar_pago_manual", "true")).lower() == "false":
                _pm_mode = "solo_tarjeta"
            if pide_pago_manual(texto) and _pm_mode == "solo_tarjeta":
                _intencion = "pago_solo_tarjeta"
                respuesta = _cfg_pm.get("pago_solo_tarjeta_message") or (
                    "Por este canal aceptamos pago con tarjeta (débito o crédito) 💳. "
                    "Si querés, seguimos con tu pedido y te mando el link de pago seguro."
                )
                _ts = _time.perf_counter()
                await deps["wa"].send_text(phone, respuesta)
                _steps["send_ms"] = int((_time.perf_counter() - _ts) * 1000)
                await deps["session"].add_message(phone, "user", texto)
                await deps["session"].add_message(phone, "assistant", respuesta)
                continue
            if pide_pago_manual(texto) and _pm_mode == "derivar":
                _intencion = "pago_manual"
                await deps["session"].set_estado(phone, "operador", motivo="transferencia_efectivo")
                respuesta = _cfg_pm.get("pago_manual_message") or (
                    "Dale! Para pagar por ese medio te paso con alguien del equipo, "
                    "que lo coordina con vos 🙌. ¡En un momento te contactamos!"
                )
                _ts = _time.perf_counter()
                await deps["wa"].send_text(phone, respuesta)
                _steps["send_ms"] = int((_time.perf_counter() - _ts) * 1000)
                await deps["session"].add_message(phone, "user", texto)
                await deps["session"].add_message(phone, "assistant", respuesta)
                continue

            # ── Pide hablar con una persona → derivar (sin generar link) ─────
            if pide_humano(texto):
                _intencion = "derivado_humano"
                await deps["session"].set_estado(phone, "operador", motivo="pidio_humano")
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
                    _es_retiro = match_retiro(texto_lower)
                    _es_envio = match_envio(texto_lower) or afirma_envio(texto_lower)
                    if not _es_retiro and not _es_envio:
                        # No eligió entrega: está preguntando otra cosa (precio,
                        # demora, si llega a tal zona). Se responde la consulta y
                        # se vuelve a ofrecer la elección, en lugar de repetir la
                        # pregunta ignorando lo que preguntó.
                        _intencion = "consulta_en_entrega"
                        respuesta = await _responder_consulta_en_flujo(
                            deps, phone, session, texto, _ctx_socio,
                            "El cliente ya confirmó este pedido y está eligiendo cómo recibirlo. "
                            "Respondé su consulta con los datos del pedido y terminá preguntándole "
                            "si prefiere *retiro en sucursal* o *envío a domicilio*. "
                            "No generes links de pago ni cambies el producto.",
                            "¿Preferís *retiro en sucursal* o *envío a domicilio*? 🙂",
                        )
                    else:
                        respuesta, _intencion = await resolver_entrega(
                            deps["payment"], deps["session"], deps["socios"],
                            phone, session,
                            es_retiro=_es_retiro, es_envio=_es_envio,
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
                elif extraer_direccion_de(texto) or parece_direccion(texto):
                    respuesta, _intencion = await capturar_direccion(
                        deps["payment"], deps["session"], phone, session, texto,
                    )
                else:
                    # No es una dirección: es una consulta ("¿cuánto sale el
                    # envío?"). Antes se tomaba el texto como dirección y el
                    # link salía con esa frase como domicilio de entrega.
                    _intencion = "consulta_en_direccion"
                    respuesta = await _responder_consulta_en_flujo(
                        deps, phone, session, texto, _ctx_socio,
                        "El cliente está por darnos la dirección de envío de este pedido. "
                        "Respondé su consulta y terminá pidiéndole la dirección completa "
                        "(calle, número y localidad). No generes links de pago.",
                        "Pasame la dirección completa (calle, número y localidad) y te lo enviamos 🚚",
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
                if not resultados_sku:
                    # Nos pidieron algo que no tenemos (ni por texto ni por
                    # significado): el ranking de estos términos le dice a la
                    # farmacia qué le falta al catálogo.
                    await deps["metrics"].evento(
                        "busqueda_sin_resultado", phone=phone,
                        dato=" ".join((entidad or "").lower().split())[:80],
                    )

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
                    #    Evita que un índice equivocado deje pendiente otro producto.
                    if producto_elegido and respuesta:
                        _por_precio = producto_respaldado(respuesta, resultados_sku)
                        if _por_precio and _por_precio["sku_id"] != producto_elegido["sku_id"]:
                            producto_elegido = _por_precio
                            logger.info(f"SKU corregido por precio: {_por_precio['nombre']}")

                    # 3. Sin índice de Claude: NO adivinar con el primer resultado.
                    #    index=null significa "ninguno es el que pide" (ej. respondió
                    #    "no me figura disponible"). Sólo se acepta un producto si su
                    #    precio aparece en la respuesta enviada al cliente — evidencia
                    #    de que el bot efectivamente lo está ofreciendo.
                    if not producto_elegido:
                        producto_elegido = producto_respaldado(respuesta, resultados_sku)
                        if producto_elegido:
                            logger.info(f"SKU inferido por precio en la respuesta: {producto_elegido['nombre']}")
                        else:
                            logger.info(
                                f"Sin SKU seleccionado para {entidad!r} — no se deja pendiente "
                                f"({len(resultados_sku)} resultados descartados)"
                            )
                    # Solo se marca como pendiente (comprable) si es VENDIBLE.
                    # Si es sin stock / sin precio, no entra al flujo de compra —
                    # Claude ya respondió ofreciendo las alternativas disponibles.
                    if producto_elegido and producto_elegido.get("vendible", True):
                        await deps["session"].set_pending(
                            phone=phone,
                            sku_id=producto_elegido["sku_id"],
                            sku_nombre=producto_elegido["nombre"],
                            precio=producto_elegido["precio"],
                            cantidad=cantidad,
                            opciones=resultados_sku,
                        )
                        _sku_pendiente_nuevo = producto_elegido["sku_id"]
                        await deps["metrics"].evento(
                            "producto_ofrecido", phone=phone,
                            dato=producto_elegido["nombre"][:120],
                            monto=producto_elegido["precio"], ref=producto_elegido["sku_id"],
                        )
                        send_images_cfg = await deps["config"].get("send_images")
                        await _maybe_send_image(
                            deps["wa"], phone, resultados_sku,
                            producto_elegido, solicita_imagen, send_images_cfg,
                        )

                # Pidió un producto y no hay nada que ofrecerle (no está en el
                # catálogo o está sin stock): según config, ofrecer consultarlo
                # con el equipo o derivar directo. Antes la consulta moría acá.
                if not _sku_pendiente_nuevo and intencion in ("pedido", "consulta_precio", "consulta_stock"):
                    _cfg_ss = await deps["config"].get_all()
                    _modo_ss = (_cfg_ss.get("sin_stock_mode") or "preguntar").lower()
                    if _modo_ss == "derivar":
                        _intencion = "sin_stock_derivado"
                        await deps["session"].set_estado(phone, "operador", motivo="sin_stock")
                        respuesta = _cfg_ss.get("sin_stock_derivar_message") or respuesta
                    elif _modo_ss == "preguntar":
                        _intencion = "sin_stock_ofrecido"
                        _oferta = _cfg_ss.get("sin_stock_ofrecer_message") or ""
                        # Si Claude ya ofreció consultarlo, no repetirlo.
                        if _oferta and not any(k in (respuesta or "").lower()
                                               for k in ("consult", "encarg", "equipo")):
                            respuesta = f"{respuesta}\n\n{_oferta}".strip()
                        _s = await deps["session"].get(phone)
                        _s["derivacion_ofrecida"] = entidad or texto[:60]
                        await deps["session"].save(phone, _s)

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
                # Histórico permanente para el dashboard (Postgres, best-effort)
                await deps["metrics"].record(phone, _tipo, _intencion, _total,
                                             dict(_steps), _apis)
                # Historial permanente en Postgres (best-effort, no-op sin DB)
                try:
                    if texto:
                        await deps["msgs"].save(phone, "user", texto)
                    if respuesta:
                        await deps["msgs"].save(phone, "assistant", respuesta)
                except Exception:
                    pass

    return {"status": "ok"}
