"""
Job de fin de vínculo (§6.3 punto 6). Idempotente y auditado; lo disparan
las cinco causas de §2.2 (duración, pedido, caída > 72 h, migración, baja),
más el reemplazo por un vínculo nuevo y el QR abandonado.

1. Marca el vínculo `cerrando`: desde ahí el receptor ignora sus session.status.
2. Si la sesión está STOPPED/FAILED y NO hay restricción activa, intenta start
   y espera hasta 3 min (el desvínculo del lado de WhatsApp solo ocurre con la
   sesión WORKING).
3. Un único DELETE /api/sessions/{name}. Nunca logout.
4. Borra las claves de esa sesión y verifica 404 y cero claves.
5. Borra la clave de lectura local, registra el resultado y avisa al dueño.

Un paso fallido no bloquea los siguientes: si al final la sesión no quedó
borrada o quedan claves, el vínculo sigue `cerrando` y el job se reintenta.
Nuestra base no se toca (tramo 6).
"""

import json
import logging
from datetime import timedelta, timezone
from typing import Literal

from app.radar import auditoria, eventos_producto
from app.radar.contexto import RadarContexto
from app.radar.jobs import Job
from app.radar.mailer import Email
from app.radar.vinculo_estados import ACTIVOS, restriccion_activa
from app.radar.waha.gestor import terminar_sesion
from app.radar.workers import cliente_de, leer_worker, nombre_clave_lectura

logger = logging.getLogger("app.radar.fin_vinculo")

ESPERA_START_S = 180.0
INTERVALO_START_S = 5.0
SIN_AVISO = ("reemplazado", "qr_abandonado")
ASUNTO_AVISO = "Tu WhatsApp se desconectó de Radar"
TEXTO_AVISO = (
    "Desconectamos una línea de tu cuenta de Radar y borramos la sesión de conexión y sus credenciales.\n\n"
    "Revisá WhatsApp → Dispositivos vinculados. Si todavía ves este dispositivo, quitalo desde ahí. "
    "Las credenciales ya fueron destruidas y no se puede volver a usar.\n\n"
    "Conservamos el tablero y las conversaciones ya importadas según la configuración de la línea. "
    "Para borrarlas, usá «Desconectar y borrar todo».\n\n{url}\n"
)
# Aviso de caída (Estados especiales): solo la fecha, ningún dato de conversación.
HORA_ARGENTINA = timezone(timedelta(hours=-3))   # sin horario de verano; no depende de tzdata
ASUNTO_CAIDA = "Tu WhatsApp se desconectó de Radar"
TEXTO_CAIDA = (
    "Tu WhatsApp se desconectó de Radar el {fecha}.\n\n"
    "Entrá a Radar y tocá «Reconectar» para volver a vincularlo. Si no se reconecta en 72 horas, "
    "cerramos el vínculo y borramos la sesión de conexión.\n\n{url}\n"
)


class FinIncompleto(RuntimeError):
    pass


async def ejecutar(ctx: RadarContexto, job: Job, *, espera_start_s: float = ESPERA_START_S,
                   intervalo_s: float = INTERVALO_START_S) -> Literal["hecho"]:
    async with ctx.db.tenant_tx(job.tenant_id) as con:
        link = await con.fetchrow(
            "SELECT id, line_id, worker_id, session_name, estado, restriccion_hasta, restriccion_sin_fecha, fin_causa "
            "FROM links WHERE id = $1 FOR UPDATE", job.link_id)
        if link is None or link["estado"] in ("cerrado", "abortado"):
            return "hecho"
        causa = link["fin_causa"] or job.causa or "pedido_kis"
        await con.execute("UPDATE links SET estado = 'cerrando', fin_causa = $2, updated_at = now() WHERE id = $1",
                          link["id"], causa)
        ahora = await con.fetchval("SELECT now()")

    restringida = restriccion_activa(link["restriccion_hasta"], link["restriccion_sin_fecha"], ahora)
    worker = await leer_worker(ctx, link["worker_id"])
    async with cliente_de(ctx, worker) as cli:
        res = await terminar_sesion(cli, link["session_name"], intentar_start=not restringida,
                                    espera_start_s=espera_start_s, intervalo_s=intervalo_s)
    ctx.secretos.delete(nombre_clave_lectura(link["id"]))
    res["causa"] = causa
    ok = bool(res["ok"])

    duenos: list[str] = []
    async with ctx.db.tenant_tx(job.tenant_id) as con:
        await con.execute(
            """
            UPDATE links SET fin_resultado = $2::jsonb,
                   desvinculo_confirmado = COALESCE(desvinculo_confirmado, FALSE) OR $3,
                   estado = CASE WHEN $4 THEN 'cerrado' ELSE estado END,
                   cerrado_at = CASE WHEN $4 THEN now() ELSE cerrado_at END,
                   updated_at = now()
             WHERE id = $1
            """, link["id"], json.dumps(res), bool(res["desvinculo_confirmado"]), ok)
        if ok:
            activos = await con.fetchval("SELECT count(*) FROM links WHERE line_id = $1 AND estado = ANY($2::text[])",
                                         link["line_id"], list(ACTIVOS))
            if activos == 0:
                await con.execute("UPDATE lines SET estado = 'sin_vinculo', updated_at = now() "
                                  "WHERE id = $1 AND estado = 'vinculada'", link["line_id"])
            await auditoria.registrar(con, tenant_id=job.tenant_id, actor_user_id=None, actor_rol="sistema",
                                      accion="vinculo_cerrado", tipo_objeto="link", objeto_id=link["id"],
                                      detalle={"causa": causa, "ok": True})
            if causa not in SIN_AVISO:
                duenos = await _emails_duenos(con)
    if not ok:
        logger.warning("ALERTA fin de vínculo %s incompleto: sesión borrada=%s, error de claves=%s",
                       link["id"], res["sesion_borrada"], res["error_claves"])
        raise FinIncompleto("fin de vínculo incompleto; se reintenta")
    # El vínculo ya está cerrado: un mail que falla no convierte el job en falla
    # (el reintento no lo reenviaría, porque encuentra el vínculo cerrado).
    url = ctx.settings.public_base_url.rstrip("/") + "/radar/"
    enviados = await _avisar(ctx, duenos, ASUNTO_AVISO, TEXTO_AVISO.format(url=url), link["id"])
    async with ctx.db.tenant_tx(job.tenant_id) as con:
        await eventos_producto.registrar_evento(
            con, tenant_id=job.tenant_id, evento="vinculo_cerrado", line_id=link["line_id"], objeto_id=link["id"],
            valores={"desvinculo_confirmado": 1 if res["desvinculo_confirmado"] else 0,
                     "aviso_enviado": 1 if enviados else 0})
    return "hecho"


async def _emails_duenos(con) -> list[str]:
    return [f["email"] for f in await con.fetch(
        "SELECT u.email FROM memberships m JOIN users u ON u.id = m.user_id WHERE m.rol = 'dueno'")]


async def _avisar(ctx: RadarContexto, destinatarios: list[str], asunto: str, texto: str, link_id) -> int:
    """Manda el aviso a cada destinatario sin relanzar (como enviar_link_seguro
    del tramo 1). Devuelve cuántos salieron. Solo se loguea el tipo de error."""
    enviados = 0
    for email in destinatarios:
        try:
            await ctx.mailer.enviar(Email(para=email, asunto=asunto, texto=texto, huella="sin-token"))
            enviados += 1
        except Exception as e:
            logger.warning("aviso del vínculo %s no enviado: %s", link_id, type(e).__name__)
    return enviados


async def avisar_caida(ctx: RadarContexto, job: Job) -> Literal["hecho"]:
    """Job aviso_caida (Estados especiales, "Vínculo caído"): email al dueño con
    la fecha de la caída. Si el vínculo ya se recuperó o está cerrando, no avisa."""
    async with ctx.db.tenant_tx(job.tenant_id) as con:
        link = await con.fetchrow("SELECT id, estado, caido_desde FROM links WHERE id = $1", job.link_id)
        if link is None or link["estado"] != "caido" or link["caido_desde"] is None:
            return "hecho"
        duenos = await _emails_duenos(con)
    fecha = link["caido_desde"].astimezone(HORA_ARGENTINA).strftime("%d/%m")
    url = ctx.settings.public_base_url.rstrip("/") + "/radar/"
    await _avisar(ctx, duenos, ASUNTO_CAIDA, TEXTO_CAIDA.format(fecha=fecha, url=url), link["id"])
    return "hecho"
