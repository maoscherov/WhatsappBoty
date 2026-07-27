"""
Tests de la lógica de negocio que no depende de servicios externos:
búsqueda de catálogo, receta desde categoría, matchers de entrega/humano.
"""

import csv
import os
import tempfile

import pytest

from app.services.sku_service import SKUService, requiere_derivacion
from app.services import checkout_helper as ch


# ── Catálogo de prueba (formato base, con categorías) ──────────────────────────
_ROWS = [
    # SKU, Nombre, Precio, Marca, Laboratorio, barras..., Categoria, Es_Medicamento
    ("1", "Platsul A Cre X800", "193316", "Soubeiran", "Soubeiran", "111", "Medicamentos Bajo Receta", "true"),
    ("2", "Ibuprofeno Fecofar Sus X90", "606.45", "Fecofar", "Fecofar", "222", "Medicamentos Bajo Receta", "true"),
    ("3", "Buscapina N Cto Gts X20", "5000", "Boehringer", "Boehringer", "333", "Venta Libre", "true"),
    ("4", "Shampoo Sedal Ceramidas 190 ml", "3000", "Sedal", "Unilever", "444", "Shampoo", "false"),
    ("5", "Bagovit A Plus Cre X100", "54734", "Bago", "Bago", "555", "Dermocosmética", "false"),
]


@pytest.fixture
def sku_svc():
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["SKU", "Nombre", "Precio", "Marca", "Laboratorio",
                    "Codigo_Barras_1", "Categoria", "Es_Medicamento"])
        w.writerows(_ROWS)
    svc = SKUService(path)
    yield svc
    os.remove(path)


# ── Receta derivada de la categoría ────────────────────────────────────────────
def test_receta_desde_categoria(sku_svc):
    platsul = sku_svc.get_by_barcode("111")
    ibu = sku_svc.get_by_barcode("222")
    busca = sku_svc.get_by_barcode("333")
    shampoo = sku_svc.get_by_barcode("444")
    assert platsul.requiere_receta == "si"
    assert ibu.requiere_receta == "si"
    assert busca.requiere_receta == "no"   # Venta Libre
    assert shampoo.requiere_receta == "no"


def test_requiere_derivacion():
    assert requiere_derivacion("si", "conservador") is True
    assert requiere_derivacion("si", "estricto") is True
    assert requiere_derivacion("ambiguo", "conservador") is True
    assert requiere_derivacion("ambiguo", "estricto") is False
    assert requiere_derivacion("no", "conservador") is False


# ── Búsqueda ────────────────────────────────────────────────────────────────────
def test_busqueda_encuentra(sku_svc):
    r = sku_svc.buscar("platsul")
    assert r and "platsul" in r[0]["nombre"].lower()


def test_busqueda_sin_resultados(sku_svc):
    assert sku_svc.buscar("producto inexistente xyzzy") == []


def test_busqueda_sinonimo(sku_svc):
    # "ibuprofeno" no está literal pero el producto sí; el catálogo lo tiene por nombre
    r = sku_svc.buscar("ibuprofeno")
    assert any("ibuprofeno" in x["nombre"].lower() for x in r)


# ── Matchers de entrega / humano ────────────────────────────────────────────────
@pytest.mark.parametrize("txt", ["retiro", "lo paso a buscar", "voy a la sucursal"])
def test_match_retiro(txt):
    assert ch.match_retiro(txt) is True
    assert ch.match_envio(txt) is False


@pytest.mark.parametrize("txt", ["envio", "a domicilio", "mandámelo a casa"])
def test_match_envio(txt):
    assert ch.match_envio(txt) is True
    assert ch.match_retiro(txt) is False


@pytest.mark.parametrize("txt", ["si ahí", "dale ahí", "sí, a mi domicilio"])
def test_afirma_envio(txt):
    assert ch.afirma_envio(txt) is True


@pytest.mark.parametrize("txt", ["quiero hablar con una persona", "me pasás con un asesor?",
                                 "necesito atención humana"])
def test_pide_humano(txt):
    assert ch.pide_humano(txt) is True


@pytest.mark.parametrize("txt", ["quiero buscapina", "algo para una persona mayor", "hola"])
def test_no_pide_humano(txt):
    assert ch.pide_humano(txt) is False


# ── Derivación por receta: hand-off limpio ──────────────────────────────────────
async def test_derivar_receta_limpia_pending():
    """Al derivar por receta, se limpia el pending y queda en operador —
    así no queda un producto que re-dispare la derivación en cada mensaje."""
    from app.services.session_service import SessionService

    ss = SessionService("redis://127.0.0.1:1")  # sin Redis → in-memory
    await ss.set_pending("549", sku_id="X1", sku_nombre="Lotrial", precio=100.0,
                         cantidad=1, opciones=[])

    class _FakeSku:
        def get_by_id(self, sku_id):
            return type("S", (), {"requiere_receta": "si"})()

    msg = await ch.derivar_si_receta(_FakeSku(), ss, {"receta_mode": "conservador"}, "549", "X1")
    assert msg is not None
    sess = await ss.get("549")
    assert sess["estado"] == "operador"
    assert sess["pending_sku_id"] is None   # producto limpiado
