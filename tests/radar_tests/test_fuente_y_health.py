"""
Almacén de fuente separado (§6.4): en este tramo solo el permanente, sin
tablas de conversación, con marcador de esquema y health check.
"""
import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

from app.radar.app import crear_app_radar, validar_settings
from app.radar.contexto import RadarContexto
from app.radar.fuente import ALMACENES_DISPONIBLES, FuenteStore
from app.radar.settings import RadarSettings


def test_solo_existe_el_almacen_permanente():
    assert ALMACENES_DISPONIBLES == frozenset({"permanente"})


async def test_fuente_conecta_y_reporta_almacen(radar_urls):
    fuente = FuenteStore(radar_urls["fuente"])
    await fuente.connect()
    try:
        assert fuente.almacen == "permanente"
        assert await fuente.salud() == {"ok": True, "almacen": "permanente"}
    finally:
        await fuente.close()


async def test_fuente_sin_tablas_de_conversacion(radar_urls):
    con = await asyncpg.connect(radar_urls["fuente"])
    try:
        tablas = {r["table_name"] for r in await con.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
    finally:
        await con.close()
    assert tablas == {"fuente_meta", "alembic_version_fuente"}


async def test_resultados_no_usa_la_tabla_de_version_del_bot(radar_urls):
    con = await asyncpg.connect(radar_urls["migrator"])
    try:
        tablas = {r["table_name"] for r in await con.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
    finally:
        await con.close()
    assert "alembic_version_radar" in tablas
    assert "alembic_version" not in tablas


def test_validar_settings_exige_las_urls_y_el_secreto():
    with pytest.raises(RuntimeError, match="database_url"):
        validar_settings(RadarSettings(_env_file=None))
    # Un secreto corto firma sesiones de 30 días con HMAC sobre casi nada: no arranca.
    with pytest.raises(RuntimeError, match="32"):
        validar_settings(RadarSettings(_env_file=None, database_url="x", migrator_database_url="x",
                                       fuente_database_url="x", cookie_secret="corto"))
    validar_settings(RadarSettings(_env_file=None, database_url="x", migrator_database_url="x",
                                   fuente_database_url="x", cookie_secret="secreto-de-test-de-32-caracteres!"))


async def test_health_ok_con_contexto(radar_ctx):
    app = crear_app_radar(radar_ctx.settings, contexto=radar_ctx)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        r = await c.get("/health")
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["status"] == "ok"
    assert cuerpo["resultados"] == {"ok": True}
    assert cuerpo["fuente"] == {"ok": True, "almacen": "permanente"}


async def test_health_degradado_si_la_fuente_cae(radar_ctx):
    await radar_ctx.fuente.close()
    app = crear_app_radar(radar_ctx.settings, contexto=radar_ctx)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        r = await c.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "degradado"
    assert r.json()["fuente"]["ok"] is False


async def test_lifespan_migra_y_arma_el_contexto(radar_urls, tmp_path):
    rs = RadarSettings(
        _env_file=None,
        database_url=radar_urls["app"],
        migrator_database_url=radar_urls["migrator"],
        fuente_database_url=radar_urls["fuente"],
        cookie_secret="secreto-de-test-de-32-caracteres!",
        secrets_dir=str(tmp_path / "secretos"),
    )
    app = crear_app_radar(rs)          # sin contexto: lo arma el lifespan
    async with app.router.lifespan_context(app):
        assert isinstance(app.state.radar, RadarContexto)
        assert app.state.radar.fuente.almacen == "permanente"
    with pytest.raises(RuntimeError):   # cerrado al salir
        app.state.radar.db.pool
