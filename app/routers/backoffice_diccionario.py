"""
ABM del diccionario del catálogo (2/10): abreviaturas de góndola y sinónimos
de cliente, editables por la farmacia sin deploy. Cada cambio recarga el
diccionario en memoria y reconstruye el índice de búsqueda en segundo plano.
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.config import get_settings
from app.routers.backoffice import _auth
from app.services import diccionario_service as dic
from app.services.db import get_db
from app.services.sku_service import get_sku_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bo")

_recarga: Optional[asyncio.Task] = None


class EntradaIn(BaseModel):
    tipo: str                       # abreviatura | sinonimo
    termino: str                    # sigla del catálogo / palabra del cliente
    equivale: str                   # palabra del cliente / términos del catálogo (coma)
    estado: str = "activa"
    nota: Optional[str] = None
    autor: Optional[str] = None


class CambioIn(BaseModel):
    estado: Optional[str] = None
    equivale: Optional[str] = None
    nota: Optional[str] = None
    autor: Optional[str] = None


def _db():
    db = get_db(get_settings().database_url)
    if not db.available():
        raise HTTPException(status_code=503, detail="Base de datos no disponible")
    return db


async def _reconstruir(db):
    """Aplica el diccionario y reconstruye el índice (fuera del request)."""
    try:
        await dic.cargar(db)
        from app.services.catalog_source import aplicar_fuente
        await aplicar_fuente()
    except Exception as e:
        logger.error(f"Diccionario: no se pudo reconstruir el índice: {e}")


def _programar(db):
    global _recarga
    if _recarga is not None and not _recarga.done():
        _recarga.cancel()
    _recarga = asyncio.create_task(_reconstruir(db))


@router.get("/diccionario")
async def bo_diccionario_listar(_=Depends(_auth), tipo: Optional[str] = Query(None),
                                estado: Optional[str] = Query(None), q: str = Query("")):
    filas = await dic.listar(_db(), tipo, estado, q)
    return {"total": len(filas), "items": filas,
            "propuestas": sum(1 for f in filas if f["estado"] == "propuesta")}


@router.post("/diccionario")
async def bo_diccionario_guardar(body: EntradaIn, _=Depends(_auth)):
    db = _db()
    try:
        fila = await dic.guardar(db, body.tipo, body.termino, body.equivale, body.estado,
                                 body.nota, body.autor)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    _programar(db)
    return fila


@router.patch("/diccionario/{id_}")
async def bo_diccionario_cambiar(id_: int, body: CambioIn, _=Depends(_auth)):
    db = _db()
    try:
        fila = await dic.actualizar(db, id_, body.estado, body.equivale, body.nota, body.autor)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if fila is None:
        raise HTTPException(status_code=404, detail="No existe esa entrada")
    _programar(db)
    return fila


@router.delete("/diccionario/{id_}")
async def bo_diccionario_borrar(id_: int, _=Depends(_auth)):
    db = _db()
    if not await dic.eliminar(db, id_):
        raise HTTPException(status_code=404, detail="No existe esa entrada")
    _programar(db)
    return {"ok": True}


@router.get("/diccionario/sugerencias")
async def bo_diccionario_sugerencias(_=Depends(_auth), minimo: int = Query(5, ge=2)):
    """Siglas frecuentes en los productos con stock que el diccionario no conoce."""
    return {"items": dic.sugerencias(get_sku_service(get_settings().sku_csv_path), minimo)}


@router.get("/diccionario/probar")
async def bo_diccionario_probar(_=Depends(_auth), q: str = Query(..., min_length=2)):
    """Qué encuentra hoy el bot para lo que escribiría un cliente."""
    res = get_sku_service(get_settings().sku_csv_path).buscar(q, top_n=8)
    return {"q": q, "items": [{"sku_id": r["sku_id"], "nombre": r["nombre"],
                               "precio": r["precio"], "estado": r.get("estado"),
                               "requiere_receta": r.get("requiere_receta")} for r in res]}
