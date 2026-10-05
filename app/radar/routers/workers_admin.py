"""
/radar/admin/workers: servidores WAHA desde la Consola KIS (listar, registrar, reemplazar la clave, cargar el disco).

La clave admin de WAHA es un secreto: llega por acá una sola vez, se prueba contra el propio WAHA y va directo al
SecretStore (app.radar.workers). No vuelve en una respuesta, en un detail ni en un log.
"""

import re
import uuid
from typing import Literal

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, SecretStr, field_validator

from app.radar.auth import Sesion, requiere_rol
from app.radar.contexto import contexto
from app.radar.workers import (VerificacionFallida, actualizar_disco, clave_cargada, listar_workers, registrar_worker,
                               reemplazar_clave, verificar_clave_worker)


class RutaSinEco(APIRoute):
    """Un 422 de validación que no repite lo que se mandó. El de FastAPI trae `input`: el valor inválido (la clave, si
    es corta) o el cuerpo entero (si falta un campo), y con un NaN ni siquiera se puede serializar. Acá solo salen el
    campo y el tipo de error."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def sin_eco(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError as e:
                return JSONResponse({"detail": [{"loc": list(err["loc"]), "type": err["type"]} for err in e.errors()]},
                                    status_code=422)
        return sin_eco


router = APIRouter(prefix="/radar/admin/workers", tags=["radar-workers"], route_class=RutaSinEco)
# Lo mismo que los CHECK de waha_workers (nombre y http/https sin espacios), pero la URL solo con ASCII visible.
_NOMBRE = r"^[a-z0-9_-]{1,40}$"
_BASE_URL = re.compile(r"https?://[\x21-\x7e]{1,200}")
MAX_GB = 100_000          # NUMERIC(8, 2) llega a 999999.99; esto ya es un servidor de 100 TB


class WorkerIn(BaseModel):
    nombre: str = Field(pattern=_NOMBRE)
    base_url: str
    engine: Literal["NOWEB", "GOWS"]
    max_sesiones: int = Field(ge=1, le=500)
    disco_max_gb: float = Field(ge=0.01, le=MAX_GB, allow_inf_nan=False)      # 0.01: el mínimo que guarda la columna
    admin_key: SecretStr = Field(min_length=16, max_length=256, repr=False)

    @field_validator("base_url")
    @classmethod
    def _base_url_valida(cls, v: str) -> str:
        if not _BASE_URL.fullmatch(v):
            raise ValueError("base_url: tiene que empezar con http:// o https://, sin espacios")
        try:
            url = httpx.URL(v)
        except httpx.InvalidURL:
            raise ValueError("base_url: no es una URL válida") from None
        # La lista de servidores devuelve base_url: una credencial ahí la vería cualquier admin. La clave viaja solo
        # en una cabecera de la prueba contra WAHA, nunca en la URL.
        if not url.host or url.userinfo or url.query or url.fragment:
            raise ValueError("base_url: sin usuario, contraseña, query ni fragmento")
        return v


class ClaveIn(BaseModel):
    admin_key: SecretStr = Field(min_length=16, max_length=256, repr=False)


class DiscoIn(BaseModel):
    usado_gb: float = Field(ge=0, le=MAX_GB, allow_inf_nan=False)


def _rechazo(e: VerificacionFallida) -> HTTPException:
    return HTTPException(status_code=422, detail={"error": e.codigo})


def _worker_inexistente() -> HTTPException:
    return HTTPException(status_code=404, detail={"error": "worker_inexistente"})


@router.get("")
async def listar(request: Request, admin: Sesion = Depends(requiere_rol("admin"))):
    """No audita: es una lectura que la Consola repite, y abrir la Consola ya queda auditado."""
    ctx = contexto(request)
    return [{"id": str(w.id), "nombre": w.nombre, "base_url": w.base_url, "engine": w.engine,
             "max_sesiones": w.max_sesiones, "sesiones": w.sesiones, "disco_max_gb": w.disco_max_gb,
             "disco_usado_gb": w.disco_usado_gb, "activo": w.activo, "clave_cargada": clave_cargada(ctx, w.id)}
            for w in await listar_workers(ctx)]


@router.post("", status_code=201)
async def registrar(body: WorkerIn, request: Request, admin: Sesion = Depends(requiere_rol("admin"))):
    """La clave se prueba contra WAHA ANTES de insertar: si no la acepta no queda fila, ni clave, ni auditoría."""
    ctx = contexto(request)
    clave = body.admin_key.get_secret_value()
    try:
        info = await verificar_clave_worker(ctx, base_url=body.base_url, engine=body.engine, admin_key=clave)
        wid = await registrar_worker(ctx, nombre=body.nombre, base_url=body.base_url, engine=body.engine,
                                     max_sesiones=body.max_sesiones, disco_max_gb=body.disco_max_gb, admin_key=clave,
                                     actor_user_id=admin.user_id)
    except VerificacionFallida as e:
        raise _rechazo(e) from None
    except asyncpg.UniqueViolationError:         # nombre repetido: falla el INSERT, antes de guardar la clave
        raise HTTPException(status_code=409, detail={"error": "worker_duplicado"}) from None
    return {"id": str(wid), "nombre": body.nombre, "version": info["version"], "engine": info["engine"]}


@router.put("/{worker_id}/clave")
async def reemplazar(worker_id: uuid.UUID, body: ClaveIn, request: Request,
                     admin: Sesion = Depends(requiere_rol("admin"))):
    try:
        info = await reemplazar_clave(contexto(request), worker_id, body.admin_key.get_secret_value(), admin.user_id)
    except LookupError:
        raise _worker_inexistente() from None
    except VerificacionFallida as e:
        raise _rechazo(e) from None
    return {"id": str(worker_id), "version": info["version"], "engine": info["engine"]}


@router.put("/{worker_id}/disco")
async def disco(worker_id: uuid.UUID, body: DiscoIn, request: Request, admin: Sesion = Depends(requiere_rol("admin"))):
    try:
        await actualizar_disco(contexto(request), worker_id, body.usado_gb, actor_user_id=admin.user_id)
    except LookupError:
        raise _worker_inexistente() from None
    return {"id": str(worker_id)}
