"""
Normalización a E.164 de números argentinos. Cubre los tres formatos que
Radar recibe: el id de WhatsApp (549..., con o sin @c.us), el internacional
con + y el nacional con 0 y 15 opcional ("Suprimir contacto", tramo 6).
"""
import pytest

from app.radar.telefonos import TelefonoNoSoportado, normalizar_e164


@pytest.mark.parametrize("entrada,esperado", [
    ("+54 9 341 123 4567", "+5493411234567"),
    ("+5493411234567", "+5493411234567"),
    ("5493411234567", "+5493411234567"),
    ("5493411234567@c.us", "+5493411234567"),
    ("54 9 11 1234 5678", "+5491112345678"),
    ("0341 15 123 4567", "+5493411234567"),
    ("0341-15-123-4567", "+5493411234567"),
    ("011 15 1234 5678", "+5491112345678"),
    ("02966 15 123456", "+5492966123456"),
    ("0341 412 3456", "+543414123456"),        # fijo, sin 9
    ("+54 341 412 3456", "+543414123456"),
    ("  +54 9 341 123 4567  ", "+5493411234567"),
])
def test_normaliza(entrada, esperado):
    assert normalizar_e164(entrada) == esperado


@pytest.mark.parametrize("entrada", [
    "",
    "hola",
    "+1 555 123 4567",          # otro país
    "341 123 4567",             # nacional sin el 0
    "0341 15 12",               # demasiado corto
    "+54 9 341 123 4567 89",    # demasiado largo
    "+54 8 341 123 4567",       # 11 dígitos sin el 9 de móvil
])
def test_rechaza(entrada):
    with pytest.raises(TelefonoNoSoportado):
        normalizar_e164(entrada)
