"""
Descuento unificado (empleado 20% no acumulable / socio), dirección del socio
en la pregunta de entrega y aviso de presentación distinta (25/9).
"""
import sys
import types

import pytest

from app.services import checkout_helper as ch


class _Socios:
    def __init__(self, socios):
        self.s = socios

    def find_by_phone(self, phone):
        return self.s.get(phone)


@pytest.fixture
def empleados(monkeypatch):
    """Empleado de prueba sin depender de la implementación de la tabla."""
    padron = {}
    mod = types.ModuleType("app.services.empleado_service")

    class _Svc:
        def find_by_phone(self, phone):
            return padron.get(phone)
    svc = _Svc()
    mod.get_empleado_service = lambda: svc
    monkeypatch.setitem(sys.modules, "app.services.empleado_service", mod)
    return padron


CFG = {"socio_discount_pct": "15", "empleado_discount_pct": "20"}


def test_descuento_empleado_reemplaza_al_de_socio(empleados):
    socios = _Socios({"549A": {"nombre": "Ana"}, "549B": {"nombre": "Beto"}})
    empleados["549A"] = {"nombre_pila": "Ana", "grupo": "mutual"}      # socia Y empleada
    empleados["549C"] = {"nombre_pila": "Caro", "grupo": "cooperativa"}  # solo empleada
    assert ch.descuento_para("549A", CFG, socios) == (20.0, "empleado")   # no 35
    assert ch.descuento_para("549B", CFG, socios) == (15.0, "socio")
    assert ch.descuento_para("549C", CFG, socios) == (20.0, "empleado")
    assert ch.descuento_para("549Z", CFG, socios) == (0.0, "")


def test_descuento_empleado_apagado_cae_al_de_socio(empleados):
    socios = _Socios({"549A": {"nombre": "Ana"}})
    empleados["549A"] = {"nombre_pila": "Ana"}
    assert ch.descuento_para("549A", {**CFG, "empleado_discount_pct": "0"}, socios) == (15.0, "socio")


def test_catalogo_con_descuento_de_empleado(empleados):
    empleados["549C"] = {"nombre_pila": "Caro"}
    res, pct = ch.aplicar_descuento_socio(
        [{"sku_id": "1", "nombre": "Perfume", "precio": 10000.0, "requiere_receta": "no"},
         {"sku_id": "2", "nombre": "Atenolol", "precio": 9000.0, "requiere_receta": "si"}],
        "549C", CFG, _Socios({}))
    assert pct == 20.0
    assert res[0]["precio"] == pytest.approx(8000.0)
    assert res[1]["precio"] == 9000.0          # con receta: no se toca


def test_cotizacion_receta_con_etiqueta():
    from app.services.receta_ocr import cotizar_receta
    c = cotizar_receta(10000, es_socio=True, pct_socio=20, etiqueta="empleado")
    assert "por ser empleado" in c["desglose"] and c["precio_final"] == 8000.0
    assert "por ser socio" in cotizar_receta(10000, es_socio=True, pct_socio=15)["desglose"]


# ── dirección del socio en la pregunta de entrega ───────────────────────────────
def test_pregunta_entrega_con_domicilio():
    socios = _Socios({"549A": {"domicilio": "San Javier 837"}, "549B": {"domicilio": ""}})
    cfg = {"envio_costo": "2000"}
    con = ch.pregunta_entrega(cfg, saludo=False, phone="549A", socio_svc=socios)
    assert "*envío a San Javier 837* (+$2,000)" in con and "otra dirección" in con
    sin = ch.pregunta_entrega(cfg, saludo=False, phone="549B", socio_svc=socios)
    assert "*envío a domicilio*" in sin and "otra dirección" not in sin
    assert "*envío a domicilio*" in ch.pregunta_entrega(cfg)          # sin teléfono


# ── presentación distinta ───────────────────────────────────────────────────────
@pytest.mark.parametrize("pedido,ofrecido,esperado", [
    ("Atenolol 50 x50", "ATENOLOL GADOR 50 mg COM x 30", True),
    ("atenolol 50", "ATENOLOL GADOR 50 mg COM x 30", False),
    ("taural f 20 mg x 30", "TAURAL F MAX 10 mg COM x 10", True),
    ("actron 600", "ACTRON 600 mg x 10", False),
    ("ibuprofeno", "IBUPIRAC 400 x 10", False),
    ("ibuprofeno 600", "IBUPIRAC 400 mg x 10", True),
])
def test_presentacion_distinta(pedido, ofrecido, esperado):
    assert ch.presentacion_distinta(pedido, ofrecido) is esperado


def test_aviso_presentacion():
    r = ch.aviso_presentacion("Atenolol 50 x50", "Tengo el Atenolol Gador 50 mg x 30 a $20.174,01.")
    assert r.startswith("No tengo Atenolol 50 x50 en esa presentación.")
    ya = "Justo no tengo el de 50, tengo el de 30 a $20.174,01."
    assert ch.aviso_presentacion("Atenolol 50 x50", ya) == ya
