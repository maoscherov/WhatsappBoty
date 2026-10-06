"""
Las 9 conversaciones marcadas el 5/10 (C-3854 … C-4156), como regresión.
"""
import pytest

from app.services import checkout_helper as ch
from app.services.sku_service import SKUService


# C-3854 / C-3912: palabras de envío
@pytest.mark.parametrize("txt", ["mandamelo", "mandámelo", "me lo envías", "me lo envias",
                                 "me lo podés enviar", "traémelo", "envialo a casa", "a domicilio"])
def test_es_envio(txt):
    assert ch.match_envio(txt.lower())


@pytest.mark.parametrize("txt", ["mandame el link", "me mandás el link de pago", "paso a buscarlo"])
def test_no_es_envio(txt):
    assert not ch.match_envio(txt.lower())


# C-3854: domicilio precargado con la última dirección usada
def test_domicilio_de_ultima_direccion():
    class _SinSocio:
        def find_by_phone(self, p):
            return None
    ch.recordar_direccion("5493410000777", "San Javier 837")
    ch.recordar_direccion("5493410000778", "te pedi 1 blister")      # no es dirección
    assert ch.domicilio_de("5493410000777", _SinSocio()) == "San Javier 837"
    assert ch.domicilio_de("5493410000778", _SinSocio()) == ""
    assert "San Javier 837" in ch.pregunta_entrega({}, phone="5493410000777", socio_svc=_SinSocio())


# C-4115: saldo con error de tipeo
@pytest.mark.parametrize("txt", ["Quisiera saber lo qie debo?", "lo q debo", "lo qe te debo"])
def test_saldo_con_tipeo(txt):
    assert ch.consulta_saldo(txt)


def test_lo_que_debo_tomar_no_es_saldo():
    assert not ch.consulta_saldo("lo que debo tomar para la garganta")


# C-3996: el mismo producto duplicado con precio viejo
def _cat():
    base = {"hash": "a" * 64, "barcodes": [], "troquel": None, "brand": "", "drug": None,
            "form": None, "category": "Perfumeria", "rubro": "", "subrubro": "",
            "therapeutic_actions": [], "stock": 5, "visible": True, "active": True,
            "requiere_receta": "no", "source": "t"}
    return SKUService.from_rows([
        {**base, "external_id": "1", "name": "ELVIVE COLOR VIVE SHA X 400", "price": 1006.18},
        {**base, "external_id": "2", "name": "ELVIVE COLOR VIVE (NUEVO) SHA x 400", "price": 9591.85},
        {**base, "external_id": "3", "name": "DERMAGLOS F LECHE LIMP EMU x 200", "price": 26000.0},
        {**base, "external_id": "4", "name": "DERMAGLOS SOLAR F50 FACIAL EMU x 50", "price": 30000.0},
        {**base, "external_id": "5", "name": "BAGOVIT SOLAR FPS50 EMU x 200", "price": 25000.0},
    ])


def test_precio_viejo_duplicado_no_se_vende():
    svc = _cat()
    res = svc.buscar("shampoo elvive color vive 400")
    out = ch.marcar_precio_dudoso(res, {"precio_minimo_venta": "300"}, svc)
    por_id = {r["sku_id"]: r for r in out}
    assert por_id["1"].get("precio_dudoso") and por_id["1"]["vendible"] is False
    assert not por_id["2"].get("precio_dudoso")


# C-4148: producto inventado sin precio
def test_nombre_inventado():
    res = [{"nombre": "INECTO ARGAN OIL x 300"}]
    malo = 'Te recomiendo el "Tratamiento Alisante Sin Formol" de la marca "Liss".'
    assert ch.nombres_inventados(malo, res)
    assert not ch.nombres_inventados('Tengo el "Inecto Argan Oil" a $7.135', res)


# C-4051: "protector solar" → "Dermaglos"
def test_refinamiento_de_marca():
    assert ch.es_refinamiento_de_marca("Dermaglos", "protector solar drenarlos")
    assert not ch.es_refinamiento_de_marca("crema dermaglos", "protector solar")   # trae tipo
    assert not ch.es_refinamiento_de_marca("Dermaglos", "dermaglos")              # sin tipo previo
    svc = _cat()
    r = svc.buscar("protector solar drenarlos Dermaglos")
    assert r and ch.marca_en_resultado("Dermaglos", r[0]["nombre"]) and "SOLAR" in r[0]["nombre"]
