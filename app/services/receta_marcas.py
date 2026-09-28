"""
Marcas manuales de receta por producto (28/9).

El operador marca un producto como venta libre / con receta desde el ABM
"Productos · Receta" o desde la conversación que el bot derivó. La marca va a
catalog_extras.requiere_receta_override (gana sobre la referencia y sobre la
regla, y sobrevive a los syncs del ERP y a las sincronizaciones de la
referencia) y cada cambio queda en receta_cambios: quién, cuándo y desde
dónde. Es un riesgo regulatorio: tiene que quedar trazado.
"""

import logging
import time
from typing import Optional

from app.services.catalog_rules import ORIGENES, explicar_receta

logger = logging.getLogger(__name__)

MARCAS = ("si", "no", "ambiguo")
ETIQUETAS_ORIGEN = {**ORIGENES, "manual": "Marca manual"}
ORIGENES_CAMBIO = ("conversacion", "abm", "lote")

_COLUMNAS = (
    "SELECT i.branch_id, i.external_id, i.name, i.barcodes, i.category, i.rubro, i.subrubro, "
    "i.stock, i.price, i.active, i.requiere_receta, e.requiere_receta_override AS override "
    "FROM catalog_items i LEFT JOIN catalog_extras e "
    "ON e.branch_id = i.branch_id AND e.external_id = i.external_id "
)
_EFECTIVA = "COALESCE(e.requiere_receta_override, i.requiere_receta)"


class ConflictoMarca(Exception):
    """La marca no se puede aplicar tal como se pidió (→ 409)."""


def _num(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def producto_a_dict(r, ultimo: Optional[dict] = None) -> dict:
    """Fila de catalog_items (+ override) → dict del backoffice, con la marca
    efectiva y de dónde sale."""
    override = r["override"]
    if override:
        origen = "manual"
    else:
        origen = explicar_receta(r["category"], r["rubro"], r["subrubro"], r["name"],
                                 r["barcodes"] or [])[1]
    return {
        "external_id": r["external_id"],
        "nombre": r["name"],
        "codigos": list(r["barcodes"] or []),
        "categoria": r["category"] or "",
        "stock": _num(r["stock"]),
        "precio": _num(r["price"]),
        "activo": bool(r["active"]),
        "marca": override or r["requiere_receta"],
        "calculada": r["requiere_receta"],
        "marca_manual": override,
        "origen": origen,
        "origen_etiqueta": ETIQUETAS_ORIGEN.get(origen, origen),
        "ultimo_cambio": ultimo,
    }


async def producto(db, branch: str, external_id: str):
    filas = await db.fetch(_COLUMNAS + "WHERE i.branch_id = $1 AND i.external_id = $2",
                           branch, external_id)
    return filas[0] if filas else None


async def ultimos_cambios(db, branch: str, external_ids: list[str]) -> dict[str, dict]:
    """Último cambio manual de cada producto: {external_id: {autor, fecha, origen, marca}}."""
    if not external_ids:
        return {}
    filas = await db.fetch(
        "SELECT DISTINCT ON (external_id) external_id, autor, origen, marca, created_at "
        "FROM receta_cambios WHERE branch_id = $1 AND external_id = ANY($2::text[]) "
        "ORDER BY external_id, created_at DESC, id DESC",
        branch, list(external_ids))
    return {f["external_id"]: {"autor": f["autor"], "origen": f["origen"], "marca": f["marca"],
                               "fecha": f["created_at"].isoformat()} for f in filas}


def _aplicar_en_memoria(external_id: str, marca: str) -> None:
    """El bot toma la marca en el acto, sin esperar la recarga del catálogo."""
    try:
        from app.services.sku_service import get_sku_service
        sku = get_sku_service().get_by_id(external_id)
        if sku is not None:
            sku.requiere_receta = marca
    except Exception as e:
        logger.warning(f"Marca de receta de {external_id}: no se aplicó en memoria: {e}")


async def marcar(db, branch: str, external_id: str, marca: Optional[str], autor: Optional[str],
                 origen: str, phone: Optional[str] = None) -> dict:
    """
    Marca manual ('si' | 'no' | 'ambiguo') o None para sacarla (vuelve a
    mandar la referencia / la regla). La marca y su registro se escriben en
    la misma transacción: no puede quedar una sin la otra.
    """
    if marca is not None and marca not in MARCAS:
        raise ValueError("La marca tiene que ser si, no, ambiguo o vacía (sacar la marca)")
    if origen not in ORIGENES_CAMBIO:
        raise ValueError(f"Origen inválido: {origen}")
    r = await producto(db, branch, external_id)
    if r is None:
        raise LookupError(f"No existe el producto {external_id}")
    anterior = r["override"] or r["requiere_receta"]
    nueva = marca or r["requiere_receta"]
    if (r["override"] or None) == marca:
        return {**producto_a_dict(r), "anterior": anterior, "cambio": False}

    async with db.transaction() as con:
        await con.execute(
            "INSERT INTO catalog_extras (branch_id, external_id, requiere_receta_override, updated_at) "
            "VALUES ($1, $2, $3, now()) ON CONFLICT (branch_id, external_id) DO UPDATE "
            "SET requiere_receta_override = EXCLUDED.requiere_receta_override, updated_at = now()",
            branch, external_id, marca)
        await con.execute(
            "INSERT INTO receta_cambios (branch_id, external_id, nombre, barcode, anterior, nuevo, "
            "marca, autor, origen, phone) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
            branch, external_id, r["name"] or "", (list(r["barcodes"] or []) or [None])[0],
            anterior, nueva, marca, (autor or None), origen, (phone or None))
    _aplicar_en_memoria(external_id, nueva)
    logger.info(f"Receta: {external_id} ({r['name']}) {anterior} → {nueva} "
                f"por {autor or '?'} desde {origen}")
    r = await producto(db, branch, external_id)
    ultimo = (await ultimos_cambios(db, branch, [external_id])).get(external_id)
    return {**producto_a_dict(r, ultimo), "anterior": anterior, "cambio": True}


async def listar(db, branch: str, q: str = "", marca: str = "ambiguo", con_stock: bool = True,
                 solo_medicamentos: bool = False, page: int = 1, page_size: int = 50,
                 sin_limite: bool = False) -> tuple[int, list[dict]]:
    """
    Productos activos para el ABM. `marca`: si | no | ambiguo | manual | todas
    (sobre la marca EFECTIVA). Por defecto "a validar con stock": lo único que
    hace falta revisar para que el bot venda.
    """
    conds = ["i.branch_id = $1", "i.active"]
    args: list = [branch]
    if marca in MARCAS:
        args.append(marca)
        conds.append(f"{_EFECTIVA} = ${len(args)}")
    elif marca == "manual":
        conds.append("e.requiere_receta_override IS NOT NULL")
    elif marca not in ("todas", "", None):
        raise ValueError("marca: si | no | ambiguo | manual | todas")
    if con_stock:
        conds.append("i.stock > 0")
    if solo_medicamentos:
        conds.append("(i.category ILIKE '%medicament%' OR i.rubro ILIKE '%medicament%')")
    digitos = "".join(ch for ch in (q or "") if ch.isdigit())
    palabras = [p for p in (q or "").split() if p.strip()]
    if digitos and len(digitos) >= 6 and len(digitos) == len((q or "").strip()):
        args.append(f"%{digitos.lstrip('0')}%")
        conds.append(f"array_to_string(i.barcodes, ' ') LIKE ${len(args)}")
    else:
        for p in palabras:
            args.append(f"%{p}%")
            conds.append(f"i.name ILIKE ${len(args)}")
    where = " WHERE " + " AND ".join(conds)
    total_f = await db.fetch(
        "SELECT COUNT(*) AS n FROM catalog_items i LEFT JOIN catalog_extras e "
        "ON e.branch_id = i.branch_id AND e.external_id = i.external_id" + where, *args)
    total = int(total_f[0]["n"]) if total_f else 0
    orden = " ORDER BY i.name, i.external_id"
    if sin_limite:
        filas = await db.fetch(_COLUMNAS + where + orden + " LIMIT 20000", *args)
    else:
        page, page_size = max(1, page), max(1, min(page_size, 200))
        args += [page_size, (page - 1) * page_size]
        filas = await db.fetch(
            _COLUMNAS + where + orden + f" LIMIT ${len(args) - 1} OFFSET ${len(args)}", *args)
    ultimos = await ultimos_cambios(db, branch, [f["external_id"] for f in filas
                                                  if f["override"]])
    return total, [producto_a_dict(f, ultimos.get(f["external_id"])) for f in filas]


async def resumen(db, branch: str) -> dict:
    """Conteos para los filtros del ABM (productos activos, marca efectiva)."""
    filas = await db.fetch(
        f"SELECT {_EFECTIVA} AS marca, COUNT(*) AS n, "
        f"COUNT(*) FILTER (WHERE i.stock > 0) AS con_stock, "
        f"COUNT(*) FILTER (WHERE e.requiere_receta_override IS NOT NULL) AS manuales "
        f"FROM catalog_items i LEFT JOIN catalog_extras e "
        f"ON e.branch_id = i.branch_id AND e.external_id = i.external_id "
        f"WHERE i.branch_id = $1 AND i.active GROUP BY 1", branch)
    out = {"si": 0, "no": 0, "ambiguo": 0, "a_validar_con_stock": 0, "manuales": 0}
    for f in filas:
        if f["marca"] in MARCAS:
            out[f["marca"]] += int(f["n"])
        if f["marca"] == "ambiguo":
            out["a_validar_con_stock"] += int(f["con_stock"])
        out["manuales"] += int(f["manuales"])
    return out


async def cambios(db, branch: Optional[str] = None, external_id: Optional[str] = None,
                  limit: int = 100) -> list[dict]:
    """Registro de marcas manuales, más nuevas primero."""
    conds, args = [], []
    if branch:
        args.append(branch)
        conds.append(f"branch_id = ${len(args)}")
    if external_id:
        args.append(external_id)
        conds.append(f"external_id = ${len(args)}")
    args.append(max(1, min(limit, 1000)))
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    filas = await db.fetch(
        "SELECT id, external_id, nombre, barcode, anterior, nuevo, marca, autor, origen, phone, "
        f"created_at FROM receta_cambios{where} ORDER BY created_at DESC, id DESC "
        f"LIMIT ${len(args)}", *args)
    return [{**{k: f[k] for k in ("id", "external_id", "nombre", "barcode", "anterior", "nuevo",
                                  "marca", "autor", "origen", "phone")},
             "fecha": f["created_at"].isoformat()} for f in filas]


# ── Productos de una conversación ───────────────────────────────────────────────
async def productos_de_conversacion(db, branch: Optional[str], session: dict) -> list[dict]:
    """
    Los productos que frenaron la venta por receta en esta conversación (el
    bot los guarda al derivar) y los del pedido en curso, con su marca.
    """
    vistos: dict[str, dict] = {}
    for p in session.get("_productos_receta") or []:
        if p.get("sku_id"):
            vistos[str(p["sku_id"])] = {"motivo": "frenado_por_receta",
                                        "cantidad": int(p.get("cantidad") or 1),
                                        "nombre": p.get("nombre") or ""}
    pendientes = list(session.get("pending_items") or [])
    if not pendientes and session.get("pending_sku_id"):
        pendientes = [{"sku_id": session["pending_sku_id"],
                       "nombre": session.get("pending_sku_nombre") or "",
                       "cantidad": session.get("pending_cantidad") or 1}]
    for it in pendientes:
        sid = str(it.get("sku_id") or "")
        if sid and sid not in vistos:
            vistos[sid] = {"motivo": "en_el_pedido", "cantidad": int(it.get("cantidad") or 1),
                           "nombre": it.get("nombre") or ""}
    if not vistos:
        return []
    filas = {}
    if branch:
        for f in await db.fetch(_COLUMNAS + "WHERE i.branch_id = $1 AND i.external_id = ANY($2::text[])",
                                branch, list(vistos)):
            filas[f["external_id"]] = f
    ultimos = await ultimos_cambios(db, branch, list(filas)) if branch else {}
    out = []
    for sid, extra in vistos.items():
        if sid in filas:
            out.append({**producto_a_dict(filas[sid], ultimos.get(sid)), **extra,
                        "marcable": True})
        else:
            # Catálogo CSV (sin ERP) o producto dado de baja: se muestra, sin marcar.
            out.append({"external_id": sid, "nombre": extra["nombre"], "marca": None,
                        "marcable": False, **extra})
    return out


# ── Devolver al bot: retomar la venta ───────────────────────────────────────────
async def retomar_venta(phone: str, sku_id: str) -> str:
    """
    El operador marcó el producto como venta libre y eligió devolver la
    conversación al bot: el bot le vuelve a ofrecer el producto con precio y
    sigue la venta (retiro / envío) como si el cliente lo hubiera confirmado.
    Devuelve el mensaje enviado. ConflictoMarca si no se puede.
    """
    from app.routers.webhook import _deps
    from app.services.checkout_helper import (aplicar_descuento_socio, bot_encendido,
                                              confirmar_pedido)
    from app.services.message_store import guardar_historico
    from app.services.socio_service import nombre_de_pila

    deps = _deps()
    cfg = await deps["config"].get_all()
    if not bot_encendido(cfg):
        raise ConflictoMarca("El bot está apagado: la conversación sigue con el operador")
    sku = deps["sku"].get_by_id(sku_id)
    if sku is None or not sku.vendible:
        raise ConflictoMarca("El producto no se puede vender ahora (sin stock, sin precio o "
                             "pausado): la conversación sigue con el operador")
    session = await deps["session"].get(phone)
    cantidad = next((int(p.get("cantidad") or 1) for p in session.get("_productos_receta") or []
                     if str(p.get("sku_id")) == str(sku_id)), 1)
    nombre = sku.sku_nombre_original or sku.sku_nombre
    items, pct = aplicar_descuento_socio(
        [{"sku_id": sku_id, "nombre": nombre, "precio": sku.precio_venta,
          "requiere_receta": sku.requiere_receta}], phone, cfg, deps["socios"])
    precio = items[0]["precio"]

    await deps["session"].liberar(phone)
    await deps["session"].set_pending(phone, sku_id, nombre, precio, cantidad, opciones=[])
    session = await deps["session"].get(phone)
    pila = nombre_de_pila(deps["socios"].find_by_phone(phone))
    respuesta, intencion = await confirmar_pedido(
        deps["sku"], deps["payment"], deps["session"], deps["socios"], cfg, phone, session,
        nombre=pila)

    saludo = f"¡Buenas noticias, {pila}!" if pila else "¡Buenas noticias!"
    cant = f" x{cantidad}" if cantidad > 1 else ""
    if intencion == "esperando_entrega" or intencion == "pedido_confirmado":
        desc = f", ya con tu {pct:g}% de descuento" if pct > 0 else ""
        texto = (f"{saludo} Confirmamos que {nombre}{cant} se vende sin receta 🙌 "
                 f"Sale ${precio * cantidad:,.2f}{desc}.\n\n{respuesta}")
    else:
        # Sin stock en vivo u otra traba: se avisa sin prometer precio.
        texto = f"{saludo} Confirmamos que {nombre} se vende sin receta 🙌\n\n{respuesta}"
    await deps["wa"].send_text(phone, texto)
    await deps["session"].add_message(phone, "assistant", texto)
    await guardar_historico(phone, "assistant", texto)
    logger.info(f"Venta retomada por el bot para {phone}: {sku_id} ({intencion})")
    return texto


async def recordar_producto_por_receta(session_svc, sku_svc, phone: str, sku_id: str,
                                       cantidad: Optional[int] = None) -> None:
    """
    Guarda en la sesión qué producto frenó la venta por receta. Al derivar se
    borra el pendiente y se perdía cuál era: el operador no podía marcarlo
    como venta libre desde la conversación. Best-effort.
    """
    try:
        sku = sku_svc.get_by_id(sku_id) if sku_id else None
        if sku is None:
            return
        s = await session_svc.get(phone)
        if cantidad is None:
            cantidad = s.get("pending_cantidad") or 1
            for it in s.get("pending_items") or []:
                if str(it.get("sku_id")) == str(sku_id):
                    cantidad = it.get("cantidad") or 1
        lista = [p for p in (s.get("_productos_receta") or [])
                 if str(p.get("sku_id")) != str(sku_id)]
        lista.append({"sku_id": str(sku_id), "nombre": sku.sku_nombre_original or sku.sku_nombre,
                      "cantidad": int(cantidad or 1), "ts": time.time()})
        s["_productos_receta"] = lista[-5:]
        await session_svc.save(phone, s)
    except Exception as e:
        logger.debug(f"recordar_producto_por_receta({phone}, {sku_id}): {e}")
