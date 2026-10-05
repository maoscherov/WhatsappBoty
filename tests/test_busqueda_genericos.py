"""
Genéricos "G-xxx" y "una tira de" (5/10): "famotidina" no encontraba
"G-famotidina ENV x 10" (venta libre, con stock) y el bot ofrecía
Lazartidina con receta; "1 tira de famotidina" traía tiras reactivas.
"""
from app.services.sku_service import SKUService, separar_guiones


def _filas():
    base = {"hash": "a" * 64, "barcodes": [], "troquel": None, "brand": "", "drug": None,
            "form": None, "category": "Medicamentos", "rubro": "", "subrubro": "",
            "therapeutic_actions": [], "stock": 5, "visible": True, "active": True,
            "requiere_receta": "no", "source": "t"}
    return [
        {**base, "external_id": "1", "name": "G-famotidina ENV x 10", "price": 3800.0},
        {**base, "external_id": "2", "name": "LAZARTIDINA 20 mg COM x 60", "price": 9000.0,
         "drug": "Famotidina", "requiere_receta": "si"},
        {**base, "external_id": "3", "name": "tiras medidor de insulina on call ENV x 50",
         "price": 20000.0, "category": "General"},
        {**base, "external_id": "4", "name": "G-KETOROLAC 20 MG COM x 15", "price": 5200.0},
    ]


def test_separar_guiones():
    assert separar_guiones("G-famotidina ENV x 10") == "G famotidina ENV x 10"
    assert separar_guiones("NONISEC P-AD") == "NONISEC P AD"
    assert separar_guiones("rango 10-20") == "rango 10-20"


def test_generico_con_guion_se_encuentra():
    svc = SKUService.from_rows(_filas())
    assert svc.buscar("famotidina")[0]["sku_id"] == "1"
    assert svc.buscar("ketorolac")[0]["sku_id"] == "4"


def test_tira_de_es_la_unidad_no_el_producto():
    svc = SKUService.from_rows(_filas())
    assert svc.buscar("1 tira de famotidina")[0]["sku_id"] == "1"
    assert svc.buscar("una tira de famotidina")[0]["sku_id"] == "1"
    assert svc.buscar("tiras reactivas insulina")[0]["sku_id"] == "3"
