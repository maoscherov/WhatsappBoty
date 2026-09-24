"""
Chequeo de salud de cada vínculo vivo, cada 5 min (Estados especiales:
"session.status + chequeo de salud cada 5 min").

Lee el estado real en WAHA y lo aplica con la misma máquina de estados que el
webhook. Después decide si corresponde encolar el fin del vínculo:
- caído hace más de 72 h (dispositivo quitado desde el teléfono, teléfono
  inactivo): el fin corre a las 72 h si no hubo reconexión antes;
- en esperando_qr hace más de 30 min (QR abandonado);
- duracion_vinculo_dias > 0 cumplida desde conectado_at.
Un vínculo que quedó en 'creando' más de 15 min (el proceso murió entre el
INSERT y WAHA) se aborta si la sesión no existe o se cierra si existe.
Un error de red con WAHA no cambia nada: se reintenta en el próximo chequeo.
La "sospecha de silencio" necesita tráfico y llega en el tramo 3.
"""

import logging
from datetime import timedelta
from typing import Literal

from app.radar import auditoria
from app.radar import jobs as cola
from app.radar.contexto import RadarContexto
from app.radar.jobs import Job
from app.radar.vinculo_estados import CAIDA_MAXIMA, PATRON_STATUS, QR_ABANDONADO, VIVOS
from app.radar.vinculos import aplicar_status
from app.radar.waha.cliente import WahaError
from app.radar.workers import ClaveAdminAusente, cliente_de, leer_worker

logger = logging.getLogger("app.radar.salud")

INTERVALO_S = 300
# Un vínculo que sigue en 'creando' después de esto quedó huérfano (la creación
# real tarda segundos: INSERT, POST /api/sessions, verificación y clave).
CREANDO_HUERFANO = timedelta(minutes=15)


async def ejecutar(ctx: RadarContexto, job: Job) -> Literal["reprogramar", "hecho"]:
    async with ctx.db.tenant_tx(job.tenant_id) as con:
        link = await con.fetchrow("SELECT id, worker_id, session_name, estado, now() - created_at AS edad "
                                  "FROM links WHERE id = $1", job.link_id)
    if link is None or link["estado"] not in VIVOS:
        return "hecho"
    if link["estado"] == "creando" and link["edad"] <= CREANDO_HUERFANO:
        return "reprogramar"          # iniciar_vinculo puede estar hablando con WAHA: no se toca
    try:
        worker = await leer_worker(ctx, link["worker_id"])
        async with cliente_de(ctx, worker) as cli:
            sesion = await cli.leer_sesion(link["session_name"])
    except (WahaError, ClaveAdminAusente, LookupError) as e:
        logger.warning("chequeo de salud del vínculo %s: WAHA no respondió (%s)", link["id"], type(e).__name__)
        return "reprogramar"
    if link["estado"] == "creando":
        return await _cerrar_creando_huerfano(ctx, job, link["id"], sesion_existe=sesion is not None)
    status = sesion["status"] if sesion else "AUSENTE"
    if not isinstance(status, str) or not PATRON_STATUS.match(status):
        status = "DESCONOCIDO"
    async with ctx.db.tenant_tx(job.tenant_id) as con:
        await con.execute("UPDATE links SET ultimo_chequeo_at = now() WHERE id = $1", link["id"])
        await aplicar_status(con, tenant_id=job.tenant_id, link_id=link["id"], waha_status=status, origen="salud",
                             me_id=sesion["me_id"] if sesion else None)
        f = await con.fetchrow(
            "SELECT l.estado, l.caido_desde, l.conectado_at, l.created_at, li.duracion_vinculo_dias, now() AS ahora "
            "FROM links l JOIN lines li ON li.id = l.line_id WHERE l.id = $1", link["id"])
        causa = None
        if f["estado"] == "caido" and f["caido_desde"] and f["ahora"] - f["caido_desde"] > CAIDA_MAXIMA:
            causa = "caida_72h"
        elif f["estado"] == "esperando_qr" and f["ahora"] - f["created_at"] > QR_ABANDONADO:
            causa = "qr_abandonado"
        elif (f["estado"] == "vinculado" and f["duracion_vinculo_dias"] > 0 and f["conectado_at"]
              and f["ahora"] - f["conectado_at"] > timedelta(days=f["duracion_vinculo_dias"])):
            causa = "duracion"
        if causa:
            await con.execute("UPDATE links SET fin_causa = COALESCE(fin_causa, $2) WHERE id = $1", link["id"], causa)
            await cola.encolar(con, tipo="fin_vinculo", link_id=link["id"], causa=causa)
    return "reprogramar" if f["estado"] in VIVOS else "hecho"


async def _cerrar_creando_huerfano(ctx: RadarContexto, job: Job, link_id, *, sesion_existe: bool) -> Literal["hecho"]:
    """Un vínculo en 'creando' con más de CREANDO_HUERFANO: el proceso murió
    entre el INSERT y WAHA. Sin sesión en WAHA -> abortado (libera la línea: el
    índice de un activo por línea ya no lo cuenta). Con sesión -> fin de vínculo
    con causa qr_abandonado, que la borra con un único DELETE."""
    async with ctx.db.tenant_tx(job.tenant_id) as con:
        if sesion_existe:
            await con.execute("UPDATE links SET fin_causa = COALESCE(fin_causa, 'qr_abandonado'), updated_at = now() "
                              "WHERE id = $1 AND estado = 'creando'", link_id)
            await cola.encolar(con, tipo="fin_vinculo", link_id=link_id, causa="qr_abandonado")
        else:
            cambiado = await con.execute("UPDATE links SET estado = 'abortado', cerrado_at = now(), updated_at = now() "
                                         "WHERE id = $1 AND estado = 'creando'", link_id)
            if cambiado == "UPDATE 1":
                await auditoria.registrar(con, tenant_id=job.tenant_id, actor_user_id=None, actor_rol="sistema",
                                          accion="vinculo_abortado", tipo_objeto="link", objeto_id=link_id,
                                          detalle={"motivo": "waha_error"})
        logger.warning("vínculo %s huérfano en 'creando': %s", link_id,
                       "fin encolado" if sesion_existe else "abortado")
    return "hecho"
