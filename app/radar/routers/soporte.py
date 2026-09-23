"""
support_grants (§4.4): el dueño otorga acceso a Soporte KIS por 24–72 h; es
visible para el cliente, revocable y auditado. Un grant es por tenant, no por
persona de KIS: el dueño no conoce a los usuarios de KIS.
"""

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.radar import auditoria
from app.radar.auth import Sesion, ip_de, requiere_rol, sesion_actual
from app.radar.contexto import contexto

router = APIRouter(prefix="/radar/api/soporte", tags=["radar-soporte"])


class GrantIn(BaseModel):
    horas: int = Field(ge=24, le=72)


def _grant_json(f) -> dict:
    return {"id": str(f["id"]), "created_at": f["created_at"].isoformat(), "expires_at": f["expires_at"].isoformat(),
            "otorgado_por": str(f["otorgado_por"])}


@router.post("", status_code=201)
async def otorgar(body: GrantIn, request: Request, sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    async with ctx.db.tenant_tx(sesion.tenant_id) as con:
        fila = await con.fetchrow(
            "INSERT INTO support_grants (otorgado_por, expires_at) VALUES ($1, now() + $2::interval) "
            "RETURNING id, created_at, expires_at, otorgado_por",
            sesion.user_id, timedelta(hours=body.horas))
        await auditoria.registrar(con, tenant_id=sesion.tenant_id, actor_user_id=sesion.user_id, actor_rol=sesion.rol,
                                  accion="soporte_otorgado", tipo_objeto="support_grant", objeto_id=fila["id"],
                                  ip=ip_de(request), detalle={"horas": body.horas})
    return _grant_json(fila)


@router.get("")
async def vigentes(request: Request, sesion: Sesion = Depends(sesion_actual)):
    ctx = contexto(request)
    async with ctx.db.tenant_tx(sesion.tenant_id) as con:
        filas = await con.fetch(
            "SELECT id, created_at, expires_at, otorgado_por FROM support_grants "
            "WHERE revocado_at IS NULL AND expires_at > now() ORDER BY created_at")
    return [_grant_json(f) for f in filas]


@router.delete("/{grant_id}")
async def revocar(grant_id: uuid.UUID, request: Request, sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    async with ctx.db.tenant_tx(sesion.tenant_id) as con:
        revocado = await con.fetchval(
            "UPDATE support_grants SET revocado_at = now() WHERE id = $1 AND revocado_at IS NULL RETURNING id", grant_id)
        if revocado is None:
            raise HTTPException(status_code=404, detail="grant inexistente o ya revocado")
        await con.execute("UPDATE sessions SET revoked_at = now() WHERE rol = 'soporte' AND revoked_at IS NULL")
        await auditoria.registrar(con, tenant_id=sesion.tenant_id, actor_user_id=sesion.user_id, actor_rol=sesion.rol,
                                  accion="soporte_revocado", tipo_objeto="support_grant", objeto_id=grant_id,
                                  ip=ip_de(request))
    return {"ok": True}
