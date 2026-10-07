"""
Goldens de la farmacia, tomados sobre 07a1d7a ANTES del refactor del perfil de
rubro (spec 2026-10-06-vertical-petshop-design.md §2.3 y §6). Pasan en verde
con el código de hoy y tienen que seguir en verde después de cada tarea: la
farmacia no cambia ni un byte.

Van como hash adentro del test y no como .txt: el repo tiene
core.autocrlf=true sin .gitattributes, y un .txt cambiaría de bytes al hacer
checkout en Windows.
"""
import hashlib
import json
from types import SimpleNamespace

import pytest

PROMPT_FARMACIA_SHA256 = "1953a4e6815d855e635406c1da8f97ff83bb9be6a2fd040b69eabfae4c540749"
PROMPT_FARMACIA_LEN = 14680
PROMPT_MUTUAL_SHA256 = "4377db47f567816d994d1824bd25adb7cb40e5bb9a96d00b74491f81258dc469"
VISION_FARMACIA_SHA256 = "84fec1f627d68084101c2afe62d8c6c15dc7deb0ae238b30a16cc0e8bf423132"
DEFAULTS_SHA256 = "655cbe78de891191bb660b42cc1d4b3b9e325bf93205f139a6f4bfcefa910e1d"
DEFAULTS_CLAVES = 90
# Claves que la spec suma a DEFAULTS (§4.4 y §5): quedan fuera del golden.
CLAVES_NUEVAS = {"pago_mp_manual", "retiro_sucursal", "retiro_info_message"}

PAY_SHA256 = "85c4271036760ed8f4185d0d2c860bf6c7bfb926d2d36e4a882283a22f84ad52"
PAY_VENCIDO_SHA256 = "a23c99347472476c32a97742e46978de209ec2c2a8d4165ed33386df16f47283"
RETURN_OK_SHA256 = "0b8cb84ff34ec20836cd88e807afa23f41e8e0b2e9b3b5fdba629ac1d0e44eea"
RETURN_ERR_SHA256 = "a975ad681e7c07c62cf5ef0c9260c8cd109c578870b0d820376e98fa31225994"


def _sha(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def test_prompt_farmacia_golden():
    from app.services.intent_service import SYSTEM_PROMPT
    assert len(SYSTEM_PROMPT) == PROMPT_FARMACIA_LEN
    assert _sha(SYSTEM_PROMPT) == PROMPT_FARMACIA_SHA256


def test_prompt_mutual_golden():
    from app.services.mutual_helper import SYSTEM_PROMPT_MUTUAL
    assert _sha(SYSTEM_PROMPT_MUTUAL) == PROMPT_MUTUAL_SHA256


def test_prompt_vision_farmacia_golden():
    from app.services.image_service import _PROMPT
    assert _sha(_PROMPT) == VISION_FARMACIA_SHA256


def test_defaults_golden():
    from app.services.config_service import DEFAULTS
    base = {k: v for k, v in DEFAULTS.items() if k not in CLAVES_NUEVAS}
    assert len(base) == DEFAULTS_CLAVES
    assert _sha(json.dumps(base, sort_keys=True, ensure_ascii=False)) == DEFAULTS_SHA256


# ── /pay y páginas de estado de Payway (spec §4.7: "en farmacia, el body es
# idéntico a un snapshot tomado antes del cambio") ─────────────────────────────

@pytest.fixture
def payway_falso(monkeypatch):
    """pay_page sin Redis ni Payway: pago pendiente fijo y servicio falso."""
    from app.config import get_settings
    from app.routers import payway as pw

    async def _pending(pid):
        if pid == "abc123":
            return {"total": "4770.0", "sku_nombre": "IBUPROFENO 600 MG X 10",
                    "estado": "pendiente"}
        return None

    monkeypatch.setattr(pw, "_get_pending", _pending)
    monkeypatch.setattr(pw, "get_payway_service", lambda *a, **k: SimpleNamespace(
        base_url="https://pw.test/api/v2", public_key="PUBKEY"))
    monkeypatch.setattr(get_settings(), "payway_cs_org_id", "ORG")
    monkeypatch.setattr(get_settings(), "payway_cs_merchant_id", "MERCH")
    return pw


async def test_pay_page_farmacia_snapshot(payway_falso):
    r = await payway_falso.pay_page("abc123")
    assert r.status_code == 200
    assert hashlib.sha256(r.body).hexdigest() == PAY_SHA256


async def test_pay_page_vencido_farmacia_snapshot(payway_falso):
    r = await payway_falso.pay_page("vencido")
    assert r.status_code == 404
    assert hashlib.sha256(r.body).hexdigest() == PAY_VENCIDO_SHA256


async def test_payway_return_farmacia_snapshot(payway_falso):
    ok = await payway_falso.payway_return("ok")
    err = await payway_falso.payway_return("")
    assert hashlib.sha256(ok.body).hexdigest() == RETURN_OK_SHA256
    assert hashlib.sha256(err.body).hexdigest() == RETURN_ERR_SHA256
