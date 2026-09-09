"""
Chequeo de stock en vivo antes del link de pago (_chequear_stock_vivo):
stock suficiente sigue; stock 0 frena y limpia el pendiente; timeout o agente
caído siguen con el cache (fail-open, D6); precio distinto NO cambia el cobro.
"""

import pytest

import app.services.agent_registry as reg_mod
from app.config import get_settings
from app.services.agent_registry import LookupResult
from app.services.checkout_helper import _chequear_stock_vivo
from app.services.session_service import SessionService

BRANCH = "farmacia-live"


class _FakeRegistry:
    def __init__(self, resultado):
        self._resultado = resultado
        self.consultas = []

    def connected(self, branch_id):
        return True

    async def lookup(self, branch_id, barcodes=None, ids=None, timeout=5.0):
        self.consultas.append(ids or [])
        return self._resultado


@pytest.fixture
def erp_activo(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "default_branch_id", BRANCH)
    monkeypatch.setattr(s, "live_stock_check", "stock")

    def _instalar(resultado):
        fake = _FakeRegistry(resultado)
        monkeypatch.setattr(reg_mod, "_instance", fake)
        return fake

    yield _instalar


def _sesion(sku_id="55", precio=1000.0, cantidad=1) -> dict:
    return {"pending_sku_id": sku_id, "pending_sku_nombre": "Producto Live",
            "pending_precio": precio, "pending_cantidad": cantidad}


class TestChequeoVivo:
    async def test_stock_suficiente_sigue(self, erp_activo):
        erp_activo(LookupResult(items=[{"external_id": "55", "stock": 10,
                                        "price": "1000.00"}]))
        ss = SessionService("redis://127.0.0.1:1")
        msg, precio_erp = await _chequear_stock_vivo(_sesion(), "549L1", ss, {})
        assert msg is None and precio_erp is None

    async def test_stock_cero_frena_y_limpia(self, erp_activo):
        erp_activo(LookupResult(items=[{"external_id": "55", "stock": 0}]))
        ss = SessionService("redis://127.0.0.1:1")
        s = await ss.get("549L2")
        s.update(_sesion())
        await ss.save("549L2", s)

        msg, _ = await _chequear_stock_vivo(_sesion(), "549L2", ss, {})
        assert msg and "Producto Live" in msg
        despues = await ss.get("549L2")
        assert not despues.get("pending_sku_id")   # clear_pending

    async def test_derivar_si_config_lo_pide(self, erp_activo):
        erp_activo(LookupResult(items=[{"external_id": "55", "stock": 0}]))
        ss = SessionService("redis://127.0.0.1:1")
        msg, _ = await _chequear_stock_vivo(
            _sesion(), "549L3", ss, {"sin_stock_mode": "derivar"})
        assert msg
        assert (await ss.get("549L3"))["estado"] == "operador"

    async def test_missing_es_stock_cero(self, erp_activo):
        erp_activo(LookupResult(items=[], missing=["55"]))
        ss = SessionService("redis://127.0.0.1:1")
        msg, _ = await _chequear_stock_vivo(_sesion(), "549L4", ss, {})
        assert msg is not None   # el ERP ya no lo conoce → no se vende

    async def test_timeout_sigue_con_cache(self, erp_activo):
        erp_activo(None)   # lookup devolvió None (timeout/sin agente)
        ss = SessionService("redis://127.0.0.1:1")
        msg, precio = await _chequear_stock_vivo(_sesion(), "549L5", ss, {})
        assert msg is None and precio is None

    async def test_precio_distinto_no_frena_pero_se_reporta(self, erp_activo):
        erp_activo(LookupResult(items=[{"external_id": "55", "stock": 9,
                                        "price": "1350.00"}]))
        ss = SessionService("redis://127.0.0.1:1")
        msg, precio_erp = await _chequear_stock_vivo(
            _sesion(precio=1000.0), "549L6", ss, {})
        assert msg is None            # se cobra el cotizado (D6)
        assert precio_erp == 1350.0   # queda para el evento link_enviado

    async def test_apagado_no_consulta(self, monkeypatch, erp_activo):
        fake = erp_activo(LookupResult(items=[{"external_id": "55", "stock": 0}]))
        monkeypatch.setattr(get_settings(), "live_stock_check", "off")
        ss = SessionService("redis://127.0.0.1:1")
        msg, _ = await _chequear_stock_vivo(_sesion(), "549L7", ss, {})
        assert msg is None and fake.consultas == []

    async def test_mensaje_configurable(self, erp_activo):
        erp_activo(LookupResult(items=[{"external_id": "55", "stock": 0}]))
        ss = SessionService("redis://127.0.0.1:1")
        msg, _ = await _chequear_stock_vivo(
            _sesion(), "549L8", ss,
            {"live_sin_stock_message": "Uy, {producto} se terminó."})
        assert msg == "Uy, Producto Live se terminó."

    async def test_carrito_multiple_frena_por_uno(self, erp_activo):
        erp_activo(LookupResult(items=[
            {"external_id": "1", "stock": 5, "price": "100.00"},
            {"external_id": "2", "stock": 0},
        ]))
        ss = SessionService("redis://127.0.0.1:1")
        session = {"pending_items": [
            {"sku_id": "1", "nombre": "A", "precio": 100.0, "cantidad": 1},
            {"sku_id": "2", "nombre": "B", "precio": 200.0, "cantidad": 2},
        ]}
        msg, _ = await _chequear_stock_vivo(session, "549L9", ss, {})
        assert msg and "B" in msg
