"""
"¿Qué ve el bot?" — vista previa de stock para el backoffice (22/9).

Corre la MISMA cadena que el webhook antes de ofrecer un producto (buscar →
verificar en vivo lo dudoso → filtrar por stock → descuento de socio) y
explica, por producto, qué dato tiene el bot y si lo ofrecería. Sirve para
cerrar en un minuto casos como "el bot dice que no hay Aveno y tengo dos".
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

MAX_RESULTADOS = 6


def _motivo_no_ofrecible(r: dict, receta_mode: str) -> Optional[str]:
    from app.services.sku_service import requiere_derivacion
    if float(r.get("precio") or 0) <= 0:
        return "sin_precio"
    if r.get("sin_stock") or r.get("estado") == "sin_stock":
        return "sin_stock"
    if not r.get("vendible", True):
        return "no_vendible"
    if requiere_derivacion(r.get("requiere_receta") or "no", receta_mode):
        return "requiere_receta"
    return None


async def vista_previa_stock(q: str, sku_svc, cfg: dict, phone: str = "",
                             vivo: bool = False, socio_svc=None,
                             timeout: Optional[float] = None) -> dict:
    """
    Devuelve lo que el bot ofrecería para `q`. `vivo=True` fuerza la consulta
    al ERP de TODOS los resultados (no solo los dudosos): pega al ERP, usar a
    demanda. `phone` aplica el descuento de socio como en la conversación.
    """
    from app.services.catalog_live import (
        es_dudoso, filtrar_por_stock, lookup_y_aplicar, refrescar_ofertas_en_vivo,
        OFERTA_TIMEOUT_SECS,
    )
    from app.services.catalog_source import estado as estado_catalogo
    from app.services.checkout_helper import aplicar_descuento_socio

    timeout = timeout or OFERTA_TIMEOUT_SECS
    receta_mode = cfg.get("receta_mode", "conservador")

    cache = sku_svc.buscar(q, top_n=MAX_RESULTADOS)
    antes = {str(r["sku_id"]): dict(r) for r in cache}
    dudosos = {str(r["sku_id"]) for r in cache if es_dudoso(r)}

    vivos: dict[str, dict] = {}
    canal_ok: Optional[bool] = None
    if cache:
        try:
            if vivo:
                res = await lookup_y_aplicar([str(r["sku_id"]) for r in cache],
                                             timeout=timeout, sku_svc=sku_svc)
                canal_ok = res is not None
                vivos = res or {}
                actuales = []
                for r in cache:
                    s = sku_svc.get_by_id(str(r["sku_id"]))
                    actuales.append(sku_svc._to_response(s) if s else r)
            else:
                actuales = await refrescar_ofertas_en_vivo(cache, sku_svc, timeout=timeout)
                if dudosos:
                    # refrescar_ofertas_en_vivo no expone si hubo canal: se
                    # infiere de si algún dudoso cambió.
                    canal_ok = any(actuales[i] != cache[i] for i in range(len(cache)))
        except Exception as e:
            logger.warning(f"stock_preview: verificación en vivo falló: {e}")
            actuales = cache
            canal_ok = False
    else:
        actuales = []

    ofrecidos = filtrar_por_stock(list(actuales))
    if phone:
        ofrecidos, pct = aplicar_descuento_socio(ofrecidos, phone, cfg, socio_svc)
    else:
        pct = 0.0
    ofrecidos_ids = {str(r["sku_id"]) for r in ofrecidos}
    precio_socio = {str(r["sku_id"]): r.get("precio") for r in ofrecidos}

    productos = []
    for r in actuales:
        sid = str(r["sku_id"])
        a = antes.get(sid, {})
        v = vivos.get(sid)
        if v is None and sid in dudosos and canal_ok and a != r:
            v = {"stock": r.get("cantidad_visible"), "precio": r.get("precio")}
        motivo = _motivo_no_ofrecible(r, receta_mode)
        productos.append({
            "sku_id": sid,
            "nombre": r.get("nombre"),
            "barcode": r.get("barcode"),
            "precio": r.get("precio"),
            "precio_socio": precio_socio.get(sid) if phone and sid in ofrecidos_ids else None,
            "requiere_receta": r.get("requiere_receta"),
            "stock_cache": {"unidades": a.get("cantidad_visible"), "estado": a.get("estado"),
                            "precio": a.get("precio")},
            "stock_vivo": (None if v is None else
                           {"consultado": True, "encontrado": not v.get("missing"),
                            "unidades": v.get("stock"), "precio": v.get("precio")}),
            "verificado_en_vivo": v is not None,
            "ofrecible": sid in ofrecidos_ids and motivo is None,
            "motivo": motivo if sid not in ofrecidos_ids or motivo else None,
        })

    try:
        fuente = await estado_catalogo()
    except Exception as e:
        fuente = {"error": str(e)}

    return {
        "query": q,
        "total_catalogo": sku_svc.total,
        "fuente": fuente,
        "verificacion_vivo": {
            "modo": "todos" if vivo else ("dudosos" if dudosos else "no_necesaria"),
            "canal_disponible": canal_ok,
            "ids_consultados": sorted(vivos.keys()) if vivo else sorted(dudosos),
        },
        "descuento_socio_pct": pct if phone else None,
        "productos": productos,
        "respuesta_esperada": _resumen(productos),
    }


def _resumen(productos: list[dict]) -> str:
    """Una línea con lo que el bot diría, para leer de un vistazo."""
    ofrecibles = [p for p in productos if p["ofrecible"]]
    if not productos:
        return "No encuentra nada en el catálogo: diría que no lo tiene y ofrecería consultarlo o encargarlo."
    if not ofrecibles:
        motivos = sorted({p["motivo"] for p in productos if p.get("motivo")})
        return (f"Encuentra {len(productos)} producto(s) pero ninguno ofrecible "
                f"({', '.join(motivos) or 'sin motivo'}): diría que no lo tiene y ofrecería encargarlo.")
    lista = "; ".join(f"{p['nombre']} — ${float(p['precio_socio'] or p['precio']):,.2f}" for p in ofrecibles[:3])
    return f"Ofrecería {len(ofrecibles)} producto(s): {lista}."
