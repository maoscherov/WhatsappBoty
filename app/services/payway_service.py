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


class PaywayService:
    def __init__(self, public_key: str, private_key: str, sandbox: bool = True):
        self._public = public_key
        self._private = private_key
        self._base = _SANDBOX_BASE if sandbox else _PROD_BASE

    @property
    def base_url(self) -> str:
        return self._base

    @property
    def public_key(self) -> str:
        return self._public

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
        if email:
            payload["customer"] = {"email": email}

        headers = {"apikey": self._private, "Content-Type": "application/json", "Cache-Control": "no-cache"}
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


def get_payway_service(public_key: str, private_key: str, sandbox: bool = True) -> PaywayService:
    global _instance
    if _instance is None:
        _instance = PaywayService(public_key, private_key, sandbox)
    return _instance
