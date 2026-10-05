"""Admisión por capacidad del worker (§6.2) y clave admin fuera de la base."""
import pathlib
import uuid

import pytest

from app.radar.constantes import TENANT_KIS
from app.radar.waha.cliente import WahaCliente
from app.radar.workers import (ClaveAdminAusente, ClaveIncorrecta, MotorDistinto, SinCapacidad, VerificacionFallida,
                               WahaNoResponde, Worker, actualizar_disco, admite, clave_cargada, cliente_de, elegir,
                               elegir_worker, listar_workers, nombre_clave_admin, registrar_worker,
                               reemplazar_clave, verificar_clave_worker)

from .helpers import (crear_consentimiento_directo, crear_link_directo, crear_linea_directa, crear_tenant_directo,
                      crear_usuario)


def _w(nombre="w", sesiones=0, max_s=50, usado=0.0, disco=10.0, activo=True):
    return Worker(id=uuid.uuid4(), nombre=nombre, base_url="http://w", engine="NOWEB", max_sesiones=max_s,
                  disco_max_gb=disco, disco_usado_gb=usado, activo=activo, sesiones=sesiones)


def test_admision_respeta_80_por_ciento_de_sesiones_y_70_de_disco():
    assert admite(_w(sesiones=39)) is True        # con la nueva: 40/50 = 80 %
    assert admite(_w(sesiones=40)) is False       # con la nueva: 41/50 > 80 %
    assert admite(_w(usado=7.0)) is True          # 70 %
    assert admite(_w(usado=7.1)) is False
    assert admite(_w(activo=False)) is False


def test_elegir_el_de_menor_ocupacion():
    assert elegir([_w("a", sesiones=30), _w("b", sesiones=10), _w("c", activo=False)]).nombre == "b"
    assert elegir([_w("d", usado=6.0), _w("e", sesiones=20)]).nombre == "e"   # d: 60 % de disco, e: 40 %


def test_sin_candidatos_devuelve_none():
    assert elegir([_w(sesiones=45), _w(usado=9.0)]) is None
    assert elegir([]) is None


async def test_registrar_guarda_la_clave_admin_fuera_de_la_base(radar_ctx):
    wid = await registrar_worker(radar_ctx, nombre="w1", base_url="http://waha.interno", engine="GOWS",
                                 max_sesiones=50, disco_max_gb=20, admin_key="clave-admin-larga-123")
    assert radar_ctx.secretos.get(nombre_clave_admin(wid)) == b"clave-admin-larga-123"
    async with radar_ctx.db.tenant_tx(TENANT_KIS) as con:
        fila = await con.fetchrow("SELECT * FROM waha_workers WHERE id = $1", wid)
        auditada = await con.fetchval(
            "SELECT count(*) FROM access_audit_log WHERE accion = 'worker_registrado' AND objeto_id = $1", wid)
    assert "clave-admin-larga-123" not in str(dict(fila)) and auditada == 1
    [w] = await listar_workers(radar_ctx)
    assert (w.nombre, w.engine, w.sesiones) == ("w1", "GOWS", 0)


async def test_elegir_worker_cuenta_sesiones_de_todos_los_tenants(radar_ctx):
    wid = await registrar_worker(radar_ctx, nombre="w1", base_url="http://waha.interno", engine="NOWEB",
                                 max_sesiones=5, disco_max_gb=10, admin_key="clave-admin-larga-123")
    assert (await elegir_worker(radar_ctx)).id == wid
    for nombre in ("A", "B"):
        t = await crear_tenant_directo(radar_ctx.db, nombre)
        u = await crear_usuario(radar_ctx.db, t, "dueno@cliente.com", "dueno")
        for i in range(2):
            li = await crear_linea_directa(radar_ctx.db, t, f"Línea {i}")
            c = await crear_consentimiento_directo(radar_ctx.db, t, li, u)
            await crear_link_directo(radar_ctx.db, t, li, wid, c, estado="vinculado")
    with pytest.raises(SinCapacidad):          # 4 sesiones: con una más serían 5/5 > 80 %
        await elegir_worker(radar_ctx)


async def test_cliente_de_sin_clave_admin_lanza(radar_ctx):
    wid = await registrar_worker(radar_ctx, nombre="w1", base_url="http://waha.interno", engine="NOWEB",
                                 max_sesiones=5, disco_max_gb=10, admin_key="clave-admin-larga-123")
    radar_ctx.secretos.delete(nombre_clave_admin(wid))
    [w] = await listar_workers(radar_ctx)
    with pytest.raises(ClaveAdminAusente):
        cliente_de(radar_ctx, w)


def test_la_clave_admin_solo_se_lee_en_workers():
    raiz = pathlib.Path(__file__).resolve().parents[2] / "app" / "radar"

    def con(texto: str) -> list[str]:
        return sorted(a.relative_to(raiz).as_posix() for a in raiz.rglob("*.py")
                      if texto in a.read_text(encoding="utf-8"))

    assert con("waha_admin:") == ["workers.py"]
    assert con("nombre_clave_admin(") == ["workers.py"]
    assert con("X-Api-Key") == ["waha/cliente.py"]


async def test_clave_cargada_es_solo_un_booleano(radar_ctx):
    wid = await registrar_worker(radar_ctx, nombre="w1", base_url="http://waha.interno", engine="NOWEB",
                                 max_sesiones=5, disco_max_gb=10, admin_key="clave-admin-larga-123")
    assert clave_cargada(radar_ctx, wid) is True
    radar_ctx.secretos.delete(nombre_clave_admin(wid))
    assert clave_cargada(radar_ctx, wid) is False


async def test_actualizar_disco_sin_actor_se_audita_como_sistema(radar_ctx):
    """scripts/radar_workers.py actualiza el disco sin un admin detrás: como registrar_worker, queda como `sistema`."""
    wid = await registrar_worker(radar_ctx, nombre="w1", base_url="http://waha.interno", engine="NOWEB",
                                 max_sesiones=5, disco_max_gb=10, admin_key="clave-admin-larga-123")
    await actualizar_disco(radar_ctx, wid, 3.5)
    async with radar_ctx.db.tenant_tx(TENANT_KIS) as con:
        [fila] = await con.fetch("SELECT actor_user_id, actor_rol, tipo_objeto, objeto_id FROM access_audit_log "
                                 "WHERE accion = 'worker_disco_actualizado'")
    assert (fila["actor_user_id"], fila["actor_rol"], fila["tipo_objeto"], fila["objeto_id"]) == \
           (None, "sistema", "waha_worker", wid)
    [w] = await listar_workers(radar_ctx)
    assert w.disco_usado_gb == 3.5


async def test_actualizar_disco_de_un_worker_inexistente_lanza_y_no_audita(radar_ctx):
    with pytest.raises(LookupError):
        await actualizar_disco(radar_ctx, uuid.uuid4(), 1.0)
    async with radar_ctx.db.tenant_tx(TENANT_KIS) as con:
        assert await con.fetchval("SELECT count(*) FROM access_audit_log") == 0


CLAVE = "clave-admin-larga-123"


async def test_verificar_clave_worker_devuelve_version_motor_y_tier(radar_ctx, waha):
    radar_ctx.waha_transport = waha.transporte()
    info = await verificar_clave_worker(radar_ctx, base_url="http://waha.interno", engine="NOWEB", admin_key=CLAVE)
    assert info == {"version": "2026.8.2", "engine": "NOWEB", "tier": "CORE"}


@pytest.mark.parametrize("atributo,valor,error", [
    ("version_status", 401, ClaveIncorrecta), ("version_status", 403, ClaveIncorrecta),
    ("version_status", 500, WahaNoResponde), ("version_sin_red", True, WahaNoResponde),
    ("version_crudo", b"esto no es JSON", WahaNoResponde), ("version_crudo", b"[1]", WahaNoResponde),
    ("version_engine", "GOWS", MotorDistinto)])
async def test_verificar_clave_worker_traduce_lo_que_contesta_waha(radar_ctx, waha, atributo, valor, error):
    radar_ctx.waha_transport = waha.transporte()
    setattr(waha, atributo, valor)
    with pytest.raises(error) as e:
        await verificar_clave_worker(radar_ctx, base_url="http://waha.interno", engine="NOWEB", admin_key=CLAVE)
    assert isinstance(e.value, VerificacionFallida) and e.value.codigo
    assert CLAVE not in str(e.value) and "waha.interno" not in str(e.value)      # ni la clave ni la URL en el error


@pytest.mark.parametrize("version_status", [200, 401])
async def test_verificar_clave_worker_cierra_el_cliente_salga_como_salga(radar_ctx, waha, monkeypatch,
                                                                          version_status):
    cerrados = []
    cerrar = WahaCliente.aclose

    async def contando(self):
        cerrados.append(1)
        await cerrar(self)

    monkeypatch.setattr(WahaCliente, "aclose", contando)
    radar_ctx.waha_transport = waha.transporte()
    waha.version_status = version_status
    try:
        await verificar_clave_worker(radar_ctx, base_url="http://waha.interno", engine="NOWEB", admin_key=CLAVE)
    except ClaveIncorrecta:
        pass
    assert cerrados == [1]


async def test_reemplazar_clave_que_no_se_puede_guardar_no_deja_auditoria(radar_ctx, waha, monkeypatch):
    """Si el volumen de secretos no deja escribir: la clave vieja sigue y no queda auditado un cambio que no ocurrió."""
    wid = await registrar_worker(radar_ctx, nombre="w1", base_url="http://waha.interno", engine="NOWEB",
                                 max_sesiones=5, disco_max_gb=10, admin_key=CLAVE)
    admin = await crear_usuario(radar_ctx.db, TENANT_KIS, "admin@keepitsimple.com.ar", "admin")
    radar_ctx.waha_transport = waha.transporte()

    def no_puede(nombre, valor):
        raise OSError("volumen de solo lectura")

    monkeypatch.setattr(radar_ctx.secretos, "set", no_puede)
    with pytest.raises(OSError):
        await reemplazar_clave(radar_ctx, wid, "clave-nueva-larga-9999", admin)
    assert radar_ctx.secretos.get(nombre_clave_admin(wid)) == CLAVE.encode()
    async with radar_ctx.db.tenant_tx(TENANT_KIS) as con:
        assert await con.fetchval("SELECT count(*) FROM access_audit_log "
                                  "WHERE accion = 'worker_clave_reemplazada'") == 0
