"""
Envía mensajes y descarga archivos de audio a través de la API de WhatsApp Business.

Proveedor conmutable por env vars (por número/farmacia):
  WA_PROVIDER=meta  (default) → Cloud API directa de Meta con WHATSAPP_TOKEN.
  WA_PROVIDER=kapso           → proxy API-compatible de Kapso con KAPSO_API_KEY.
La URL base se puede pisar con WA_API_BASE en ambos casos.
"""

import logging

import httpx
from typing import Optional

logger = logging.getLogger(__name__)

WA_BASE = "https://graph.facebook.com/v19.0"
# Kapso incluye la versión de la API de Meta en la ruta; sin ella devuelve 404.
KAPSO_BASE = "https://api.kapso.ai/meta/whatsapp/v24.0"


async def _registrar_fallo(to: str, tipo: str, detalle: str):
    """
    Deja registro de un envío rechazado por WhatsApp. Sin esto el cliente se
    queda sin respuesta y nadie se entera. Best-effort: nunca propaga error.
    """
    logger.warning(f"WhatsApp NO envió {tipo} a …{to[-4:]}: {detalle[:300]}")
    try:
        from app.config import get_settings
        from app.services.db import get_db
        from app.services.metrics_store import get_metrics_store
        await get_metrics_store(get_db(get_settings().database_url)).evento(
            "wa_send_fallo", phone=to, dato=tipo, extra={"detalle": detalle[:500]},
        )
    except Exception:
        pass


class WhatsAppService:
    def __init__(self, token: str, phone_number_id: str,
                 base_url: str = "", headers: Optional[dict] = None):
        self._token = token
        self._phone_id = phone_number_id
        self._base = (base_url or WA_BASE).rstrip("/")
        self._headers = headers if headers is not None else {"Authorization": f"Bearer {token}"}

    async def send_text(self, to: str, text: str, simulate_typing: bool = True) -> bool:
        # simulate_typing conservado como parámetro por compatibilidad,
        # pero sin delay: Claude 1 + Claude 2 ya suman ~7s en flujos con SKU.

        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text, "preview_url": False},
        }
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    f"{self._base}/{self._phone_id}/messages",
                    headers={**self._headers, "Content-Type": "application/json"},
                    json=payload,
                    timeout=10,
                )
                if resp.status_code != 200:
                    await _registrar_fallo(to, "text", f"HTTP {resp.status_code}: {resp.text}")
                    return False
                return True
            except Exception as e:
                await _registrar_fallo(to, "text", f"{type(e).__name__}: {e}")
                return False

    async def send_image(self, to: str, url: str, caption: str = "") -> bool:
        """Envía una imagen por WhatsApp. url debe ser HTTPS pública."""
        payload: dict = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "image",
            "image": {"link": url},
        }
        if caption:
            payload["image"]["caption"] = caption
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    f"{self._base}/{self._phone_id}/messages",
                    headers={**self._headers, "Content-Type": "application/json"},
                    json=payload,
                    timeout=10,
                )
                if resp.status_code != 200:
                    await _registrar_fallo(to, "image", f"HTTP {resp.status_code}: {resp.text}")
                    return False
                return True
            except Exception as e:
                await _registrar_fallo(to, "image", f"{type(e).__name__}: {e}")
                return False

    async def mark_read(self, message_id: str):
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }
        async with httpx.AsyncClient() as client:
            try:
                await client.post(
                    f"{self._base}/{self._phone_id}/messages",
                    headers={**self._headers, "Content-Type": "application/json"},
                    json=payload,
                    timeout=5,
                )
            except Exception:
                pass

    async def _download_media(self, media_id: str) -> Optional[bytes]:
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(
                    f"{self._base}/{media_id}",
                    headers=self._headers,
                    timeout=10,
                )
                resp.raise_for_status()
                url = resp.json().get("url")
                if not url:
                    return None
                media_resp = await client.get(url, headers=self._headers, timeout=30)
                media_resp.raise_for_status()
                return media_resp.content
            except Exception:
                return None

    async def download_audio(self, media_id: str) -> Optional[bytes]:
        return await self._download_media(media_id)

    async def download_image(self, media_id: str) -> Optional[bytes]:
        return await self._download_media(media_id)


_instance: Optional[WhatsAppService] = None


def get_whatsapp_service(token: str, phone_number_id: str) -> WhatsAppService:
    """
    Singleton. El proveedor (Meta directo o Kapso) se resuelve desde settings,
    así el resto del código no cambia según la farmacia/número.
    """
    global _instance
    if _instance is None:
        from app.config import get_settings
        s = get_settings()
        if s.wa_provider == "kapso":
            base = s.wa_api_base or KAPSO_BASE
            headers = {"X-API-Key": s.kapso_api_key}
        else:
            base = s.wa_api_base or WA_BASE
            headers = {"Authorization": f"Bearer {token}"}
        _instance = WhatsAppService(token, phone_number_id, base_url=base, headers=headers)
    return _instance
