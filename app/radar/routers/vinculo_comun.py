"""Modelos y respuestas que comparten la Consola KIS y la pantalla del dueño."""

import uuid
from datetime import datetime
from typing import Optional

from fastapi import HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app.radar.contexto import RadarContexto
from app.radar.vinculos import VinculoRechazado

# El QR y el código son credenciales efímeras: nunca en caché.
SIN_CACHE = {"Cache-Control": "no-store"}


class VincularIn(BaseModel):
    full_sync: bool = False


class CodigoIn(BaseModel):
    telefono: str = Field(min_length=6, max_length=32)


class DesconectarIn(BaseModel):
    confirmar: bool = False


class BorrarIn(BaseModel):
    confirmar: bool = False
    nombre_linea: str = Field(default="", max_length=80)


class RestriccionIn(BaseModel):
    hasta: Optional[datetime] = None


def http_de(e: VinculoRechazado) -> HTTPException:
    return HTTPException(status_code=e.status, detail={"error": e.codigo})


def respuesta_png(contenido: bytes) -> Response:
    return Response(content=contenido, media_type="image/png", headers=SIN_CACHE)


def respuesta_codigo(codigo: str) -> JSONResponse:
    return JSONResponse({"codigo": codigo}, headers=SIN_CACHE)


def exigir_confirmacion(body: Optional[DesconectarIn]) -> None:
    if body is None or not body.confirmar:
        raise HTTPException(status_code=422, detail={"error": "falta_confirmacion"})


async def exigir_nombre(ctx: RadarContexto, tenant_id: uuid.UUID, line_id: uuid.UUID, body: Optional[BorrarIn]) -> None:
    """Segunda confirmación de "Desconectar y borrar todo": el nombre exacto de la línea."""
    if body is None or not body.confirmar:
        raise HTTPException(status_code=422, detail={"error": "falta_confirmacion"})
    async with ctx.db.tenant_tx(tenant_id) as con:
        nombre = await con.fetchval("SELECT nombre FROM lines WHERE id = $1", line_id)
    if nombre is None:
        raise HTTPException(status_code=404, detail={"error": "linea_inexistente"})
    if body.nombre_linea.strip() != nombre:
        raise HTTPException(status_code=422, detail={"error": "confirmacion_incorrecta"})
