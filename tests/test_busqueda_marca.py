"""
Búsqueda por marca (1/10): "unesia crema" ofrecía Dermaglos y Micoclin
porque UNESIA viene en ungüento; "protector facial eximia" daba Lucy
Anderson porque los de Eximia se llaman "SOLAIRE". Y "¿será más efectivo la
laca?" se tomaba como pedido de pagar en efectivo.
"""
import pytest

from app.services.checkout_helper import pide_efectivo
from app.services.sku_service import SKUService


def _fila(eid, name, stock=3, category="Cosméticos"):
    return {"external_id": eid, "hash": "a" * 64, "barcodes": [], "troquel": None, "name": name,
            "brand": "", "drug": None, "form": None, "category": category, "rubro": "",
            "subrubro": "", "therapeutic_actions": [], "price": 1000.0, "stock": stock,
            "visible": True, "active": True, "requiere_receta": "no", "source": "t"}


@pytest.fixture(scope="module")
def svc():
    filas = [
        _fila("u1", "UNESIA UNG x 20", stock=8),
        _fila("u2", "COMBO PIECIDEX + UNESIA cr.x20g ENV x 2", stock=0),
        _fila("u3", "UNESIA LAKESIA AMOROLFINA LACA SOL x 5", stock=1),
        _fila("d1", "DERMAGLOS F GLICOLICO MANDELICO CRE x 50"),
        _fila("m1", "MICOCLIN CRE x 20"),
        _fila("e1", "EXIMIA SOLAIRE EXTREME FLUIDE CRE x 50"),
        _fila("e2", "EXIMIA HYALU ABSOLUT NOCHE CRE x 50"),
        _fila("l1", "lucy anderson protector facial 50 ENV x 50"),
        _fila("t1", "REXONA EFFICIENT ORIGINAL TAL x 100"),
    ]
    # Rexona es una marca GRANDE: muchos productos (desodorantes en crema).
    filas += [_fila(f"r{i}", f"REXONA CLINICAL DESOD {i} CRE x 48") for i in range(40)]
    filas += [_fila(f"x{i}", f"PRODUCTO GENERICO {i} SHA x 200") for i in range(200)]
    return SKUService.from_rows(filas)


def _nombres(svc, q):
    return [r["nombre"] for r in svc.buscar(q)]


def test_marca_chica_en_otra_presentacion(svc):
    r = _nombres(svc, "unesia crema")
    assert "UNESIA UNG x 20" in r
    assert not any("DERMAGLOS" in n or "MICOCLIN" in n for n in r)


def test_protector_de_la_marca_pedida(svc):
    r = _nombres(svc, "protector facial eximia")
    assert r and r[0].startswith("EXIMIA SOLAIRE")
    assert not any("lucy" in n.lower() for n in r)


def test_marca_grande_respeta_el_tipo(svc):
    r = _nombres(svc, "talco rexona")
    assert r and all("TAL" in n for n in r)          # no un desodorante Rexona


def test_lo_que_tiene_stock_no_queda_afuera():
    filas = [_fila(f"o{i}", f"OLEO CALCAREO MARCA{i} EMU x 200", stock=0) for i in range(6)]
    filas.append(_fila("ok", "OLEO CALCAREO DISPONIBLE EMU x 500", stock=2))
    filas += [_fila(f"x{i}", f"PRODUCTO GENERICO {i} SHA x 200") for i in range(100)]
    r = SKUService.from_rows(filas).buscar("oleo calcareo")
    assert any(x["nombre"].startswith("OLEO CALCAREO DISPONIBLE") for x in r)


@pytest.mark.parametrize("texto,es", [
    ("necesito unesia en crema o será más efectivo la laca?", False),
    ("cuál es más efectiva?", False),
    ("pago en efectivo", True),
    ("efectivo", True),
    ("lo abono en efectivo cuando lo retiro", True),
])
def test_efectivo_de_pago_vs_eficaz(texto, es):
    assert pide_efectivo(texto) is es
