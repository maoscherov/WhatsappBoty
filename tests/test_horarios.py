"""
Horario cortado de la farmacia (29/9): lunes a viernes de 7:30 a 13 y de 16
a 19:30, sábados de 8:30 a 12:30. Antes el sistema admitía una sola franja
por día y el bot no sabía contestar "¿qué horario tienen?".
"""
from datetime import datetime as _dt

import pytest

from app.services import config_service as cs
from app.services.checkout_helper import pregunta_horario, responder_horario

LV = {"active": True, "open": "07:30", "close": "19:30",
      "ranges": [{"open": "07:30", "close": "13:00"}, {"open": "16:00", "close": "19:30"}]}
HORARIO = {
    "enabled": True,
    "closed_message": "x",
    "schedule": {**{d: dict(LV) for d in ("mon", "tue", "wed", "thu", "fri")},
                 "sat": {"active": True, "open": "08:30", "close": "12:30"},
                 "sun": {"active": False, "open": "09:00", "close": "13:00"}},
}


@pytest.fixture
def reloj(monkeypatch):
    class _Reloj(_dt):
        ahora = None

        @classmethod
        def now(cls, tz=None):
            return cls.ahora.replace(tzinfo=tz)
    monkeypatch.setattr(cs, "datetime", _Reloj)
    return _Reloj


@pytest.fixture
def svc():
    return cs.ConfigService.__new__(cs.ConfigService)


def test_franjas(svc):
    assert svc.franjas(LV) == [("07:30", "13:00"), ("16:00", "19:30")]
    assert svc.franjas({"active": True, "open": "08:30", "close": "12:30"}) == [("08:30", "12:30")]
    assert svc.franjas({"active": False, "open": "08:30", "close": "12:30"}) == []


@pytest.mark.parametrize("cuando,abierto", [
    (_dt(2026, 9, 29, 7, 0), False),     # martes antes de abrir
    (_dt(2026, 9, 29, 10, 0), True),     # mañana
    (_dt(2026, 9, 29, 14, 30), False),   # corte del mediodía
    (_dt(2026, 9, 29, 17, 0), True),     # tarde
    (_dt(2026, 9, 29, 20, 0), False),    # noche
    (_dt(2026, 10, 3, 11, 0), True),     # sábado
    (_dt(2026, 10, 3, 15, 0), False),    # sábado a la tarde
    (_dt(2026, 10, 4, 10, 0), False),    # domingo
])
def test_abierto_con_horario_cortado(svc, reloj, cuando, abierto):
    reloj.ahora = cuando
    assert svc.is_open_now(HORARIO) is abierto


def test_proxima_apertura(svc, reloj):
    reloj.ahora = _dt(2026, 9, 29, 14, 0)          # martes al mediodía
    assert svc.proxima_apertura(HORARIO) == "hoy a las 16:00"
    reloj.ahora = _dt(2026, 9, 29, 21, 0)          # martes a la noche
    assert svc.proxima_apertura(HORARIO) == "mañana a las 7:30"
    reloj.ahora = _dt(2026, 10, 2, 21, 0)          # viernes a la noche
    assert svc.proxima_apertura(HORARIO) == "mañana a las 8:30"
    reloj.ahora = _dt(2026, 10, 3, 13, 0)          # sábado después de cerrar
    assert svc.proxima_apertura(HORARIO) == "el lunes a las 7:30"


def test_texto_horario(svc):
    assert svc.texto_horario(HORARIO) == (
        "de lunes a viernes de 7:30 a 13:00 y de 16:00 a 19:30 y los sábados de 8:30 a 12:30")


def test_texto_de_retiro(svc, reloj):
    reloj.ahora = _dt(2026, 9, 29, 14, 0)
    assert "hoy de 16:00 a 19:30" in svc.get_pickup_text(HORARIO, 0)
    reloj.ahora = _dt(2026, 9, 29, 9, 0)
    assert "hoy de 7:30 a 13:00 y de 16:00 a 19:30" in svc.get_pickup_text(HORARIO, 0)
    reloj.ahora = _dt(2026, 10, 3, 13, 0)
    assert "el lunes de 7:30 a 13:00 y de 16:00 a 19:30" in svc.get_pickup_text(HORARIO, 0)


@pytest.mark.parametrize("texto,es", [
    ("que horario tienen?", True), ("¿A qué hora abren?", True), ("están abiertos?", True),
    ("abren el sábado?", True), ("hasta qué hora atienden", True),
    ("tenés ibuprofeno?", False), ("a qué hora llega el envío?", False),
])
def test_pregunta_horario(texto, es):
    assert pregunta_horario(texto) is es


def test_respuesta_de_horario(svc, reloj):
    reloj.ahora = _dt(2026, 9, 29, 14, 0)
    r = responder_horario(svc, HORARIO)
    assert r.startswith("Atendemos de lunes a viernes de 7:30 a 13:00 y de 16:00 a 19:30")
    assert "Ahora estamos cerrados: abrimos hoy a las 16:00." in r
    reloj.ahora = _dt(2026, 9, 29, 10, 0)
    assert "Ahora estamos abiertos" in responder_horario(svc, HORARIO)
    assert responder_horario(svc, {"enabled": False, "schedule": {}}) == ""
