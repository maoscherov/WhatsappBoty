"""
/radar/admin/*: operaciones de los admins de KIS sobre los clientes. El
tenant destino va en la ruta; la sesión del admin vive en el tenant KIS.
Lo cruzado entre tenants pasa por las funciones SECURITY DEFINER y queda
auditado.
"""

import uuid
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.radar import auditoria
from app.radar.admin_kis import crear_tenant_con_dueno
from app.radar.auth import Sesion, ip_de, requiere_rol
from app.radar.constantes import TENANT_KIS
from app.radar.contexto import contexto
from app.radar.fuente import ALMACENES_DISPONIBLES
from app.radar.lineas import AlmacenNoDisponible, crear_linea, resolver_valores_linea
from app.radar.links import EmailInvalido, enviar_link, normalizar_email
from app.radar.parametros import ValorInvalido, perfil_por_rubro, propuesta_para_perfil

router = APIRouter(prefix="/radar/admin", tags=["radar-admin"])
_RUBRO = r"^[a-z_]{1,40}$"


class ParametrosTenantIn(BaseModel):
    retencion_fichas_meses: Optional[int] = None
    retener_fragmentos: Optional[bool] = None
    ia_habilitada: Optional[bool] = None
    via_llm: Optional[str] = None


class ParametrosLineaIn(BaseModel):
    duracion_vinculo_dias: Optional[int] = None
    retencion_fuente_dias: Optional[int] = None
    retencion_tras_desvinculo_dias: Optional[int] = None
    tope_ia_mensual_usd: Optional[Decimal] = None


class LineaIn(BaseModel):
    nombre: str = Field(min_length=1, max_length=80)
    parametros: ParametrosLineaIn = ParametrosLineaIn()


class DuenoIn(BaseModel):
    email: str = Field(max_length=254)
    nombre: str = Field(default="", max_length=120)


class TenantIn(BaseModel):
    nombre: str = Field(min_length=1, max_length=120)
    rubro: str = Field(pattern=_RUBRO)
    perfil_de_datos: Optional[str] = None
    parametros: ParametrosTenantIn = ParametrosTenantIn()
    dueno: DuenoIn
    linea: LineaIn


class InvitacionIn(BaseModel):
    email: str = Field(max_length=254)


def _422(e: Exception) -> HTTPException:
    return HTTPException(status_code=422, detail=str(e))


@router.get("/propuesta")
async def propuesta(rubro: str = Query(pattern=_RUBRO), admin: Sesion = Depends(requiere_rol("admin"))):
    perfil = perfil_por_rubro(rubro)
    p = propuesta_para_perfil(perfil)
    return {
        "perfil_de_datos": perfil,
        "parametros_tenant": {k: v for k, v in p.items() if k != "retencion_fuente_dias"},
        "parametros_linea": {k: v for k, v in p.items() if k == "retencion_fuente_dias"},
        "almacenes_disponibles": sorted(ALMACENES_DISPONIBLES),
    }


@router.post("/tenants", status_code=201)
async def crear_tenant(body: TenantIn, request: Request, admin: Sesion = Depends(requiere_rol("admin"))):
    ctx = contexto(request)
    try:
        alta = await crear_tenant_con_dueno(
            ctx, actor=admin, ip=ip_de(request), nombre=body.nombre, rubro=body.rubro,
            perfil=body.perfil_de_datos or perfil_por_rubro(body.rubro),
            parametros_tenant=body.parametros.model_dump(), dueno_email=body.dueno.email,
            dueno_nombre=body.dueno.nombre, linea_nombre=body.linea.nombre,
            parametros_linea=body.linea.parametros.model_dump())
    except (ValorInvalido, AlmacenNoDisponible, EmailInvalido) as e:
        raise _422(e)
    return {"tenant_id": str(alta.tenant_id), "line_id": str(alta.line_id), "user_id": str(alta.user_id),
            "invitacion_enviada": alta.invitacion_enviada}


@router.get("/tenants")
async def listar_tenants(request: Request, admin: Sesion = Depends(requiere_rol("admin"))):
    ctx = contexto(request)
    async with ctx.db.sin_tenant() as con:
        filas = await con.fetch("SELECT * FROM radar_admin_listar_tenants()")
    async with ctx.db.tenant_tx(TENANT_KIS) as con:
        await auditoria.registrar(con, tenant_id=TENANT_KIS, actor_user_id=admin.user_id, actor_rol=admin.rol,
                                  accion="tenants_listados", tipo_objeto="tenant", ip=ip_de(request),
                                  detalle={"cantidad": len(filas)})
    return [{"id": str(f["id"]), "nombre": f["nombre"], "rubro": f["rubro"], "perfil_de_datos": f["perfil_de_datos"],
             "created_at": f["created_at"].isoformat()} for f in filas]


@router.post("/tenants/{tenant_id}/lineas", status_code=201)
async def crear_linea_admin(tenant_id: uuid.UUID, body: LineaIn, request: Request,
                            admin: Sesion = Depends(requiere_rol("admin"))):
    ctx = contexto(request)
    try:
        valores = resolver_valores_linea(body.parametros.model_dump())
    except ValorInvalido as e:
        raise _422(e)
    async with ctx.db.tenant_tx(tenant_id) as con:
        if await con.fetchval("SELECT count(*) FROM tenants WHERE id = $1 AND NOT es_kis", tenant_id) == 0:
            raise HTTPException(status_code=404, detail="tenant inexistente")
        try:
            line_id = await crear_linea(con, tenant_id=tenant_id, nombre=body.nombre, valores=valores)
        except AlmacenNoDisponible as e:
            raise _422(e)
        await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=admin.user_id, actor_rol=admin.rol,
                                  accion="linea_creada", tipo_objeto="line", objeto_id=line_id, ip=ip_de(request))
    return {"line_id": str(line_id)}


@router.post("/tenants/{tenant_id}/invitaciones", status_code=202)
async def reenviar_invitacion(tenant_id: uuid.UUID, body: InvitacionIn, request: Request,
                              admin: Sesion = Depends(requiere_rol("admin"))):
    ctx = contexto(request)
    try:
        email = normalizar_email(body.email)
    except EmailInvalido as e:
        raise _422(e)
    async with ctx.db.tenant_tx(tenant_id) as con:
        user_id = await con.fetchval("SELECT id FROM users WHERE email = $1", email)
        if user_id is None:
            raise HTTPException(status_code=404, detail="usuario inexistente en este tenant")
        await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=admin.user_id, actor_rol=admin.rol,
                                  accion="invitacion_reenviada", tipo_objeto="user", objeto_id=user_id,
                                  ip=ip_de(request))
    enviada = await enviar_link(ctx, tenant_id=tenant_id, user_id=user_id, email=email,
                                proposito="invitacion", ip=ip_de(request))
    return {"enviada": enviada}
