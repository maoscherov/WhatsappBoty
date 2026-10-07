"""
Operadores del backoffice (6/10): la lista para elegir "¿quién sos?" al
tomar conversaciones, marcar errores, escribir y armar pedidos, en vez de
escribir el nombre a mano ("Lore", "lorena", "02").
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.config import get_settings
from app.routers.backoffice import _auth
from app.services import operadores_service as ops
from app.services.db import get_db

router = APIRouter(prefix="/bo")


class OperadorIn(BaseModel):
    nombre: str
    aliases: list[str] = []
    nota: Optional[str] = None


class OperadorCambio(BaseModel):
    nombre: Optional[str] = None
    activo: Optional[bool] = None
    aliases: Optional[list[str]] = None
    nota: Optional[str] = None


class FusionIn(BaseModel):
    con_id: int          # el operador que absorbe a este (destino)


def _db():
    db = get_db(get_settings().database_url)
    if not db.available():
        raise HTTPException(status_code=503, detail="Base de datos no disponible")
    return db


@router.get("/operadores")
async def bo_operadores(_=Depends(_auth), todos: bool = Query(False)):
    """Operadores activos (con `todos=true`, también los inactivos)."""
    return {"items": await ops.listar(_db(), todos)}


@router.get("/operadores/actividad")
async def bo_operadores_actividad(_=Depends(_auth), dias: int = Query(7, ge=1, le=90)):
    """Última acción de cada operador y cuántas hizo hoy: quién está activo."""
    return {"items": await ops.actividad(_db(), dias)}


@router.post("/operadores")
async def bo_operador_crear(body: OperadorIn, _=Depends(_auth)):
    try:
        return await ops.crear(_db(), body.nombre, body.aliases, body.nota)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.patch("/operadores/{id_}")
async def bo_operador_cambiar(id_: int, body: OperadorCambio, _=Depends(_auth)):
    try:
        f = await ops.actualizar(_db(), id_, body.nombre, body.activo, body.aliases, body.nota)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if f is None:
        raise HTTPException(status_code=404, detail="No existe ese operador")
    return f


@router.post("/operadores/{id_}/fusionar")
async def bo_operador_fusionar(id_: int, body: FusionIn, _=Depends(_auth)):
    """Este operador es el mismo que `con_id` ("lorena" → "Lore"): se borra y
    su nombre queda como alias del otro."""
    try:
        f = await ops.fusionar(_db(), id_, body.con_id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if f is None:
        raise HTTPException(status_code=404, detail="No existe alguno de los dos operadores")
    return f
