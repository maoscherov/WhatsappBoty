"""Configuración y vocabulario de auditoría que agrega el tramo 2."""
import pytest

from app.radar import auditoria
from app.radar.app import validar_settings
from app.radar.auditoria import ACCIONES, TIPOS_OBJETO, DetalleProhibido
from app.radar.constantes import CAUSAS_FIN
from app.radar.eventos_producto import EVENTOS
from app.radar.settings import RadarSettings

BASE = dict(_env_file=None, database_url="x", migrator_database_url="x", fuente_database_url="x",
            cookie_secret="secreto-de-test-de-32-caracteres!")


def test_hmac_de_webhook_corta_no_arranca():
    with pytest.raises(RuntimeError, match="WAHA_WEBHOOK_HMAC_KEY"):
        validar_settings(RadarSettings(**BASE, waha_webhook_hmac_key="corta"))


def test_hmac_vacia_o_larga_arranca():
    validar_settings(RadarSettings(**BASE))                      # vacía: el receptor rechaza todo
    validar_settings(RadarSettings(**BASE, waha_webhook_hmac_key="h" * 32))


def test_contexto_sin_transporte_waha_por_defecto(radar_ctx):
    assert radar_ctx.waha_transport is None


def test_auditoria_acepta_el_vocabulario_del_tramo2():
    nuevas = {"consentimiento_asistido", "vinculo_iniciado", "vinculo_abortado", "admision_rechazada",
              "qr_reiniciado", "codigo_solicitado", "desconexion_pedida", "borrado_pedido",
              "restriccion_marcada", "restriccion_levantada", "vinculo_cerrado", "consola_abierta",
              "worker_registrado"}
    assert nuevas <= ACCIONES
    assert {"link", "waha_worker"} <= TIPOS_OBJETO
    detalle = {"causa": "caida_72h", "modo": "presencial", "ok": True, "motivo": "sin_capacidad"}
    assert auditoria.validar_detalle(detalle) == detalle
    assert set(CAUSAS_FIN) >= {"pedido_kis", "pedido_dueno", "caida_72h", "reemplazado", "qr_abandonado"}


@pytest.mark.parametrize("malo", [{"causa": "porque_si"}, {"ok": "si"}, {"modo": "telefono"},
                                  {"motivo": "5493411234567"}])
def test_auditoria_rechaza_valores_fuera_de_lista(malo):
    with pytest.raises(DetalleProhibido):
        auditoria.validar_detalle(malo)


def test_eventos_de_producto_del_vinculo():
    assert {"vinculo_iniciado", "vinculo_working", "vinculo_cerrado"} <= EVENTOS
