"""
Operaciones de los admins de KIS (§3 P0): alta del primer admin y alta de un
cliente con su línea, su dueño y la invitación. Crear el tenant pasa por
radar_admin_crear_tenant (SECURITY DEFINER) dentro del mismo tenant_tx que el
resto del alta, así todo es atómico. La k_tenant se crea antes y se destruye
si el alta falla.
"""

import uuid
from dataclasses import dataclass
from typing import Optional

from app.radar import auditoria, eventos_producto
from app.radar.auth import Sesion
from app.radar.constantes import TENANT_KIS
from app.radar.contexto import RadarContexto
from app.radar.lineas import crear_linea, resolver_valores_linea
from app.radar.links import enviar_link, enviar_link_seguro, normalizar_email
from app.radar.parametros import DE_TENANT, iniciales, propuesta_para_perfil, validar
from app.radar.secrets import crear_k_tenant, destruir_k_tenant


def resolver_valores_tenant(perfil: str, parciales: dict) -> dict:
    """Iniciales de §2.1, pisados por la propuesta del perfil (§7: `sensible`
    arranca con IA apagada y vía sincrónica) y estos por los valores explícitos
    del admin (None = no enviado). "Propone, no fuerza" (§2.1): el admin puede
    pisar la propuesta, pero no queda IA encendida por omisión sobre datos de
    salud. La parte de LÍNEA de la propuesta (`retencion_fuente_dias = 7`) no
    se aplica: exige el almacén purgable, que no existe hasta el tramo 6
    (Decisión 5); GET /radar/admin/propuesta la muestra igual."""
    valores = iniciales("tenant")
    valores["perfil_de_datos"] = validar("perfil_de_datos", perfil)
    for nombre, valor in propuesta_para_perfil(valores["perfil_de_datos"]).items():
        if nombre in DE_TENANT:
            valores[nombre] = valor
    for nombre, valor in parciales.items():
        if nombre not in DE_TENANT:
            raise ValueError(f"no es un parámetro de tenant: {nombre}")
        if valor is not None:
            valores[nombre] = validar(nombre, valor)
    return valores


async def crear_admin_kis(ctx: RadarContexto, *, email: str, nombre: str, ip: Optional[str] = None) -> uuid.UUID:
    email = normalizar_email(email)
    async with ctx.db.tenant_tx(TENANT_KIS) as con:
        uid = await con.fetchval(
            "INSERT INTO users (email, nombre) VALUES ($1, $2) "
            "ON CONFLICT (tenant_id, email) DO UPDATE SET nombre = EXCLUDED.nombre RETURNING id",
            email, nombre)
        await con.execute(
            "INSERT INTO memberships (user_id, rol) VALUES ($1, 'admin') "
            "ON CONFLICT (tenant_id, user_id) DO UPDATE SET rol = 'admin'", uid)
        await auditoria.registrar(con, tenant_id=TENANT_KIS, actor_user_id=None, actor_rol="sistema",
                                  accion="usuario_invitado", tipo_objeto="user", objeto_id=uid, ip=ip,
                                  detalle={"rol_nuevo": "admin"})
    await enviar_link(ctx, tenant_id=TENANT_KIS, user_id=uid, email=email, proposito="invitacion", ip=ip)
    return uid


@dataclass(frozen=True)
class AltaTenant:
    tenant_id: uuid.UUID
    line_id: uuid.UUID
    user_id: uuid.UUID
    invitacion_enviada: bool


async def crear_tenant_con_dueno(ctx: RadarContexto, *, actor: Sesion, ip: Optional[str], nombre: str, rubro: str,
                                 perfil: str, parametros_tenant: dict, dueno_email: str, dueno_nombre: str,
                                 linea_nombre: str, parametros_linea: dict) -> AltaTenant:
    valores_t = resolver_valores_tenant(perfil, parametros_tenant)
    valores_l = resolver_valores_linea(parametros_linea)
    email = normalizar_email(dueno_email)
    tenant_id = uuid.uuid4()
    crear_k_tenant(ctx.secretos, tenant_id)
    try:
        async with ctx.db.tenant_tx(tenant_id) as con:
            await con.fetchval(
                "SELECT radar_admin_crear_tenant($1, $2, $3, $4, $5, $6, $7, $8)",
                tenant_id, nombre, rubro, valores_t["perfil_de_datos"], valores_t["retencion_fichas_meses"],
                valores_t["retener_fragmentos"], valores_t["ia_habilitada"], valores_t["via_llm"])
            line_id = await crear_linea(con, tenant_id=tenant_id, nombre=linea_nombre, valores=valores_l)
            user_id = await con.fetchval("INSERT INTO users (email, nombre) VALUES ($1, $2) RETURNING id",
                                         email, dueno_nombre)
            await con.execute("INSERT INTO memberships (user_id, rol) VALUES ($1, 'dueno')", user_id)
            comun = dict(tenant_id=tenant_id, actor_user_id=actor.user_id, actor_rol=actor.rol, ip=ip)
            await auditoria.registrar(con, accion="tenant_creado", tipo_objeto="tenant", objeto_id=tenant_id, **comun)
            await auditoria.registrar(con, accion="linea_creada", tipo_objeto="line", objeto_id=line_id, **comun)
            await auditoria.registrar(con, accion="usuario_invitado", tipo_objeto="user", objeto_id=user_id,
                                      detalle={"rol_nuevo": "dueno"}, **comun)
            await eventos_producto.registrar_evento(con, tenant_id=tenant_id, evento="linea_creada", line_id=line_id)
    except Exception:
        destruir_k_tenant(ctx.secretos, tenant_id)
        raise
    enviada = await enviar_link_seguro(ctx, tenant_id=tenant_id, user_id=user_id, email=email, proposito="invitacion", ip=ip)
    if enviada:
        async with ctx.db.tenant_tx(tenant_id) as con:
            await eventos_producto.registrar_evento(con, tenant_id=tenant_id, evento="invitacion_enviada",
                                                    line_id=line_id, user_id=user_id)
    return AltaTenant(tenant_id=tenant_id, line_id=line_id, user_id=user_id, invitacion_enviada=enviada)
