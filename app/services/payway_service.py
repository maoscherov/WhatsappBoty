"""
Integración con Payway Ventas Online (ex Decidir/Prisma) — API REST v2.

A diferencia de Mercado Pago (que da un link de checkout listo), el flujo
estándar de Payway tokeniza la tarjeta en el frontend con la PUBLIC KEY y
cobra desde el backend con la PRIVATE KEY. Por eso hosteamos una página de
pago (`/pay/{id}`) y este servicio hace el cobro.

Endpoints estándar de Decidir v2:
  Sandbox:  https://developers.decidir.com/api/v2
  Prod:     https://live.decidir.com/api/v2
  POST /tokens    (public key)  → tokeniza la tarjeta (se hace en el navegador)
  POST /payments  (private key) → ejecuta el cobro con el token
  GET  /payments/{id} (private key) → estado del pago

NOTA: montos en centavos (int). payment_method_id según tarjeta (1=Visa, etc.).
Verificar contra sandbox — los valores exactos dependen del comercio.
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_SANDBOX_BASE = "https://developers.decidir.com/api/v2"
_PROD_BASE = "https://live.decidir.com/api/v2"


_CHECKOUT_SANDBOX = "https://developers.decidir.com/web/checkout"
_CHECKOUT_PROD = "https://live.decidir.com/web/checkout"


class PaywayService:
    def __init__(self, public_key: str, private_key: str, sandbox: bool = True,
                 site_id: str = "", template_id: str = "", cybersource: bool = False):
        self._public = public_key
        self._private = private_key
        self._sandbox = sandbox
        self._site_id = site_id
        self._template_id = template_id
        self._cybersource = cybersource
        self._base = _SANDBOX_BASE if sandbox else _PROD_BASE
        self._checkout_base = _CHECKOUT_SANDBOX if sandbox else _CHECKOUT_PROD

    @property
    def base_url(self) -> str:
        return self._base

    @property
    def public_key(self) -> str:
        return self._public

    async def crear_link(
        self,
        total: float,
        site_transaction_id: str,
        success_url: str,
        cancel_url: str,
        notifications_url: str,
        installments: int = 1,
    ) -> tuple[Optional[str], Optional[str]]:
        """
        GenerateLink: crea un checkout HOSTEADO en Payway y devuelve la URL a la
        que mandar al cliente (equivalente al init_point de MP).
        Retorna (checkout_url, error). Con logging completo para ajustar en sandbox.
        """
        payload = {
            "origin_platform": "api",
            "site": self._site_id,
            "template_id": self._template_id,
            "currency": "ARS",
            "total_price": int(round(total * 100)),   # centavos (ajustar si Payway espera pesos)
            "installments": str(installments),
            "success_url": success_url,
            "cancel_url": cancel_url,
            "notifications_url": notifications_url,
            "siteOperationId": site_transaction_id,
            "public_apikey": self._public,
            "auth_3ds": False,
        }
        headers = {"apikey": self._public, "Content-Type": "application/json", "Cache-Control": "no-cache"}
        url = f"{self._base}/payments/link"
        logger.info(f"Payway GenerateLink → POST {url} payload={payload}")
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(url, headers=headers, json=payload, timeout=20)
                body = resp.text
                logger.info(f"Payway GenerateLink ← status={resp.status_code} body={body[:600]}")
                if resp.status_code not in (200, 201):
                    return None, f"HTTP {resp.status_code}: {body[:400]}"
                data = resp.json()
                # La respuesta puede traer la URL directa o un payment_id para armarla
                link = data.get("url") or data.get("payment_link") or data.get("link")
                if not link:
                    pid = data.get("id") or data.get("payment_id")
                    if pid:
                        link = f"{self._checkout_base}/{pid}"
                if not link:
                    return None, f"Respuesta sin link ni id: {data}"
                return link, None
            except httpx.TimeoutException:
                return None, "Timeout conectando a Payway"
            except Exception as e:
                return None, str(e)

    def _fraud_detection(self, amount: float, email: str) -> dict:
        """
        Bloque de datos antifraude que exige Cybersource (comercios con
        template retail / Cybersource activado). Con valores por defecto de
        sandbox — en producción conviene poblar bill_to con datos reales.
        """
        cents = int(round(amount * 100))
        return {
            "send_to_cs": True,
            "channel": "Web",
            "dispatch_method": "Store Pick Up",
            "csmdds": [
                {"code": 17, "description": "Cliente Remedia"},
            ],
            "device_unique_identifier": "remedia-web",
            "bill_to": {
                "city": "CABA",
                "country": "AR",
                "customer_id_ext": "1",
                "email": email or "cliente@remedia.ar",
                "first_name": "Cliente",
                "last_name": "Remedia",
                "phone_number": "1100000000",
                "postal_code": "1000",
                "state": "C",
                "street1": "Sin especificar",
                "street2": "",
            },
            "purchase_totals": {"currency": "ARS", "amount": cents},
        }

    async def crear_pago(
        self,
        token: str,
        amount: float,
        site_transaction_id: str,
        payment_method_id: int,
        bin: str,
        installments: int = 1,
        email: str = "",
    ) -> tuple[Optional[dict], Optional[str]]:
        """
        Ejecuta el cobro con el token (generado en el frontend con la public key).
        Retorna (respuesta_dict, error). amount en pesos → se convierte a centavos.
        """
        payload = {
            "site_transaction_id": site_transaction_id,
            "token": token,
            "payment_method_id": int(payment_method_id),
            "bin": bin,
            "amount": int(round(amount * 100)),   # centavos
            "currency": "ARS",
            "installments": int(installments),
            "description": "Compra Remedia",
            "payment_type": "single",
            "sub_payments": [],
        }
        # Cybersource: el comercio exige datos antifraude en el cobro.
        if self._cybersource:
            payload["fraud_detection"] = self._fraud_detection(amount, email)
            # Algunos comercios esperan el fingerprint también en la raíz del pago.
            payload["device_unique_identifier"] = "remedia-web"
        if email:
            payload["customer"] = {"email": email}

        headers = {"apikey": self._private, "Content-Type": "application/json", "Cache-Control": "no-cache"}
        _log_payload = {**payload, "token": "***"}
        logger.info(f"Payway payment → cybersource={self._cybersource} payload={_log_payload}")
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(f"{self._base}/payments", headers=headers, json=payload, timeout=20)
                data = resp.json()
                logger.info(f"Payway payment status={resp.status_code} id={data.get('id')} estado={data.get('status')}")
                if resp.status_code not in (200, 201):
                    return None, f"HTTP {resp.status_code}: {data}"
                return data, None
            except httpx.TimeoutException:
                return None, "Timeout conectando a Payway"
            except Exception as e:
                return None, str(e)

    async def get_payment(self, payment_id: str) -> Optional[dict]:
        """Consulta el estado de un pago por su ID."""
        headers = {"apikey": self._private, "Cache-Control": "no-cache"}
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(f"{self._base}/payments/{payment_id}", headers=headers, timeout=15)
                if resp.status_code != 200:
                    logger.warning(f"Payway get_payment status={resp.status_code}")
                    return None
                return resp.json()
            except Exception as e:
                logger.error(f"Error consultando pago Payway: {e}")
                return None


_instance: Optional[PaywayService] = None


def get_payway_service(public_key: str, private_key: str, sandbox: bool = True,
                       site_id: str = "", template_id: str = "",
                       cybersource: bool = False) -> PaywayService:
    global _instance
    if _instance is None:
        _instance = PaywayService(public_key, private_key, sandbox, site_id, template_id, cybersource)
    return _instance
