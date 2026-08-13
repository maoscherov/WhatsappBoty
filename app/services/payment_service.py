import httpx
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

MP_BASE_URL = "https://api.mercadopago.com"


class PaymentService:
    def __init__(self, access_token: str, notification_url: str = "", sandbox: bool = False):
        self._token = access_token
        self._notification_url = notification_url
        self._sandbox = sandbox
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        if sandbox:
            logger.info("PaymentService en modo SANDBOX")

    async def crear_link(
        self,
        sku_id: str,
        nombre: str,
        precio: float,
        phone: str,
        cantidad: int = 1,
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Crea una preferencia de pago.
        Retorna (init_point, error_detail).
        Si falla: (None, "descripción del error")
        """
        expiration = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S.000-00:00")

        payload = {
            "items": [{
                "id": sku_id,
                "title": nombre,
                "quantity": max(1, int(cantidad)),
                "unit_price": round(precio, 2),
                "currency_id": "ARS",
            }],
            "expiration_date_to": expiration,
            "external_reference": f"{phone}_{sku_id}",
            "statement_descriptor": "FARMACIA AMI",
        }

        if self._notification_url:
            payload["notification_url"] = self._notification_url
        else:
            # Sin esta URL, MP cobra pero no avisa: el cliente paga y no recibe
            # su código ni se crea el pedido. Falla silenciosa y cara.
            logger.error(
                "MP_NOTIFICATION_URL vacía: el link se genera SIN aviso de pago. "
                "Los cobros no se van a confirmar solos (habrá que reprocesarlos "
                "con /bo/mp/reprocesar/{id})."
            )

        logger.info(f"MP request → sku_id={sku_id} precio={precio} token_prefix={self._token[:12]}...")

        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    f"{MP_BASE_URL}/checkout/preferences",
                    headers=self._headers,
                    json=payload,
                    timeout=10,
                )
                data = resp.json()
                logger.info(f"MP response status={resp.status_code} body={data}")

                if resp.status_code != 201:
                    error = data.get("message") or data.get("error") or str(data)
                    return None, f"HTTP {resp.status_code}: {error}"

                key = "sandbox_init_point" if self._sandbox else "init_point"
                link = data.get(key)
                if not link:
                    return None, f"MP respondió 201 pero sin {key}: {data}"

                return link, None

            except httpx.TimeoutException:
                return None, "Timeout conectando a Mercado Pago"
            except Exception as e:
                return None, str(e)

    async def get_payment_info(self, payment_id: str) -> Optional[dict]:
        """
        Consulta el estado de un pago por su ID.
        Retorna el dict completo de MP o None si falla.
        """
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(
                    f"{MP_BASE_URL}/v1/payments/{payment_id}",
                    headers=self._headers,
                    timeout=10,
                )
                if resp.status_code != 200:
                    logger.warning(f"MP get_payment status={resp.status_code}")
                    return None
                return resp.json()
            except Exception as e:
                logger.error(f"Error consultando pago MP: {e}")
                return None


_instance: Optional[PaymentService] = None


def get_payment_service(access_token: str, notification_url: str = "", sandbox: bool = False) -> PaymentService:
    global _instance
    if _instance is None:
        _instance = PaymentService(access_token, notification_url, sandbox)
    return _instance
