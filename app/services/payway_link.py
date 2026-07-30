"""
Link de pago Payway para el flujo del bot.

Payway no tiene checkout hosteado para este comercio, así que el "link de
pago" es nuestra propia página /pay/{pid} (formulario que tokeniza y cobra).
Este módulo guarda el pago pendiente en Redis y expone un adapter con la
misma interfaz que PaymentService.crear_link (MP), para que el flujo del
bot sea agnóstico del proveedor (PAYMENT_PROVIDER=mercadopago|payway).
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis

from app.config import get_settings

logger = logging.getLogger(__name__)

PENDING_TTL = 60 * 60 * 24   # 24h


def _redis():
    return aioredis.from_url(get_settings().redis_url, decode_responses=True)


async def crear_pago_pendiente(phone: str, sku_id: str, sku_nombre: str,
                               cantidad: int, total: float) -> str:
    """Guarda un pago pendiente y devuelve la URL de la página de pago."""
    settings = get_settings()
    pid = uuid.uuid4().hex[:16]
    data = {
        "id": pid, "phone": phone, "sku_id": sku_id, "sku_nombre": sku_nombre,
        "cantidad": cantidad, "total": total, "estado": "pendiente",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await _redis().setex(f"payway:pending:{pid}", PENDING_TTL, json.dumps(data))
    except Exception as e:
        logger.error(f"No se pudo guardar pago pendiente Payway: {e}")
    base = settings.public_base_url.rstrip("/")
    return f"{base}/pay/{pid}"


class PaywayLinkService:
    """Misma interfaz que PaymentService (MP) para el flujo del bot."""

    async def crear_link(
        self,
        sku_id: str,
        nombre: str,
        precio: float,
        phone: str,
        cantidad: int = 1,
    ) -> tuple[Optional[str], Optional[str]]:
        settings = get_settings()
        if not settings.public_base_url:
            return None, "PUBLIC_BASE_URL no configurada"
        total = round(precio * max(1, int(cantidad)), 2)
        try:
            url = await crear_pago_pendiente(phone=phone, sku_id=sku_id,
                                             sku_nombre=nombre, cantidad=cantidad, total=total)
            return url, None
        except Exception as e:
            return None, str(e)


_instance: Optional[PaywayLinkService] = None


def get_payway_link_service() -> PaywayLinkService:
    global _instance
    if _instance is None:
        _instance = PaywayLinkService()
    return _instance
