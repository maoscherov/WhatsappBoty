"""
Parámetros por línea y por cuenta para el cliente (solo endurecer) y el
consentimiento por línea, único camino que aplica valores más laxos sobre
una línea viva (§2.1, §3 P2). El consentimiento acepta lo que un admin de
KIS propuso (parametros_propuestos); no es un canal para aflojar solo.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.radar import auditoria, eventos_producto
from app.radar.auth import Sesion, ip_de, requiere_rol, sesion_actual
from app.radar.consentimiento import VERSIONES, hash_texto, listar_consentimientos, registrar_consentimiento
from app.radar.contexto import contexto
from app.radar.lineas import leer_linea, propuesta_de_linea
from app.radar.parametros import DE_LINEA, DE_TENANT, ValorInvalido, validar
from app.radar.parametros_service import (CambioRechazado, a_json, cambiar_parametros_linea,
                                          cambiar_parametros_tenant, coincide_con_propuesta,
                                          consumir_propuesta_linea, consumir_propuesta_tenant,
                                          leer_parametros_tenant, leer_propuesta_tenant)

router = APIRouter(prefix="/radar/api", tags=["radar-parametros"])


class ConsentimientoIn(BaseModel):
    version_texto: str
    acepta: bool
    titular: bool
    parametros_linea: dict[str, Any] = {}
    parametros_tenant: dict[str, Any] = {}


def _http(e: CambioRechazado) -> HTTPException:
    return HTTPException(status_code=e.status, detail=e.detalle)


async def _linea_visible(con, sesion: Sesion, line_id: uuid.UUID) -> dict:
    fila = await leer_linea(con, line_id)
    if fila is None or not sesion.puede_ver_linea(line_id):
        raise HTTPException(status_code=404, detail={"error": "linea_inexistente"})
    return fila


def _validar_parciales(parciales: dict, permitidos: list[str]) -> dict:
    ajenos = [n for n in parciales if n not in permitidos]
    if ajenos:
        raise HTTPException(status_code=422, detail={"error": "valor_invalido", "detalle": f"parámetros desconocidos: {ajenos}"})
    try:
        return {n: validar(n, v) for n, v in parciales.items()}
    except ValorInvalido as e:
        raise HTTPException(status_code=422, detail={"error": "valor_invalido", "detalle": str(e)})


@router.get("/lineas/{line_id}/parametros")
async def ver_parametros(line_id: uuid.UUID, request: Request, sesion: Sesion = Depends(sesion_actual)):
    ctx = contexto(request)
    async with ctx.db.tenant_tx(sesion.tenant_id) as con:
        fila = await _linea_visible(con, sesion, line_id)
        tenant = await leer_parametros_tenant(con, sesion.tenant_id)
        propuesta_t = await leer_propuesta_tenant(con, sesion.tenant_id)
    return {"linea": a_json({n: fila[n] for n in DE_LINEA}), "estado": fila["estado"],
            "almacen_fuente": fila["almacen_fuente"], "tenant": a_json(tenant),
            "propuesta": {"linea": propuesta_de_linea(fila), "tenant": propuesta_t}}


@router.put("/lineas/{line_id}/parametros")
async def endurecer_linea(line_id: uuid.UUID, body: dict[str, Any], request: Request,
                          sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    try:
        async with ctx.db.tenant_tx(sesion.tenant_id) as con:
            await _linea_visible(con, sesion, line_id)
            finales = await cambiar_parametros_linea(
                con, tenant_id=sesion.tenant_id, line_id=line_id, nuevos=body, actor_user_id=sesion.user_id,
                actor_rol=sesion.rol, ip=ip_de(request), solo_endurecer=True)
    except CambioRechazado as e:
        raise _http(e)
    return a_json(finales)


@router.put("/cuenta/parametros")
async def endurecer_tenant(body: dict[str, Any], request: Request, sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    try:
        async with ctx.db.tenant_tx(sesion.tenant_id) as con:
            finales = await cambiar_parametros_tenant(
                con, tenant_id=sesion.tenant_id, nuevos=body, actor_user_id=sesion.user_id, actor_rol=sesion.rol,
                ip=ip_de(request), solo_endurecer=True)
    except CambioRechazado as e:
        raise _http(e)
    return a_json(finales)


@router.post("/lineas/{line_id}/consentimientos", status_code=201)
async def consentir(line_id: uuid.UUID, body: ConsentimientoIn, request: Request,
                    sesion: Sesion = Depends(requiere_rol("dueno"))):
    if not (body.acepta and body.titular):
        raise HTTPException(status_code=422, detail={"error": "consentimiento_no_aceptado"})
    if body.version_texto not in VERSIONES:
        raise HTTPException(status_code=422, detail={"error": "version_desconocida"})
    nuevos_linea = _validar_parciales(body.parametros_linea, DE_LINEA)
    nuevos_tenant = _validar_parciales(body.parametros_tenant, DE_TENANT)
    ctx = contexto(request)
    ip = ip_de(request)
    try:
        async with ctx.db.tenant_tx(sesion.tenant_id) as con:
            fila = await _linea_visible(con, sesion, line_id)
            # §2.1: el dueño acepta lo que KIS propuso; cualquier valor que no
            # coincida con la propuesta (ni siquiera el actual) se rechaza antes
            # de escribir nada.
            propuesta_l = propuesta_de_linea(fila)
            propuesta_t = await leer_propuesta_tenant(con, sesion.tenant_id)
            sin_propuesta = [n for n, v in nuevos_linea.items() if not coincide_con_propuesta(propuesta_l, n, v)] + \
                            [n for n, v in nuevos_tenant.items() if not coincide_con_propuesta(propuesta_t, n, v)]
            if sin_propuesta:
                raise HTTPException(status_code=422, detail={"error": "sin_propuesta", "parametros": sin_propuesta})
            finales_l = {**{n: fila[n] for n in DE_LINEA}, **nuevos_linea}
            finales_t = {**(await leer_parametros_tenant(con, sesion.tenant_id)), **nuevos_tenant}
            opciones = {"parametros_linea": a_json(finales_l), "parametros_tenant": a_json(finales_t)}
            cid = await registrar_consentimiento(con, tenant_id=sesion.tenant_id, line_id=line_id, user_id=sesion.user_id,
                                                 version=body.version_texto, opciones=opciones, ip=ip)
            await auditoria.registrar(con, tenant_id=sesion.tenant_id, actor_user_id=sesion.user_id, actor_rol=sesion.rol,
                                      accion="consentimiento_registrado", tipo_objeto="consent", objeto_id=cid, ip=ip)
            await eventos_producto.registrar_evento(con, tenant_id=sesion.tenant_id, evento="consentimiento_registrado",
                                                    line_id=line_id, user_id=sesion.user_id, objeto_id=cid)
            if nuevos_linea:
                await cambiar_parametros_linea(
                    con, tenant_id=sesion.tenant_id, line_id=line_id, nuevos=nuevos_linea, actor_user_id=sesion.user_id,
                    actor_rol=sesion.rol, ip=ip, solo_endurecer=False, consentido=True)
                await consumir_propuesta_linea(con, line_id, list(nuevos_linea))
            tenant_aplicado, pendientes = True, []
            if nuevos_tenant:
                try:
                    await cambiar_parametros_tenant(
                        con, tenant_id=sesion.tenant_id, nuevos=nuevos_tenant, actor_user_id=sesion.user_id,
                        actor_rol=sesion.rol, ip=ip, solo_endurecer=False)
                    await consumir_propuesta_tenant(con, sesion.tenant_id, list(nuevos_tenant))
                except CambioRechazado as e:
                    if e.status != 409:
                        raise
                    tenant_aplicado, pendientes = False, e.detalle["lineas"]   # la propuesta sigue pendiente
    except CambioRechazado as e:
        raise _http(e)
    return {"consent_id": str(cid), "hash_texto": hash_texto(body.version_texto),
            "tenant_aplicado": tenant_aplicado, "lineas_pendientes": pendientes}


@router.get("/lineas/{line_id}/consentimientos")
async def consentimientos(line_id: uuid.UUID, request: Request, sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    async with ctx.db.tenant_tx(sesion.tenant_id) as con:
        await _linea_visible(con, sesion, line_id)
        return await listar_consentimientos(con, line_id)
