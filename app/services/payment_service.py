"""
Genera links de pago con Mercado Pago Checkout API.
POST /checkout/preferences → devuelve init_point (link de pago).
El link tiene vigencia de 24hs configurada en el preference.
"""

import httpx
from datetime import datetime, timedelta, timezone
from typing import Optional


MP_BASE_URL = "https://api.mercadopago.com"


class PaymentService:
    def __init__(self, access_token: str, notification_url: str = ""):
        self._token = access_token
        self._notification_url = notification_url
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    async def crear_link(
        self,
        sku_id: str,
        nombre: str,
        precio: float,
        phone: str,
    ) -> Optional[str]:
        """
        Crea una preferencia de pago y devuelve el init_point (link).
        Retorna None si falla.
        """
        expiration = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S.000-00:00")

        payload = {
            "items": [{
                "id": sku_id,
                "title": nombre,
                "quantity": 1,
                "unit_price": round(precio, 2),
                "currency_id": "ARS",
            }],
            "expiration_date_to": expiration,
            "external_reference": f"{phone}_{sku_id}",
            "statement_descriptor": "FARMACIA AMI",
        }

        if self._notification_url:
            payload["notification_url"] = self._notification_url

        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    f"{MP_BASE_URL}/checkout/preferences",
                    headers=self._headers,
                    json=payload,
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("init_point")
            except Exception:
                return None


_instance: Optional[PaymentService] = None


def get_payment_service(access_token: str, notification_url: str = "") -> PaymentService:
    global _instance
    if _instance is None:
        _instance = PaymentService(access_token, notification_url)
    return _instance
