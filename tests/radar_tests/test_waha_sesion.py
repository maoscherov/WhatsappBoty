"""
Cuerpo de creación de sesión exactamente como P3 y verificación posterior:
si WAHA no guardó lo pedido, se aborta antes del QR.
"""
import copy
import uuid

import pytest

from app.radar.waha.sesion import ACCIONES_CLAVE_LECTURA, cuerpo_sesion, nombre_sesion, verificar_config

L, T, LI = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
ARGS = dict(link_id=L, tenant_id=T, line_id=LI, webhook_url="http://radar.interno/webhook/waha",
            hmac_key="h" * 32)


def test_cuerpo_noweb_es_el_de_p3():
    c = cuerpo_sesion(engine="NOWEB", **ARGS)
    assert c == {
        "name": "v_" + L.hex[:12], "start": True,
        "config": {
            "metadata": {"tenant_id": str(T), "line_id": str(LI), "link_id": str(L)},
            "ignore": {"status": True, "groups": True, "channels": True, "broadcast": True},
            "webhooks": [{"url": "http://radar.interno/webhook/waha",
                          "events": ["message.any", "message.ack", "message.edited", "message.revoked",
                                     "session.status"],
                          "hmac": {"key": "h" * 32},
                          "retries": {"policy": "exponential", "delaySeconds": 2, "attempts": 15}}],
            "noweb": {"markOnline": False, "store": {"enabled": True, "fullSync": False}},
        },
    }
    assert "deviceName" not in str(c)


def test_nombre_por_vinculo_y_opaco():
    a, b = uuid.uuid4(), uuid.uuid4()
    assert nombre_sesion(a) != nombre_sesion(b)
    assert nombre_sesion(a) == "v_" + a.hex[:12]
    assert T.hex[:12] not in nombre_sesion(a) and LI.hex[:12] not in nombre_sesion(a)


def test_cuerpo_gows_sin_bloque_noweb():
    c = cuerpo_sesion(engine="GOWS", **ARGS)
    assert "noweb" not in c["config"]
    assert c["config"]["gows"] == {"storage": {"messages": True, "chats": True, "groups": False, "labels": False,
                                               "contacts": True, "messageSecrets": True}}


def test_gows_no_acepta_profundidad_y_motor_desconocido_falla():
    with pytest.raises(ValueError):
        cuerpo_sesion(engine="GOWS", full_sync=True, **ARGS)
    with pytest.raises(ValueError):
        cuerpo_sesion(engine="WEBJS", **ARGS)
    assert cuerpo_sesion(engine="NOWEB", full_sync=True, **ARGS)["config"]["noweb"]["store"]["fullSync"] is True


def _eco(cuerpo):
    return {"name": cuerpo["name"], "status": "STARTING", "engine": "NOWEB", "config": copy.deepcopy(cuerpo["config"]),
            "me_id": None}


def test_verificar_acepta_eco_exacto_y_claves_de_mas():
    c = cuerpo_sesion(engine="NOWEB", **ARGS)
    eco = _eco(c)
    assert verificar_config(eco, c) == []
    eco["config"]["noweb"]["store"]["extra"] = 1
    eco["config"]["ignore"]["extra"] = True
    eco["config"]["webhooks"][0].pop("hmac")          # WAHA puede no devolver la clave
    assert verificar_config(eco, c) == []


@pytest.mark.parametrize("mutar,campo", [
    (lambda c: c["noweb"].__setitem__("markOnline", True), "noweb.markOnline"),
    (lambda c: c["noweb"].pop("markOnline"), "noweb.markOnline"),
    (lambda c: c["noweb"]["store"].__setitem__("enabled", False), "noweb.store"),
    (lambda c: c["noweb"]["store"].__setitem__("fullSync", True), "noweb.store"),
    (lambda c: c["ignore"].__setitem__("groups", False), "ignore"),
    (lambda c: c.pop("ignore"), "ignore"),
    (lambda c: c["webhooks"][0].__setitem__("url", "http://otro"), "webhooks.url"),
    (lambda c: c["webhooks"][0].__setitem__("events", ["message"]), "webhooks.events"),
    (lambda c: c.__setitem__("webhooks", []), "webhooks.url"),
    (lambda c: c["metadata"].__setitem__("link_id", str(uuid.uuid4())), "metadata"),
])
def test_verificar_reporta_cada_diferencia(mutar, campo):
    c = cuerpo_sesion(engine="NOWEB", **ARGS)
    eco = _eco(c)
    mutar(eco["config"])
    assert campo in verificar_config(eco, c)


def test_verificar_gows_compara_storage():
    c = cuerpo_sesion(engine="GOWS", **ARGS)
    eco = _eco(c)
    assert verificar_config(eco, c) == []
    eco["config"]["gows"]["storage"]["messages"] = False
    assert verificar_config(eco, c) == ["gows.storage"]


def test_acciones_de_la_clave_de_lectura_son_explicitas_y_sin_escritura():
    assert ACCIONES_CLAVE_LECTURA and all(isinstance(v, bool) for v in ACCIONES_CLAVE_LECTURA.values())
    assert [k for k, v in ACCIONES_CLAVE_LECTURA.items() if v] == ["read"]
