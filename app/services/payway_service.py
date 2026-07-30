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

# API de pagos (tokenize + charge)
_SANDBOX_BASE = "https://developers.decidir.com/api/v2"
_PROD_BASE = "https://ventasonline.payway.com.ar/api/v2"

# API del botón de pago / checkout hosteado (GenerateLink)
_CHECKOUT_API_SANDBOX = "https://developers.decidir.com/api/v1/checkout-payment-button"
_CHECKOUT_API_PROD = "https://ventasonline.payway.com.ar/api/v1/checkout-payment-button"

# Base de la página del formulario hosteado (donde vive {payment_id})
_CHECKOUT_WEB_SANDBOX = "https://developers.decidir.com/web/checkout"
_CHECKOUT_WEB_PROD = "https://ventasonline.payway.com.ar/web/checkout"


def _xsource() -> str:
    """Header X-Source que espera el checkout (base64 de un JSON identificador)."""
    import base64 as _b64
    import json as _json
    obj = {"service": "SDK-PYTHON", "grouper": "remedia", "developer": "remedia"}
    return _b64.b64encode(_json.dumps(obj).encode()).decode()


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
        self._checkout_api = _CHECKOUT_API_SANDBOX if sandbox else _CHECKOUT_API_PROD
        self._checkout_web = _CHECKOUT_WEB_SANDBOX if sandbox else _CHECKOUT_WEB_PROD

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
        GenerateLink (botón de pago): crea un checkout HOSTEADO en Payway y
        devuelve la URL a la que mandar al cliente (equivalente al init_point
        de MP). Payway hostea el formulario y maneja Cybersource por su cuenta.

        Endpoint (según SDK oficial): POST {checkout_api}/link con apikey pública.
        template_id: 1 = sin Cybersource, 2 = con Cybersource.
        Retorna (checkout_url, error). Con logging completo para ajustar.
        """
        # El botón de pago sólo admite template 1 (sin Cybersource) o 2 (con).
        # Cualquier otro valor se ignora y se deriva del flag cybersource.
        try:
            tpl = int(self._template_id)
        except (TypeError, ValueError):
            tpl = 0
        if tpl not in (1, 2):
            tpl = 2 if self._cybersource else 1
        payload = {
            "origin_platform": "api",
            "site": self._site_id,
            "template_id": tpl,
            "currency": "ARS",
            "total_price": round(float(total), 2),   # PESOS con decimales (no centavos)
            "installments": [int(installments)],     # array
            "success_url": success_url,
            "cancel_url": cancel_url,
            "notifications_url": notifications_url,
            "siteOperationId": site_transaction_id,
            "public_apikey": self._public,
            "auth_3ds": False,
        }
        # GenerateLink es server-a-server → va con la clave PRIVADA en el header
        # (la pública viaja en el body como public_apikey).
        headers = {
            "apikey": self._private,
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "X-Source": _xsource(),
        }
        url = f"{self._checkout_api}/link"
        logger.info(f"Payway GenerateLink → POST {url} payload={payload}")
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(url, headers=headers, json=payload, timeout=20)
                body = resp.text
                logger.info(f"Payway GenerateLink ← status={resp.status_code} body={body[:800]}")
                if resp.status_code not in (200, 201):
                    return None, f"HTTP {resp.status_code}: {body[:500]}"
                data = resp.json()
                # La respuesta puede traer la URL directa o un id para armarla
                link = data.get("url") or data.get("payment_link") or data.get("link") or data.get("checkout_url")
                if not link:
                    pid = data.get("id") or data.get("payment_id") or data.get("hash")
                    if pid:
                        link = f"{self._checkout_web}/{pid}"
                if not link:
                    return None, f"Respuesta sin link ni id: {data}"
                return link, None
            except httpx.TimeoutException:
                return None, "Timeout conectando a Payway"
            except Exception as e:
                return None, str(e)

    @staticmethod
    def _csmdds() -> list:
        """
        Merchant Defined Data para retail — el SDK oficial genera los códigos
        17-34 y 43-99 con descripción 'Campo MDD{code}'.
        """
        codes = list(range(17, 35)) + list(range(43, 100))
        return [{"code": c, "description": f"Campo MDD{c}"} for c in codes]

    def _fraud_detection(self, amount: float, email: str, device_id: str,
                         producto: str = "Producto") -> dict:
        """
        Bloque antifraude Cybersource — vertical RETAIL (según cs_retail.js del
        SDK oficial). Valores por defecto de sandbox; en producción conviene
        poblar bill_to/ship_to con datos reales del cliente.
        """
        cents = int(round(amount * 100))
        persona = {
            "city": "CABA",
            "country": "AR",
            "customer_id": "1",
            "email": email or "cliente@remedia.ar",
            "first_name": "Cliente",
            "last_name": "Remedia",
            "phone_number": "1100000000",
            "postal_code": "1000",
            "state": "C",
            "street1": "Sin especificar",
            "street2": "",
        }
        return {
            "send_to_cs": True,
            "channel": "Web",
            "dispatch_method": "Store Pick Up",
            "device_unique_identifier": device_id,
            "bill_to": persona,
            "purchase_totals": {"currency": "ARS", "amount": cents},
            "customer_in_site": {
                "days_in_site": 0,
                "is_guest": True,
                "password": "",
                "num_of_transactions": 1,
                "cellphone_number": "1100000000",
                "date_of_birth": "",
                "street": "Sin especificar",
            },
            "retail_transaction_data": {
                "ship_to": {**persona},
                "dispatch_method": "Store Pick Up",
                "days_to_delivery": "0",
                "tax_voucher_required": False,
                "customer_loyality_number": "",
                "coupon_code": "",
                "items": [{
                    "code": "1",
                    "description": producto[:80],
                    "name": producto[:80],
                    "product_name": producto[:80],
                    "sku": "1",
                    "total_amount": cents,
                    "quantity": 1,
                    "unit_price": cents,
                }],
            },
            "csmdds": self._csmdds(),
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
        device_id: str = "",
        producto: str = "Producto",
    ) -> tuple[Optional[dict], Optional[str]]:
        """
        Ejecuta el cobro con el token (generado en el frontend con la public key).
        Retorna (respuesta_dict, error). amount en pesos → se convierte a centavos.
        device_id: mismo fingerprint usado al tokenizar (requerido por Cybersource).
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
            dev = device_id or "remedia-web"
            payload["fraud_detection"] = self._fraud_detection(amount, email, dev, producto)
            # El fingerprint también en la raíz del pago.
            payload["device_unique_identifier"] = dev
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
