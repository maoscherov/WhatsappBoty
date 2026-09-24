"""
Receptor de webhooks de WAHA (§6.3 punto 1): HMAC sha512 fail-closed,
resolución por metadata + nombre de sesión, session.status aplicado con la
máquina de estados, y message.* descartado SIN persistir nada (tramo 3).
"""
import hashlib
import hmac
import json
import logging
import uuid

from .helpers import HMAC_TEST, escenario_vinculable, vincular_de_prueba


def _firmar(crudo: bytes, clave: str = HMAC_TEST) -> str:
    return hmac.new(clave.encode(), crudo, hashlib.sha512).hexdigest()


async def _post(cliente, sobre=None, *, firma=None, crudo=None):
    crudo = crudo if crudo is not None else json.dumps(sobre).encode()
    headers = {"content-type": "application/json"}
    if firma is not False:
        headers["x-webhook-hmac"] = firma or _firmar(crudo)
    return await cliente.post("/webhook/waha", content=crudo, headers=headers)


def _status(esc, v, status, me=None):
    sobre = {"event": "session.status", "session": v["session_name"],
             "metadata": {"tenant_id": str(esc["tenant_id"]), "line_id": str(esc["line_id"]),
                          "link_id": str(v["link_id"])},
             "payload": {"status": status}}
    if me:
        sobre["me"] = {"id": me, "pushName": "Negocio"}
    return sobre


async def _estado(ctx, esc, link_id):
    async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
        return await con.fetchrow("SELECT estado, numero_sufijo FROM links WHERE id = $1", link_id)


async def test_sin_clave_configurada_rechaza_todo(cliente, radar_ctx):
    r = await _post(cliente, {"event": "session.status"})
    assert r.status_code == 401


async def test_firma_invalida_o_ausente(cliente, ctx_waha):
    assert (await _post(cliente, {"event": "session.status"}, firma="00" * 64)).status_code == 401
    assert (await _post(cliente, {"event": "session.status"}, firma=False)).status_code == 401


async def test_mensajes_se_descartan_sin_persistir_nada(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)

    async def conteos():
        async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
            return tuple(await con.fetchrow(
                "SELECT (SELECT count(*) FROM link_status_events), (SELECT count(*) FROM access_audit_log), "
                "(SELECT count(*) FROM product_events), (SELECT count(*) FROM jobs)"))

    antes = await conteos()
    sobre = {**_status(esc, v, "WORKING"), "event": "message.any",
             "payload": {"id": "false_5493411111111@c.us_AAA", "from": "5493411111111@c.us",
                         "body": "hola, mi DNI es 30111222"}}
    r = await _post(cliente, sobre)
    assert r.status_code == 200 and r.json() == {"ok": True, "descartado": True}
    assert await conteos() == antes


async def test_session_status_aplica_la_transicion(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc, working=False)
    r = await _post(cliente, _status(esc, v, "WORKING", me=waha.me_id))
    assert r.status_code == 200 and r.json() == {"ok": True, "aplicado": True}
    fila = await _estado(ctx_waha, esc, v["link_id"])
    assert (fila["estado"], fila["numero_sufijo"]) == ("vinculado", "4567")


async def test_metadata_de_otro_tenant_o_de_otra_sesion_se_ignora(cliente, ctx_waha, waha):
    a = await escenario_vinculable(ctx_waha, "Farmacia A")
    b = await escenario_vinculable(ctx_waha, "Farmacia B")
    v = await vincular_de_prueba(ctx_waha, waha, a, working=False)
    cruzado = _status(a, v, "WORKING")
    cruzado["metadata"]["tenant_id"] = str(b["tenant_id"])
    assert (await _post(cliente, cruzado)).json() == {"ok": True, "ignorado": True}
    otra_sesion = {**_status(a, v, "WORKING"), "session": "v_ffffffffffff"}
    assert (await _post(cliente, otra_sesion)).json() == {"ok": True, "ignorado": True}
    assert (await _estado(ctx_waha, a, v["link_id"]))["estado"] == "esperando_qr"


async def test_metadata_invalida_se_ignora(cliente, ctx_waha):
    sobre = {"event": "session.status", "session": "v_0123456789ab",
             "metadata": {"tenant_id": "x", "line_id": str(uuid.uuid4()), "link_id": None},
             "payload": {"status": "WORKING"}}
    r = await _post(cliente, sobre)
    assert r.status_code == 200 and r.json() == {"ok": True, "ignorado": True}


async def test_cuerpo_que_no_es_json(cliente, ctx_waha):
    assert (await _post(cliente, crudo=b"no-es-json")).status_code == 400
    assert (await _post(cliente, crudo=b"[1, 2]")).status_code == 400


async def test_no_loguea_el_cuerpo(cliente, ctx_waha, waha, caplog):
    caplog.set_level(logging.DEBUG)
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc, working=False)
    sobre = {**_status(esc, v, "WORKING"), "event": "message",
             "payload": {"body": "hola secreto", "from": "5493411111111@c.us"}}
    await _post(cliente, sobre)
    await _post(cliente, _status(esc, v, "WORKING", me=waha.me_id))
    assert "hola secreto" not in caplog.text
    assert "5493411111111" not in caplog.text and "5493411234567" not in caplog.text


async def test_status_con_forma_invalida_se_ignora(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc, working=False)
    r = await _post(cliente, _status(esc, v, "working; DROP TABLE links"))
    assert r.json() == {"ok": True, "ignorado": True}
    assert (await _estado(ctx_waha, esc, v["link_id"]))["estado"] == "esperando_qr"
