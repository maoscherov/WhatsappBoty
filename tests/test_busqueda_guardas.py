"""
Guardas deterministas del buscador (plan 11/9, casos reales de la semana):
1. coincidencia: el fuzzy no ofrece productos sin relación ("dipirona" →
   acondicionador); 4. formas farmacéuticas como tipo; 6. alternativas solo con
   stock (catálogo ERP); 7. los números discriminan (F65, 600 mg, 4%).
"""

from app.services.catalog_live import filtrar_por_stock
from app.services.sku_service import (
    SKUService, normalizar_numeros, numeros_de, quitar_cantidades, resultado_coincide,
)


def _fila(external_id, name, price=1000.0, stock=5, brand="Lab"):
    return {"external_id": external_id, "hash": "a" * 64, "barcodes": [],
            "troquel": None, "name": name, "brand": brand, "drug": None,
            "form": None, "category": "Perfumeria", "rubro": "", "subrubro": "",
            "therapeutic_actions": [], "price": price, "stock": stock,
            "visible": True, "active": True, "requiere_receta": "no",
            "source": "observer-gestion"}


class TestNumeros:
    def test_normaliza_fps_y_unidades(self):
        assert normalizar_numeros("AVENO SOLAR F65 infantil") == "AVENO SOLAR fps 65 infantil"
        assert normalizar_numeros("protector fps50") == "protector fps 50"
        assert normalizar_numeros("aveno infantil por 65") == "aveno infantil fps 65"
        assert normalizar_numeros("ibuprofeno 600mg") == "ibuprofeno 600 mg"

    def test_numeros_discriminantes(self):
        assert numeros_de("ibuprofeno 600") == ["600"]
        assert numeros_de("ibumar 4% susp") == ["4%"]
        assert numeros_de("aveno F65") == ["65"]
        assert numeros_de("x 30") == ["30"]
        assert numeros_de("dame 2") == []          # 1 dígito solo no discrimina

    def test_quitar_cantidades_no_toca_dosis(self):
        assert quitar_cantidades("dame 2 ibuprofeno 600") == "dame  ibuprofeno 600"
        assert quitar_cantidades("3 cajas de tafirol") == " cajas de tafirol"
        assert quitar_cantidades("curflex x 30") == "curflex x 30"
        assert quitar_cantidades("aveno infantil 65") == "aveno infantil 65"

    def test_f65_le_gana_al_gel_de_bano(self):
        # Caso real 11/9: "tenes aveno infantil por 65?" → ofreció el gel de baño.
        svc = SKUService.from_rows([
            _fila("1", "AVENO INFANTIL gel de baño JLI x 250"),
            _fila("2", "AVENO SOLAR F65 infantil CRE x 175"),
            _fila("3", "AVENO SOLAR FPS50 EMU x 175"),
        ])
        assert svc.buscar("tenes aveno infantil por 65?")[0]["sku_id"] == "2"
        assert svc.buscar("aveno infantil fps 65")[0]["sku_id"] == "2"
        # sin número: el gel infantil sigue siendo un match válido
        assert {r["sku_id"] for r in svc.buscar("aveno infantil")} >= {"1", "2"}

    def test_dosis_discrimina(self):
        svc = SKUService.from_rows([
            _fila("400", "Ibupirac Com 400 x 10"),
            _fila("600", "Ibupirac Com 600 x 20"),
        ])
        assert svc.buscar("ibuprofeno 600")[0]["sku_id"] == "600"
        assert svc.buscar("ibupirac 400")[0]["sku_id"] == "400"


class TestCoincidencia:
    def test_dipirona_no_devuelve_acondicionador(self):
        # Caso real 11/9: "dipirona" ~ "acon-DICIONA-dor" pasaba el umbral fuzzy.
        svc = SKUService.from_rows([
            _fila("1", "Acondicionador Suave Crema Nutrición 930 ml"),
            _fila("2", "Acondicionador Dove Nutrición x 400 ml"),
            _fila("3", "Dipirona Amp. 1Gr x 2Ml x 100"),
        ])
        ids = [r["sku_id"] for r in svc.buscar("dipirona")]
        assert ids == ["3"]

    def test_sin_relacion_devuelve_vacio(self):
        svc = SKUService.from_rows([
            _fila("1", "Acondicionador Suave Crema Nutrición 930 ml"),
            _fila("2", "Te Verde Piper Pol Saquitos x 25"),
        ])
        assert svc.buscar("dipirona") == []
        assert svc.buscar("te consulto si tenes") == [] or all(
            "te" in r["nombre"].lower() for r in svc.buscar("te consulto si tenes"))

    def test_sinonimo_sigue_valiendo(self):
        svc = SKUService.from_rows([_fila("1", "Ibupirac 4% Sus x 200")])
        assert svc.buscar("ibuprofeno")[0]["sku_id"] == "1"

    def test_resultado_coincide_reglas(self):
        assert resultado_coincide(["dipirona"], "dipirona amp. 1gr x 2ml")
        assert not resultado_coincide(["dipirona"], "acondicionador suave crema nutricion")
        assert resultado_coincide(["ibuprofeno", "ibupirac"], "ibupirac 4% suspension x 200")
        # los números solos no alcanzan
        assert not resultado_coincide(["dipirona 500"], "paracetamol 500 comprimidos")


class TestFormasFarmaceuticas:
    def test_suspension_no_acepta_comprimidos(self):
        svc = SKUService.from_rows([
            _fila("c", "Ibupirac Com 600 x 20"),
            _fila("s", "Ibupirac 4% Sus x 200"),
        ])
        ids = [r["sku_id"] for r in svc.buscar("ibuprofeno suspension")]
        assert ids == ["s"]

    def test_jarabe_no_acepta_ampollas(self):
        svc = SKUService.from_rows([
            _fila("a", "Dipirona Amp. 1Gr x 2Ml x 100"),
            _fila("j", "Ditral Dipirona Jbe x 70"),
        ])
        ids = [r["sku_id"] for r in svc.buscar("dipirona jarabe")]
        assert ids == ["j"]


class TestAlternativasConStock:
    def test_con_erp_solo_con_stock(self):
        res = [{"sku_id": "1", "sin_stock": True}, {"sku_id": "2", "sin_stock": False},
               {"sku_id": "3", "sin_stock": True}]
        assert [r["sku_id"] for r in filtrar_por_stock(res, fuente="erp")] == ["2"]

    def test_sin_nada_en_stock_queda_el_primero(self):
        res = [{"sku_id": "1", "sin_stock": True}, {"sku_id": "2", "sin_stock": True}]
        assert [r["sku_id"] for r in filtrar_por_stock(res, fuente="erp")] == ["1"]

    def test_con_csv_no_filtra(self):
        res = [{"sku_id": "1", "sin_stock": True}, {"sku_id": "2", "sin_stock": False}]
        assert filtrar_por_stock(res, fuente="csv") == res
