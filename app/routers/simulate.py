"""
Endpoint de simulación para testing sin WhatsApp real.
POST /simulate  →  procesa un mensaje y devuelve la respuesta del bot.
"""

import logging
import re
import time as _time
from datetime import datetime, timezone as _tz
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from app.config import get_settings
from app.services.sku_service import get_sku_service
from app.services.session_service import get_session_service
from app.services.intent_service import get_intent_service
from app.services.payment_service import get_payment_service
from app.services.image_service import get_image_service
from app.services.perf_service import get_perf_service
from app.services.socio_service import get_socio_service
from app.services.config_service import get_config_service
from app.services.checkout_helper import (
    confirmar_pedido, resolver_entrega, capturar_direccion,
    match_retiro, match_envio, pide_humano, derivar_si_receta, afirma_envio,
    quiere_cambiar_direccion, extraer_direccion_de,
)


def _extract_link(texto: str) -> str | None:
    m = re.search(r"https?://\S+", texto or "")
    return m.group(0) if m else None

router = APIRouter()

INTENCIONES_CON_SKU = {"consulta_precio", "consulta_stock", "pedido", "consulta_abierta"}

_PALABRAS_SI = [r"\bsi\b", r"\bsí\b", r"\bdale\b", r"\bok\b", r"\blisto\b",
                r"\bperfecto\b", r"\bconfirmo\b", r"\bvamos\b", r"\bbuenisimo\b",
                r"\bva\b", r"\bconfirma\b", r"\bmanda\b", r"\bmandame\b", r"\bprocede\b"]

# NO solo matchea cuando el mensaje es una cancelación explícita y corta.
# "No me llegó el link" NO debe cancelar — el "no" tiene otro contexto.
_NO_EXACTO = [r"^no$", r"^nope$", r"^cancel$", r"^cancela$"]
_NO_FRASE  = [r"\bno quiero\b", r"\bno gracias\b", r"\bmejor no\b",
              r"\bcancela(r|me)?\b", r"\bnope\b"]

def _match_si(texto: str) -> bool:
    return any(re.search(p, texto, re.IGNORECASE) for p in _PALABRAS_SI)

def _match_no(texto: str) -> bool:
    t = texto.strip()
    # Mensaje muy corto que ES solo "no" / "cancel"
    if any(re.fullmatch(p, t, re.IGNORECASE) for p in _NO_EXACTO):
        return True
    # Frase explícita de cancelación en cualquier largo de mensaje
    return any(re.search(p, t, re.IGNORECASE) for p in _NO_FRASE)


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
    intent_svc = get_intent_service(settings.anthropic_api_key, settings.openai_api_key, settings.llm_provider)
    payment_svc = get_payment_service(settings.mp_access_token, settings.mp_notification_url, settings.mp_sandbox)
    perf_svc = get_perf_service(settings.redis_url)
    socio_svc = get_socio_service(settings.socios_path)
    config_svc = get_config_service(settings.redis_url)

    session = await session_svc.get(req.phone)
    _ctx_socio = socio_svc.contexto_para_prompt(req.phone)
    _sd = socio_svc.find_by_phone(req.phone)
    _nombre_socio = (_sd.get("nombre", "").split() or [""])[0] if _sd else ""
    texto = req.message.strip()
    productos_encontrados: list[dict] = []
    link_pago = None
    mp_error = None
    mp_token_ok = not settings.mp_access_token.startswith("placeholder")

    _t0 = _time.perf_counter()
    _steps: dict = {}
    _intencion = "desconocido"

    # ── Helper de performance ─────────────────────────────────────────────────
    async def _record(intent: str):
        total = int((_time.perf_counter() - _t0) * 1000)
        step_str = " | ".join(f"{k}={v}ms" for k, v in _steps.items()) if _steps else "—"
        logger.info(f"⏱ SIM …{req.phone[-4:]} intent={intent} total={total}ms | {step_str}")
        await perf_svc.record({
            "ts": datetime.now(_tz.utc).isoformat(),
            "phone_suffix": req.phone[-4:],
            "tipo": "simulate",
            "intencion": intent,
            "total_ms": total,
            "steps": dict(_steps),
        })

    # ── Pide hablar con una persona → derivar (sin generar link) ─────────────
    if session.get("estado") != "operador" and pide_humano(texto):
        await session_svc.set_estado(req.phone, "operador")
        respuesta = "Dale, te paso con alguien del equipo. En un momento te contactamos 🙌"
        await session_svc.add_message(req.phone, "user", texto)
        await session_svc.add_message(req.phone, "assistant", respuesta)
        await _record("derivado_humano")
        return SimulateResponse(
            respuesta=respuesta, intencion="derivado_humano",
            entidad_producto=None, productos_encontrados=[],
            estado_sesion="operador", mp_token_ok=mp_token_ok,
        )

    # ── Estado: eligiendo modo de entrega (retiro / envío) ───────────────────
    if session.get("estado") == "esperando_entrega" and session.get("pending_sku_id"):
        _dir_expl = extraer_direccion_de(texto)
        if _match_no(texto):
            await session_svc.clear_pending(req.phone)
            respuesta, _intent_out = "Dale, sin problema. ¿En qué más te puedo ayudar?", "pedido_cancelado"
        elif _dir_expl:
            respuesta, _intent_out = await capturar_direccion(
                payment_svc, session_svc, req.phone, session, _dir_expl,
            )
        else:
            respuesta, _intent_out = await resolver_entrega(
                payment_svc, session_svc, socio_svc, req.phone, session,
                es_retiro=match_retiro(texto),
                es_envio=match_envio(texto) or afirma_envio(texto),
            )
        await session_svc.add_message(req.phone, "user", texto)
        await session_svc.add_message(req.phone, "assistant", respuesta)
        session = await session_svc.get(req.phone)
        await _record(_intent_out)
        return SimulateResponse(
            respuesta=respuesta, intencion=_intent_out,
            entidad_producto=session.get("pending_sku_nombre"),
            productos_encontrados=[], estado_sesion=session.get("estado", "idle"),
            link_pago=_extract_link(respuesta), mp_token_ok=mp_token_ok,
        )

    # ── Estado: link enviado — permitir cambiar la dirección ─────────────────
    if session.get("estado") == "esperando_pago" and session.get("pending_sku_id"):
        _dir_expl = extraer_direccion_de(texto)
        if _dir_expl or quiere_cambiar_direccion(texto):
            if _dir_expl:
                respuesta, _intent_out = await capturar_direccion(
                    payment_svc, session_svc, req.phone, session, _dir_expl,
                )
            else:
                await session_svc.set_estado(req.phone, "esperando_direccion")
                respuesta, _intent_out = (
                    "Dale! Pasame la dirección nueva y te regenero el link 🚚",
                    "esperando_direccion",
                )
            await session_svc.add_message(req.phone, "user", texto)
            await session_svc.add_message(req.phone, "assistant", respuesta)
            session = await session_svc.get(req.phone)
            await _record(_intent_out)
            return SimulateResponse(
                respuesta=respuesta, intencion=_intent_out,
                entidad_producto=session.get("pending_sku_nombre"),
                productos_encontrados=[], estado_sesion=session.get("estado", "idle"),
                link_pago=_extract_link(respuesta), mp_token_ok=mp_token_ok,
            )

    # ── Estado: esperando dirección de envío ─────────────────────────────────
    if session.get("estado") == "esperando_direccion" and session.get("pending_sku_id"):
        if _match_no(texto):
            await session_svc.clear_pending(req.phone)
            respuesta, _intent_out = "Dale, sin problema. ¿En qué más te puedo ayudar?", "pedido_cancelado"
        else:
            respuesta, _intent_out = await capturar_direccion(
                payment_svc, session_svc, req.phone, session, texto,
            )
        await session_svc.add_message(req.phone, "user", texto)
        await session_svc.add_message(req.phone, "assistant", respuesta)
        session = await session_svc.get(req.phone)
        await _record(_intent_out)
        return SimulateResponse(
            respuesta=respuesta, intencion=_intent_out,
            entidad_producto=session.get("pending_sku_nombre"),
            productos_encontrados=[], estado_sesion=session.get("estado", "idle"),
            link_pago=_extract_link(respuesta), mp_token_ok=mp_token_ok,
        )

    # ── Confirmación de pedido pendiente ─────────────────────────────────────
    if session.get("estado") == "esperando_confirmacion" and session.get("pending_sku_id"):
        if _match_no(texto):
            await session_svc.clear_pending(req.phone)
            respuesta = "Dale, sin problema. ¿En qué más te puedo ayudar?"
            await session_svc.add_message(req.phone, "user", texto)
            await session_svc.add_message(req.phone, "assistant", respuesta)
            await _record("pedido_cancelado")
            return SimulateResponse(
                respuesta=respuesta, intencion="social",
                entidad_producto=None, productos_encontrados=[],
                estado_sesion="idle",
            )

        elif _match_si(texto) or match_envio(texto) or match_retiro(texto):
            _entrega = ("envio" if match_envio(texto)
                        else "retiro" if match_retiro(texto) else None)
            cfg_all = await config_svc.get_all()
            respuesta, _intent_out = await confirmar_pedido(
                sku_svc, payment_svc, session_svc, socio_svc, cfg_all, req.phone, session,
                entrega=_entrega, nombre=_nombre_socio,
            )
            await session_svc.add_message(req.phone, "user", texto)
            await session_svc.add_message(req.phone, "assistant", respuesta)
            session = await session_svc.get(req.phone)
            await _record(_intent_out)
            return SimulateResponse(
                respuesta=respuesta, intencion=_intent_out,
                entidad_producto=session.get("pending_sku_nombre"),
                productos_encontrados=[], estado_sesion=session.get("estado", "idle"),
                link_pago=_extract_link(respuesta), mp_error=mp_error, mp_token_ok=mp_token_ok,
            )

        else:
            # Ambiguo: Sonnet decide con contexto completo de las opciones mostradas.
            # Pasamos pending_opciones para que "el 1 puede ser?" sea interpretado
            # como selección de la opción 1, no como una búsqueda nueva.
            pending_opciones = session.get("pending_opciones", [])
            _tc = _time.perf_counter()
            intent_result = await intent_svc.procesar(
                mensaje=texto,
                history=session.get("history", []),
                resultados_sku=pending_opciones if pending_opciones else None,
                label_sku="OPCIONES MOSTRADAS",
                contexto_cliente=_ctx_socio,
            )
            _steps["claude1_ms"] = int((_time.perf_counter() - _tc) * 1000)

            confirmacion = intent_result.get("confirmacion")
            respuesta = intent_result.get("respuesta", "")
            _entidad_nueva = intent_result.get("entidad_producto")
            _intencion_nueva = intent_result.get("intencion", "desconocido")
            sku_index = intent_result.get("sku_seleccionado_index")

            # Paso 1: si el usuario seleccionó una opción de la lista existente,
            # actualizar pending ANTES de evaluar confirmacion/cambio.
            if sku_index is not None and pending_opciones:
                try:
                    idx = int(sku_index) - 1
                    if 0 <= idx < len(pending_opciones):
                        elegido = pending_opciones[idx]
                        nueva_cantidad = max(1, int(intent_result.get("cantidad") or session.get("pending_cantidad", 1)))
                        await session_svc.set_pending(
                            phone=req.phone,
                            sku_id=elegido["sku_id"],
                            sku_nombre=elegido["nombre"],
                            precio=elegido["precio"],
                            cantidad=nueva_cantidad,
                            opciones=pending_opciones,
                        )
                        session = await session_svc.get(req.phone)
                except (ValueError, TypeError):
                    pass

            # Paso 1b: si la opción elegida requiere receta, derivar YA (no ofrecer link)
            if sku_index is not None and session.get("pending_sku_id"):
                cfg_all = await config_svc.get_all()
                _deriv = await derivar_si_receta(
                    sku_svc, session_svc, cfg_all, req.phone, session["pending_sku_id"],
                    nombre=_nombre_socio,
                )
                if _deriv:
                    await session_svc.add_message(req.phone, "user", texto)
                    await session_svc.add_message(req.phone, "assistant", _deriv)
                    await _record("derivado_receta")
                    return SimulateResponse(
                        respuesta=_deriv, intencion="derivado_receta",
                        entidad_producto=session.get("pending_sku_nombre"),
                        productos_encontrados=[], estado_sesion="operador",
                        mp_token_ok=mp_token_ok,
                    )

            # Paso 2: _es_cambio solo aplica cuando NO hay selección de opción existente
            _es_cambio = (
                sku_index is None and (
                    confirmacion is False or
                    (confirmacion is None
                     and _entidad_nueva
                     and _intencion_nueva in INTENCIONES_CON_SKU)
                )
            )

            if confirmacion is True:
                # Claude interpretó que el usuario confirma (ej: typos, autocorrect).
                # Rutea por el flujo de receta/entrega igual que la confirmación explícita.
                cfg_all = await config_svc.get_all()
                respuesta, _intencion_conf = await confirmar_pedido(
                    sku_svc, payment_svc, session_svc, socio_svc, cfg_all, req.phone, session,
                    nombre=_nombre_socio,
                )
                link_pago = _extract_link(respuesta)
            elif _es_cambio:
                await session_svc.clear_pending(req.phone)

                # Ignorar respuesta de Claude 1: no tiene resultados SKU y genera
                # textos como "Buscame un segundito..." que no corresponden al bot.
                nueva_entidad = _entidad_nueva
                nueva_intencion = _intencion_nueva
                if nueva_entidad and nueva_intencion in INTENCIONES_CON_SKU and sku_svc:
                    _tsku = _time.perf_counter()
                    resultados_nuevos = sku_svc.buscar(nueva_entidad)
                    _steps["sku_ms"] = int((_time.perf_counter() - _tsku) * 1000)
                    if resultados_nuevos:
                        productos_encontrados = resultados_nuevos
                        _tc2 = _time.perf_counter()
                        ir2 = await intent_svc.procesar(
                            mensaje=texto,
                            history=session.get("history", []),
                            resultados_sku=resultados_nuevos,
                            contexto_cliente=_ctx_socio,
                        )
                        _steps["claude2_ms"] = int((_time.perf_counter() - _tc2) * 1000)
                        respuesta = ir2.get("respuesta", "")  # siempre usar Claude 2
                        cantidad = max(1, int(ir2.get("cantidad") or 1))
                        sku_index = ir2.get("sku_seleccionado_index")
                        producto_elegido = None
                        if sku_index is not None:
                            try:
                                idx = int(sku_index) - 1
                                if 0 <= idx < len(resultados_nuevos):
                                    producto_elegido = resultados_nuevos[idx]
                            except (ValueError, TypeError):
                                pass
                        if not producto_elegido:
                            producto_elegido = (
                                next((r for r in resultados_nuevos if r["estado"] == "disponible"), None)
                                or resultados_nuevos[0]
                            )
                        await session_svc.set_pending(
                            phone=req.phone,
                            sku_id=producto_elegido["sku_id"],
                            sku_nombre=producto_elegido["nombre"],
                            precio=producto_elegido["precio"],
                            cantidad=cantidad,
                            opciones=resultados_nuevos,
                        )
                    else:
                        # Producto nuevo no encontrado en catálogo
                        respuesta = f"No encontramos {nueva_entidad} en el catálogo en este momento. ¿Buscás algo más?"
                else:
                    # Canceló sin mencionar producto nuevo
                    respuesta = "Dale, sin problema. ¿En qué más te puedo ayudar?"

            await session_svc.add_message(req.phone, "user", texto)
            await session_svc.add_message(req.phone, "assistant", respuesta)
            session = await session_svc.get(req.phone)
            _intent_out = intent_result.get("intencion", "confirmacion_ambigua")
            await _record(_intent_out)
            return SimulateResponse(
                respuesta=respuesta,
                intencion=_intent_out,
                entidad_producto=intent_result.get("entidad_producto"),
                productos_encontrados=productos_encontrados,
                estado_sesion=session.get("estado", "idle"),
                link_pago=link_pago, mp_error=mp_error, mp_token_ok=mp_token_ok,
            )

    # ── Flujo normal ─────────────────────────────────────────────────────────
    # Claude 1 — Haiku (rápido): clasifica intención + extrae entidad.
    # Para intenciones simples (saludo, social, agradecimiento) su
    # respuesta se usa directamente sin un segundo llamado a Claude.
    _tc = _time.perf_counter()
    intent_result = await intent_svc.procesar_rapido(
        mensaje=texto,
        history=session.get("history", []),
        contexto_cliente=_ctx_socio,
    )
    _steps["claude1_ms"] = int((_time.perf_counter() - _tc) * 1000)

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
        await _record(intencion)
        return SimulateResponse(
            respuesta=respuesta, intencion=intencion,
            entidad_producto=entidad, productos_encontrados=[],
            estado_sesion=session.get("estado", "idle"),
        )

    ya_tiene_pending = session.get("estado") == "esperando_confirmacion"
    _sku_pendiente_nuevo = None   # sku elegido este turno (para chequeo de receta)

    # Buscar siempre que haya una entidad (producto detectado), aunque salude.
    if entidad and sku_svc and not ya_tiene_pending and intencion != "cambio_postventa":
        _tsku = _time.perf_counter()
        productos_encontrados = sku_svc.buscar(entidad)
        _steps["sku_ms"] = int((_time.perf_counter() - _tsku) * 1000)

        _tc2 = _time.perf_counter()
        intent_result = await intent_svc.procesar(
            mensaje=texto,
            history=session.get("history", []),
            resultados_sku=productos_encontrados,
            contexto_cliente=_ctx_socio,
        )
        _steps["claude2_ms"] = int((_time.perf_counter() - _tc2) * 1000)

        intencion = intent_result.get("intencion", "desconocido")
        entidad = intent_result.get("entidad_producto")
        cantidad = max(1, int(intent_result.get("cantidad") or 1))
        respuesta = intent_result.get("respuesta", "")

        if productos_encontrados:
            sku_index = intent_result.get("sku_seleccionado_index")
            producto_elegido = None
            if sku_index is not None:
                try:
                    idx = int(sku_index) - 1  # 1-based → 0-based
                    if 0 <= idx < len(productos_encontrados):
                        producto_elegido = productos_encontrados[idx]
                except (ValueError, TypeError):
                    pass
            # Validación por precio si el índice no coincide con el texto
            if producto_elegido and respuesta:
                import re as _re2
                for r_sku in productos_encontrados:
                    p_str = f"${r_sku['precio']:,.2f}".replace(".", ",")
                    p_str2 = f"${r_sku['precio']:.2f}".replace(".", ",")
                    if any(p in respuesta for p in [p_str, p_str2]):
                        precio_elegido = f"${producto_elegido['precio']:,.2f}".replace(".", ",")
                        if precio_elegido not in respuesta:
                            producto_elegido = r_sku
                        break
            if not producto_elegido:
                producto_elegido = (
                    next((r for r in productos_encontrados if r.get("vendible")), None)
                    or productos_encontrados[0]
                )
            # Solo comprable si es vendible (con stock y precio)
            if producto_elegido.get("vendible", True):
                await session_svc.set_pending(
                    phone=req.phone,
                    sku_id=producto_elegido["sku_id"],
                    sku_nombre=producto_elegido["nombre"],
                    precio=producto_elegido["precio"],
                    cantidad=cantidad,
                    opciones=productos_encontrados,
                )
                _sku_pendiente_nuevo = producto_elegido["sku_id"]
    elif ya_tiene_pending:
        # Hay pending activo: el usuario puede refinar la selección o la cantidad.
        pending_opciones = session.get("pending_opciones", [])
        _tc2 = _time.perf_counter()
        intent_result = await intent_svc.procesar(
            mensaje=texto,
            history=session.get("history", []),
            resultados_sku=pending_opciones if pending_opciones else None,
            label_sku="OPCIONES MOSTRADAS",
            contexto_cliente=_ctx_socio,
        )
        _steps["claude2_ms"] = int((_time.perf_counter() - _tc2) * 1000)

        cantidad_nueva = intent_result.get("cantidad")
        sku_index = intent_result.get("sku_seleccionado_index")
        respuesta = intent_result.get("respuesta", "")

        if sku_index is not None and pending_opciones:
            try:
                idx = int(sku_index) - 1  # 1-based → 0-based
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
                    _sku_pendiente_nuevo = elegido["sku_id"]
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

    # Si el producto recién elegido requiere receta, derivar ahora (no ofrecer link)
    if _sku_pendiente_nuevo:
        cfg_all = await config_svc.get_all()
        _deriv = await derivar_si_receta(
            sku_svc, session_svc, cfg_all, req.phone, _sku_pendiente_nuevo,
            nombre=_nombre_socio,
        )
        if _deriv:
            respuesta = _deriv
            intencion = "derivado_receta"

    await session_svc.add_message(req.phone, "user", texto)
    await session_svc.add_message(req.phone, "assistant", respuesta)
    session = await session_svc.get(req.phone)
    await _record(intencion)

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
    image_svc = get_image_service(settings.anthropic_api_key, settings.openai_api_key, settings.llm_provider)

    image_bytes = await image.read()
    media_type = image.content_type or "image/jpeg"

    img = await image_svc.analizar(image_bytes, media_type)

    # Receta o credencial → derivar a una persona (no vender automático)
    if img["tipo"] in ("receta", "credencial"):
        settings2 = get_settings()
        session_svc = get_session_service(settings2.redis_url)
        await session_svc.set_estado(phone, "operador")
        que = "la receta" if img["tipo"] == "receta" else "la credencial"
        return SimulateResponse(
            respuesta=(f"Recibí {que} 🙌. Para gestionarla te paso con alguien del equipo, "
                       "que la revisa y te ayuda. ¡En un momento te contactamos!"),
            intencion=f"imagen_{img['tipo']}",
            entidad_producto=None, productos_encontrados=[],
            estado_sesion="operador", texto_extraido=f"[{img['tipo']}]",
        )

    texto_extraido = img["items"]
    if not texto_extraido:
        return SimulateResponse(
            respuesta="No pude identificar el producto en la imagen. ¿Me lo escribís?",
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
