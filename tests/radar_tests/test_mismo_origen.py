"""
Guarda de origen (app/radar/app.py::solo_mismo_origen): un pedido que cambia algo (POST, PUT, PATCH, DELETE) bajo
/radar/ se atiende solo si viene del mismo origen.

Por qué: la cookie de sesión es SameSite=Lax y viaja en todo pedido same-site, el que puede mandar una página de
cualquier subdominio de keepitsimple.com.ar; y la versión fijada de FastAPI (0.115, requirements.txt) lee como JSON un
cuerpo sin Content-Type, así que ese pedido no necesita preflight (un fetch no-cors con un Blob, navigator.sendBeacon).
Lo peor que se podía hacer así: registrar un WAHA propio con la sesión de un admin (POST /radar/admin/workers).

Esta suite corre con un FastAPI más nuevo, que ya no lee ese cuerpo (strict_content_type): por eso cada rechazo se
prueba ANTES de la ruta (ni una llamada a WAHA, ni una fila, ni una auditoría, ni un mail), no solo por el 403.
"""
import json
import logging
import re
import uuid

import pytest

from app.radar.constantes import TENANT_KIS

from .helpers import (HMAC_TEST, crear_tenant_directo, crear_usuario, crear_worker_directo, entrar,
                      escenario_vinculable, vincular_de_prueba)
from .test_admin import ALTA
from .test_webhook_waha import _firmar, _status
from .test_workers_admin import URL, _alta, _claves_guardadas

RECHAZO = {"detail": {"error": "origen_no_permitido"}}
LINK = re.compile(r"/radar/login/canjear\?t=([0-9a-f-]+)#k=([A-Za-z0-9_-]+)")
# Lo que manda un navegador desde una página de Radar (en los tests, http://testserver).
MISMO_ORIGEN = {"Sec-Fetch-Site": "same-origin", "Origin": "http://testserver"}
# Lo que manda un navegador desde otra página: de otro subdominio (same-site) o de otro sitio.
OTRO_SUBDOMINIO = {"Sec-Fetch-Site": "same-site", "Origin": "https://otro.keepitsimple.com.ar"}
DE_OTRO_ORIGEN = [
    pytest.param(OTRO_SUBDOMINIO, id="same-site"),
    pytest.param({"Sec-Fetch-Site": "cross-site", "Origin": "https://ajeno.example"}, id="cross-site"),
    # Si vino Sec-Fetch-Site, decide él: Origin no se mira.
    pytest.param({"Sec-Fetch-Site": "same-site", "Origin": "http://testserver"}, id="same-site-con-origin-propio"),
    # Un navegador sin Sec-Fetch-Site: Origin tiene que ser el Host del pedido.
    pytest.param({"Origin": "https://otro.keepitsimple.com.ar"}, id="origin-de-otro-host"),
    pytest.param({"Origin": "http://testserver.ajeno.example"}, id="origin-que-empieza-igual"),
    pytest.param({"Origin": "http://testserver:8000"}, id="origin-con-otro-puerto"),
    pytest.param({"Origin": "null"}, id="origin-null"),
]


@pytest.fixture
async def ctx(radar_ctx, waha):
    """radar_ctx con el WAHA falso como transporte. Sin workers: cada test registra los suyos."""
    radar_ctx.waha_transport = waha.transporte()
    return radar_ctx


async def _admin(cliente, ctx) -> uuid.UUID:
    uid = await crear_usuario(ctx.db, TENANT_KIS, "admin@keepitsimple.com.ar", "admin")
    await entrar(cliente, ctx, TENANT_KIS, uid, "admin")
    return uid


async def _cuantas(ctx, tenant_id, sql: str) -> int:
    async with ctx.db.tenant_tx(tenant_id) as con:
        return await con.fetchval(sql)


# --- el peor caso: registrar un WAHA con la sesión de un admin -----------------------------------------------------

@pytest.mark.parametrize("cuerpo", ["json", "sin-content-type"])
@pytest.mark.parametrize("cabeceras", DE_OTRO_ORIGEN)
async def test_registrar_un_worker_desde_otro_origen_se_rechaza_antes_de_la_ruta(cliente, ctx, waha, cabeceras,
                                                                                 cuerpo):
    await _admin(cliente, ctx)
    if cuerpo == "json":
        r = await cliente.post(URL, json=_alta(), headers=cabeceras)
    else:                                    # como lo manda la página atacante sin preflight: el JSON en un Blob
        r = await cliente.post(URL, content=json.dumps(_alta()).encode(), headers=cabeceras)
    assert r.status_code == 403 and r.json() == RECHAZO
    assert r.headers.get_list("cache-control") == ["no-store"]          # sigue siendo una respuesta de la API
    assert waha.llamadas == [] and _claves_guardadas(ctx) == []         # ni se probó la clave ni se guardó
    assert await _cuantas(ctx, TENANT_KIS, "SELECT count(*) FROM waha_workers") == 0
    assert await _cuantas(ctx, TENANT_KIS, "SELECT count(*) FROM access_audit_log") == 0


@pytest.mark.parametrize("cabeceras", [
    pytest.param(MISMO_ORIGEN, id="same-origin-con-origin"),
    pytest.param({"Sec-Fetch-Site": "same-origin"}, id="same-origin"),
    pytest.param({"Sec-Fetch-Site": "none"}, id="none"),                         # lo pidió la persona, no una página
    pytest.param({"Origin": "http://testserver"}, id="origin-propio-sin-sec-fetch-site"),
    pytest.param({"Origin": "http://TESTSERVER"}, id="origin-propio-en-mayusculas"),
    pytest.param({}, id="sin-cabeceras"),                                        # curl, scripts, servidor a servidor
])
async def test_desde_el_mismo_origen_o_sin_cabeceras_registra_como_siempre(cliente, ctx, waha, cabeceras):
    await _admin(cliente, ctx)
    r = await cliente.post(URL, json=_alta(), headers=cabeceras)
    assert r.status_code == 201, r.text
    assert waha.llamadas == ["GET /api/server/version"] and len(_claves_guardadas(ctx)) == 1


async def test_el_rechazo_queda_en_el_log_en_un_solo_renglon(cliente, caplog):
    """Para ver en staging a quién se rechazó (método y ruta, sin cabeceras). La ruta llega decodificada: un separador
    de línea codificado en ella (\\n, \\r y \\t ya los saca urlsplit; \\x0b o U+2028 no) no puede partir el renglón."""
    caplog.set_level(logging.WARNING)
    r = await cliente.post("/radar/login%0B%E2%80%A8inventada", json={}, headers=OTRO_SUBDOMINIO)
    assert (r.status_code, r.json()) == (403, RECHAZO)
    [mensaje] = [x.getMessage() for x in caplog.records if x.name == "app.radar"]
    assert mensaje.startswith("pedido de otro origen rechazado: POST ") and len(mensaje.splitlines()) == 1
    assert "otro.keepitsimple.com.ar" not in caplog.text


@pytest.mark.parametrize("host, origen, permitido", [
    ("radar.keepitsimple.com.ar", "https://radar.keepitsimple.com.ar", True),      # el proxy termina el TLS
    ("radar.keepitsimple.com.ar:8443", "https://radar.keepitsimple.com.ar:8443", True),
    ("radar.keepitsimple.com.ar", "https://radar.keepitsimple.com.ar:8443", False),
    ("radar.keepitsimple.com.ar", "https://otro.keepitsimple.com.ar", False),
    ("radar.keepitsimple.com.ar", "radar.keepitsimple.com.ar", False),             # sin esquema no es un Origin
    ("radar.keepitsimple.com.ar", "https://[radar.keepitsimple.com.ar", False),    # urlsplit no lo puede leer
    ("[::1]:8000", "http://[::1]:8000", True),
])
async def test_sin_sec_fetch_site_el_origin_tiene_que_ser_el_host_del_pedido(cliente, host, origen, permitido):
    """Sin mirar el esquema: detrás del proxy de Railway el pedido llega por http y el Origin dice https."""
    r = await cliente.post("/radar/login", json={"email": "nadie@cliente.com"}, headers={"Host": host, "Origin": origen})
    assert r.status_code == (202 if permitido else 403), r.text


# --- el resto de lo que cambia algo ------------------------------------------------------------------------------

async def test_las_demas_rutas_que_cambian_algo_tambien_se_protegen(cliente, ctx, waha):
    await _admin(cliente, ctx)
    wid = await crear_worker_directo(ctx.db)
    esc = await escenario_vinculable(ctx)
    restriccion = f"/radar/admin/tenants/{esc['tenant_id']}/lineas/{esc['line_id']}/vinculo/restriccion"
    pedidos = [("POST", "/radar/admin/tenants", ALTA),                          # cliente nuevo + invitación por mail
               ("PUT", f"{URL}/{wid}/clave", {"admin_key": "clave-admin-larga-0123456789"}),
               ("PUT", f"{URL}/{wid}/disco", {"usado_gb": 5}),
               ("PUT", restriccion, {"hasta": None}),
               ("DELETE", restriccion, None),
               ("PATCH", "/radar/api/yo", {}),                  # sin ruta PATCH: la guarda va antes del ruteo
               ("POST", "/radar/logout", None)]                 # tampoco se puede cerrar la sesión desde otra página
    for metodo, ruta, cuerpo in pedidos:
        r = await cliente.request(metodo, ruta, json=cuerpo, headers=OTRO_SUBDOMINIO)
        assert (r.status_code, r.json()) == (403, RECHAZO), (metodo, ruta)
    assert waha.llamadas == [] and ctx.mailer.enviados == []
    async with ctx.db.sin_tenant() as con:
        assert [f["nombre"] for f in await con.fetch("SELECT * FROM radar_admin_listar_tenants()")] == ["Farmacia A"]
    assert await _cuantas(ctx, TENANT_KIS, "SELECT count(*) FROM access_audit_log") == 0
    assert await _cuantas(ctx, esc["tenant_id"], "SELECT count(*) FROM access_audit_log") == 0
    assert [w["disco_usado_gb"] for w in (await cliente.get(URL)).json()] == [0.0]   # la sesión sigue viva

    # /radar/api: el dueño invita a un usuario
    await entrar(cliente, ctx, esc["tenant_id"], esc["dueno_id"], "dueno")
    r = await cliente.post("/radar/api/usuarios", json={"email": "gestor@cliente.com", "nombre": "Gus", "rol": "gestor"},
                           headers=OTRO_SUBDOMINIO)
    assert (r.status_code, r.json()) == (403, RECHAZO)
    assert r.headers.get_list("cache-control") == ["no-store"]
    assert await _cuantas(ctx, esc["tenant_id"], "SELECT count(*) FROM users") == 1     # solo el dueño
    assert ctx.mailer.enviados == []
    # y desde la propia pantalla, la misma invitación sale
    r = await cliente.post("/radar/api/usuarios", json={"email": "gestor@cliente.com", "nombre": "Gus", "rol": "gestor"},
                           headers=MISMO_ORIGEN)
    assert r.status_code == 201 and [m.para for m in ctx.mailer.enviados] == ["gestor@cliente.com"]


async def test_las_lecturas_desde_otro_origen_siguen_andando(cliente, ctx):
    await _admin(cliente, ctx)
    otro = {"Sec-Fetch-Site": "cross-site", "Origin": "https://ajeno.example"}
    for ruta in (URL, "/radar/api/yo", "/radar/admin/consola/lineas", "/radar/consola", "/radar/login"):
        assert (await cliente.get(ruta, headers=otro)).status_code == 200, ruta
    assert (await cliente.options(URL, headers=otro)).status_code != 403        # OPTIONS tampoco pasa por la guarda


async def test_el_webhook_de_waha_no_pasa_por_la_guarda(cliente, ctx_waha, waha):
    """Está fuera de /radar/ y se autentica con su firma HMAC, no con la cookie."""
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc, working=False)
    crudo = json.dumps(_status(esc, v, "WORKING", me=waha.me_id)).encode()
    r = await cliente.post("/webhook/waha", content=crudo,
                           headers={"content-type": "application/json", "x-webhook-hmac": _firmar(crudo, HMAC_TEST),
                                    "Sec-Fetch-Site": "cross-site", "Origin": "https://waha.ajeno.example"})
    assert r.status_code == 200 and r.json() == {"ok": True, "aplicado": True}


async def test_el_canje_del_link_entra_desde_su_pagina_y_no_desde_otra(cliente, radar_ctx):
    t = await crear_tenant_directo(radar_ctx.db, "Farmacia A")
    await crear_usuario(radar_ctx.db, t, "dueno@cliente.com", "dueno")
    r = await cliente.post("/radar/login", json={"email": "dueno@cliente.com"}, headers=MISMO_ORIGEN)
    assert r.status_code == 202
    tid, k = LINK.search(radar_ctx.mailer.enviados[0].texto).groups()
    # login CSRF: otra página no puede hacer entrar al navegador con un link (ni gastarlo)
    r = await cliente.post("/radar/login/canjear", data={"t": tid, "k": k}, headers=OTRO_SUBDOMINIO)
    assert (r.status_code, r.json()) == (403, RECHAZO) and "set-cookie" not in r.headers
    # el formulario de la página de canje es del mismo origen: entra, con el link todavía sin usar
    r = await cliente.post("/radar/login/canjear", data={"t": tid, "k": k}, headers=MISMO_ORIGEN)
    assert r.status_code == 303 and r.headers["location"] == "/radar/inicio"
    assert (await cliente.get("/radar/api/yo")).status_code == 200
