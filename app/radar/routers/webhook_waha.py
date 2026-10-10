"""
POST /webhook/waha (§6.3 punto 1).

- HMAC sha512 del cuerpo CRUDO en X-Webhook-Hmac, verificado antes de parsear.
  Fail-closed: sin RADAR_WAHA_WEBHOOK_HMAC_KEY (32+) responde 401 a todo.
- En este tramo solo aplica session.status. Los message.* se aceptan con 200
  (para que WAHA no reintente) y se descartan SIN persistir nada: la ingesta,
  el filtro de exclusión y webhook_inbox son del tramo 3.
- Resuelve tenant, línea y vínculo por metadata y exige que el nombre de
  sesión coincida con el del vínculo: un evento cruzado se ignora.
- Responde rápido: una transacción corta y ninguna llamada a WAHA.
- Nunca loguea el cuerpo, ni en errores.
"""

import hashlib
import hmac
import json
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.radar.contexto import contexto
from app.radar.vinculo_estados import PATRON_STATUS
from app.radar.vinculos import aplicar_status

router = APIRouter(tags=["radar-webhook"])

_MIN_CLAVE = 32
_IGNORADO = {"ok": True, "ignorado": True}


def verificar_hmac(crudo: bytes, cabecera: Optional[str], clave: str) -> bool:
    if not cabecera or not clave or len(clave) < _MIN_CLAVE:
        return False
    esperado = hmac.new(clave.encode(), crudo, hashlib.sha512).hexdigest()
    return hmac.compare_digest(esperado.encode(), cabecera.strip().lower().encode())


def _uuid(valor: Any) -> Optional[uuid.UUID]:
    if not isinstance(valor, str):
        return None
    try:
        return uuid.UUID(valor)
    except ValueError:
        return None


@router.post("/webhook/waha")
async def recibir(request: Request):
    ctx = contexto(request)
    crudo = await request.body()
    if not verificar_hmac(crudo, request.headers.get("x-webhook-hmac"), ctx.settings.waha_webhook_hmac_key):
        return JSONResponse({"ok": False}, status_code=401)
    try:
        sobre = json.loads(crudo)
    except ValueError:
        return JSONResponse({"ok": False}, status_code=400)
    if not isinstance(sobre, dict) or not isinstance(sobre.get("event"), str):
        return JSONResponse({"ok": False}, status_code=400)
    evento = sobre["event"]
    if evento.startswith("message"):
        return {"ok": True, "descartado": True}
    if evento != "session.status":
        return _IGNORADO
    meta = sobre.get("metadata") if isinstance(sobre.get("metadata"), dict) else {}
    tenant_id, line_id, link_id = _uuid(meta.get("tenant_id")), _uuid(meta.get("line_id")), _uuid(meta.get("link_id"))
    payload = sobre.get("payload") if isinstance(sobre.get("payload"), dict) else {}
    status = payload.get("status")
    if not (tenant_id and line_id and link_id) or not isinstance(status, str) or not PATRON_STATUS.match(status):
        return _IGNORADO
    me = sobre.get("me")
    me_id = me.get("id") if isinstance(me, dict) else None
    async with ctx.db.tenant_tx(tenant_id) as con:
        fila = await con.fetchrow("SELECT line_id, session_name FROM links WHERE id = $1", link_id)
        if fila is None or fila["line_id"] != line_id or fila["session_name"] != sobre.get("session"):
            return _IGNORADO
        r = await aplicar_status(con, tenant_id=tenant_id, link_id=link_id, waha_status=status, origen="webhook",
                                 me_id=me_id)
    return {"ok": True, "aplicado": r["aplicado"]}
