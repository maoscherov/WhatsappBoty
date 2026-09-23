"""API autenticada de la cuenta: quién soy, líneas, usuarios y roles (§4.4)."""

import uuid
from typing import Literal, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.radar import auditoria
from app.radar.auth import Sesion, ip_de, requiere_rol, revocar_sesiones_de, sesion_actual
from app.radar.contexto import contexto
from app.radar.lineas import linea_json, listar_lineas
from app.radar.links import EmailInvalido, enviar_link_seguro, normalizar_email

router = APIRouter(prefix="/radar/api", tags=["radar-cuenta"])


def _sesion_json(sesion: Sesion) -> dict:
    return {
        "user_id": str(sesion.user_id),
        "tenant_id": str(sesion.tenant_id),
        "rol": sesion.rol,
        "email": sesion.email,
        "es_kis": sesion.es_kis,
        "lineas_permitidas": [str(x) for x in sesion.lineas_permitidas]
        if sesion.lineas_permitidas is not None else None,
    }


def _usuario_json(fila) -> dict:
    return {"user_id": str(fila["id"]), "email": fila["email"], "nombre": fila["nombre"], "rol": fila["rol"],
            "lineas_permitidas": [str(x) for x in fila["lineas_permitidas"]]
            if fila["lineas_permitidas"] is not None else None}


class InvitacionUsuarioIn(BaseModel):
    email: str = Field(max_length=254)
    nombre: str = Field(default="", max_length=120)
    rol: Literal["gestor", "lector"]
    lineas_permitidas: Optional[list[uuid.UUID]] = None


class RolIn(BaseModel):
    rol: Literal["dueno", "gestor", "lector"]
    lineas_permitidas: Optional[list[uuid.UUID]] = None


@router.get("/yo")
async def yo(sesion: Sesion = Depends(sesion_actual)):
    return _sesion_json(sesion)


@router.get("/lineas")
async def lineas(request: Request, sesion: Sesion = Depends(sesion_actual)):
    ctx = contexto(request)
    async with ctx.db.tenant_tx(sesion.tenant_id) as con:
        filas = await listar_lineas(con, sesion.lineas_permitidas)
    return [linea_json(f) for f in filas]


@router.get("/usuarios")
async def usuarios(request: Request, sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    async with ctx.db.tenant_tx(sesion.tenant_id) as con:
        filas = await con.fetch(
            "SELECT u.id, u.email, u.nombre, m.rol, m.lineas_permitidas FROM users u "
            "JOIN memberships m ON m.user_id = u.id ORDER BY u.created_at")
    return [_usuario_json(f) for f in filas]


@router.post("/usuarios", status_code=201)
async def invitar_usuario(body: InvitacionUsuarioIn, request: Request,
                          sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    try:
        email = normalizar_email(body.email)
    except EmailInvalido as e:
        raise HTTPException(status_code=422, detail=str(e))
    ip = ip_de(request)
    async with ctx.db.tenant_tx(sesion.tenant_id) as con:
        try:
            user_id = await con.fetchval("INSERT INTO users (email, nombre) VALUES ($1, $2) RETURNING id",
                                         email, body.nombre)
        except asyncpg.UniqueViolationError:
            raise HTTPException(status_code=409, detail="ese email ya tiene usuario en esta cuenta")
        await con.execute("INSERT INTO memberships (user_id, rol, lineas_permitidas) VALUES ($1, $2, $3)",
                          user_id, body.rol, body.lineas_permitidas)
        await auditoria.registrar(con, tenant_id=sesion.tenant_id, actor_user_id=sesion.user_id, actor_rol=sesion.rol,
                                  accion="usuario_invitado", tipo_objeto="user", objeto_id=user_id, ip=ip,
                                  detalle={"rol_nuevo": body.rol})
    enviada = await enviar_link_seguro(ctx, tenant_id=sesion.tenant_id, user_id=user_id, email=email,
                                       proposito="invitacion", ip=ip)
    return {"user_id": str(user_id), "invitacion_enviada": enviada}


@router.put("/usuarios/{user_id}/rol")
async def cambiar_rol(user_id: uuid.UUID, body: RolIn, request: Request,
                      sesion: Sesion = Depends(requiere_rol("dueno"))):
    if user_id == sesion.user_id and body.rol != "dueno":
        raise HTTPException(status_code=400, detail="no podés quitarte tu propio rol de dueño")
    ctx = contexto(request)
    async with ctx.db.tenant_tx(sesion.tenant_id) as con:
        anterior = await con.fetchval("SELECT rol FROM memberships WHERE user_id = $1", user_id)
        if anterior is None:
            raise HTTPException(status_code=404, detail="usuario inexistente")
        await con.execute("UPDATE memberships SET rol = $2, lineas_permitidas = $3 WHERE user_id = $1",
                          user_id, body.rol, body.lineas_permitidas)
        await revocar_sesiones_de(con, user_id)
        await auditoria.registrar(con, tenant_id=sesion.tenant_id, actor_user_id=sesion.user_id, actor_rol=sesion.rol,
                                  accion="rol_cambiado", tipo_objeto="membership", objeto_id=user_id, ip=ip_de(request),
                                  detalle={"rol_anterior": anterior, "rol_nuevo": body.rol})
    return {"user_id": str(user_id), "rol": body.rol,
            "lineas_permitidas": [str(x) for x in body.lineas_permitidas] if body.lineas_permitidas is not None else None}
