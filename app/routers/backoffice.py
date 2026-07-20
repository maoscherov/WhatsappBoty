"""
Backoffice API — endpoints para el dashboard de gestión.
GET /bo/health     → estado del sistema
GET /bo/stats      → métricas agregadas de sesiones
GET /bo/sessions   → lista de sesiones activas
GET /bo/session/{phone} → detalle completo de una sesión
"""

import logging
import statistics
from fastapi import APIRouter, HTTPException, Query, Depends, UploadFile, File
from pathlib import Path
from pydantic import BaseModel

from app.config import get_settings
from app.services.session_service import get_session_service
from app.services.sku_service import get_sku_service, reload_sku_service
from app.services.perf_service import get_perf_service
from app.services.config_service import get_config_service
from app.services.whatsapp_service import get_whatsapp_service
from app.services.socio_service import get_socio_service, reload_socio_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bo")


def _auth(key: str = Query(None, alias="key")):
    settings = get_settings()
    if settings.bo_key and key != settings.bo_key:
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


@router.get("/sessions")
async def bo_sessions(_=Depends(_auth)):
    settings = get_settings()
    session_svc = get_session_service(settings.redis_url)
    sessions = await session_svc.list_all()

    result = []
    for phone, s in sessions:
        history = s.get("history", [])
        ultimo = next(
            (m["content"] for m in reversed(history) if m["role"] == "user"), None
        )
        result.append({
            "phone": phone,
            "estado": s.get("estado", "idle"),
            "pending_sku_nombre": s.get("pending_sku_nombre"),
            "pending_precio": s.get("pending_precio"),
            "pending_cantidad": s.get("pending_cantidad", 1),
            "mensajes": len(history),
            "ultimo_mensaje": (ultimo[:80] + "…") if ultimo and len(ultimo) > 80 else ultimo,
        })

    return result


@router.get("/session/{phone}")
async def bo_session_detail(phone: str, _=Depends(_auth)):
    settings = get_settings()
    session_svc = get_session_service(settings.redis_url)
    session = await session_svc.get(phone)
    return {"phone": phone, **session}


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
        return {"status": "ok", "total": svc.total, "csv_path": str(csv_path)}
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Error procesando CSV: {e}")


# ── Padrón de socios (personalización) ────────────────────────────────────────

@router.get("/socios/info")
async def bo_socios_info(_=Depends(_auth)):
    settings = get_settings()
    try:
        svc = get_socio_service(settings.socios_path)
        return {"total": svc.total, "path": settings.socios_path}
    except Exception as e:
        return {"total": 0, "error": str(e)}


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
        return {"status": "ok", "total": svc.total, "path": str(dest)}
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
    send_images: str | None = None  # "always" | "on_request"


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
