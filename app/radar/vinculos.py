"""
Servicio de vínculos (§3 P3, §3.1 C2/C4, Estados especiales, §6.6). Lo usan
igual la Consola KIS y la pantalla del dueño: una sola máquina de estados.

- Sin una fila en `consents` para la línea no se crea sesión.
- Un vínculo activo por línea; reconectar = vínculo nuevo de la misma línea.
- Con restricción de cuenta activa no se vincula ni se reconecta; el fin sí.
- Sin worker con capacidad no se crea sesión ni se muestra QR.
- Las llamadas a WAHA van fuera de las transacciones.
- QR, códigos, teléfonos y me.id no se guardan ni se loguean.
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Literal, Optional

import asyncpg

from app.radar import auditoria, eventos_producto
from app.radar import jobs as cola
from app.radar.contexto import RadarContexto
from app.radar.telefonos import TelefonoNoSoportado, normalizar_e164
from app.radar.vinculo_estados import (ACTIVOS, MAX_REINICIOS_QR, PATRON_STATUS, SIN_EVENTOS_POLLING, VIVOS,
                                       Transicion, restriccion_activa, sufijo_de, transicion)
from app.radar.waha.cliente import WahaError
from app.radar.waha.gestor import (ConfigNoCoincide, crear_clave_lectura, crear_sesion_verificada,
                                   terminar_sesion)
from app.radar.waha.sesion import cuerpo_sesion, nombre_sesion
from app.radar.workers import (ClaveAdminAusente, SinCapacidad, cliente_de, elegir_worker, leer_worker,
                               nombre_clave_lectura)

logger = logging.getLogger("app.radar.vinculos")

COLUMNAS_LINK = ("id", "line_id", "worker_id", "session_name", "engine", "full_sync", "estado", "waha_status",
                 "qr_reinicios", "numero_sufijo", "conectado_at", "caido_desde", "ultimo_status_at",
                 "restriccion_hasta", "restriccion_sin_fecha", "fin_causa", "desvinculo_confirmado",
                 "observado_hasta", "created_at", "cerrado_at")
_SELECT_LINK = "SELECT " + ", ".join(COLUMNAS_LINK) + " FROM links"


class VinculoRechazado(Exception):
    def __init__(self, status: int, codigo: str) -> None:
        super().__init__(codigo)
        self.status = status
        self.codigo = codigo


async def ultimo_link(con: asyncpg.Connection, line_id: uuid.UUID) -> Optional[dict]:
    fila = await con.fetchrow(_SELECT_LINK + " WHERE line_id = $1 ORDER BY created_at DESC LIMIT 1", line_id)
    return dict(fila) if fila else None


def _iso(v: Optional[datetime]) -> Optional[str]:
    return v.isoformat() if v else None


def estado_json(link: Optional[dict], *, linea_nombre: str, tiene_consentimiento: bool, ahora: datetime) -> dict:
    base: dict[str, Any] = {"linea_nombre": linea_nombre, "tiene_consentimiento": tiene_consentimiento}
    if link is None:
        return {**base, "link_id": None, "estado": "sin_vinculo", "waha_status": None, "qr_disponible": False,
                "qr_vencido": False, "passkey": False, "reinicios_restantes": MAX_REINICIOS_QR, "numero": None,
                "restriccion_activa": False, "restriccion_hasta": None, "caido_desde": None, "conectado_at": None,
                "fin_causa": None, "desvinculo_confirmado": None}
    ws = link["waha_status"]
    en_qr = link["estado"] == "esperando_qr"
    return {
        **base,
        "link_id": str(link["id"]),
        "estado": link["estado"],
        "waha_status": ws,
        "qr_disponible": en_qr and ws in (None, "STARTING", "SCAN_QR_CODE"),
        "qr_vencido": en_qr and ws in ("FAILED", "STOPPED"),
        "passkey": ws == "PASSKEY_REQUIRED",
        "reinicios_restantes": MAX_REINICIOS_QR - link["qr_reinicios"],
        "numero": ("…" + link["numero_sufijo"]) if link["numero_sufijo"] else None,
        "restriccion_activa": restriccion_activa(link["restriccion_hasta"], link["restriccion_sin_fecha"], ahora),
        "restriccion_hasta": _iso(link["restriccion_hasta"]),
        "caido_desde": _iso(link["caido_desde"]),
        "conectado_at": _iso(link["conectado_at"]),
        "fin_causa": link["fin_causa"],
        "desvinculo_confirmado": link["desvinculo_confirmado"],
    }


async def _leer_estado(con: asyncpg.Connection, line_id: uuid.UUID):
    linea = await con.fetchrow("SELECT nombre, estado FROM lines WHERE id = $1", line_id)
    if linea is None:
        raise VinculoRechazado(404, "linea_inexistente")
    consentida = await con.fetchval("SELECT EXISTS (SELECT 1 FROM consents WHERE line_id = $1)", line_id)
    link = await ultimo_link(con, line_id)
    ahora = await con.fetchval("SELECT now()")
    return linea, consentida, link, ahora


async def aplicar_status(con: asyncpg.Connection, *, tenant_id: uuid.UUID, link_id: uuid.UUID, waha_status: str,
                         origen: str, me_id: Optional[str] = None,
                         leido_desde: Optional[datetime] = None) -> dict:
    """Aplica un estado de WAHA a un vínculo, dentro de la transacción del llamador.

    `leido_desde`: para polling y salud, el now() de la base tomado ANTES del GET
    a WAHA. Si el vínculo recibió un status después (el webhook), lo leído es
    viejo y no se aplica: si no, un SCAN_QR_CODE leído antes del WORKING del
    webhook tiraría el vínculo a 'caido' y mandaría un falso aviso de caída."""
    if not PATRON_STATUS.match(waha_status or ""):
        raise ValueError("status de WAHA inválido")
    link = await con.fetchrow("SELECT id, line_id, estado, created_at, ultimo_status_at FROM links "
                              "WHERE id = $1 FOR UPDATE", link_id)
    if link is None:
        return {"aplicado": False, "estado_anterior": None, "estado": None, "efectos": []}
    if leido_desde is not None and link["ultimo_status_at"] is not None and link["ultimo_status_at"] > leido_desde:
        return {"aplicado": False, "estado_anterior": link["estado"], "estado": link["estado"],
                "efectos": ["obsoleto"]}
    t = transicion(link["estado"], waha_status)
    if "ignorar" in t.efectos:
        return {"aplicado": False, "estado_anterior": link["estado"], "estado": link["estado"], "efectos": ["ignorar"]}
    if link["estado"] == "caido" and t.estado == "vinculado":
        otro = await con.fetchval("SELECT id FROM links WHERE line_id = $1 AND id <> $2 AND estado = ANY($3::text[])",
                                  link["line_id"], link_id, list(ACTIVOS))
        if otro is not None:
            # La línea ya tiene un vínculo nuevo: el viejo que revive se cierra (§6.6).
            await con.execute("UPDATE links SET fin_causa = COALESCE(fin_causa, 'reemplazado') WHERE id = $1", link_id)
            await cola.encolar(con, tipo="fin_vinculo", link_id=link_id, causa="reemplazado")
            t = Transicion("caido", frozenset())
    efectos = t.efectos
    conectado = "conectado" in efectos
    await con.execute(
        """
        UPDATE links SET estado = $2, waha_status = $3, ultimo_status_at = now(), updated_at = now(),
               conectado_at = CASE WHEN $4 THEN COALESCE(conectado_at, now()) ELSE conectado_at END,
               caido_desde = CASE WHEN $4 THEN NULL WHEN $5 THEN COALESCE(caido_desde, now()) ELSE caido_desde END,
               numero_sufijo = COALESCE($6, numero_sufijo)
         WHERE id = $1
        """,
        link_id, t.estado, waha_status, conectado, "caida" in efectos,
        sufijo_de(me_id) if waha_status == "WORKING" else None)
    await con.execute("INSERT INTO link_status_events (link_id, waha_status, origen, estado_link) "
                      "VALUES ($1, $2, $3, $4)", link_id, waha_status, origen, t.estado)
    if conectado:
        await con.execute("UPDATE lines SET estado = 'vinculada', updated_at = now() "
                          "WHERE id = $1 AND estado <> 'de_baja'", link["line_id"])
        segundos = await con.fetchval("SELECT extract(epoch FROM now() - $1::timestamptz)::float8", link["created_at"])
        await eventos_producto.registrar_evento(con, tenant_id=tenant_id, evento="vinculo_working",
                                                line_id=link["line_id"], objeto_id=link_id,
                                                valores={"segundos": round(segundos, 1)})
    if "reemplazar_anteriores" in efectos:
        for anterior in await con.fetch("SELECT id FROM links WHERE line_id = $1 AND id <> $2 AND estado = 'caido'",
                                        link["line_id"], link_id):
            await con.execute("UPDATE links SET fin_causa = COALESCE(fin_causa, 'reemplazado') WHERE id = $1",
                              anterior["id"])
            await cola.encolar(con, tipo="fin_vinculo", link_id=anterior["id"], causa="reemplazado")
    if "abandonar" in efectos:
        await con.execute("UPDATE links SET fin_causa = COALESCE(fin_causa, 'qr_abandonado') WHERE id = $1", link_id)
        await cola.encolar(con, tipo="fin_vinculo", link_id=link_id, causa="qr_abandonado")
    if "caida" in efectos and link["estado"] == "vinculado":
        # Estados especiales, "Vínculo caído": banner con fecha y email al dueño.
        # El mail sale fuera de esta transacción, desde el job aviso_caida
        # (fin_vinculo.avisar_caida). Un vínculo en `cerrando` nunca llega acá:
        # la máquina de estados lo ignora.
        await cola.encolar(con, tipo="aviso_caida", link_id=link_id)
    return {"aplicado": True, "estado_anterior": link["estado"], "estado": t.estado, "efectos": sorted(efectos)}


async def iniciar_vinculo(ctx: RadarContexto, *, tenant_id: uuid.UUID, line_id: uuid.UUID,
                          actor_user_id: Optional[uuid.UUID], actor_rol: str, ip: Optional[str],
                          full_sync: bool = False) -> dict:
    rs = ctx.settings
    if not rs.waha_webhook_url or len(rs.waha_webhook_hmac_key) < 32:
        raise VinculoRechazado(503, "waha_sin_configurar")
    async with ctx.db.tenant_tx(tenant_id) as con:
        linea = await con.fetchrow("SELECT estado FROM lines WHERE id = $1", line_id)
        if linea is None:
            raise VinculoRechazado(404, "linea_inexistente")
        if linea["estado"] == "de_baja":
            raise VinculoRechazado(409, "linea_de_baja")
        consent_id = await con.fetchval(
            "SELECT id FROM consents WHERE line_id = $1 ORDER BY created_at DESC LIMIT 1", line_id)
        if consent_id is None:
            raise VinculoRechazado(409, "sin_consentimiento")
        if await con.fetchval("SELECT EXISTS (SELECT 1 FROM links WHERE line_id = $1 AND estado = ANY($2::text[]))",
                              line_id, list(ACTIVOS)):
            raise VinculoRechazado(409, "vinculo_activo")
        if await con.fetchval("SELECT EXISTS (SELECT 1 FROM links WHERE line_id = $1 "
                              "AND (restriccion_sin_fecha OR restriccion_hasta > now()))", line_id):
            raise VinculoRechazado(409, "restriccion_activa")
    try:
        worker = await elegir_worker(ctx)
    except SinCapacidad:
        async with ctx.db.tenant_tx(tenant_id) as con:
            await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=actor_user_id, actor_rol=actor_rol,
                                      accion="admision_rechazada", tipo_objeto="line", objeto_id=line_id, ip=ip,
                                      detalle={"motivo": "sin_capacidad"})
        raise VinculoRechazado(503, "sin_capacidad")

    full_sync = bool(full_sync) and worker.engine == "NOWEB"
    link_id = uuid.uuid4()
    nombre = nombre_sesion(link_id)
    try:
        async with ctx.db.tenant_tx(tenant_id) as con:
            await con.execute(
                "INSERT INTO links (id, line_id, worker_id, consent_id, session_name, engine, full_sync, creado_por) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                link_id, line_id, worker.id, consent_id, nombre, worker.engine, full_sync, actor_user_id)
    except asyncpg.UniqueViolationError:
        raise VinculoRechazado(409, "vinculo_activo")

    cuerpo = cuerpo_sesion(link_id=link_id, tenant_id=tenant_id, line_id=line_id, engine=worker.engine,
                           webhook_url=rs.waha_webhook_url, hmac_key=rs.waha_webhook_hmac_key, full_sync=full_sync)
    motivo: Optional[str] = None
    # Solo se marca 'abortado' si está confirmado que la sesión no existe en
    # WAHA: 'abortado' no es VIVO y nada lo vuelve a mirar (ni la salud ni el fin).
    sin_sesion = False
    key_id = valor = ""
    try:
        async with cliente_de(ctx, worker) as cli:
            try:
                await crear_sesion_verificada(cli, cuerpo)
                key_id, valor = await crear_clave_lectura(cli, nombre)
            except ConfigNoCoincide as e:
                motivo = "config_no_coincide"
                sin_sesion = bool(e.resultado.get("ok"))
                logger.warning("ALERTA vínculo %s abortado: WAHA no guardó %s", link_id, ", ".join(e.problemas))
            except WahaError as e:
                motivo = "waha_error"
                logger.warning("vínculo %s abortado: WAHA falló al crear (%s)", link_id, type(e).__name__)
                try:
                    sin_sesion = bool((await terminar_sesion(cli, nombre, intentar_start=False))["ok"])
                except WahaError:
                    sin_sesion = False
    except ClaveAdminAusente:
        motivo = "waha_error"
        sin_sesion = True          # sin clave admin no se llegó a llamar a WAHA
        logger.warning("vínculo %s abortado: falta la clave admin del worker", link_id)
    if motivo:
        async with ctx.db.tenant_tx(tenant_id) as con:
            if sin_sesion:
                await con.execute("UPDATE links SET estado = 'abortado', cerrado_at = now(), updated_at = now() "
                                  "WHERE id = $1", link_id)
            else:
                # No se pudo confirmar el borrado: queda a cargo del fin (reintenta
                # con backoff y, agotado, lo re-encola la programación de salud).
                logger.warning("vínculo %s: limpieza en WAHA sin confirmar, se encola el fin", link_id)
                await con.execute("UPDATE links SET fin_causa = COALESCE(fin_causa, 'qr_abandonado'), "
                                  "updated_at = now() WHERE id = $1", link_id)
                await cola.encolar(con, tipo="fin_vinculo", link_id=link_id, causa="qr_abandonado")
            await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=actor_user_id, actor_rol=actor_rol,
                                      accion="vinculo_abortado", tipo_objeto="link", objeto_id=link_id, ip=ip,
                                      detalle={"motivo": motivo})
        raise VinculoRechazado(502, motivo)

    ctx.secretos.set(nombre_clave_lectura(link_id), valor.encode())
    async with ctx.db.tenant_tx(tenant_id) as con:
        await con.execute("UPDATE links SET key_id = $2, updated_at = now() WHERE id = $1", link_id, key_id)
        await con.execute("UPDATE links SET estado = 'esperando_qr' WHERE id = $1 AND estado = 'creando'", link_id)
        await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=actor_user_id, actor_rol=actor_rol,
                                  accion="vinculo_iniciado", tipo_objeto="link", objeto_id=link_id, ip=ip)
        await eventos_producto.registrar_evento(con, tenant_id=tenant_id, evento="vinculo_iniciado", line_id=line_id,
                                                user_id=actor_user_id, objeto_id=link_id)
        linea, consentida, link, ahora = await _leer_estado(con, line_id)
    return estado_json(link, linea_nombre=linea["nombre"], tiene_consentimiento=consentida, ahora=ahora)


async def estado_de_linea(ctx: RadarContexto, *, tenant_id: uuid.UUID, line_id: uuid.UUID) -> dict:
    async with ctx.db.tenant_tx(tenant_id) as con:
        linea, consentida, link, ahora = await _leer_estado(con, line_id)
    sin_eventos = link is not None and (link["ultimo_status_at"] is None
                                        or ahora - link["ultimo_status_at"] > SIN_EVENTOS_POLLING)
    if link is not None and link["estado"] == "esperando_qr" and sin_eventos:
        # P3: "sin eventos por 20 s → polling de respaldo", hecho por el servidor.
        leido_desde = ahora          # now() de la base, tomado antes del GET a WAHA
        try:
            worker = await leer_worker(ctx, link["worker_id"])
            async with cliente_de(ctx, worker) as cli:
                sesion = await cli.leer_sesion(link["session_name"])
            status = sesion["status"] if sesion else "AUSENTE"
            if isinstance(status, str) and PATRON_STATUS.match(status):
                async with ctx.db.tenant_tx(tenant_id) as con:
                    await aplicar_status(con, tenant_id=tenant_id, link_id=link["id"], waha_status=status,
                                         origen="polling", me_id=sesion["me_id"] if sesion else None,
                                         leido_desde=leido_desde)
                    linea, consentida, link, ahora = await _leer_estado(con, line_id)
        except (WahaError, ClaveAdminAusente, LookupError) as e:
            logger.warning("vínculo %s: polling de respaldo falló (%s)", link["id"], type(e).__name__)
    return estado_json(link, linea_nombre=linea["nombre"], tiene_consentimiento=consentida, ahora=ahora)


async def _link_en_qr(ctx: RadarContexto, tenant_id: uuid.UUID, line_id: uuid.UUID) -> dict:
    async with ctx.db.tenant_tx(tenant_id) as con:
        _, _, link, _ = await _leer_estado(con, line_id)
    if link is None or link["estado"] != "esperando_qr":
        raise VinculoRechazado(409, "sin_qr")
    return link


async def qr_png(ctx: RadarContexto, *, tenant_id: uuid.UUID, line_id: uuid.UUID) -> bytes:
    link = await _link_en_qr(ctx, tenant_id, line_id)
    try:
        worker = await leer_worker(ctx, link["worker_id"])
        async with cliente_de(ctx, worker) as cli:
            png = await cli.qr_png(link["session_name"])
    except (WahaError, ClaveAdminAusente, LookupError):
        png = None
    if png is None:
        raise VinculoRechazado(404, "qr_no_disponible")
    return png


async def reiniciar_qr(ctx: RadarContexto, *, tenant_id: uuid.UUID, line_id: uuid.UUID,
                       actor_user_id: Optional[uuid.UUID], actor_rol: str, ip: Optional[str]) -> dict:
    link = await _link_en_qr(ctx, tenant_id, line_id)
    if link["qr_reinicios"] >= MAX_REINICIOS_QR:
        raise VinculoRechazado(409, "sin_reinicios")
    # Estados especiales, Restricción: mientras esté activa el sistema no
    # reinicia la sesión (se puede marcar con el vínculo en esperando_qr).
    async with ctx.db.tenant_tx(tenant_id) as con:
        ahora = await con.fetchval("SELECT now()")
    if restriccion_activa(link["restriccion_hasta"], link["restriccion_sin_fecha"], ahora):
        raise VinculoRechazado(409, "restriccion_activa")
    try:
        worker = await leer_worker(ctx, link["worker_id"])
        async with cliente_de(ctx, worker) as cli:
            await cli.reiniciar_sesion(link["session_name"])
    except (WahaError, ClaveAdminAusente, LookupError):
        raise VinculoRechazado(502, "waha_error")
    async with ctx.db.tenant_tx(tenant_id) as con:
        await con.execute("UPDATE links SET qr_reinicios = qr_reinicios + 1, waha_status = NULL, "
                          "ultimo_status_at = now(), updated_at = now() WHERE id = $1", link["id"])
        await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=actor_user_id, actor_rol=actor_rol,
                                  accion="qr_reiniciado", tipo_objeto="link", objeto_id=link["id"], ip=ip)
    return await estado_de_linea(ctx, tenant_id=tenant_id, line_id=line_id)


async def pedir_codigo(ctx: RadarContexto, *, tenant_id: uuid.UUID, line_id: uuid.UUID, telefono: str,
                       actor_user_id: Optional[uuid.UUID], actor_rol: str, ip: Optional[str]) -> str:
    """Código de vinculación (P3). Ni el teléfono ni el código se guardan o se loguean."""
    try:
        e164 = normalizar_e164(telefono)
    except TelefonoNoSoportado:
        raise VinculoRechazado(422, "telefono_invalido")
    link = await _link_en_qr(ctx, tenant_id, line_id)
    try:
        worker = await leer_worker(ctx, link["worker_id"])
        async with cliente_de(ctx, worker) as cli:
            codigo = await cli.pedir_codigo(link["session_name"], e164[1:])
    except (WahaError, ClaveAdminAusente, LookupError):
        codigo = None
    if codigo is None:
        raise VinculoRechazado(502, "codigo_no_disponible")
    async with ctx.db.tenant_tx(tenant_id) as con:
        await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=actor_user_id, actor_rol=actor_rol,
                                  accion="codigo_solicitado", tipo_objeto="link", objeto_id=link["id"], ip=ip)
    return codigo


async def pedir_fin(ctx: RadarContexto, *, tenant_id: uuid.UUID, line_id: uuid.UUID,
                    causa: Literal["pedido_kis", "pedido_dueno"], borrar: bool, actor_user_id: Optional[uuid.UUID],
                    actor_rol: str, ip: Optional[str]) -> dict:
    """Desconectar (o desconectar y borrar). La parte de WAHA la hace el job de
    fin; el borrado de nuestra base lo toma el tramo 6 desde borrado_solicitado_at."""
    if causa not in ("pedido_kis", "pedido_dueno"):
        raise ValueError("causa de fin pedido inválida")
    async with ctx.db.tenant_tx(tenant_id) as con:
        if not await con.fetchval("SELECT EXISTS (SELECT 1 FROM lines WHERE id = $1)", line_id):
            raise VinculoRechazado(404, "linea_inexistente")
        vivos = await con.fetch("SELECT id FROM links WHERE line_id = $1 AND estado = ANY($2::text[]) "
                                "ORDER BY created_at", line_id, list(VIVOS))
        if not vivos and not borrar:
            raise VinculoRechazado(409, "sin_vinculo")
        for fila in vivos:
            await con.execute("UPDATE links SET fin_causa = COALESCE(fin_causa, $2), updated_at = now() WHERE id = $1",
                              fila["id"], causa)
            await cola.encolar(con, tipo="fin_vinculo", link_id=fila["id"], causa=causa)
        if borrar:
            await con.execute("UPDATE lines SET borrado_solicitado_at = COALESCE(borrado_solicitado_at, now()), "
                              "updated_at = now() WHERE id = $1", line_id)
        await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=actor_user_id, actor_rol=actor_rol,
                                  accion="borrado_pedido" if borrar else "desconexion_pedida", tipo_objeto="line",
                                  objeto_id=line_id, ip=ip, detalle={"causa": causa})
    return {"vinculos": [str(f["id"]) for f in vivos], "borrado_solicitado": borrar}


async def marcar_restriccion(ctx: RadarContexto, *, tenant_id: uuid.UUID, line_id: uuid.UUID,
                             hasta: Optional[datetime], actor_user_id: Optional[uuid.UUID], actor_rol: str,
                             ip: Optional[str]) -> dict:
    """Restricción de cuenta (Estados especiales). Sin fecha: la levanta un admin de KIS."""
    async with ctx.db.tenant_tx(tenant_id) as con:
        _, _, link, _ = await _leer_estado(con, line_id)
        if link is None:
            raise VinculoRechazado(409, "sin_vinculo")   # 409 como en pedir_fin: la línea existe
        await con.execute("UPDATE links SET restriccion_hasta = $2, restriccion_sin_fecha = ($2::timestamptz IS NULL), "
                          "updated_at = now() WHERE id = $1", link["id"], hasta)
        await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=actor_user_id, actor_rol=actor_rol,
                                  accion="restriccion_marcada", tipo_objeto="link", objeto_id=link["id"], ip=ip)
    return await estado_de_linea(ctx, tenant_id=tenant_id, line_id=line_id)


async def levantar_restriccion(ctx: RadarContexto, *, tenant_id: uuid.UUID, line_id: uuid.UUID,
                               actor_user_id: Optional[uuid.UUID], actor_rol: str, ip: Optional[str]) -> dict:
    async with ctx.db.tenant_tx(tenant_id) as con:
        await _leer_estado(con, line_id)
        await con.execute("UPDATE links SET restriccion_hasta = NULL, restriccion_sin_fecha = FALSE, "
                          "updated_at = now() WHERE line_id = $1", line_id)
        await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=actor_user_id, actor_rol=actor_rol,
                                  accion="restriccion_levantada", tipo_objeto="line", objeto_id=line_id, ip=ip)
    return await estado_de_linea(ctx, tenant_id=tenant_id, line_id=line_id)
