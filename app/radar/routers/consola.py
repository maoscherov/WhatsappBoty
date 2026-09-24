"""
API de la Consola KIS (§3.1). Solo el rol `admin` (tenant KIS). Todo lo que
toca WAHA lo hace el backend; ninguna clave de WAHA llega al navegador.
Cada acción queda auditada en el tenant del cliente, con el admin como actor.

GET  /radar/admin/consola/lineas                                   C1
GET  /radar/admin/tenants/{t}/lineas/{l}/consentimiento-asistido/texto
POST /radar/admin/tenants/{t}/lineas/{l}/consentimiento-asistido   C2 paso 2
POST /radar/admin/tenants/{t}/lineas/{l}/vinculo                   C2 paso 3-4 (y "Reconectar")
GET  /radar/admin/tenants/{t}/lineas/{l}/vinculo                   estado en vivo (polling)
GET  …/vinculo/qr · POST …/vinculo/codigo · POST …/vinculo/reiniciar-qr
POST …/vinculo/desconectar · POST …/vinculo/desconectar-y-borrar   C4
PUT|DELETE …/vinculo/restriccion                                   C4 (restricción de cuenta)
"""

import logging
import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.radar import auditoria, eventos_producto
from app.radar.auth import Sesion, ip_de, requiere_rol
from app.radar.consentimiento import VERSIONES
from app.radar.consentimiento_asistido import (email_copia_consentimiento, registrar_consentimiento_asistido,
                                               texto_para_linea)
from app.radar.consola import listar_lineas_consola
from app.radar.contexto import contexto
from app.radar.parametros_service import (a_json, leer_parametros_tenant, leer_propuesta_tenant,
                                          tenant_ya_consentido)
from app.radar.routers.vinculo_comun import (BorrarIn, CodigoIn, DesconectarIn, RestriccionIn, VincularIn,
                                             exigir_confirmacion, exigir_nombre, http_de, respuesta_codigo,
                                             respuesta_png)
from app.radar.vinculos import (VinculoRechazado, estado_de_linea, iniciar_vinculo, levantar_restriccion,
                                marcar_restriccion, pedir_codigo, pedir_fin, qr_png, reiniciar_qr)

logger = logging.getLogger("app.radar.consola")

router = APIRouter(prefix="/radar/admin", tags=["radar-consola"])
_LINEA = "/tenants/{tenant_id}/lineas/{line_id}"
_ESTADO_LINEA = r"^(vinculada|sin_vinculo|de_baja)$"


async def _tenant_cliente(tenant_id: uuid.UUID, request: Request,
                          admin: Sesion = Depends(requiere_rol("admin"))) -> Sesion:
    """Como admin.py del tramo 1: las rutas de línea solo operan sobre un tenant
    cliente que existe (nunca sobre el tenant KIS). Depende de requiere_rol, así
    que primero responde 401/403 y recién después mira el tenant."""
    async with contexto(request).db.tenant_tx(tenant_id) as con:
        existe = await con.fetchval("SELECT count(*) FROM tenants WHERE id = $1 AND NOT es_kis", tenant_id)
    if existe == 0:
        raise HTTPException(status_code=404, detail={"error": "tenant_inexistente"})
    return admin


class ConsentimientoAsistidoIn(BaseModel):
    version_texto: str = Field(pattern=r"^v[0-9]+$")
    titular_leyo_y_acepto: bool
    modo: Literal["presencial", "videollamada"]
    nombre: str = Field(min_length=1, max_length=120)


def _actor(admin: Sesion, request: Request) -> dict:
    return {"actor_user_id": admin.user_id, "actor_rol": admin.rol, "ip": ip_de(request)}


@router.get("/consola/lineas")
async def lineas(request: Request, estado: Optional[str] = Query(default=None, pattern=_ESTADO_LINEA),
                 tenant_id: Optional[uuid.UUID] = None, admin: Sesion = Depends(requiere_rol("admin"))):
    return await listar_lineas_consola(contexto(request), estado=estado, tenant_id=tenant_id)


@router.get(_LINEA + "/consentimiento-asistido/texto")
async def texto(tenant_id: uuid.UUID, line_id: uuid.UUID, request: Request,
                admin: Sesion = Depends(_tenant_cliente)):
    async with contexto(request).db.tenant_tx(tenant_id) as con:
        t = await texto_para_linea(con, tenant_id, line_id)
    if t is None:
        raise HTTPException(status_code=404, detail={"error": "linea_inexistente"})
    return t


@router.post(_LINEA + "/consentimiento-asistido", status_code=201)
async def consentimiento_asistido(tenant_id: uuid.UUID, line_id: uuid.UUID, body: ConsentimientoAsistidoIn,
                                  request: Request, admin: Sesion = Depends(_tenant_cliente)):
    if not body.titular_leyo_y_acepto:
        raise HTTPException(status_code=422, detail={"error": "consentimiento_no_aceptado"})
    if body.version_texto not in VERSIONES:
        raise HTTPException(status_code=422, detail={"error": "version_desconocida"})
    ctx = contexto(request)
    ip = ip_de(request)
    async with ctx.db.tenant_tx(tenant_id) as con:
        t = await texto_para_linea(con, tenant_id, line_id)
        if t is None:
            raise HTTPException(status_code=404, detail={"error": "linea_inexistente"})
        if t["linea_estado"] == "de_baja":
            raise HTTPException(status_code=409, detail={"error": "linea_de_baja"})
        dueno = await con.fetchrow("SELECT u.id, u.email FROM memberships m JOIN users u ON u.id = m.user_id "
                                   "WHERE m.rol = 'dueno' ORDER BY m.created_at LIMIT 1")
        if dueno is None:
            raise HTTPException(status_code=409, detail={"error": "sin_dueno"})
        # Como el consentimiento propio del tramo 1 (routers/parametros.py): este
        # pasa a ser "el último" de la línea, así que arrastra los valores de la
        # propuesta de tenant vigente que el anterior ya aceptaba; si no, una
        # línea vinculada que ya había aceptado volvería a quedar pendiente.
        ya_consentidos = await tenant_ya_consentido(con, line_id, await leer_propuesta_tenant(con, tenant_id))
        parametros_tenant = a_json({**(await leer_parametros_tenant(con, tenant_id)), **ya_consentidos})
        opciones = {"parametros_linea": t["parametros_linea"], "parametros_tenant": parametros_tenant}
        cid = await registrar_consentimiento_asistido(
            con, tenant_id=tenant_id, line_id=line_id, dueno_user_id=dueno["id"], admin_user_id=admin.user_id,
            version=body.version_texto, opciones=opciones, ip=ip, modo_asistencia=body.modo,
            aceptado_por_nombre=body.nombre.strip())
        await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=admin.user_id, actor_rol=admin.rol,
                                  accion="consentimiento_asistido", tipo_objeto="consent", objeto_id=cid, ip=ip,
                                  detalle={"modo": body.modo})
        await eventos_producto.registrar_evento(con, tenant_id=tenant_id, evento="consentimiento_registrado",
                                                line_id=line_id, user_id=dueno["id"], objeto_id=cid)
    # El consentimiento ya quedó guardado: un proveedor de mail caído no lo
    # convierte en 500 (mismo criterio que enviar_link_seguro del tramo 1).
    try:
        await ctx.mailer.enviar(email_copia_consentimiento(dueno["email"], version=body.version_texto))
        copia = True
    except Exception as e:
        logger.warning("copia del consentimiento %s no enviada: %s", cid, type(e).__name__)
        copia = False
    return {"consent_id": str(cid), "copia_enviada": copia}


@router.post(_LINEA + "/vinculo", status_code=201)
async def vincular(tenant_id: uuid.UUID, line_id: uuid.UUID, request: Request, body: Optional[VincularIn] = None,
                   admin: Sesion = Depends(_tenant_cliente)):
    try:
        return await iniciar_vinculo(contexto(request), tenant_id=tenant_id, line_id=line_id,
                                     full_sync=body.full_sync if body else False, **_actor(admin, request))
    except VinculoRechazado as e:
        raise http_de(e)


@router.get(_LINEA + "/vinculo")
async def estado(tenant_id: uuid.UUID, line_id: uuid.UUID, request: Request,
                 admin: Sesion = Depends(_tenant_cliente)):
    try:
        return await estado_de_linea(contexto(request), tenant_id=tenant_id, line_id=line_id)
    except VinculoRechazado as e:
        raise http_de(e)


@router.get(_LINEA + "/vinculo/qr")
async def qr(tenant_id: uuid.UUID, line_id: uuid.UUID, request: Request,
             admin: Sesion = Depends(_tenant_cliente)):
    try:
        return respuesta_png(await qr_png(contexto(request), tenant_id=tenant_id, line_id=line_id))
    except VinculoRechazado as e:
        raise http_de(e)


@router.post(_LINEA + "/vinculo/codigo")
async def codigo(tenant_id: uuid.UUID, line_id: uuid.UUID, body: CodigoIn, request: Request,
                 admin: Sesion = Depends(_tenant_cliente)):
    try:
        return respuesta_codigo(await pedir_codigo(contexto(request), tenant_id=tenant_id, line_id=line_id,
                                                   telefono=body.telefono, **_actor(admin, request)))
    except VinculoRechazado as e:
        raise http_de(e)


@router.post(_LINEA + "/vinculo/reiniciar-qr")
async def reiniciar(tenant_id: uuid.UUID, line_id: uuid.UUID, request: Request,
                    admin: Sesion = Depends(_tenant_cliente)):
    try:
        return await reiniciar_qr(contexto(request), tenant_id=tenant_id, line_id=line_id, **_actor(admin, request))
    except VinculoRechazado as e:
        raise http_de(e)


@router.post(_LINEA + "/vinculo/desconectar", status_code=202)
async def desconectar(tenant_id: uuid.UUID, line_id: uuid.UUID, request: Request,
                      body: Optional[DesconectarIn] = None, admin: Sesion = Depends(_tenant_cliente)):
    exigir_confirmacion(body)
    try:
        return await pedir_fin(contexto(request), tenant_id=tenant_id, line_id=line_id, causa="pedido_kis",
                               borrar=False, **_actor(admin, request))
    except VinculoRechazado as e:
        raise http_de(e)


@router.post(_LINEA + "/vinculo/desconectar-y-borrar", status_code=202)
async def desconectar_y_borrar(tenant_id: uuid.UUID, line_id: uuid.UUID, request: Request,
                               body: Optional[BorrarIn] = None, admin: Sesion = Depends(_tenant_cliente)):
    ctx = contexto(request)
    await exigir_nombre(ctx, tenant_id, line_id, body)
    try:
        return await pedir_fin(ctx, tenant_id=tenant_id, line_id=line_id, causa="pedido_kis", borrar=True,
                               **_actor(admin, request))
    except VinculoRechazado as e:
        raise http_de(e)


@router.put(_LINEA + "/vinculo/restriccion")
async def marcar(tenant_id: uuid.UUID, line_id: uuid.UUID, body: RestriccionIn, request: Request,
                 admin: Sesion = Depends(_tenant_cliente)):
    try:
        return await marcar_restriccion(contexto(request), tenant_id=tenant_id, line_id=line_id, hasta=body.hasta,
                                        **_actor(admin, request))
    except VinculoRechazado as e:
        raise http_de(e)


@router.delete(_LINEA + "/vinculo/restriccion")
async def levantar(tenant_id: uuid.UUID, line_id: uuid.UUID, request: Request,
                   admin: Sesion = Depends(_tenant_cliente)):
    try:
        return await levantar_restriccion(contexto(request), tenant_id=tenant_id, line_id=line_id,
                                          **_actor(admin, request))
    except VinculoRechazado as e:
        raise http_de(e)
