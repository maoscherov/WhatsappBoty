"""
Ciclo de vida de una sesión: creación verificada (aborta y borra si WAHA no
guardó lo pedido), clave de lectura, y fin con un único DELETE, borrado de
las claves de ESA sesión y verificación 404. Nunca logout.
"""
import uuid

import pytest

from app.radar.waha.cliente import WahaCliente
from app.radar.waha.gestor import (ConfigNoCoincide, crear_clave_lectura, crear_sesion_verificada,
                                   terminar_sesion)
from app.radar.waha.sesion import ACCIONES_CLAVE_LECTURA, cuerpo_sesion

from .waha_falso import WahaFalso

CUERPO = cuerpo_sesion(link_id=uuid.uuid4(), tenant_id=uuid.uuid4(), line_id=uuid.uuid4(), engine="NOWEB",
                       webhook_url="http://radar.interno/webhook/waha", hmac_key="h" * 32)
N = CUERPO["name"]
OTRA = "v_ffffffffffff"


def _cli(waha):
    return WahaCliente("http://waha.interno", "clave-admin", transport=waha.transporte())


async def test_crear_verificada_crea_y_relee():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        sesion = await crear_sesion_verificada(cli, CUERPO)
    assert sesion["name"] == N
    assert waha.llamadas == ["POST /api/sessions", f"GET /api/sessions/{N}"]


async def test_aborta_y_borra_si_mark_online_quedo_en_true():
    waha = WahaFalso()
    waha.mutar_eco = lambda c: c["noweb"].__setitem__("markOnline", True)
    async with _cli(waha) as cli:
        with pytest.raises(ConfigNoCoincide) as e:
            await crear_sesion_verificada(cli, CUERPO)
    assert e.value.problemas == ["noweb.markOnline"]
    assert waha.llamadas[-1] == f"DELETE /api/sessions/{N}" and waha.sesiones == {}


async def test_aborta_si_la_metadata_no_coincide():
    waha = WahaFalso()
    waha.mutar_eco = lambda c: c.pop("metadata")
    async with _cli(waha) as cli:
        with pytest.raises(ConfigNoCoincide) as e:
            await crear_sesion_verificada(cli, CUERPO)
    assert "metadata" in e.value.problemas


async def test_clave_de_lectura_con_actions_explicito():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        kid, valor = await crear_clave_lectura(cli, N)
    assert (kid, valor) == ("k1", "valor-secreto-1")
    assert waha.claves[0]["actions"] == ACCIONES_CLAVE_LECTURA and waha.claves[0]["isAdmin"] is False


async def test_fin_un_solo_delete_sin_logout_y_solo_sus_claves():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        await crear_sesion_verificada(cli, CUERPO)
        await crear_clave_lectura(cli, N)
        await crear_clave_lectura(cli, N)
        await crear_clave_lectura(cli, OTRA)
        waha.sesiones[N]["status"] = "WORKING"
        waha.llamadas.clear()
        res = await terminar_sesion(cli, N, intentar_start=True)
    assert res == {"status_antes": "WORKING", "start_intentado": False, "desvinculo_confirmado": True,
                   "delete_status": 200, "sesion_borrada": True, "claves_borradas": 2, "claves_restantes": 0,
                   "error_claves": None, "ok": True}
    assert waha.llamadas.count(f"DELETE /api/sessions/{N}") == 1
    assert not any("logout" in x for x in waha.llamadas)
    assert [k["session"] for k in waha.claves] == [OTRA]


async def test_fin_con_sesion_detenida_intenta_start_y_confirma():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        await crear_sesion_verificada(cli, CUERPO)
        waha.sesiones[N]["status"] = "STOPPED"
        res = await terminar_sesion(cli, N, intentar_start=True, espera_start_s=1, intervalo_s=0)
    assert res["start_intentado"] is True and res["desvinculo_confirmado"] is True
    assert f"POST /api/sessions/{N}/start" in waha.llamadas


async def test_fin_sin_permiso_de_start_borra_igual():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        await crear_sesion_verificada(cli, CUERPO)
        waha.sesiones[N]["status"] = "FAILED"
        res = await terminar_sesion(cli, N, intentar_start=False)
    assert res["start_intentado"] is False and res["desvinculo_confirmado"] is False and res["ok"] is True
    assert f"POST /api/sessions/{N}/start" not in waha.llamadas


async def test_start_que_no_llega_a_working_no_confirma():
    waha = WahaFalso()
    waha.start_da = "STARTING"
    async with _cli(waha) as cli:
        await crear_sesion_verificada(cli, CUERPO)
        waha.sesiones[N]["status"] = "FAILED"
        res = await terminar_sesion(cli, N, intentar_start=True, espera_start_s=0.05, intervalo_s=0.01)
    assert res["start_intentado"] is True and res["desvinculo_confirmado"] is False
    assert res["sesion_borrada"] is True and waha.llamadas.count(f"DELETE /api/sessions/{N}") == 1


async def test_error_en_claves_no_bloquea_la_verificacion():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        await crear_sesion_verificada(cli, CUERPO)
        waha.falla_claves = True
        res = await terminar_sesion(cli, N, intentar_start=False)
    assert res["sesion_borrada"] is True and res["error_claves"] == "WahaHttpError"
    assert res["claves_restantes"] is None and res["ok"] is False


async def test_fin_de_sesion_que_ya_no_existe():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        res = await terminar_sesion(cli, N, intentar_start=True)
    assert res["status_antes"] is None and res["delete_status"] == 404
    assert res["sesion_borrada"] is True and res["ok"] is True
    assert waha.llamadas.count(f"DELETE /api/sessions/{N}") == 1
