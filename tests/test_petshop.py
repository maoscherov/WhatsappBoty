"""
Petshop por capacidad (spec §6.2, `tests/test_petshop.py`): cada test de
petshop tiene su par de farmacia "igual que hoy". Los perfiles se eligen con la
fixture `usar_perfil` (tests/conftest.py).
"""

# ── Recetas: _formatear_productos (intent_service) ──────────────────────────────
_PIPETA = {"nombre": "PIPETA FRONTLINE PLUS PERRO 10-20KG", "precio": 15000.0, "estado": "disponible",
           "cantidad_visible": 4, "sku_id": "20", "requiere_receta": "si"}
_PIPETA_URGENTE = {**_PIPETA, "sku_id": "21", "requiere_receta": "ambiguo", "urgente": True}


def test_formatear_productos_petshop_no_marca_receta(usar_perfil):
    from app.services.intent_service import IntentService
    usar_perfil("petshop")
    txt = IntentService("")._formatear_productos([_PIPETA, _PIPETA_URGENTE])
    assert txt == (
        "1. PIPETA FRONTLINE PLUS PERRO 10-20KG | $15000.00 | Disponible (cantidad aprox: 4) | ID: 20\n"
        "2. PIPETA FRONTLINE PLUS PERRO 10-20KG | $15000.00 | Disponible (cantidad aprox: 4)"
        " | STOCK BAJO - ofrecer con urgencia | ID: 21")


def test_formatear_productos_farmacia_marca_receta_como_hoy(usar_perfil):
    from app.services.intent_service import IntentService
    usar_perfil("farmacia")
    txt = IntentService("")._formatear_productos([_PIPETA, _PIPETA_URGENTE])
    assert txt == (
        "1. PIPETA FRONTLINE PLUS PERRO 10-20KG | $15000.00 | Disponible (cantidad aprox: 4)"
        " | REQUIERE RECETA | ID: 20\n"
        "2. PIPETA FRONTLINE PLUS PERRO 10-20KG | $15000.00 | Disponible (cantidad aprox: 4)"
        " | STOCK BAJO - ofrecer con urgencia | REQUIERE RECETA | ID: 21")


# ══════════════════════════════════════════════════════════════════════════════
# Recetas (recetas = False)
# ══════════════════════════════════════════════════════════════════════════════
import pytest

from app.services.sku_service import SKUService

_PIPETA_ERP = ("MEDICAMENTOS", "PERROS", "ANTIPARASITARIOS", "Pipeta Frontline 10-20kg")


def test_explicar_receta_petshop_no_vende_con_receta(usar_perfil):
    from app.services.catalog_rules import ORIGENES, explicar_receta
    usar_perfil("petshop")
    # La referencia diría "si": el perfil sin recetas gana antes de consultarla.
    assert explicar_receta(*_PIPETA_ERP, referencia=lambda b: "si") == ("no", "sin_recetas")
    assert ORIGENES["sin_recetas"] == "Este comercio no vende con receta"


def test_explicar_receta_farmacia_igual_que_hoy(usar_perfil):
    from app.services.catalog_rules import explicar_receta
    usar_perfil("farmacia")
    assert explicar_receta(*_PIPETA_ERP, referencia=lambda b: None) == ("ambiguo", "sin_referencia")
    assert explicar_receta("Medicamentos Bajo Receta", "", "", "Pipeta Frontline 10-20kg",
                           referencia=lambda b: None) == ("si", "categoria_bajo_receta")


def _item_medicamento():
    from app.models.sync import CatalogItemIn
    return CatalogItemIn(external_id="9001", hash="a" * 64,
                         name="PIPETA FRONTLINE PLUS PERRO 10-20KG", category="MEDICAMENTOS",
                         rubro="PERROS", subrubro="ANTIPARASITARIOS",
                         barcodes=["7790000000001"])


def test_fila_del_catalogo_petshop_no_requiere_receta(usar_perfil, monkeypatch):
    from app.services import receta_referencia
    from app.services.catalog_store import _fila
    usar_perfil("petshop")
    monkeypatch.setattr(receta_referencia, "_MAPA", {"7790000000001": "si"})
    fila = _fila("mascotas-oeste", _item_medicamento(), "mercurio")
    assert fila[17] == "no"          # requiere_receta ($18 del upsert)


def test_fila_del_catalogo_farmacia_igual_que_hoy(usar_perfil, monkeypatch):
    from app.services import receta_referencia
    from app.services.catalog_store import _fila
    usar_perfil("farmacia")
    monkeypatch.setattr(receta_referencia, "_MAPA", {})
    fila = _fila("farmacia-centro", _item_medicamento(), "observer-gestion")
    assert fila[17] == "ambiguo"


def _catalogo_con_receta():
    base = {"hash": "a" * 64, "troquel": None, "brand": "", "drug": None, "form": None,
            "rubro": "PERROS", "subrubro": "", "therapeutic_actions": [], "stock": 5,
            "visible": True, "active": True, "source": "mercurio"}
    return SKUService.from_rows([
        {**base, "external_id": "20", "name": "PIPETA FRONTLINE PLUS PERRO 10-20KG",
         "price": 25000, "barcodes": ["7790000000020"],
         "category": "Medicamentos Bajo Receta", "requiere_receta": "si"},
        {**base, "external_id": "21", "name": "DRONTAL PLUS PERRO X 2", "price": 9000,
         "barcodes": [], "category": "MEDICAMENTOS", "requiere_receta": "ambiguo"},
    ])


@pytest.mark.parametrize("modo", ["conservador", "estricto"])
def test_necesita_receta_petshop_nunca(usar_perfil, modo):
    from app.services.checkout_helper import necesita_receta
    usar_perfil("petshop")
    sku = _catalogo_con_receta()
    assert necesita_receta(sku, "20", modo) is False
    assert necesita_receta(sku, "21", modo) is False


def test_necesita_receta_farmacia_igual_que_hoy(usar_perfil):
    from app.services.checkout_helper import necesita_receta
    usar_perfil("farmacia")
    sku = _catalogo_con_receta()
    assert necesita_receta(sku, "20", "conservador") is True
    assert necesita_receta(sku, "20", "estricto") is True
    assert necesita_receta(sku, "21", "conservador") is True
    assert necesita_receta(sku, "21", "estricto") is False
