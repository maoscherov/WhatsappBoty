"""
Cambios de parámetros con la regla de §2.1:
- endurecer se aplica directo y queda auditado (una fila por parámetro);
- el cliente solo puede endurecer (solo_endurecer=True → 403 si algo es más laxo);
- un valor más laxo sobre una línea `vinculada` exige un consentimiento nuevo
  del dueño: 409 salvo consentido=True, que solo usa el endpoint de consentimientos;
- un valor de tenant más laxo con líneas vivas exige que CADA línea vinculada
  tenga un consentimiento (el último) cuyas opciones.parametros_tenant incluyan
  esos valores; hasta entonces, 409 con las líneas que faltan.
- Los valores más laxos los PROPONE un admin de KIS ("los fija un admin de
  KIS", §2.1): el 409 del admin guarda la propuesta en
  lines/tenants.parametros_propuestos, y el consentimiento del dueño solo
  acepta valores que coincidan con ella (coincide_con_propuesta). Al aplicarse,
  los nombres consentidos se quitan de la propuesta (consumir_propuesta_*).
"""

import json
import uuid
from decimal import Decimal
from typing import Any, Optional

import asyncpg

from app.radar import auditoria
from app.radar.lineas import AlmacenNoDisponible, verificar_almacen
from app.radar.parametros import DE_LINEA, DE_TENANT, ValorInvalido, clasificar_cambios, validar


class CambioRechazado(Exception):
    def __init__(self, status: int, detalle: dict):
        super().__init__(detalle)
        self.status = status
        self.detalle = detalle


def a_json(valores: dict) -> dict:
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in valores.items()}


def _cambios(actuales: dict, nuevos: dict, permitidos: list[str]) -> dict:
    ajenos = [n for n in nuevos if n not in permitidos]
    if ajenos:
        raise CambioRechazado(422, {"error": "valor_invalido", "detalle": f"parámetros desconocidos: {ajenos}"})
    try:
        return clasificar_cambios(actuales, nuevos)
    except ValorInvalido as e:
        raise CambioRechazado(422, {"error": "valor_invalido", "detalle": str(e)})


def _rechazar_laxos(cambios: dict, solo_endurecer: bool) -> list[str]:
    laxos = [n for n, c in cambios.items() if c == "mas_laxo"]
    if laxos and solo_endurecer:
        raise CambioRechazado(403, {"error": "solo_endurecer", "parametros": laxos})
    return laxos


async def _auditar(con, *, tenant_id, objeto_id, tipo_objeto, actor_user_id, actor_rol, ip, ambito, actuales, finales, cambios):
    for nombre in cambios:
        await auditoria.registrar(
            con, tenant_id=tenant_id, actor_user_id=actor_user_id, actor_rol=actor_rol, accion="parametro_cambiado",
            tipo_objeto=tipo_objeto, objeto_id=objeto_id, ip=ip,
            detalle={"parametro": nombre, "valor_anterior": actuales[nombre], "valor_nuevo": finales[nombre],
                     "ambito": ambito})


async def leer_parametros_tenant(con: asyncpg.Connection, tenant_id: uuid.UUID) -> dict:
    fila = await con.fetchrow("SELECT " + ", ".join(DE_TENANT) + " FROM tenants WHERE id = $1", tenant_id)
    return {n: fila[n] for n in DE_TENANT}


async def leer_propuesta_tenant(con: asyncpg.Connection, tenant_id: uuid.UUID) -> dict:
    crudo = await con.fetchval("SELECT parametros_propuestos FROM tenants WHERE id = $1", tenant_id)
    return json.loads(crudo) if crudo else {}


def coincide_con_propuesta(propuesta: dict, nombre: str, valor: Any) -> bool:
    """True si `nombre` está en la propuesta con exactamente ese valor. Se
    compara validado (Decimal("20") == Decimal("20.00")), no como texto."""
    return nombre in propuesta and validar(nombre, propuesta[nombre]) == valor


async def _auditar_propuesta(con, *, tenant_id, objeto_id, tipo_objeto, ambito, actor_user_id, actor_rol, ip, valores):
    for nombre, valor in valores.items():
        await auditoria.registrar(
            con, tenant_id=tenant_id, actor_user_id=actor_user_id, actor_rol=actor_rol, accion="parametro_propuesto",
            tipo_objeto=tipo_objeto, objeto_id=objeto_id, ip=ip,
            detalle={"parametro": nombre, "valor_nuevo": valor, "ambito": ambito})


async def proponer_parametros_linea(con: asyncpg.Connection, *, tenant_id: uuid.UUID, line_id: uuid.UUID, propuesta: dict,
                                    actor_user_id: uuid.UUID, actor_rol: str, ip: Optional[str]) -> dict:
    """Guarda (fusionando con la anterior) la propuesta de un admin de KIS para
    una línea viva y la audita. Devuelve la parte nueva en JSON."""
    valores = {n: validar(n, v) for n, v in propuesta.items()}
    await con.execute(
        "UPDATE lines SET parametros_propuestos = COALESCE(parametros_propuestos, '{}'::jsonb) || $2::jsonb, "
        "updated_at = now() WHERE id = $1",
        line_id, json.dumps(a_json(valores)))
    await _auditar_propuesta(con, tenant_id=tenant_id, objeto_id=line_id, tipo_objeto="line", ambito="linea",
                             actor_user_id=actor_user_id, actor_rol=actor_rol, ip=ip, valores=valores)
    return a_json(valores)


async def proponer_parametros_tenant(con: asyncpg.Connection, *, tenant_id: uuid.UUID, propuesta: dict,
                                     actor_user_id: uuid.UUID, actor_rol: str, ip: Optional[str]) -> dict:
    valores = {n: validar(n, v) for n, v in propuesta.items()}
    await con.execute(
        "UPDATE tenants SET parametros_propuestos = COALESCE(parametros_propuestos, '{}'::jsonb) || $2::jsonb, "
        "parametros_propuestos_at = now(), updated_at = now() WHERE id = $1",
        tenant_id, json.dumps(a_json(valores)))
    await _auditar_propuesta(con, tenant_id=tenant_id, objeto_id=tenant_id, tipo_objeto="tenant", ambito="tenant",
                             actor_user_id=actor_user_id, actor_rol=actor_rol, ip=ip, valores=valores)
    return a_json(valores)


async def consumir_propuesta_linea(con: asyncpg.Connection, line_id: uuid.UUID, nombres: list[str]) -> None:
    """Quita de la propuesta los parámetros ya consentidos; NULL si no queda nada."""
    await con.execute(
        "UPDATE lines SET parametros_propuestos = "
        "NULLIF(COALESCE(parametros_propuestos, '{}'::jsonb) - $2::text[], '{}'::jsonb) WHERE id = $1",
        line_id, nombres)


async def consumir_propuesta_tenant(con: asyncpg.Connection, tenant_id: uuid.UUID, nombres: list[str]) -> None:
    await con.execute(
        "UPDATE tenants SET parametros_propuestos = "
        "NULLIF(COALESCE(parametros_propuestos, '{}'::jsonb) - $2::text[], '{}'::jsonb), "
        # consumida entera: sin propuesta vigente, ningún consentimiento previo cuenta
        "parametros_propuestos_at = CASE WHEN NULLIF(COALESCE(parametros_propuestos, '{}'::jsonb) - $2::text[], "
        "'{}'::jsonb) IS NULL THEN NULL ELSE parametros_propuestos_at END WHERE id = $1",
        tenant_id, nombres)


async def lineas_vivas_sin_consentir(con: asyncpg.Connection, valores_tenant: dict) -> list[uuid.UUID]:
    filas = await con.fetch(
        """
        SELECT l.id FROM lines l
        WHERE l.estado = 'vinculada' AND NOT EXISTS (
            SELECT 1 FROM consents c
            WHERE c.id = (SELECT c2.id FROM consents c2 WHERE c2.line_id = l.id ORDER BY c2.created_at DESC LIMIT 1)
              AND c.opciones->'parametros_tenant' @> $1::jsonb
              -- solo un consentimiento posterior a la propuesta vigente (NULL: ninguno)
              AND c.created_at >= (SELECT t.parametros_propuestos_at FROM tenants t WHERE t.id = l.tenant_id))
        ORDER BY l.created_at
        """,
        json.dumps(valores_tenant, default=str))
    return [f["id"] for f in filas]


async def cambiar_parametros_linea(con: asyncpg.Connection, *, tenant_id: uuid.UUID, line_id: uuid.UUID, nuevos: dict,
                                   actor_user_id: uuid.UUID, actor_rol: str, ip: Optional[str],
                                   solo_endurecer: bool, consentido: bool = False) -> dict:
    fila = await con.fetchrow("SELECT estado, " + ", ".join(DE_LINEA) + " FROM lines WHERE id = $1 FOR UPDATE", line_id)
    if fila is None:
        raise CambioRechazado(404, {"error": "linea_inexistente"})
    actuales = {n: fila[n] for n in DE_LINEA}
    cambios = _cambios(actuales, nuevos, DE_LINEA)
    if not cambios:
        return actuales
    laxos = _rechazar_laxos(cambios, solo_endurecer)
    if laxos and fila["estado"] == "vinculada" and not consentido:
        raise CambioRechazado(409, {"error": "requiere_consentimiento", "parametros": laxos})
    finales = {**actuales, **{n: validar(n, nuevos[n]) for n in cambios}}
    try:
        almacen = verificar_almacen(finales)
    except AlmacenNoDisponible as e:
        raise CambioRechazado(422, {"error": "almacen_no_disponible", "detalle": str(e)})
    await con.execute(
        "UPDATE lines SET duracion_vinculo_dias = $2, retencion_fuente_dias = $3, retencion_tras_desvinculo_dias = $4, "
        "tope_ia_mensual_usd = $5, almacen_fuente = $6, updated_at = now() WHERE id = $1",
        line_id, finales["duracion_vinculo_dias"], finales["retencion_fuente_dias"],
        finales["retencion_tras_desvinculo_dias"], finales["tope_ia_mensual_usd"], almacen)
    await _auditar(con, tenant_id=tenant_id, objeto_id=line_id, tipo_objeto="line", actor_user_id=actor_user_id,
                   actor_rol=actor_rol, ip=ip, ambito="linea", actuales=actuales, finales=finales, cambios=cambios)
    return finales


async def cambiar_parametros_tenant(con: asyncpg.Connection, *, tenant_id: uuid.UUID, nuevos: dict,
                                    actor_user_id: uuid.UUID, actor_rol: str, ip: Optional[str],
                                    solo_endurecer: bool) -> dict:
    fila = await con.fetchrow("SELECT " + ", ".join(DE_TENANT) + " FROM tenants WHERE id = $1 FOR UPDATE", tenant_id)
    if fila is None:
        raise CambioRechazado(404, {"error": "tenant_inexistente"})
    actuales = {n: fila[n] for n in DE_TENANT}
    cambios = _cambios(actuales, nuevos, DE_TENANT)
    if not cambios:
        return actuales
    laxos = _rechazar_laxos(cambios, solo_endurecer)
    if laxos:
        pendientes = await lineas_vivas_sin_consentir(con, {n: validar(n, nuevos[n]) for n in laxos})
        if pendientes:
            raise CambioRechazado(409, {"error": "requiere_consentimiento", "parametros": laxos,
                                        "lineas": [str(x) for x in pendientes]})
    finales = {**actuales, **{n: validar(n, nuevos[n]) for n in cambios}}
    await con.execute(
        "UPDATE tenants SET retencion_fichas_meses = $2, perfil_de_datos = $3, retener_fragmentos = $4, "
        "ia_habilitada = $5, via_llm = $6, updated_at = now() WHERE id = $1",
        tenant_id, finales["retencion_fichas_meses"], finales["perfil_de_datos"], finales["retener_fragmentos"],
        finales["ia_habilitada"], finales["via_llm"])
    await _auditar(con, tenant_id=tenant_id, objeto_id=tenant_id, tipo_objeto="tenant", actor_user_id=actor_user_id,
                   actor_rol=actor_rol, ip=ip, ambito="tenant", actuales=actuales, finales=finales, cambios=cambios)
    return finales


async def tenant_ya_consentido(con: asyncpg.Connection, line_id: uuid.UUID, propuesta_t: dict) -> dict:
    """Valores de la propuesta de tenant vigente que el último consentimiento
    de esta línea ya aceptó, siempre que sea posterior a esa propuesta (un
    consentimiento viejo no vale para una propuesta nueva). Lo usan el
    consentimiento del dueño (routers/parametros.py) y el asistido de la Consola."""
    crudo = await con.fetchval(
        "SELECT c.opciones->'parametros_tenant' FROM consents c "
        "WHERE c.id = (SELECT c2.id FROM consents c2 WHERE c2.line_id = $1 ORDER BY c2.created_at DESC LIMIT 1) "
        "AND c.created_at >= (SELECT t.parametros_propuestos_at FROM tenants t WHERE t.id = c.tenant_id)",
        line_id)
    previos = json.loads(crudo) if crudo else {}
    return {n: validar(n, v) for n, v in propuesta_t.items()
            if n in previos and coincide_con_propuesta(propuesta_t, n, validar(n, previos[n]))}
