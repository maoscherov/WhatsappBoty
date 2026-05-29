"""
Envía mensajes y descarga archivos de audio a través de la API de WhatsApp Business.
"""

import httpx
from typing import Optional

WA_BASE = "https://graph.facebook.com/v19.0"


class WhatsAppService:
    def __init__(self, token: str, phone_number_id: str):
        self._token = token
        self._phone_id = phone_number_id
        self._headers = {"Authorization": f"Bearer {token}"}

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
                    f"{WA_BASE}/{self._phone_id}/messages",
                    headers={**self._headers, "Content-Type": "application/json"},
                    json=payload,
                    timeout=10,
                )
                return resp.status_code == 200
            except Exception:
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
                    f"{WA_BASE}/{self._phone_id}/messages",
                    headers={**self._headers, "Content-Type": "application/json"},
                    json=payload,
                    timeout=10,
                )
                return resp.status_code == 200
            except Exception:
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
                    f"{WA_BASE}/{self._phone_id}/messages",
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
                    f"{WA_BASE}/{media_id}",
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
    global _instance
    if _instance is None:
        _instance = WhatsAppService(token, phone_number_id)
    return _instance
