"""
Operadores del backoffice y pedidos con cliente / quién lo hizo (6/10).
"""
import pytest

from app.services import operadores_service as ops
from app.services.db import Database


@pytest.fixture
async def db(pg_dsn):
    d = Database(pg_dsn)
    assert await d.connect()
    await d.execute("DELETE FROM operadores")
    yield d
    await d.execute("DELETE FROM operadores")
    ops._CANON.clear()
    await d.close()


async def test_alta_renombrar_y_fusionar(db):
    lore = await ops.crear(db, "Lore")
    lorena = await ops.crear(db, " lorena ")
    dos = await ops.crear(db, "02")
    with pytest.raises(ValueError):
        await ops.crear(db, "LORE")

    # "02" es María: el nombre viejo queda como alias
    maria = await ops.actualizar(db, dos["id"], nombre="María")
    assert maria["nombre"] == "María" and "02" in maria["aliases"]
    assert ops.canonico("02") == "María"

    # "lorena" es la misma que "Lore"
    f = await ops.fusionar(db, lorena["id"], lore["id"])
    assert "lorena" in f["aliases"]
    assert ops.canonico("LORENA") == "Lore"
    assert [o["nombre"] for o in await ops.listar(db)] == ["Lore", "María"]

    # desconocido: se deja tal cual
    assert ops.canonico("  Juan  Pérez ") == "Juan Pérez"


async def test_baja_y_actividad(db):
    cl = await ops.crear(db, "claudia", aliases=["Clau"])
    await db.execute("INSERT INTO eventos (tipo, phone, ref) VALUES ('conversacion_tomada', '549', 'Clau')")
    act = await ops.actividad(db)
    assert act[0]["nombre"] == "claudia" and act[0]["acciones_hoy"] >= 1 and act[0]["ultima_actividad"]
    await ops.actualizar(db, cl["id"], activo=False)
    assert await ops.listar(db) == []
    assert len(await ops.listar(db, todos=True)) == 1
    await db.execute("DELETE FROM eventos WHERE phone = '549'")


async def test_pedido_con_cliente_y_quien_lo_hizo(monkeypatch):
    from app.routers import orders_api
    from app.routers import backoffice as bo
    monkeypatch.setattr(bo, "_datos_cliente", lambda p: {"nombre": "Muff Claudia", "tipo_cliente": "empleado"})
    ops._CANON.update({"lore": "Lore", "lorena": "Lore"})
    d = orders_api._con_personas({"phone": "549", "origen": "operador", "armado_por": "lorena"})
    assert d["cliente"] == {"nombre": "Muff Claudia", "tipo": "empleado", "nro_socio": None}
    assert d["hecho_por"] == "Lore" and d["atendido_por"] == "Lore"
    d2 = orders_api._con_personas({"phone": "549", "atendido_por": "claudia"})
    assert d2["hecho_por"] == "Bot" and d2["atendido_por"] == "claudia"
    ops._CANON.clear()


async def test_cotizacion_guarda_quien_cotizo():
    from app.services.session_service import SessionService
    ss = SessionService("redis://127.0.0.1:1")
    await ss.armar_cotizacion("5490000000444", sku_id="A", sku_nombre="A", precio=10.0,
                              delegar=True, agente="claudia")
    assert (await ss.get("5490000000444"))["_cotizado_por"] == "claudia"
