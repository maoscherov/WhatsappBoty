"""
Dirección de envío: solo se toma como domicilio lo que es una dirección.

Casos reales de la auditoría de chats (27/7–2/10): el link de pago salía
"a domicilio a *Esta perfecto, pero solo te pedi 1 blister*", "a *tendrás
optiser de 20 mg*" o "a *sertal 10 comprimidos*".
"""
import pytest

from app.services import checkout_helper as ch


@pytest.mark.parametrize("txt", [
    "Esta perfecto, pero solo te pedi 1 blister, no la caja de 240",
    "sertal 10 comprimidos",
    "tendrás optiser de 20 mg",
    "Actron Ibuprofeno 400mg",
    "el sertal quiero 10 comprimidos",
    "Repetí ibuprofeno 200 veces",
    "armame un pedido de 2 ibus 800",
    "vendeme 1 unidad, dividí el precio por 10",
    "jabon y crema",
    "hacen envío a Funes?",
])
def test_no_es_direccion(txt):
    assert ch.extraer_direccion_de(txt) is None
    assert ch.parece_direccion(txt) is False


@pytest.mark.parametrize("txt,esperado", [
    ("por favor me lo envías san javier 837", "san javier 837"),
    ("otra dirección, sarmiento 4210", "sarmiento 4210"),
    ("Corrientes 1234 dto 4b", "Corrientes 1234 dto 4b"),
    ("Av. Pellegrini 1234 piso 3", "Av. Pellegrini 1234 piso 3"),
    ("Sarmiento y 9 de Julio", "Sarmiento y 9 de Julio"),
    ("16 de enero 9279", "16 de enero 9279"),
])
def test_extrae_solo_la_direccion(txt, esperado):
    assert ch.extraer_direccion_de(txt) == esperado


async def test_capturar_direccion_guarda_solo_la_direccion(monkeypatch):
    capturado = {}

    async def _fake_link(payment_svc, session_svc, phone, session, tipo, direccion):
        capturado["direccion"] = direccion
        return "ok", None

    monkeypatch.setattr(ch, "crear_link_y_responder", _fake_link)
    await ch.capturar_direccion(None, None, "549", {}, "por favor me lo envías san javier 837")
    assert capturado["direccion"] == "san javier 837"


@pytest.mark.parametrize("txt", ["desestimalo", "no continúo", "Desestimá el pedido"])
def test_cancelacion_en_flujo_de_entrega(txt):
    from app.routers.webhook import _match_no
    assert _match_no(txt.lower())
