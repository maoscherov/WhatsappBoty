"""
Marcas manuales de receta: ABM "Productos · Receta" y marcas desde una
conversación (28/9). Cualquier operador puede marcar; queda registrado quién,
cuándo y desde dónde (receta_cambios).
"""

import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from app.config import get_settings
from app.routers.backoffice import _auth
from app.services import receta_marcas
from app.services.catalog_source import resolver_branch_default
from app.services.db import get_db
from app.services.session_service import get_session_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bo")


class MarcaRecetaIn(BaseModel):
    requiere_receta: str                 # "no" (venta libre) | "si" | "ambiguo" | "" (sacar la marca)
    autor: Optional[str] = None
    phone: Optional[str] = None          # desde una conversación
    devolver_al_bot: bool = False        # solo con phone y "no": el bot retoma la venta


class LoteRecetaIn(BaseModel):
    external_ids: list[str]
    requiere_receta: str
    autor: Optional[str] = None


def _db():
    db = get_db(get_settings().database_url)
    if not db.available():
        raise HTTPException(status_code=503, detail="Base de datos no disponible")
    return db


async def _branch() -> str:
    branch = await resolver_branch_default()
    if not branch:
        raise HTTPException(status_code=409,
                            detail="Sin catálogo del ERP activo: las marcas se aplican sobre él")
    return branch


def _programar_recarga(branch: str, ids: set[str]) -> None:
    """La marca ya se aplicó en memoria; la recarga (con debounce) cubre el
    caso de una recarga del catálogo que estuviera corriendo en ese momento
    y trajera los datos de antes."""
    try:
        from app.services.catalog_refresher import get_catalog_refresher
        get_catalog_refresher().schedule(branch, ids)
    except Exception as e:
        logger.warning(f"Marca de receta: no se programó la recarga: {e}")


def _marca(valor: str) -> Optional[str]:
    v = (valor or "").strip().lower()
    if v in ("", "sacar", "ninguna"):
        return None
    if v not in receta_marcas.MARCAS:
        raise HTTPException(status_code=422,
                            detail="requiere_receta: no (venta libre) | si | ambiguo | vacío")
    return v


@router.get("/sku/receta")
async def bo_receta_listar(_=Depends(_auth), q: str = Query(""),
                           marca: str = Query("ambiguo"), con_stock: bool = Query(True),
                           solo_medicamentos: bool = Query(False),
                           page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200)):
    """
    ABM de receta. Por defecto: "a validar con stock" (lo que el bot deriva y
    hoy se podría vender). `marca`: si | no | ambiguo | manual | todas.
    """
    db, branch = _db(), await _branch()
    try:
        total, items = await receta_marcas.listar(db, branch, q, marca, con_stock,
                                                  solo_medicamentos, page, page_size)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"total": total, "page": page, "page_size": page_size, "productos": items}


@router.get("/sku/receta/resumen")
async def bo_receta_resumen(_=Depends(_auth)):
    db, branch = _db(), await _branch()
    return await receta_marcas.resumen(db, branch)


@router.get("/sku/receta/export.csv")
async def bo_receta_export(_=Depends(_auth), q: str = Query(""), marca: str = Query("ambiguo"),
                           con_stock: bool = Query(True), solo_medicamentos: bool = Query(False)):
    """Misma lista que el ABM, completa, para revisarla con el farmacéutico."""
    db, branch = _db(), await _branch()
    try:
        _, items = await receta_marcas.listar(db, branch, q, marca, con_stock,
                                              solo_medicamentos, sin_limite=True)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["external_id", "nombre", "codigos", "categoria", "stock", "precio", "marca",
                "origen", "marca_manual", "ultimo_cambio_autor", "ultimo_cambio_fecha"])
    for p in items:
        u = p.get("ultimo_cambio") or {}
        w.writerow([p["external_id"], p["nombre"], " ".join(p["codigos"]), p["categoria"],
                    p["stock"], p["precio"], p["marca"], p["origen_etiqueta"],
                    p["marca_manual"] or "", u.get("autor") or "", u.get("fecha") or ""])
    return Response(content=buf.getvalue().encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="productos_receta.csv"'})


@router.post("/sku/receta/lote")
async def bo_receta_lote(body: LoteRecetaIn, _=Depends(_auth)):
    """Marca varios productos de una vez (seleccionados en el ABM)."""
    db, branch = _db(), await _branch()
    marca = _marca(body.requiere_receta)
    if not body.external_ids:
        raise HTTPException(status_code=422, detail="Elegí al menos un producto")
    cambiados, sin_cambio, no_encontrados = 0, 0, []
    for eid in dict.fromkeys(body.external_ids):
        try:
            r = await receta_marcas.marcar(db, branch, eid, marca, body.autor, "lote")
            cambiados += 1 if r["cambio"] else 0
            sin_cambio += 0 if r["cambio"] else 1
        except LookupError:
            no_encontrados.append(eid)
    if cambiados:
        _programar_recarga(branch, set(body.external_ids) - set(no_encontrados))
    return {"cambiados": cambiados, "sin_cambio": sin_cambio, "no_encontrados": no_encontrados}


@router.post("/sku/{external_id}/receta")
async def bo_receta_marcar(external_id: str, body: MarcaRecetaIn, _=Depends(_auth)):
    """
    Marca un producto como venta libre / con receta (o saca la marca manual).
    Desde una conversación (`phone`) y con `devolver_al_bot` + venta libre, el
    bot retoma la venta: le vuelve a ofrecer el producto con precio y sigue
    con retiro o envío. Si eso no se puede, la marca queda igual y se avisa.
    """
    db, branch = _db(), await _branch()
    marca = _marca(body.requiere_receta)
    origen = "conversacion" if body.phone else "abm"
    try:
        r = await receta_marcas.marcar(db, branch, external_id, marca, body.autor, origen,
                                       phone=body.phone)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if r["cambio"]:
        _programar_recarga(branch, {external_id})
    r.update({"devuelto_al_bot": False, "mensaje": None, "aviso": None})
    if body.devolver_al_bot and body.phone:
        if r["marca"] != "no":
            r["aviso"] = "Solo se devuelve al bot un producto de venta libre"
        else:
            try:
                r["mensaje"] = await receta_marcas.retomar_venta(body.phone, external_id)
                r["devuelto_al_bot"] = True
            except receta_marcas.ConflictoMarca as e:
                r["aviso"] = f"La marca se guardó, pero no se devolvió al bot: {e}"
            except Exception as e:
                logger.error(f"Retomar la venta de {body.phone} falló: {e}")
                r["aviso"] = ("La marca se guardó, pero no se pudo devolver al bot. "
                              "Seguí la conversación a mano.")
    return r


@router.get("/sku/{external_id}/receta/cambios")
async def bo_receta_cambios_producto(external_id: str, _=Depends(_auth)):
    db, branch = _db(), await _branch()
    return {"cambios": await receta_marcas.cambios(db, branch, external_id)}


@router.get("/receta/cambios")
async def bo_receta_cambios(_=Depends(_auth), limit: int = Query(100, ge=1, le=1000)):
    """Registro de marcas manuales (quién marcó qué, cuándo y desde dónde)."""
    return {"cambios": await receta_marcas.cambios(_db(), limit=limit)}


@router.get("/session/{phone}/productos")
async def bo_session_productos(phone: str, _=Depends(_auth)):
    """
    Productos de la conversación con su marca de receta: los que frenaron la
    venta (el bot derivó por receta) y los del pedido en curso.
    """
    session = await get_session_service(get_settings().redis_url).get(phone)
    db = get_db(get_settings().database_url)
    branch = await resolver_branch_default() if db.available() else None
    productos = await receta_marcas.productos_de_conversacion(db, branch, session)
    return {"phone": phone, "estado": session.get("estado"),
            "derivada_motivo": session.get("derivada_motivo"), "productos": productos}
