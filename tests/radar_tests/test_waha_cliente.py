"""
Cliente de WAHA (§6.1): lista blanca de rutas, solo sesiones v_<12 hex>,
clave de lectura con actions explícito y nada de contenido en los logs.
"""
import logging

import pytest

from app.radar.waha.cliente import (RutaNoPermitida, SesionProhibida, WahaCliente, WahaError, WahaHttpError,
                                    verificar_nombre_sesion, verificar_ruta)

from .waha_falso import PNG, WahaFalso

S = "v_0123456789ab"
ACC = {"read": True, "control": False, "send": False, "media": False}


def _cli(waha):
    return WahaCliente("http://waha.interno", "clave-admin", transport=waha.transporte())


@pytest.mark.parametrize("metodo,ruta", [
    ("POST", "/api/sendText"),
    ("POST", "/api/sendSeen"),
    ("POST", "/api/startTyping"),
    ("POST", f"/api/{S}/presence"),
    ("POST", f"/api/sessions/{S}/logout"),
    ("GET", f"/api/{S}/chats/overview"),
    ("GET", f"/api/{S}/chats"),
    ("GET", f"/api/{S}/chats/x/messages"),
    ("POST", f"/api/{S}/chats/x/messages/read"),
    ("DELETE", f"/api/{S}/chats/x"),
    ("PUT", f"/api/sessions/{S}"),
    ("GET", "/api/server/environment"),
])
async def test_rutas_fuera_de_la_lista_blanca_no_salen(metodo, ruta):
    waha = WahaFalso()
    async with _cli(waha) as cli:
        with pytest.raises(RutaNoPermitida):
            await cli._request(metodo, ruta)
    assert waha.llamadas == []


def test_rutas_permitidas_devuelven_la_sesion():
    assert verificar_ruta("GET", f"/api/sessions/{S}") == S
    assert verificar_ruta("GET", f"/api/{S}/auth/qr") == S
    assert verificar_ruta("POST", "/api/keys") is None
    assert verificar_ruta("DELETE", "/api/keys/k1") is None


async def test_solo_sesiones_v_con_12_hex():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        for malo in ("MaroSession", "spike_nw0", "v_XYZ", "v_0123456789abc"):
            with pytest.raises(SesionProhibida):
                await cli.leer_sesion(malo)
        with pytest.raises(SesionProhibida):
            await cli.crear_sesion({"name": "MaroSession", "config": {}})
        with pytest.raises(SesionProhibida):
            await cli.crear_clave("MaroSession", actions=ACC)
    assert waha.llamadas == []


async def test_crear_y_leer_sesion_devuelve_solo_la_vista():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        creada = await cli.crear_sesion({"name": S, "start": True, "config": {"ignore": {}}})
        assert creada == {"name": S, "status": "STARTING", "engine": "NOWEB", "config": {"ignore": {}}, "me_id": None}
        waha.sesiones[S]["status"] = "WORKING"
        leida = await cli.leer_sesion(S)
    assert set(leida) == {"name", "status", "engine", "config", "me_id"}
    assert leida["me_id"] == waha.me_id


async def test_leer_sesion_inexistente_es_none():
    async with _cli(WahaFalso()) as cli:
        assert await cli.leer_sesion(S) is None


async def test_borrar_sesion_devuelve_el_status_sin_lanzar():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        assert await cli.borrar_sesion(S) == 404
        await cli.crear_sesion({"name": S, "config": {}})
        assert await cli.borrar_sesion(S) == 200


async def test_qr_y_codigo():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        assert await cli.qr_png(S) is None
        await cli.crear_sesion({"name": S, "config": {}})
        assert await cli.qr_png(S) == PNG
        assert await cli.pedir_codigo(S, "5493411234567") == "ABCD-EFGH"
        waha.falla_codigo = True
        assert await cli.pedir_codigo(S, "5493411234567") is None


async def test_crear_clave_exige_actions_explicito():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        with pytest.raises(ValueError):
            await cli.crear_clave(S, actions={})
        assert await cli.crear_clave(S, actions=ACC) == ("k1", "valor-secreto-1")
    assert waha.claves[0]["actions"] == ACC and waha.claves[0]["isAdmin"] is False


async def test_borrar_clave_de_otra_sesion_se_rechaza():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        kid, _ = await cli.crear_clave(S, actions=ACC)
        waha.claves.append({"id": "k99", "session": "MaroSession", "isAdmin": False})
        with pytest.raises(SesionProhibida):
            await cli.borrar_clave("k99")
        assert await cli.borrar_clave(kid) == 200
    assert [k["id"] for k in waha.claves] == ["k99"]


async def test_error_http_inesperado_lanza_con_status():
    waha = WahaFalso()
    waha.falla_crear = True
    async with _cli(waha) as cli:
        with pytest.raises(WahaHttpError) as e:
            await cli.crear_sesion({"name": S, "config": {}})
    assert e.value.status == 500


def test_httpx_queda_en_warning():
    WahaCliente("http://waha.interno", "clave-admin")
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


async def test_no_loguea_codigo_ni_numero(caplog):
    caplog.set_level(logging.DEBUG)
    waha = WahaFalso()
    async with _cli(waha) as cli:
        await cli.crear_sesion({"name": S, "config": {}})
        waha.sesiones[S]["status"] = "WORKING"
        await cli.leer_sesion(S)
        await cli.qr_png(S)
        await cli.pedir_codigo(S, "5493411234567")
    assert "ABCD" not in caplog.text and "5493411234567" not in caplog.text and "clave-admin" not in caplog.text


async def test_salto_de_linea_final_no_pasa_la_lista_blanca():
    """Final B5: con `$` y re.match, un salto de línea final pasaba; ahora es fullmatch."""
    nl = chr(10)
    waha = WahaFalso()
    async with _cli(waha) as cli:
        with pytest.raises(SesionProhibida):
            await cli.leer_sesion(S + nl)
    for metodo, ruta in (("GET", "/api/server/version" + nl), ("POST", "/api/sessions" + nl),
                         ("POST", f"/api/sessions/{S}/start" + nl), ("DELETE", "/api/keys/k1" + nl)):
        with pytest.raises(RutaNoPermitida):
            verificar_ruta(metodo, ruta)
    with pytest.raises(SesionProhibida):
        verificar_nombre_sesion(verificar_ruta("GET", f"/api/sessions/{S}" + nl))
    assert waha.llamadas == []


async def test_version_del_servidor_devuelve_version_motor_y_tier():
    async with _cli(WahaFalso()) as cli:
        assert await cli.version_servidor() == {"version": "2026.8.2", "engine": "NOWEB", "tier": "CORE"}


@pytest.mark.parametrize("cuerpo", [b"<html>esto no es WAHA</html>", b'["NOWEB"]', b'"NOWEB"', b"5", b"null"])
async def test_version_con_una_respuesta_que_no_es_un_objeto_json_es_un_waha_error(cuerpo):
    """Un 200 que no es JSON (o no es un objeto) no es de WAHA: WahaError, nunca un ValueError ni un AttributeError."""
    waha = WahaFalso()
    waha.version_crudo = cuerpo
    async with _cli(waha) as cli:
        with pytest.raises(WahaError):
            await cli.version_servidor()
