"""
Backoffice API — endpoints para el dashboard de gestión.
GET /bo/health     → estado del sistema
GET /bo/stats      → métricas agregadas de sesiones
GET /bo/sessions   → lista de sesiones activas
GET /bo/session/{phone} → detalle completo de una sesión
"""

import logging
from fastapi import APIRouter, HTTPException, Query, Depends

from app.config import get_settings
from app.services.session_service import get_session_service
from app.services.sku_service import get_sku_service

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
