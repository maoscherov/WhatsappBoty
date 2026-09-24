"""Máquina de estados del vínculo (P3, Estados especiales, §6.3 punto 6). Pura."""
from datetime import datetime, timedelta, timezone

import pytest

from app.radar.vinculo_estados import restriccion_activa, semaforo, sufijo_de, transicion

CONECTA = {"conectado", "reemplazar_anteriores"}


@pytest.mark.parametrize("estado,status,nuevo,efectos", [
    ("creando", "STARTING", "esperando_qr", set()),
    ("esperando_qr", "SCAN_QR_CODE", "esperando_qr", set()),
    ("esperando_qr", "WORKING", "vinculado", CONECTA),
    ("creando", "WORKING", "vinculado", CONECTA),
    ("caido", "WORKING", "vinculado", CONECTA),
    ("vinculado", "WORKING", "vinculado", set()),
    ("esperando_qr", "FAILED", "esperando_qr", {"qr_vencido"}),
    ("vinculado", "FAILED", "caido", {"caida"}),
    ("vinculado", "STOPPED", "caido", {"caida"}),
    ("vinculado", "SCAN_QR_CODE", "caido", {"caida"}),       # el teléfono quitó el dispositivo
    ("caido", "FAILED", "caido", {"caida"}),
    ("vinculado", "AUSENTE", "caido", {"caida"}),
    ("esperando_qr", "AUSENTE", "caido", {"caida", "abandonar"}),
    ("esperando_qr", "PASSKEY_REQUIRED", "esperando_qr", {"passkey"}),
    ("vinculado", "STARTING", "vinculado", set()),
    ("vinculado", "ALGO_NUEVO", "vinculado", {"desconocido"}),
])
def test_transiciones(estado, status, nuevo, efectos):
    t = transicion(estado, status)
    assert (t.estado, set(t.efectos)) == (nuevo, efectos)


@pytest.mark.parametrize("estado", ["cerrando", "cerrado", "abortado"])
def test_vinculo_que_cierra_ignora_todo(estado):
    t = transicion(estado, "WORKING")
    assert t.estado == estado and t.efectos == {"ignorar"}


def test_sufijo_del_numero_solo_4_digitos():
    assert sufijo_de("5493411234567@c.us") == "4567"
    assert sufijo_de("5493411234567:12@s.whatsapp.net") == "4567"
    assert sufijo_de("123456789@lid") is None
    assert sufijo_de("abc@c.us") is None
    assert sufijo_de(None) is None


AHORA = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


def _fila(**kw):
    base = {"link_estado": "vinculado", "waha_status": "WORKING", "restriccion_hasta": None,
            "restriccion_sin_fecha": False, "worker_max_sesiones": 50, "worker_sesiones": 1}
    base.update(kw)
    return base


@pytest.mark.parametrize("fila,color", [
    (_fila(link_estado=None, waha_status=None, restriccion_sin_fecha=None, worker_max_sesiones=None,
           worker_sesiones=None), "gris"),
    (_fila(link_estado="cerrado"), "gris"),
    (_fila(), "verde"),
    (_fila(link_estado="esperando_qr", waha_status="SCAN_QR_CODE"), "amarillo"),
    (_fila(link_estado="caido", waha_status="FAILED"), "rojo"),
    (_fila(restriccion_hasta=AHORA + timedelta(days=1)), "rojo"),
    (_fila(restriccion_hasta=AHORA - timedelta(days=1)), "verde"),
    (_fila(worker_sesiones=40), "rojo"),
])
def test_semaforo(fila, color):
    assert semaforo(fila, AHORA) == color


def test_restriccion_activa():
    assert restriccion_activa(None, True, AHORA) is True
    assert restriccion_activa(AHORA + timedelta(hours=1), False, AHORA) is True
    assert restriccion_activa(AHORA - timedelta(hours=1), False, AHORA) is False
    assert restriccion_activa(None, None, AHORA) is False
