"""
Backoffice API — endpoints para el dashboard de gestión.
GET /bo/health     → estado del sistema
GET /bo/stats      → métricas agregadas de sesiones
GET /bo/sessions   → lista de sesiones activas
GET /bo/session/{phone} → detalle completo de una sesión
"""

import logging
import statistics
from fastapi import APIRouter, HTTPException, Query, Depends, UploadFile, File, Header
from pathlib import Path
from pydantic import BaseModel

from app.config import get_settings
from app.services.session_service import get_session_service
from app.services.sku_service import get_sku_service, reload_sku_service
from app.services.perf_service import get_perf_service
from app.services.config_service import get_config_service
from app.services.whatsapp_service import get_whatsapp_service
from app.services.socio_service import get_socio_service, reload_socio_service
from app.services.blob_store import get_blob_store
from app.services.db import get_db
from app.services.embeddings import get_embedding_service
from app.services.rag_service import get_rag_service
from app.services.message_store import get_message_store
from app.services.order_service import get_order_service


def _rag():
    settings = get_settings()
    db = get_db(settings.database_url)
    emb = get_embedding_service(settings.openai_api_key)
    return get_rag_service(db, emb)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bo")


def _auth(key: str = Query(None, alias="key"), x_bo_key: str = Header(None, alias="x-bo-key")):
    # Acepta la clave por query (?key=) o por header (x-bo-key). El header evita
    # problemas de encoding cuando el token tiene caracteres como + / = espacio.
    settings = get_settings()
    provista = key if key is not None else x_bo_key
    if settings.bo_key and provista != settings.bo_key:
        raise HTTPException(status_code=403, detail="Acceso denegado")


@router.get("/health")
async def bo_health(_=Depends(_auth)):
    settings = get_settings()
    session_svc = get_session_service(settings.redis_url)
    redis_ok = await session_svc.ping()

    try:
        sku_svc = get_sku_service(settings.sku_csv_path)
        sku_total = sku_svc.total
    except Exception:
        sku_total = 0

    return {
        "redis": redis_ok,
        "sku_total": sku_total,
        "anthropic_key_ok": bool(settings.anthropic_api_key and not settings.anthropic_api_key.startswith("placeholder")),
        "mp_token_ok": bool(settings.mp_access_token and not settings.mp_access_token.startswith("placeholder")),
        # Sin URL de aviso, MP cobra pero no confirma: hay que verlo en el panel.
        "mp_notification_ok": bool(settings.mp_notification_url),
        "mp_notification_url": settings.mp_notification_url or "(sin configurar)",
        "whatsapp_ok": bool(settings.whatsapp_token and settings.whatsapp_phone_number_id),
    }


@router.get("/stats")
async def bo_stats(_=Depends(_auth)):
    settings = get_settings()
    session_svc = get_session_service(settings.redis_url)
    sessions = await session_svc.list_all()

    total = len(sessions)
    by_estado = {}
    total_mensajes = 0
    pending_value = 0.0

    for _, s in sessions:
        estado = s.get("estado", "idle")
        by_estado[estado] = by_estado.get(estado, 0) + 1
        total_mensajes += len(s.get("history", []))
        if estado in ("esperando_confirmacion", "esperando_pago"):
            precio = s.get("pending_precio") or 0
            cantidad = s.get("pending_cantidad") or 1
            pending_value += precio * cantidad

    return {
        "sesiones_activas": total,
        "by_estado": by_estado,
        "total_mensajes": total_mensajes,
        "valor_pendiente": round(pending_value, 2),
    }


async def _pedidos_pendientes() -> dict[str, int]:
    """{phone: pedidos en estado pendiente} para el chip "N pedidos" de la cola
    unificada. Si falla, devuelve {} — el listado no depende de esto."""
    try:
        settings = get_settings()
        return await get_order_service(settings.redis_url).pendientes_por_phone()
    except Exception as e:
        logger.warning(f"No se pudieron contar pedidos pendientes: {e}")
        return {}


def _nombre_socio(phone: str) -> str | None:
    """Nombre del cliente si el número está en el padrón de socios."""
    try:
        settings = get_settings()
        socio = get_socio_service(settings.socios_path).find_by_phone(phone)
        return (socio or {}).get("nombre") or None
    except Exception:
        return None


@router.get("/sessions")
async def bo_sessions(_=Depends(_auth)):
    settings = get_settings()
    session_svc = get_session_service(settings.redis_url)
    sessions = await session_svc.list_all()
    pendientes = await _pedidos_pendientes()

    result = []
    for phone, s in sessions:
        history = s.get("history", [])
        ultimo = next(
            (m["content"] for m in reversed(history) if m["role"] == "user"), None
        )
        ultimo_ts = next(
            (m.get("ts") for m in reversed(history) if m["role"] == "user"), None
        )
        result.append({
            "phone": phone,
            "nombre": _nombre_socio(phone),
            "estado": s.get("estado", "idle"),
            "pending_sku_nombre": s.get("pending_sku_nombre"),
            "pending_precio": s.get("pending_precio"),
            "pending_cantidad": s.get("pending_cantidad", 1),
            "mensajes": len(history),
            "ultimo_mensaje": (ultimo[:80] + "…") if ultimo and len(ultimo) > 80 else ultimo,
            "ultimo_mensaje_at": ultimo_ts,                    # epoch (seg) del último msj del cliente
            "ultima_actividad": s.get("_last_activity"),       # epoch de la última actividad de la sesión
            "derivada_at": s.get("derivada_at"),
            "derivada_motivo": s.get("derivada_motivo"),
            "agente": s.get("agente"),
            "pedidos_pendientes": pendientes.get(phone, 0),
        })

    return result


@router.get("/derivadas")
async def bo_derivadas(_=Depends(_auth)):
    """
    Conversaciones derivadas a atención humana (estado=operador), para la
    alerta sonora del backoffice: el front puede pollear este endpoint y
    sonar cuando aumenta `count` (igual que con pedidos).
    """
    settings = get_settings()
    session_svc = get_session_service(settings.redis_url)
    sessions = await session_svc.list_all()
    pendientes = await _pedidos_pendientes()
    derivadas = []
    for phone, s in sessions:
        if s.get("estado") != "operador":
            continue
        history = s.get("history", [])
        ultimo = next((m["content"] for m in reversed(history) if m["role"] == "user"), None)
        derivadas.append({
            "phone": phone,
            "nombre": _nombre_socio(phone),
            "derivada_at": s.get("derivada_at"),
            "derivada_motivo": s.get("derivada_motivo"),
            "agente": s.get("agente"),
            "pedidos_pendientes": pendientes.get(phone, 0),
            "ultimo_mensaje": (ultimo[:80] + "…") if ultimo and len(ultimo) > 80 else ultimo,
            # OCR de la receta (si receta_ocr_enabled): paciente, medicamento,
            # candidatos del catálogo y cruce con el padrón — todo para operar.
            "receta_info": s.get("receta_info"),
        })
    derivadas.sort(key=lambda d: d.get("derivada_at") or 0, reverse=True)
    return {"count": len(derivadas), "derivadas": derivadas}


@router.get("/session/{phone}")
async def bo_session_detail(phone: str, _=Depends(_auth)):
    settings = get_settings()
    session_svc = get_session_service(settings.redis_url)
    session = await session_svc.get(phone)
    return {"phone": phone, "nombre": _nombre_socio(phone), **session}


@router.get("/perf")
async def bo_perf(_=Depends(_auth), n: int = Query(100, le=300)):
    """
    Últimos N registros de performance con percentiles y promedios por paso.
    """
    settings = get_settings()
    perf_svc = get_perf_service(settings.redis_url)
    entries = await perf_svc.get_recent(n)

    if not entries:
        return {"entries": [], "agregados": None, "total_registros": 0}

    totals = [e["total_ms"] for e in entries if "total_ms" in e]

    def pct(data: list, p: int) -> int:
        if not data:
            return 0
        s = sorted(data)
        idx = max(0, min(int(len(s) * p / 100), len(s) - 1))
        return s[idx]

    # Promedio por paso (sólo entradas que tengan ese step)
    step_keys = ["claude1_ms", "sku_ms", "claude2_ms", "send_ms",
                 "transcripcion_ms", "vision_ms"]
    step_avgs = {}
    step_counts = {}
    for k in step_keys:
        vals = [e["steps"][k] for e in entries
                if "steps" in e and k in e["steps"] and e["steps"][k] > 0]
        if vals:
            step_avgs[k] = round(statistics.mean(vals))
            step_counts[k] = len(vals)

    # Distribución de intenciones
    intenciones: dict[str, int] = {}
    tipos: dict[str, int] = {}
    for e in entries:
        i = e.get("intencion", "desconocido")
        intenciones[i] = intenciones.get(i, 0) + 1
        t = e.get("tipo", "text")
        tipos[t] = tipos.get(t, 0) + 1

    # Lentos = más de 4s total
    lentos = [e for e in entries if e.get("total_ms", 0) > 4000]

    return {
        "total_registros": len(entries),
        "agregados": {
            "p50_ms":  pct(totals, 50),
            "p75_ms":  pct(totals, 75),
            "p95_ms":  pct(totals, 95),
            "p99_ms":  pct(totals, 99),
            "avg_ms":  round(statistics.mean(totals)) if totals else 0,
            "min_ms":  min(totals) if totals else 0,
            "max_ms":  max(totals) if totals else 0,
            "lentos_gt4s": len(lentos),
            "pct_lentos": round(len(lentos) / len(totals) * 100, 1) if totals else 0,
            "step_avgs":   step_avgs,
            "step_counts": step_counts,
            "intenciones": dict(sorted(intenciones.items(), key=lambda x: -x[1])),
            "tipos":       tipos,
        },
        "entries": entries[:30],  # últimas 30 para la tabla
        "lentos":  lentos[:10],   # top 10 más lentos para debug
    }


# ── SKU import ────────────────────────────────────────────────────────────────

@router.get("/sku/info")
async def bo_sku_info(_=Depends(_auth)):
    settings = get_settings()
    try:
        svc = get_sku_service(settings.sku_csv_path)
        return {"total": svc.total, "csv_path": settings.sku_csv_path}
    except Exception as e:
        return {"total": 0, "error": str(e)}


@router.get("/diag/claude")
async def bo_diag_claude(_=Depends(_auth)):
    """
    Diagnóstico de los LLMs: prueba Anthropic y OpenAI y devuelve el error
    EXACTO si alguno falla. Requiere clave (query ?key= o header x-bo-key).
    """
    import anthropic
    import openai
    from app.services.intent_service import _MODELS
    settings = get_settings()
    out = {
        "llm_provider": settings.llm_provider,
        "anthropic_key_set": bool(settings.anthropic_api_key),
        "openai_key_set": bool(settings.openai_api_key),
    }
    # Anthropic (modelo fast)
    try:
        ac = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key or "x")
        r = await ac.messages.create(model=_MODELS["anthropic"]["fast"], max_tokens=20,
                                     messages=[{"role": "user", "content": "respondé solo: ok"}])
        out["anthropic"] = {"ok": True, "respuesta": (r.content[0].text if r.content else "")}
    except Exception as e:
        out["anthropic"] = {"ok": False, "error_type": type(e).__name__, "error": str(e)[:300]}
    # OpenAI (modelo fast)
    try:
        oc = openai.AsyncOpenAI(api_key=settings.openai_api_key or "x")
        r = await oc.chat.completions.create(model=_MODELS["openai"]["fast"], max_tokens=20,
                                             messages=[{"role": "user", "content": "respondé solo: ok"}])
        out["openai"] = {"ok": True, "respuesta": (r.choices[0].message.content or "")}
    except Exception as e:
        out["openai"] = {"ok": False, "error_type": type(e).__name__, "error": str(e)[:300]}
    return out


@router.get("/sku/check")
async def bo_sku_check(_=Depends(_auth), q: str = Query(...)):
    """
    Diagnóstico: muestra qué productos y qué flag de receta tiene el bot
    CARGADO AHORA MISMO para una búsqueda (para confirmar el catálogo en vivo).
    """
    settings = get_settings()
    svc = get_sku_service(settings.sku_csv_path)
    res = svc.buscar(q, top_n=6)
    return {
        "query": q,
        "csv_path": settings.sku_csv_path,
        "total_catalogo": svc.total,
        "resultados": [
            {"sku_id": r["sku_id"], "nombre": r["nombre"], "precio": r["precio"],
             "requiere_receta": r["requiere_receta"], "estado": r.get("estado")}
            for r in res
        ],
    }


@router.post("/sku/import")
async def bo_sku_import(file: UploadFile = File(...), _=Depends(_auth)):
    """Reemplaza el catálogo con el CSV subido y recarga el servicio en memoria."""
    settings = get_settings()
    csv_path = Path(settings.sku_csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Archivo vacío")

    csv_path.write_bytes(content)
    try:
        svc = reload_sku_service(str(csv_path))
        # Copia en Redis para sobrevivir deploys (fs efímero de Railway)
        await get_blob_store(settings.redis_url).save("catalogo", content, ".csv")
        return {"status": "ok", "total": svc.total, "csv_path": str(csv_path)}
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Error procesando CSV: {e}")


@router.post("/sku/import-pdf")
async def bo_sku_import_pdf(files: list[UploadFile] = File(...), _=Depends(_auth)):
    """
    Actualiza precio y stock del catálogo a partir de los PDFs "Informe de
    existencias" del sistema de la farmacia (uno o más: general,
    medicamentos, alimentos, cosméticos). NO reemplaza el catálogo: fusiona
    por código de barras — los productos que no vienen en los PDF quedan
    intactos, y el flag de receta de un producto ya existente nunca se toca
    (solo se calcula para productos nuevos que no estaban antes).
    """
    from app.services.catalogo_pdf import fusionar_con_catalogo

    settings = get_settings()
    pdfs = []
    for f in files:
        data = await f.read()
        if data:
            pdfs.append((data, f.filename or "sin_nombre.pdf"))
    if not pdfs:
        raise HTTPException(status_code=400, detail="Sin archivos")

    catalogo_actual = get_sku_service(settings.sku_csv_path).todos()
    try:
        contenido, resumen = fusionar_con_catalogo(catalogo_actual, pdfs)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Error leyendo PDF: {e}")
    actualizados = resumen.get("Actualizados (precio/stock)", 0)
    nuevos = resumen.get("Nuevos (no estaban en el catálogo)", 0)
    if actualizados + nuevos < 50:
        # Un reporte real actualiza miles de productos: un total ínfimo
        # delata un PDF con otro formato — mejor rebotar que tocar el catálogo.
        raise HTTPException(
            status_code=422,
            detail=f"Solo se reconocieron {actualizados + nuevos} productos — "
                   f"¿es el 'Informe de existencias' correcto? El catálogo NO "
                   f"se modificó. {resumen}")

    csv_path = Path(settings.sku_csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    contenido_bytes = contenido.encode("utf-8")
    csv_path.write_bytes(contenido_bytes)
    try:
        svc = reload_sku_service(str(csv_path))
        # Copia en Redis para sobrevivir deploys (fs efímero de Railway)
        await get_blob_store(settings.redis_url).save("catalogo", contenido_bytes, ".csv")
        return {"status": "ok", "total": svc.total, "resumen": resumen,
                "csv_path": str(csv_path)}
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Error cargando catálogo: {e}")


@router.post("/sku/import-receta")
async def bo_sku_import_receta(file: UploadFile = File(...), _=Depends(_auth)):
    """
    Actualiza SOLO el flag de receta del catálogo desde el Excel "Base
    Predictiva de Stock" (cruce por SKU individual). No toca nombre, precio
    ni stock.

    La whitelist de venta libre confirmada por la farmacia (minuta 31/7)
    queda blindada: si el Excel dice "con receta" para un producto de esa
    lista, NO se aplica — se reporta como conflicto para revisión de Belén
    (decisión 21/8: el cruce de receta del Excel marcaba como "con receta"
    OTC muy comunes — Ibupirac, Actron, Tafirol — sin documentar su método).
    """
    from app.services.receta_excel import parsear_excel, fusionar_receta

    settings = get_settings()
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Archivo vacío")

    try:
        por_barcode = parsear_excel(data)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Error leyendo Excel: {e}")
    if len(por_barcode) < 50:
        raise HTTPException(
            status_code=422,
            detail=f"Solo se reconocieron {len(por_barcode)} productos con cruce — "
                   f"¿es el archivo correcto? El catálogo NO se modificó.")

    catalogo_actual = get_sku_service(settings.sku_csv_path).todos()
    contenido, resumen = fusionar_receta(catalogo_actual, por_barcode)

    csv_path = Path(settings.sku_csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    contenido_bytes = contenido.encode("utf-8")
    csv_path.write_bytes(contenido_bytes)
    try:
        svc = reload_sku_service(str(csv_path))
        await get_blob_store(settings.redis_url).save("catalogo", contenido_bytes, ".csv")
        return {"status": "ok", "total": svc.total, "resumen": resumen,
                "csv_path": str(csv_path)}
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Error cargando catálogo: {e}")


# ── Padrón de socios (personalización) ────────────────────────────────────────

@router.get("/socios/info")
async def bo_socios_info(_=Depends(_auth)):
    settings = get_settings()
    try:
        svc = get_socio_service(settings.socios_path)
        return {"total": svc.total, "path": settings.socios_path}
    except Exception as e:
        return {"total": 0, "error": str(e)}


@router.get("/socios/check/{phone}")
async def bo_socios_check(phone: str, _=Depends(_auth)):
    """
    Diagnóstico: ¿este número matchea contra el padrón?
    Usar el número tal como aparece en la lista de sesiones del backoffice.
    """
    settings = get_settings()
    svc = get_socio_service(settings.socios_path)
    socio = svc.find_by_phone(phone)
    return {
        "phone_consultado": phone,
        "padron_total": svc.total,
        "match": bool(socio),
        "socio": {
            "nombre": socio["nombre"],
            "apellido": socio["apellido"],
            "nro_socio": socio["nro_socio"],
            "celular_padron": socio["celular"],
        } if socio else None,
        "contexto_prompt": svc.contexto_para_prompt(phone),
    }


@router.post("/socios/import")
async def bo_socios_import(file: UploadFile = File(...), _=Depends(_auth)):
    """
    Reemplaza el padrón de socios con el archivo subido (CSV o XLSX) y
    recarga el servicio. Columnas esperadas: APELLIDO, NOMBRE, DNI, SOCIO,
    CELULAR, DOMICILIO (los nombres admiten variantes).
    """
    settings = get_settings()
    filename = (file.filename or "").lower()
    suffix = ".xlsx" if filename.endswith((".xlsx", ".xls")) else ".csv"

    # El padrón se guarda con la extensión del archivo subido; socios_path
    # define la base, la extensión se ajusta al formato real.
    dest = Path(settings.socios_path).with_suffix(suffix)
    dest.parent.mkdir(parents=True, exist_ok=True)

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Archivo vacío")
    dest.write_bytes(content)

    # Si quedó un padrón viejo con la otra extensión, eliminarlo para no confundir
    otro = Path(settings.socios_path).with_suffix(".csv" if suffix == ".xlsx" else ".xlsx")
    if otro != dest and otro.exists():
        otro.unlink()

    try:
        svc = reload_socio_service(str(dest))
        if svc.total == 0:
            raise ValueError("no se reconocieron socios (¿faltan las columnas NOMBRE y CELULAR?)")
        # Actualizar el path efectivo para los próximos get_socio_service
        settings.socios_path = str(dest)
        # Copia en Redis para sobrevivir deploys (fs efímero de Railway)
        await get_blob_store(settings.redis_url).save("socios", content, suffix)
        return {"status": "ok", "total": svc.total, "path": str(dest)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Error procesando padrón: {e}")


# ── Horarios de atención ───────────────────────────────────────────────────────

@router.get("/config/hours")
async def bo_hours_get(_=Depends(_auth)):
    settings = get_settings()
    cfg_svc = get_config_service(settings.redis_url)
    return await cfg_svc.get_hours()


@router.put("/config/hours")
async def bo_hours_set(body: dict, _=Depends(_auth)):
    settings = get_settings()
    cfg_svc = get_config_service(settings.redis_url)
    await cfg_svc.set_hours(body)
    return await cfg_svc.get_hours()


@router.delete("/perf")
async def bo_perf_clear(_=Depends(_auth)):
    """Borra el historial de performance (útil para empezar medición limpia)."""
    settings = get_settings()
    perf_svc = get_perf_service(settings.redis_url)
    await perf_svc.clear()
    return {"status": "ok", "message": "Historial de performance borrado"}


# ── Configuración del bot ──────────────────────────────────────────────────────

class ConfigUpdate(BaseModel):
    send_images: str | None = None    # "always" | "on_request"
    pickup_minutes: str | None = None
    receta_mode: str | None = None    # "conservador" | "estricto"
    envio_enabled: str | None = None  # "true" | "false"
    payment_provider: str | None = None          # "payway" | "mercadopago"
    sin_stock_mode: str | None = None            # "preguntar" | "derivar" | "nunca"
    sin_stock_ofrecer_message: str | None = None
    sin_stock_derivar_message: str | None = None
    derivar_pago_manual: str | None = None       # compat: "false" = solo_tarjeta
    pago_manual_mode: str | None = None          # "derivar" | "solo_tarjeta"
    pago_manual_message: str | None = None
    pago_solo_tarjeta_message: str | None = None
    auto_liberar_minutos: str | None = None      # derivación sin atender vuelve al bot (0 = nunca)
    auto_liberar_message: str | None = None
    inactivity_minutes: str | None = None        # cierre por inactividad (min)
    inactivity_close_message: str | None = None
    inactivity_minutes_pago: str | None = None   # cierre cuando hay link de pago enviado
    inactivity_close_message_pago: str | None = None
    handoff_reminder_minutes: str | None = None  # aviso de demora post-derivación ("0" = off)
    handoff_reminder_message: str | None = None
    receta_ocr_enabled: str | None = None        # "true" = leer recetas al derivar
    contexto_reinicio_minutos: str | None = None  # pausa que arranca charla nueva ("0" = nunca)
    socio_discount_pct: str | None = None        # "0" = apagado, ej "15"
    socio_discount_en_catalogo: str | None = None  # "true" = precio bonificado ya al ofrecer
    socio_discount_message: str | None = None    # admite {pct} y {antes}
    socio_discount_info_message: str | None = None   # respuesta fija con descuento activo ({pct})
    socio_discount_off_message: str | None = None    # respuesta fija con descuento apagado
    derivadas_poll_seconds: str | None = None    # intervalo de polleo de /bo/derivadas


@router.get("/config")
async def bo_config_get(_=Depends(_auth)):
    settings = get_settings()
    cfg_svc = get_config_service(settings.redis_url)
    return await cfg_svc.get_all()


@router.patch("/config")
async def bo_config_update(body: ConfigUpdate, _=Depends(_auth)):
    settings = get_settings()
    cfg_svc = get_config_service(settings.redis_url)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="Sin campos para actualizar")
    await cfg_svc.set_many(updates)
    return await cfg_svc.get_all()


# ── Handoff operador ───────────────────────────────────────────────────────────

class OperatorMessage(BaseModel):
    text: str


@router.post("/session/{phone}/takeover")
async def bo_takeover(phone: str, _=Depends(_auth)):
    """Operador toma la conversación — el bot deja de responder."""
    settings = get_settings()
    session_svc = get_session_service(settings.redis_url)
    await session_svc.set_estado(phone, "operador")
    return {"status": "ok", "estado": "operador", "phone": phone}


@router.post("/session/{phone}/release")
async def bo_release(phone: str, _=Depends(_auth)):
    """Devuelve la conversación al bot."""
    settings = get_settings()
    session_svc = get_session_service(settings.redis_url)
    await session_svc.set_estado(phone, "idle")
    return {"status": "ok", "estado": "idle", "phone": phone}


class PaylinkIn(BaseModel):
    phone: str                    # cliente al que corresponde el pago
    sku_id: str | None = None     # opción A: producto del catálogo
    detalle: str | None = None    # opción B: concepto libre…
    monto: float | None = None    # …con su monto (o para pisar el precio del SKU)
    cantidad: int = 1
    enviar: bool = False          # True → se lo manda por WhatsApp al cliente
    mensaje: str | None = None    # texto opcional para acompañar el link


@router.post("/paylink")
async def bo_paylink(body: PaylinkIn, _=Depends(_auth)):
    """
    Genera un link de pago desde el backoffice (operador): por SKU del catálogo
    o por detalle+monto libres. Devuelve el link (para copiar) y opcionalmente
    lo envía por WhatsApp al cliente. Usa el proveedor de pago activo
    (PAYMENT_PROVIDER). Sin control de receta: el operador ya validó el caso.
    """
    settings = get_settings()

    # Resolver nombre y precio unitario
    cantidad = max(1, int(body.cantidad))
    if body.sku_id:
        sku = get_sku_service(settings.sku_csv_path).get_by_id(body.sku_id)
        if not sku:
            raise HTTPException(status_code=404, detail=f"SKU {body.sku_id} no encontrado")
        nombre = sku.sku_nombre
        precio = float(body.monto) if body.monto else float(sku.precio_venta or 0)
        sku_id = sku.sku_id
    else:
        if not body.detalle or not body.monto:
            raise HTTPException(status_code=400, detail="Sin sku_id hacen falta detalle y monto")
        nombre = body.detalle.strip()
        precio = float(body.monto)
        sku_id = "MANUAL"
    if precio <= 0:
        raise HTTPException(status_code=400, detail="El monto debe ser mayor a 0")
    total = round(precio * cantidad, 2)

    # Generar el link con el proveedor activo (el mismo que usa el bot)
    from app.routers.webhook import payment_svc_para
    payment_svc = payment_svc_para(await get_config_service(settings.redis_url).get_all(), settings)
    link, err = await payment_svc.crear_link(
        sku_id=sku_id, nombre=nombre, precio=precio, phone=body.phone, cantidad=cantidad,
    )
    if not link:
        return {"ok": False, "error": err or "no se pudo generar el link"}

    # Envío opcional por WhatsApp + registro en el historial de la conversación
    enviado = False
    nombre_cant = nombre + (f" x{cantidad}" if cantidad > 1 else "")
    mensaje = body.mensaje or (
        f"Acá te mando el link de pago para {nombre_cant} (${total:,.2f}):\n\n{link}\n\n"
        "El link tiene vigencia de 24hs. ¡Cualquier cosa me avisás!"
    )
    if body.enviar:
        wa = get_whatsapp_service(settings.whatsapp_token, settings.whatsapp_phone_number_id)
        enviado = await wa.send_text(body.phone, mensaje)
        if enviado:
            await get_session_service(settings.redis_url).add_message(body.phone, "assistant", mensaje)

    return {"ok": True, "link": link, "detalle": nombre_cant, "total": total,
            "enviado": enviado, "mensaje": mensaje}


@router.post("/session/{phone}/take")
async def bo_take(phone: str, agente: str = Query(...), _=Depends(_auth)):
    """
    Registra qué agente toma la conversación (número/nombre de usuario, sin
    contraseña adicional — decisión de la farmacia, minuta 2026-07-31).
    Permite el filtro "mis conversaciones" y trazar quién atendió cada chat.
    """
    settings = get_settings()
    session_svc = get_session_service(settings.redis_url)
    session = await session_svc.get(phone)
    session["agente"] = agente.strip()
    await session_svc.save(phone, session)
    return {"status": "ok", "phone": phone, "agente": agente.strip()}


@router.post("/session/{phone}/close")
async def bo_close(phone: str, _=Depends(_auth)):
    """Cierra la conversación: elimina la sesión (sale de la lista de activas)."""
    settings = get_settings()
    session_svc = get_session_service(settings.redis_url)
    await session_svc.delete(phone)
    return {"status": "ok", "closed": phone}


@router.post("/sessions/liberar-todas")
async def bo_liberar_todas(_=Depends(_auth)):
    """
    Devuelve al bot todas las conversaciones que quedaron en atención humana.
    Útil cuando nadie está atendiendo y quedaron mudas.
    """
    settings = get_settings()
    session_svc = get_session_service(settings.redis_url)
    liberadas = []
    for phone, s in await session_svc.list_all():
        if s.get("estado") == "operador":
            await session_svc.liberar(phone)
            liberadas.append(phone)
    logger.info(f"Liberadas {len(liberadas)} conversaciones del modo operador")
    return {"ok": True, "liberadas": liberadas}


@router.post("/sessions/clear")
async def bo_sessions_clear(_=Depends(_auth)):
    """Elimina TODAS las sesiones activas (resetear el tablero antes de una demo)."""
    settings = get_settings()
    session_svc = get_session_service(settings.redis_url)
    n = await session_svc.delete_all()
    return {"status": "ok", "eliminadas": n}


# ── Historial permanente (Postgres) ────────────────────────────────────────────

@router.get("/session/{phone}/resumen")
async def bo_resumen(phone: str, _=Depends(_auth)):
    """
    Resumen corto de la conversación (LLM) para retomar un chat derivado sin
    releer todo el historial. Usa Postgres si está; si no, la sesión de Redis.
    """
    settings = get_settings()

    # Historial: Postgres (completo) o Redis (últimos mensajes)
    mensajes = []
    db = get_db(settings.database_url)
    if db.available():
        store = get_message_store(db)
        mensajes = await store.history(phone, 60)
    if not mensajes:
        session = await get_session_service(settings.redis_url).get(phone)
        mensajes = session.get("history", [])
    if not mensajes:
        return {"ok": False, "resumen": "", "detail": "Sin historial para este número"}

    charla = "\n".join(
        f"{'Cliente' if m.get('role') == 'user' else 'Bot'}: {m.get('content', '')[:300]}"
        for m in mensajes[-40:]
    )
    prompt = (
        "Sos asistente de una farmacia. Resumí esta conversación de WhatsApp en 3-4 "
        "líneas para que un agente humano la retome rápido. Incluí: qué pide el cliente, "
        "productos/precios mencionados, estado del pedido o pago, y qué falta resolver. "
        "Respondé SOLO el resumen, en español.\n\n" + charla
    )

    resumen, err = "", None
    for proveedor in ([settings.llm_provider, "openai" if settings.llm_provider == "anthropic" else "anthropic"]):
        try:
            if proveedor == "openai" and settings.openai_api_key:
                from openai import AsyncOpenAI
                r = await AsyncOpenAI(api_key=settings.openai_api_key).chat.completions.create(
                    model="gpt-4o-mini", max_tokens=250,
                    messages=[{"role": "user", "content": prompt}])
                resumen = (r.choices[0].message.content or "").strip()
            elif proveedor == "anthropic" and settings.anthropic_api_key:
                from anthropic import AsyncAnthropic
                r = await AsyncAnthropic(api_key=settings.anthropic_api_key).messages.create(
                    model="claude-haiku-4-5-20251001", max_tokens=250,
                    messages=[{"role": "user", "content": prompt}])
                resumen = (r.content[0].text or "").strip()
            if resumen:
                return {"ok": True, "resumen": resumen, "mensajes": len(mensajes), "proveedor": proveedor}
        except Exception as e:
            err = str(e)
    return {"ok": False, "resumen": "", "detail": err or "sin proveedor LLM configurado"}


@router.get("/wa/config-check")
async def bo_wa_config(_=Depends(_auth)):
    """Config de WhatsApp cargada (claves enmascaradas) y URL efectiva de envío."""
    from app.services.whatsapp_service import get_whatsapp_service
    s = get_settings()
    wa = get_whatsapp_service(s.whatsapp_token, s.whatsapp_phone_number_id)

    def _mask(v: str) -> str:
        v = v or ""
        return f"{v[:4]}…{v[-4:]} ({len(v)})" if len(v) >= 8 else ("(vacío)" if not v else "***")

    return {
        "proveedor": s.wa_provider,
        "phone_number_id": s.whatsapp_phone_number_id or "(vacío)",
        "url_envio": f"{wa.base_url}/{s.whatsapp_phone_number_id}/messages",
        "whatsapp_token": _mask(s.whatsapp_token),
        "kapso_api_key": _mask(s.kapso_api_key),
        "kapso_webhook_secret": _mask(s.kapso_webhook_secret),
        "vertical": s.vertical,
    }


@router.post("/wa/test")
async def bo_wa_test(to: str = Query(...), texto: str = Query("Prueba de envío ✅"),
                     _=Depends(_auth)):
    """
    Envía un mensaje de prueba y devuelve la respuesta cruda del proveedor.
    Sirve para ver el error exacto cuando el bot recibe pero no responde.
    """
    import httpx
    from app.services.whatsapp_service import get_whatsapp_service
    s = get_settings()
    wa = get_whatsapp_service(s.whatsapp_token, s.whatsapp_phone_number_id)
    url = f"{wa.base_url}/{s.whatsapp_phone_number_id}/messages"
    headers = ({"X-API-Key": s.kapso_api_key} if s.wa_provider == "kapso"
               else {"Authorization": f"Bearer {s.whatsapp_token}"})
    payload = {"messaging_product": "whatsapp", "to": to.lstrip("+"),
               "type": "text", "text": {"body": texto}}
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(url, headers={**headers, "Content-Type": "application/json"},
                                  json=payload, timeout=20)
            return {"ok": r.status_code == 200, "status": r.status_code,
                    "url": url, "proveedor": s.wa_provider, "respuesta": r.text[:800]}
        except Exception as e:
            return {"ok": False, "url": url, "error": f"{type(e).__name__}: {e}"}


@router.get("/mp/pago/{payment_id}")
async def bo_mp_pago(payment_id: str, _=Depends(_auth)):
    """
    Diagnóstico: consulta un pago en Mercado Pago tal como lo ve el sistema.
    Sirve para saber por qué una compra no se cerró (¿llegó? ¿está aprobada?
    ¿trae la referencia con el teléfono?).
    """
    from app.services.payment_service import get_payment_service
    settings = get_settings()
    svc = get_payment_service(settings.mp_access_token, settings.mp_notification_url, settings.mp_sandbox)
    pago = await svc.get_payment_info(payment_id)
    if not pago:
        return {"ok": False, "detail": "MP no devolvió el pago (¿id incorrecto o token de otra cuenta?)"}
    items = (pago.get("additional_info") or {}).get("items") or []
    return {
        "ok": True,
        "id": pago.get("id"),
        "estado": pago.get("status"),
        "detalle_estado": pago.get("status_detail"),
        "monto": pago.get("transaction_amount"),
        "external_reference": pago.get("external_reference"),
        "notification_url": pago.get("notification_url"),
        "producto": items[0].get("title") if items else None,
        "fecha": pago.get("date_approved") or pago.get("date_created"),
    }


@router.post("/mp/reprocesar/{payment_id}")
async def bo_mp_reprocesar(payment_id: str, _=Depends(_auth)):
    """
    Reprocesa un pago de Mercado Pago cuya notificación se perdió: crea el
    pedido, envía el WhatsApp con el código y actualiza la sesión, igual que
    haría el webhook. Recupera la venta sin que el cliente tenga que pagar de nuevo.
    """
    from app.routers.mp_webhook import procesar_pago
    resultado = await procesar_pago(payment_id)
    logger.info(f"Reproceso manual del pago {payment_id}: {resultado}")
    return resultado


@router.get("/dashboard")
async def bo_dashboard(_=Depends(_auth), days: int = Query(7, ge=1, le=90)):
    """
    Métricas históricas para el dashboard del backoffice: volumen diario,
    intenciones, tiempos de respuesta (avg/p50/p95), derivaciones, tipos de
    mensaje y distribución horaria. Fuente: Postgres (tabla interacciones).
    """
    from app.services.metrics_store import get_metrics_store
    settings = get_settings()
    store = get_metrics_store(get_db(settings.database_url))
    data = await store.dashboard(days)
    if data is None:
        return {"available": False,
                "detail": "Postgres no disponible — el dashboard requiere base de datos"}
    return {"available": True, **data,
            "embudo": await store.embudo(days),
            "envios_fallidos": await store.envios_fallidos(days),
            "pagos_por_marca": await store.pagos_por_marca(days),
            "busquedas_sin_resultado": await store.busquedas_sin_resultado(days),
            "kpis_conversacionales": await store.kpis_conversacionales(days)}


@router.get("/conversaciones")
async def bo_conversaciones(_=Depends(_auth), days: int = Query(30, ge=1, le=365),
                            q: str = Query(""), limit: int = Query(50, le=200)):
    """
    Conversaciones históricas (Postgres): una fila por teléfono con actividad
    en el rango, ordenadas por última actividad. `q` filtra por teléfono.
    El detalle de cada una se abre con GET /bo/history/{phone}.
    """
    from app.services.metrics_store import get_metrics_store
    settings = get_settings()
    convs = await get_metrics_store(get_db(settings.database_url)).conversaciones(days, q.strip(), limit)
    for c in convs:
        c["nombre"] = _nombre_socio(c["phone"])
    return {"available": get_db(settings.database_url).available(), "conversaciones": convs}


@router.get("/history/{phone}")
async def bo_history(phone: str, _=Depends(_auth), limit: int = Query(200, le=1000)):
    """Historial completo de la conversación desde Postgres (persistente)."""
    settings = get_settings()
    db = get_db(settings.database_url)
    if not db.available():
        return {"available": False, "messages": []}
    store = get_message_store(db)
    return {"available": True, "messages": await store.history(phone, limit)}


# ── RAG: indexación y estado ────────────────────────────────────────────────────

@router.get("/rag/status")
async def bo_rag_status(_=Depends(_auth)):
    rag = _rag()
    return {"enabled": rag.enabled(), "productos_indexados": await rag.count_indexed()}


@router.post("/rag/reindex")
async def bo_rag_reindex(_=Depends(_auth)):
    """Embebe el catálogo actual en pgvector (búsqueda semántica)."""
    settings = get_settings()
    rag = _rag()
    if not rag.enabled():
        raise HTTPException(status_code=400, detail="RAG no disponible (falta DATABASE_URL o OPENAI_API_KEY)")
    sku_svc = get_sku_service(settings.sku_csv_path)
    productos = [sku_svc._to_response(s) for s in sku_svc._skus]
    total = await rag.reindex_catalogo(productos)
    return {"status": "ok", "indexados": total}


# ── Base de conocimiento ────────────────────────────────────────────────────────

class KBDoc(BaseModel):
    titulo: str = ""
    contenido: str


@router.get("/kb")
async def bo_kb_list(_=Depends(_auth)):
    return await _rag().kb_list()


@router.post("/kb")
async def bo_kb_add(body: KBDoc, _=Depends(_auth)):
    rag = _rag()
    if not rag.enabled():
        raise HTTPException(status_code=400, detail="RAG no disponible (falta DATABASE_URL o OPENAI_API_KEY)")
    ok = await rag.kb_add(body.titulo, body.contenido)
    if not ok:
        raise HTTPException(status_code=422, detail="No se pudo guardar el documento")
    return {"status": "ok"}


@router.get("/kb/buscar")
async def bo_kb_buscar(_=Depends(_auth), q: str = Query(...), n: int = Query(5)):
    """
    Diagnóstico: qué documentos encuentra la búsqueda para una consulta y con
    qué puntaje. Sirve para ver si al bot le está llegando el dato o no.
    """
    rag = _rag()
    if not rag.enabled():
        return {"ok": False, "detail": "RAG deshabilitado (falta Postgres u OPENAI_API_KEY)"}
    docs = await rag.kb_search(q, n=n, min_score=0.0)   # sin filtrar, para ver todo
    return {
        "ok": True, "consulta": q,
        "total_en_base": len(await rag.kb_list()),
        "resultados": [
            {"titulo": d["titulo"], "score": round(d["score"], 3),
             "extracto": d["contenido"][:120]}
            for d in docs
        ],
    }


@router.post("/kb/cargar-mutual")
async def bo_kb_cargar_mutual(_=Depends(_auth), reemplazar: bool = Query(False)):
    """
    Carga la base de conocimiento de Mutual AMI (horarios, préstamos, AMT,
    beneficios) desde la especificación. Evita tener que correr el script a mano
    en el servidor. Con `reemplazar=true` borra lo existente y recarga.
    """
    from scripts.cargar_kb_mutual import DOCUMENTOS
    rag = _rag()
    if not rag.enabled():
        return {"ok": False, "detail": "Falta OPENAI_API_KEY para generar los embeddings"}

    if reemplazar:
        for doc in await rag.kb_list():
            await rag.kb_delete(doc["id"])

    existentes = {d["titulo"] for d in await rag.kb_list()}
    nuevos, fallidos = [], []
    for titulo, contenido in DOCUMENTOS:
        if titulo in existentes:
            continue
        if await rag.kb_add(titulo, contenido):
            nuevos.append(titulo)
        else:
            fallidos.append(titulo)
    return {"ok": not fallidos, "cargados": nuevos, "fallidos": fallidos,
            "total_en_base": len(await rag.kb_list())}


@router.delete("/kb/{doc_id}")
async def bo_kb_delete(doc_id: int, _=Depends(_auth)):
    await _rag().kb_delete(doc_id)
    return {"status": "ok"}


@router.post("/session/{phone}/message")
async def bo_send_message(phone: str, body: OperatorMessage, _=Depends(_auth)):
    """Operador envía un mensaje al cliente por WhatsApp."""
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="Mensaje vacío")
    settings = get_settings()
    wa = get_whatsapp_service(settings.whatsapp_token, settings.whatsapp_phone_number_id)
    session_svc = get_session_service(settings.redis_url)
    sent = await wa.send_text(phone, body.text.strip(), simulate_typing=False)
    if not sent:
        raise HTTPException(status_code=502, detail="Error enviando mensaje por WhatsApp")
    await session_svc.add_message(phone, "operator", body.text.strip())
    return {"status": "ok", "sent": True}
