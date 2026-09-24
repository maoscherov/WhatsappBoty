"""
Cuerpo de creación de sesión (spec §3 P3) y verificación posterior.

- Nombre por VÍNCULO: v_ + 12 hex del link_id. Nunca por tenant ni por línea:
  ningún residuo de un vínculo anterior coincide con una sesión nueva.
- NOWEB: markOnline:false (con el default true el teléfono del cliente deja de
  notificar) y store activo desde antes del QR (no se puede cambiar después).
- GOWS: la profundidad del historial es del servidor (variables
  WAHA_GOWS_DEVICE_*); por sesión solo se elige `storage`.
- Sin deviceName: rompe el código de vinculación.
- La verificación relee la sesión y compara: si WAHA no guardó lo pedido, el
  gestor borra la sesión y aborta antes de mostrar el QR (`events` no se
  valida del lado de WAHA y un markOnline mal ubicado deja el default true).
"""

import uuid
from typing import Any

EVENTOS_WEBHOOK = ["message.any", "message.ack", "message.edited", "message.revoked", "session.status"]
IGNORE = {"status": True, "groups": True, "channels": True, "broadcast": True}
REINTENTOS = {"policy": "exponential", "delaySeconds": 2, "attempts": 15}
GOWS_STORAGE = {"messages": True, "chats": True, "groups": False, "labels": False, "contacts": True,
                "messageSecrets": True}
# Clave de solo lectura por sesión (§6.1). Los nombres de las acciones se
# verifican contra el servidor de staging (runbook del tramo 2, paso 5).
ACCIONES_CLAVE_LECTURA = {"read": True, "control": False, "send": False, "media": False}


def nombre_sesion(link_id: uuid.UUID) -> str:
    return "v_" + link_id.hex[:12]


def cuerpo_sesion(*, link_id: uuid.UUID, tenant_id: uuid.UUID, line_id: uuid.UUID, engine: str,
                  webhook_url: str, hmac_key: str, full_sync: bool = False) -> dict[str, Any]:
    webhook = {"url": webhook_url, "events": list(EVENTOS_WEBHOOK), "hmac": {"key": hmac_key},
               "retries": dict(REINTENTOS)}
    config: dict[str, Any] = {
        "metadata": {"tenant_id": str(tenant_id), "line_id": str(line_id), "link_id": str(link_id)},
        "ignore": dict(IGNORE),
        "webhooks": [webhook],
    }
    if engine == "NOWEB":
        config["noweb"] = {"markOnline": False, "store": {"enabled": True, "fullSync": bool(full_sync)}}
    elif engine == "GOWS":
        if full_sync:
            raise ValueError("en GOWS la profundidad la fijan variables del servidor, no la sesión")
        config["gows"] = {"storage": dict(GOWS_STORAGE)}
    else:
        raise ValueError(f"motor no soportado: {engine}")
    return {"name": nombre_sesion(link_id), "start": True, "config": config}


def _incluye(obtenido: Any, esperado: dict[str, Any]) -> bool:
    return isinstance(obtenido, dict) and all(obtenido.get(k) == v for k, v in esperado.items())


def verificar_config(sesion: dict[str, Any], cuerpo: dict[str, Any]) -> list[str]:
    obtenido = (sesion or {}).get("config") or {}
    pedido = cuerpo["config"]
    problemas: list[str] = []
    if not _incluye(obtenido.get("metadata"), pedido["metadata"]):
        problemas.append("metadata")
    if not _incluye(obtenido.get("ignore"), pedido["ignore"]):
        problemas.append("ignore")
    if "noweb" in pedido:
        noweb = obtenido.get("noweb") or {}
        if noweb.get("markOnline") is not False:
            problemas.append("noweb.markOnline")
        if not _incluye(noweb.get("store"), pedido["noweb"]["store"]):
            problemas.append("noweb.store")
    if "gows" in pedido:
        if not _incluye((obtenido.get("gows") or {}).get("storage"), pedido["gows"]["storage"]):
            problemas.append("gows.storage")
    hooks = obtenido.get("webhooks") or []
    if not hooks or hooks[0].get("url") != pedido["webhooks"][0]["url"]:
        problemas.append("webhooks.url")
    elif set(hooks[0].get("events") or []) != set(pedido["webhooks"][0]["events"]):
        problemas.append("webhooks.events")
    return problemas
