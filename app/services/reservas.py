"""
Stock comprometido por pedidos del bot que el ERP todavía no descontó (16/10).

C-5596: había 1 Roby Antifrizz y el bot vendió dos pedidos de 1 a cuenta
corriente, uno atrás del otro. El ERP descuenta recién cuando la farmacia
factura (al retirar), así que el chequeo en vivo seguía viendo 1. Acá se
suma lo que ya tiene pedido el bot: pedidos pendientes o preparados de las
últimas 48 h (no retirados, no rechazados) — al retirarse se factura y lo
descuenta el ERP.

Se cachea en memoria (instancia única) y se refresca cada `TTL` segundos;
cada pedido nuevo se suma al toque para que el siguiente lo vea.
"""

import logging
import time
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

TTL = 60
VENTANA_HORAS = 48
_ABIERTOS = ("pendiente", "preparado")

_cache: dict = {"ts": 0.0, "por_sku": {}}


def items_de(sku_id: str, cantidad: int, items: Optional[list] = None) -> list[dict]:
    """Lo que reserva un pedido: sus ítems, o el producto único."""
    if items:
        return [{"sku_id": str(i.get("sku_id")), "cantidad": int(i.get("cantidad", 1) or 1)}
                for i in items if i.get("sku_id")]
    if sku_id and sku_id != "MULTI":
        return [{"sku_id": str(sku_id), "cantidad": int(cantidad or 1)}]
    return []


def _reserva(order: dict) -> list[dict]:
    if order.get("estado") not in _ABIERTOS:
        return []
    if order.get("transferencia") == "rechazada":
        return []
    try:
        creado = datetime.fromisoformat(order["created_at"]).timestamp()
    except Exception:
        return []
    if time.time() - creado > VENTANA_HORAS * 3600:
        return []
    return items_de(order.get("sku_id") or "", order.get("cantidad", 1), order.get("items_stock"))


def calcular(orders: list[dict]) -> dict[str, int]:
    por_sku: dict[str, int] = {}
    for o in orders or []:
        for it in _reserva(o):
            por_sku[it["sku_id"]] = por_sku.get(it["sku_id"], 0) + it["cantidad"]
    return por_sku


async def refrescar(forzar: bool = False) -> dict[str, int]:
    if not forzar and time.time() - _cache["ts"] < TTL:
        return _cache["por_sku"]
    try:
        from app.config import get_settings
        from app.services.order_service import get_order_service
        orders = await get_order_service(get_settings().redis_url).list_all()
        _cache.update(ts=time.time(), por_sku=calcular(orders))
    except Exception as e:
        logger.debug(f"reservas: no se pudieron leer los pedidos: {e}")
        _cache["ts"] = time.time()        # sin Redis no se reintenta en cada mensaje
    return _cache["por_sku"]


def sumar(items: list[dict]) -> None:
    """Un pedido recién creado reserva al toque (sin esperar al refresco)."""
    for it in items or []:
        sid = str(it["sku_id"])
        _cache["por_sku"][sid] = _cache["por_sku"].get(sid, 0) + int(it.get("cantidad", 1) or 1)


def invalidar() -> None:
    _cache["ts"] = 0.0


def reservado(sku_id) -> int:
    """Unidades comprometidas (del último refresco)."""
    return int(_cache["por_sku"].get(str(sku_id), 0))


def disponible(stock: Optional[int], sku_id) -> Optional[int]:
    """Stock menos lo reservado; None si no se conoce el stock."""
    if stock is None or stock < 0:
        return None
    return max(0, int(stock) - reservado(sku_id))


def descontar(resultados: list[dict]) -> list[dict]:
    """
    Resultados de búsqueda con el stock reservado descontado: si lo que queda
    es 0, el producto se muestra sin stock (no se cotiza como disponible).
    Devuelve copias.
    """
    if not resultados or not _cache["por_sku"]:
        return resultados
    out = []
    for r in resultados:
        res = reservado(r.get("sku_id"))
        cant = r.get("cantidad_visible")
        if res and isinstance(cant, (int, float)) and cant > 0 and not r.get("sin_stock"):
            queda = max(0, int(cant) - res)
            r = dict(r, cantidad_visible=queda)
            if queda <= 0:
                r.update(sin_stock=True, vendible=False, estado="sin_stock")
        out.append(r)
    return out
