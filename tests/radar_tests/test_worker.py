"""Worker de la cola: despacha, completa, reprograma el chequeo, reintenta y para limpio."""
import asyncio

from app.radar import jobs as cola
from app.radar import worker
from app.radar.app import crear_app_radar
from app.radar.settings import RadarSettings
from app.radar.vinculos import aplicar_status, pedir_fin

from .helpers import escenario_vinculable, vincular_de_prueba


async def _sql(ctx, esc, sql, *args):
    async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
        return await con.fetchrow(sql, *args)


async def _pedir(ctx, esc):
    await pedir_fin(ctx, tenant_id=esc["tenant_id"], line_id=esc["line_id"], causa="pedido_kis", borrar=False,
                    actor_user_id=None, actor_rol="admin", ip=None)


async def test_correr_una_vez_despacha_el_fin_y_lo_completa(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    await _pedir(ctx_waha, esc)
    assert await worker.correr_una_vez(ctx_waha) == 1
    job = await _sql(ctx_waha, esc, "SELECT estado FROM jobs WHERE link_id = $1", v["link_id"])
    link = await _sql(ctx_waha, esc, "SELECT estado FROM links WHERE id = $1", v["link_id"])
    assert (job["estado"], link["estado"]) == ("hecho", "cerrado")


async def test_chequeo_de_salud_se_reprograma_a_5_minutos(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    assert await cola.programar_salud(ctx_waha.db) == 1
    await worker.correr_una_vez(ctx_waha)
    job = await _sql(ctx_waha, esc, "SELECT estado, intentos, ejecutar_desde - now() AS falta FROM jobs "
                                    "WHERE link_id = $1 AND tipo = 'chequeo_salud'", v["link_id"])
    assert (job["estado"], job["intentos"]) == ("pendiente", 0) and job["falta"].total_seconds() > 250


async def test_excepcion_en_el_handler_reintenta_con_backoff(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    await _pedir(ctx_waha, esc)
    waha.falla_claves = True
    await worker.correr_una_vez(ctx_waha)
    job = await _sql(ctx_waha, esc, "SELECT estado, intentos, ultimo_error FROM jobs WHERE link_id = $1",
                     v["link_id"])
    assert (job["estado"], job["intentos"], job["ultimo_error"]) == ("pendiente", 1, "FinIncompleto")


async def test_tipo_sin_handler_falla_sin_romper_el_ciclo(ctx_waha, waha, monkeypatch):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    await _pedir(ctx_waha, esc)
    monkeypatch.delitem(worker.HANDLERS, "fin_vinculo")
    assert await worker.correr_una_vez(ctx_waha) == 1
    job = await _sql(ctx_waha, esc, "SELECT estado, ultimo_error FROM jobs WHERE link_id = $1", v["link_id"])
    assert (job["estado"], job["ultimo_error"]) == ("pendiente", "LookupError")


async def test_aviso_de_caida_se_despacha_y_completa(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await aplicar_status(con, tenant_id=esc["tenant_id"], link_id=v["link_id"], waha_status="FAILED",
                             origen="webhook")
    antes = len(ctx_waha.mailer.enviados)
    assert await worker.correr_una_vez(ctx_waha) == 1
    job = await _sql(ctx_waha, esc, "SELECT estado FROM jobs WHERE link_id = $1 AND tipo = 'aviso_caida'",
                     v["link_id"])
    assert job["estado"] == "hecho" and len(ctx_waha.mailer.enviados) == antes + 1


async def test_bucle_programa_salud_y_para(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    parar = asyncio.Event()

    async def cortar():
        await asyncio.sleep(0.3)
        parar.set()

    await asyncio.gather(worker.bucle(ctx_waha, parar=parar, pausa_s=0.05, salud_cada_s=3600), cortar())
    job = await _sql(ctx_waha, esc, "SELECT count(*) AS n, min(estado) AS estado FROM jobs "
                                    "WHERE link_id = $1 AND tipo = 'chequeo_salud'", v["link_id"])
    assert (job["n"], job["estado"]) == (1, "pendiente")


async def test_lifespan_arranca_y_detiene_el_worker_embebido(radar_urls, tmp_path):
    rs = RadarSettings(_env_file=None, database_url=radar_urls["app"], migrator_database_url=radar_urls["migrator"],
                       fuente_database_url=radar_urls["fuente"], cookie_secret="secreto-de-test-de-32-caracteres!",
                       secrets_dir=str(tmp_path / "secretos"), worker_embebido=True)
    app = crear_app_radar(rs)
    async with app.router.lifespan_context(app):
        tarea = app.state.radar_worker
        await asyncio.sleep(0.05)
        assert tarea is not None and not tarea.done()
    assert tarea.done()
