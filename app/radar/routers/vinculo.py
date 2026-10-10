"""
P3 del dueño (§3 P3) sobre el mismo backend que la Consola KIS: mismos
servicios de app.radar.vinculos y misma máquina de estados. El tenant sale de
la sesión, nunca de la URL. Ver el estado puede cualquier rol que vea la línea;
operar, solo el dueño.
"""

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from app.radar.auth import Sesion, ip_de, requiere_rol, sesion_actual
from app.radar.consentimiento_asistido import texto_para_linea
from app.radar.contexto import RadarContexto, contexto
from app.radar.lineas import leer_linea
from app.radar.routers.vinculo_comun import (BorrarIn, CodigoIn, DesconectarIn, VincularIn, exigir_confirmacion,
                                             exigir_nombre, http_de, respuesta_codigo, respuesta_png)
from app.radar.vinculos import (VinculoRechazado, estado_de_linea, iniciar_vinculo, pedir_codigo, pedir_fin, qr_png,
                                reiniciar_qr)

router = APIRouter(prefix="/radar/api/lineas/{line_id}/vinculo", tags=["radar-vinculo"])


async def _linea_visible(ctx: RadarContexto, sesion: Sesion, line_id: uuid.UUID) -> None:
    if not sesion.puede_ver_linea(line_id):
        raise HTTPException(status_code=404, detail={"error": "linea_inexistente"})
    async with ctx.db.tenant_tx(sesion.tenant_id) as con:
        if await leer_linea(con, line_id) is None:
            raise HTTPException(status_code=404, detail={"error": "linea_inexistente"})


def _actor(sesion: Sesion, request: Request) -> dict:
    return {"actor_user_id": sesion.user_id, "actor_rol": sesion.rol, "ip": ip_de(request)}


@router.get("")
async def estado(line_id: uuid.UUID, request: Request, sesion: Sesion = Depends(sesion_actual)):
    ctx = contexto(request)
    await _linea_visible(ctx, sesion, line_id)
    try:
        return await estado_de_linea(ctx, tenant_id=sesion.tenant_id, line_id=line_id)
    except VinculoRechazado as e:
        raise http_de(e)


@router.get("/texto-consentimiento")
async def texto(line_id: uuid.UUID, request: Request, sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    await _linea_visible(ctx, sesion, line_id)
    async with ctx.db.tenant_tx(sesion.tenant_id) as con:
        return await texto_para_linea(con, sesion.tenant_id, line_id)


@router.post("", status_code=201)
async def vincular(line_id: uuid.UUID, request: Request, body: Optional[VincularIn] = None,
                   sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    await _linea_visible(ctx, sesion, line_id)
    try:
        return await iniciar_vinculo(ctx, tenant_id=sesion.tenant_id, line_id=line_id,
                                     full_sync=body.full_sync if body else False, **_actor(sesion, request))
    except VinculoRechazado as e:
        raise http_de(e)


@router.get("/qr")
async def qr(line_id: uuid.UUID, request: Request, sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    await _linea_visible(ctx, sesion, line_id)
    try:
        return respuesta_png(await qr_png(ctx, tenant_id=sesion.tenant_id, line_id=line_id))
    except VinculoRechazado as e:
        raise http_de(e)


@router.post("/codigo")
async def codigo(line_id: uuid.UUID, body: CodigoIn, request: Request, sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    await _linea_visible(ctx, sesion, line_id)
    try:
        return respuesta_codigo(await pedir_codigo(ctx, tenant_id=sesion.tenant_id, line_id=line_id,
                                                   telefono=body.telefono, **_actor(sesion, request)))
    except VinculoRechazado as e:
        raise http_de(e)


@router.post("/reiniciar-qr")
async def reiniciar(line_id: uuid.UUID, request: Request, sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    await _linea_visible(ctx, sesion, line_id)
    try:
        return await reiniciar_qr(ctx, tenant_id=sesion.tenant_id, line_id=line_id, **_actor(sesion, request))
    except VinculoRechazado as e:
        raise http_de(e)


@router.post("/desconectar", status_code=202)
async def desconectar(line_id: uuid.UUID, request: Request, body: Optional[DesconectarIn] = None,
                      sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    await _linea_visible(ctx, sesion, line_id)
    exigir_confirmacion(body)
    try:
        return await pedir_fin(ctx, tenant_id=sesion.tenant_id, line_id=line_id, causa="pedido_dueno", borrar=False,
                               **_actor(sesion, request))
    except VinculoRechazado as e:
        raise http_de(e)


@router.post("/desconectar-y-borrar", status_code=202)
async def desconectar_y_borrar(line_id: uuid.UUID, request: Request, body: Optional[BorrarIn] = None,
                               sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    await _linea_visible(ctx, sesion, line_id)
    await exigir_nombre(ctx, sesion.tenant_id, line_id, body)
    try:
        return await pedir_fin(ctx, tenant_id=sesion.tenant_id, line_id=line_id, causa="pedido_dueno", borrar=True,
                               **_actor(sesion, request))
    except VinculoRechazado as e:
        raise http_de(e)
