"""
RLS efectiva, no declarativa (§6.4). Todos los tests corren como radar_app.
"""
import uuid

import asyncpg
import pytest

from app.radar.constantes import TENANT_KIS
from app.radar.db import RadarDB


async def _crear_tenant(db: RadarDB, nombre: str) -> uuid.UUID:
    tid = uuid.uuid4()
    async with db.tenant_tx(tid) as con:
        await con.fetchval(
            "SELECT radar_admin_crear_tenant($1, $2, 'farmacia', 'estandar', 12, FALSE, TRUE, 'lotes')",
            tid, nombre,
        )
    return tid


async def test_connect_rechaza_superusuario(radar_urls):
    db = RadarDB(radar_urls["super"])
    with pytest.raises(RuntimeError, match="BYPASSRLS|superusuario"):
        await db.connect()


async def test_tenant_tx_solo_acepta_uuid(radar_db):
    with pytest.raises(TypeError):
        async with radar_db.tenant_tx("no-es-uuid"):
            pass


async def test_sin_tenant_cero_filas(radar_db):
    await _crear_tenant(radar_db, "A")
    async with radar_db.sin_tenant() as con:
        assert await con.fetchval("SELECT count(*) FROM tenants") == 0


async def test_conexion_devuelta_al_pool_no_conserva_tenant(radar_urls):
    db = RadarDB(radar_urls["app"], max_size=1)   # una sola conexión: la misma vuelve
    await db.connect()
    try:
        a = await _crear_tenant(db, "A")
        async with db.tenant_tx(a) as con:
            assert await con.fetchval("SELECT count(*) FROM tenants") == 1
        async with db.pool.acquire() as con:
            assert await con.fetchval("SELECT current_setting('app.tenant_id', true)") in ("", None)
            assert await con.fetchval("SELECT count(*) FROM tenants") == 0
    finally:
        await db.close()


async def test_tenant_a_no_lee_ni_toca_b(radar_db):
    a = await _crear_tenant(radar_db, "A")
    b = await _crear_tenant(radar_db, "B")
    async with radar_db.tenant_tx(a) as con:
        nombres = [r["nombre"] for r in await con.fetch("SELECT nombre FROM tenants")]
        assert nombres == ["A"]
        # SQL directo con el id de B: RLS lo vuelve invisible
        assert await con.fetchval("SELECT count(*) FROM tenants WHERE id = $1", b) == 0
        assert await con.execute("UPDATE tenants SET nombre = 'hackeado' WHERE id = $1", b) == "UPDATE 0"
    async with radar_db.tenant_tx(b) as con:
        assert await con.fetchval("SELECT nombre FROM tenants WHERE id = $1", b) == "B"


async def test_radar_app_no_puede_insertar_tenants_directo(radar_db):
    tid = uuid.uuid4()
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with radar_db.tenant_tx(tid) as con:
            await con.execute("INSERT INTO tenants (id, nombre) VALUES ($1, 'x')", tid)


async def test_radar_app_no_puede_asumir_radar_admin(radar_db):
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with radar_db.sin_tenant() as con:
            await con.execute("SET ROLE radar_admin")


async def test_listar_tenants_via_funcion_admin_excluye_kis(radar_db):
    await _crear_tenant(radar_db, "A")
    await _crear_tenant(radar_db, "B")
    async with radar_db.sin_tenant() as con:
        filas = await con.fetch("SELECT * FROM radar_admin_listar_tenants()")
    assert sorted(r["nombre"] for r in filas) == ["A", "B"]
    assert TENANT_KIS not in {r["id"] for r in filas}


async def test_tenant_kis_sembrado(radar_db):
    async with radar_db.tenant_tx(TENANT_KIS) as con:
        assert await con.fetchval("SELECT es_kis FROM tenants WHERE id = $1", TENANT_KIS) is True


def test_radar_no_usa_el_db_del_bot():
    """execute()/fetch() de app/services/db.py tragan errores y toman una
    conexión cualquiera del pool: prohibidos en Radar (§6.4)."""
    import pathlib
    raiz = pathlib.Path(__file__).resolve().parents[2] / "app" / "radar"
    for archivo in raiz.rglob("*.py"):
        assert "app.services.db" not in archivo.read_text(encoding="utf-8"), archivo


async def test_radar_app_no_puede_marcarse_es_kis(radar_db):
    a = await _crear_tenant(radar_db, "A")
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        async with radar_db.tenant_tx(a) as con:
            await con.execute("UPDATE tenants SET es_kis = TRUE WHERE id = $1", a)


async def test_radar_app_actualiza_nombre_de_su_tenant(radar_db):
    a = await _crear_tenant(radar_db, "A")
    async with radar_db.tenant_tx(a) as con:
        assert await con.execute("UPDATE tenants SET nombre = 'A2' WHERE id = $1", a) == "UPDATE 1"
    async with radar_db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT nombre FROM tenants WHERE id = $1", a) == "A2"
