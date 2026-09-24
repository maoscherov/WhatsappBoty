"""Chequeo de salud (Estados especiales): estado real, caída > 72 h, QR abandonado y duración."""
import uuid

from app.radar import salud
from app.radar.jobs import Job
from app.radar.waha.sesion import nombre_sesion
from app.radar.workers import listar_workers

from .helpers import como_superusuario, crear_link_directo, escenario_vinculable, vincular_de_prueba


def _job(esc, link_id):
    return Job(id=uuid.uuid4(), tenant_id=esc["tenant_id"], tipo="chequeo_salud", link_id=link_id, causa=None,
               intentos=1)


async def _sql(ctx, esc, sql, *args):
    async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
        return await con.fetchrow(sql, *args)


async def _fin_encolado(ctx, esc, link_id):
    fila = await _sql(ctx, esc, "SELECT causa FROM jobs WHERE link_id = $1 AND tipo = 'fin_vinculo'", link_id)
    return fila["causa"] if fila else None


async def test_vinculo_sano_se_reprograma(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    assert await salud.ejecutar(ctx_waha, _job(esc, v["link_id"])) == "reprogramar"
    link = await _sql(ctx_waha, esc, "SELECT estado, ultimo_chequeo_at FROM links WHERE id = $1", v["link_id"])
    assert link["estado"] == "vinculado" and link["ultimo_chequeo_at"] is not None
    ev = await _sql(ctx_waha, esc, "SELECT origen FROM link_status_events ORDER BY id DESC LIMIT 1")
    assert ev["origen"] == "salud"


async def test_sesion_ausente_marca_caida(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    del waha.sesiones[v["session_name"]]
    await salud.ejecutar(ctx_waha, _job(esc, v["link_id"]))
    link = await _sql(ctx_waha, esc, "SELECT estado, caido_desde, waha_status FROM links WHERE id = $1", v["link_id"])
    assert (link["estado"], link["waha_status"]) == ("caido", "AUSENTE") and link["caido_desde"] is not None


async def test_caida_de_mas_de_72_horas_encola_el_fin(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    waha.sesiones[v["session_name"]]["status"] = "FAILED"
    await _sql(ctx_waha, esc, "UPDATE links SET estado = 'caido', caido_desde = now() - interval '73 hours' "
                              "WHERE id = $1", v["link_id"])
    assert await salud.ejecutar(ctx_waha, _job(esc, v["link_id"])) == "reprogramar"
    assert await _fin_encolado(ctx_waha, esc, v["link_id"]) == "caida_72h"


async def test_caida_reciente_no_encola(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    waha.sesiones[v["session_name"]]["status"] = "FAILED"
    await _sql(ctx_waha, esc, "UPDATE links SET estado = 'caido', caido_desde = now() - interval '1 hour' "
                              "WHERE id = $1", v["link_id"])
    await salud.ejecutar(ctx_waha, _job(esc, v["link_id"]))
    assert await _fin_encolado(ctx_waha, esc, v["link_id"]) is None


async def test_qr_abandonado_encola_el_fin(ctx_waha, waha, radar_urls):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc, working=False)
    await como_superusuario(radar_urls, "UPDATE links SET created_at = now() - interval '31 minutes' WHERE id = $1",
                            v["link_id"])
    await salud.ejecutar(ctx_waha, _job(esc, v["link_id"]))
    assert await _fin_encolado(ctx_waha, esc, v["link_id"]) == "qr_abandonado"


async def test_duracion_del_vinculo_cumplida_encola_el_fin(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    await _sql(ctx_waha, esc, "UPDATE lines SET duracion_vinculo_dias = 1 WHERE id = $1", esc["line_id"])
    await _sql(ctx_waha, esc, "UPDATE links SET conectado_at = now() - interval '2 days' WHERE id = $1", v["link_id"])
    await salud.ejecutar(ctx_waha, _job(esc, v["link_id"]))
    assert await _fin_encolado(ctx_waha, esc, v["link_id"]) == "duracion"


async def test_error_de_red_no_cambia_el_estado(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    waha.falla_leer = True
    assert await salud.ejecutar(ctx_waha, _job(esc, v["link_id"])) == "reprogramar"
    link = await _sql(ctx_waha, esc, "SELECT estado FROM links WHERE id = $1", v["link_id"])
    assert link["estado"] == "vinculado"


async def test_vinculo_cerrado_termina_el_chequeo(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    await _sql(ctx_waha, esc, "UPDATE links SET estado = 'cerrado' WHERE id = $1", v["link_id"])
    waha.llamadas.clear()
    assert await salud.ejecutar(ctx_waha, _job(esc, v["link_id"])) == "hecho"
    assert waha.llamadas == []


async def _creando(ctx, esc):
    """Vínculo que quedó en 'creando' (el proceso murió entre el INSERT y WAHA)."""
    [w] = await listar_workers(ctx)
    return await crear_link_directo(ctx.db, esc["tenant_id"], esc["line_id"], w.id, esc["consent_id"],
                                    estado="creando")


async def test_creando_huerfano_sin_sesion_se_aborta_y_libera_la_linea(ctx_waha, waha, radar_urls):
    esc = await escenario_vinculable(ctx_waha)
    k = await _creando(ctx_waha, esc)
    waha.llamadas.clear()
    assert await salud.ejecutar(ctx_waha, _job(esc, k)) == "reprogramar"      # reciente: puede estar creándose
    assert waha.llamadas == []
    await como_superusuario(radar_urls, "UPDATE links SET created_at = now() - interval '16 minutes' WHERE id = $1", k)
    assert await salud.ejecutar(ctx_waha, _job(esc, k)) == "hecho"
    link = await _sql(ctx_waha, esc, "SELECT estado, cerrado_at FROM links WHERE id = $1", k)
    assert link["estado"] == "abortado" and link["cerrado_at"] is not None
    audit = await _sql(ctx_waha, esc, "SELECT detalle::text AS d FROM access_audit_log WHERE accion = 'vinculo_abortado'")
    assert "waha_error" in audit["d"]
    v = await vincular_de_prueba(ctx_waha, waha, esc)                          # la línea ya no está bloqueada
    assert v["link_id"] != k


async def test_creando_huerfano_con_sesion_encola_el_fin(ctx_waha, waha, radar_urls):
    esc = await escenario_vinculable(ctx_waha)
    k = await _creando(ctx_waha, esc)
    n = nombre_sesion(k)
    waha.sesiones[n] = {"name": n, "status": "SCAN_QR_CODE", "engine": "NOWEB", "config": {}, "me_id": None}
    await como_superusuario(radar_urls, "UPDATE links SET created_at = now() - interval '16 minutes' WHERE id = $1", k)
    assert await salud.ejecutar(ctx_waha, _job(esc, k)) == "hecho"
    assert await _fin_encolado(ctx_waha, esc, k) == "qr_abandonado"
