"""
Corrección del catálogo con datos EN VIVO del ERP (vía el agente por WebSocket).

Hallazgo 11/9: el endpoint de lotes de Observer devuelve precio 0 y stock 0
para productos que la consulta individual trae bien (Aveno solar: lote 0/0,
en vivo 2/$32.409). Hasta que el agente haga el "pase de verdad" por códigos
de barras, el servidor corrige el dato dudoso preguntándole al agente en el
momento: al ofrecer (acá), al cobrar (checkout_helper) y en los lookups del
backoffice. Cada respuesta del ERP actualiza memoria + Postgres, así el
catálogo se va curando solo con el uso.

Todo best-effort: sin agente, timeout o error se sigue con el dato cacheado.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

OFERTA_TIMEOUT_SECS = 2.0
MAX_IDS_POR_OFERTA = 3


def es_dudoso(r: dict) -> bool:
    """Precio 0 o sin stock en el cache: candidatos a verificar en vivo."""
    return (float(r.get("precio") or 0) <= 0
            or bool(r.get("sin_stock"))
            or r.get("estado") in ("sin_stock", "consultar"))


async def aplicar_items_vivos(items: list[dict], branch_id: str, sku_svc=None) -> dict[str, dict]:
    """
    Vuelca lo que devolvió el ERP en memoria (SKUService) y Postgres
    (catalog_items). Devuelve {external_id: item}. Best-effort por ítem.
    """
    from app.config import get_settings
    from app.services.db import get_db
    from app.services.catalog_store import get_catalog_store
    from app.services.sku_service import get_sku_service

    settings = get_settings()
    sku_svc = sku_svc or get_sku_service(settings.sku_csv_path)
    store = get_catalog_store(get_db(settings.database_url))
    vivos: dict[str, dict] = {}
    for it in items or []:
        sid = str(it.get("external_id") or "")
        if not sid:
            continue
        vivos[sid] = it
        try:
            stock = int(it.get("stock") or 0)
            precio_raw = it.get("price")
            precio = float(precio_raw) if precio_raw not in (None, "") else None
            sku = sku_svc.get_by_id(sid)
            if sku is not None:
                sku.stock_actual = float(stock)
                sku.cantidad_visible = max(stock, 0)
                if precio is not None and precio > 0:
                    sku.precio_venta = precio
            await store.update_stock(branch_id, sid, stock, price=precio_raw)
        except Exception as e:
            logger.debug(f"catalog_live: no se pudo aplicar {sid}: {e}")
    return vivos


async def lookup_y_aplicar(ids: list[str], timeout: float = OFERTA_TIMEOUT_SECS,
                           sku_svc=None) -> Optional[dict[str, dict]]:
    """
    Pregunta al agente por `ids` y aplica la respuesta. None = sin agente,
    sin sucursal ERP o timeout (seguir con el cache).
    """
    from app.services.agent_registry import get_agent_registry
    from app.services.catalog_source import resolver_branch_default

    branch = await resolver_branch_default()
    if not branch or not ids:
        return None
    registry = get_agent_registry()
    if not registry.connected(branch):
        return None
    res = await registry.lookup(branch, ids=list(ids), timeout=timeout)
    if res is None:
        return None
    vivos = await aplicar_items_vivos(res.items, branch, sku_svc=sku_svc)
    for m in res.missing or []:
        vivos.setdefault(str(m), {"external_id": str(m), "stock": 0, "missing": True})
    return vivos


async def refrescar_ofertas_en_vivo(resultados: list[dict], sku_svc,
                                    timeout: float = OFERTA_TIMEOUT_SECS) -> list[dict]:
    """
    Antes de ofrecer: si algún resultado tiene precio 0 o sin stock en el
    cache, se verifica en vivo (hasta MAX_IDS_POR_OFERTA ids, un solo
    round-trip) y se devuelven los resultados reconstruidos con el dato real.
    """
    if not resultados:
        return resultados
    dudosos = [str(r["sku_id"]) for r in resultados if es_dudoso(r)][:MAX_IDS_POR_OFERTA]
    if not dudosos:
        return resultados
    try:
        vivos = await lookup_y_aplicar(dudosos, timeout=timeout, sku_svc=sku_svc)
    except Exception as e:
        logger.warning(f"catalog_live: verificación al ofrecer falló: {e}")
        return resultados
    if not vivos:
        return resultados
    salida = []
    for r in resultados:
        sid = str(r.get("sku_id"))
        if sid in vivos and not vivos[sid].get("missing"):
            sku = sku_svc.get_by_id(sid)
            if sku is not None:
                salida.append(sku_svc._to_response(sku))
                continue
        salida.append(r)
    corregidos = [s for s in dudosos if s in vivos and not vivos[s].get("missing")]
    if corregidos:
        logger.info(f"catalog_live: {len(corregidos)} oferta(s) corregidas con dato en vivo: {corregidos}")
    return salida
