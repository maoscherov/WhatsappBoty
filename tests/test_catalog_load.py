"""
Unidad: reglas de receta del catálogo ERP y carga de SKUService desde filas
de Postgres (from_rows) — sin base de datos.
"""

from app.services.catalog_rules import derivar_requiere_receta
from app.services.sku_service import SKUService


def _fila(**kw) -> dict:
    base = {
        "external_id": "100", "hash": "a" * 64, "barcodes": ["779000000001"],
        "troquel": None, "name": "Producto Test", "brand": "MarcaX",
        "drug": None, "form": None, "category": "Medicamentos",
        "rubro": "", "subrubro": "", "therapeutic_actions": [],
        "price": 1500.0, "stock": 5, "visible": True, "active": True,
        "requiere_receta": "no", "source": "observer-gestion",
    }
    base.update(kw)
    return base


class TestDerivarRequiereReceta:
    def test_categoria_bajo_receta(self):
        assert derivar_requiere_receta("Medicamentos Bajo Receta", "", "", "Clonazepam 2mg") == "si"
        assert derivar_requiere_receta("", "medicamentos bajo receta", "", "X") == "si"
        assert derivar_requiere_receta("", "", "MEDICAMENTOS BAJO RECETA", "X") == "si"

    def test_otc_en_lista_blanca_gana(self):
        # Ibupirac está en VENTA_LIBRE: aunque la categoría diga bajo receta.
        assert derivar_requiere_receta("Medicamentos Bajo Receta", "", "", "Ibupirac 600") == "no"

    def test_perfumeria_nunca_receta(self):
        assert derivar_requiere_receta("Perfumería", "", "", "Shampoo Sedal") == "no"

    def test_default_no(self):
        assert derivar_requiere_receta("", "", "", "Algo") == "no"
        assert derivar_requiere_receta("Medicamentos", "", "", "Algo") == "no"


class TestFromRows:
    def test_mapeo_completo(self):
        svc = SKUService.from_rows([_fila(drug="ibuprofeno", rubro="Analgésicos")])
        sku = svc.get_by_id("100")
        assert sku and sku.sku_nombre == "Producto Test"
        assert sku.barcode == "779000000001"
        assert sku.marca == "MarcaX" and sku.laboratorio == "MarcaX"
        assert sku.es_medicamento is True
        assert sku.precio_venta == 1500.0
        assert sku.estado == "disponible"
        # la droga y el rubro entran al índice de búsqueda
        assert svc.buscar("ibuprofeno")[0]["sku_id"] == "100"

    def test_precio_null_es_consultar(self):
        svc = SKUService.from_rows([_fila(price=None)])
        assert svc.get_by_id("100").estado == "consultar"

    def test_visible_false_es_pausado(self):
        svc = SKUService.from_rows([_fila(visible=False)])
        assert svc.get_by_id("100").pausado is True
        assert svc.buscar("Producto Test") == []   # no se ofrece

    def test_inactivo_se_carga_pausado(self):
        # El manifiesto lo desactivó pero un cliente lo tenía pendiente:
        # get_by_id lo sigue encontrando; el chequeo en vivo lo frena al cobrar.
        svc = SKUService.from_rows([_fila(active=False)])
        sku = svc.get_by_id("100")
        assert sku is not None and sku.pausado is True

    def test_override_de_receta_desde_extras(self):
        svc = SKUService.from_rows(
            [_fila(requiere_receta="no")],
            {"100": {"requiere_receta_override": "si"}})
        assert svc.get_by_id("100").requiere_receta == "si"

    def test_pausa_manual_desde_extras(self):
        svc = SKUService.from_rows([_fila()], {"100": {"pausado_manual": True}})
        assert svc.get_by_id("100").pausado is True

    def test_cb_compartido_gana_el_que_tiene_stock(self):
        cb = "779999999999"
        svc = SKUService.from_rows([
            _fila(external_id="1", name="Sin stock", barcodes=[cb], stock=0),
            _fila(external_id="2", name="Con stock", barcodes=[cb], stock=8),
        ])
        assert svc.get_by_barcode(cb).sku_id == "2"

    def test_todos_los_cb_indexados(self):
        svc = SKUService.from_rows([_fila(barcodes=["111", "222", "333"])])
        for cb in ("111", "222", "333"):
            assert svc.get_by_barcode(cb).sku_id == "100"
