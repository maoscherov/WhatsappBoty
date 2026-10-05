"""
Diccionario del catálogo (2/10): abreviaturas y sinónimos en Postgres.

Caso real: "ESTRELLA HIS POT x 125" son hisopos, pero "HIS" no estaba en la
lista fija de siglas y "hisopos estrella" no lo encontraba.
"""
import pytest

from app.services import catalogo_enriquecido as ce
from app.services import diccionario_service as dic
from app.services import sku_service as ss
from app.services.db import Database
from app.services.sku_service import SKUService


def _filas():
    base = {"hash": "a" * 64, "barcodes": [], "troquel": None, "brand": "", "drug": None,
            "form": None, "category": "Perfumeria", "rubro": "", "subrubro": "",
            "therapeutic_actions": [], "stock": 5, "visible": True, "active": True,
            "requiere_receta": "no", "source": "t"}
    return [{**base, "external_id": "1", "name": "ESTRELLA HIS POT x 125", "price": 2500.0},
            {**base, "external_id": "2", "name": "ESTRELLA ALGODON ENV x 140", "price": 3000.0},
            {**base, "external_id": "3", "name": "G-Solucion fisiologica SCH x 500", "price": 1500.0},
            {**base, "external_id": "4", "name": "DOVE ORIGINAL JAB x 90", "price": 1800.0}]


@pytest.fixture(autouse=True)
def _restaurar():
    yield
    dic.aplicar([])            # vuelve a la base del código: no contamina otros tests


def test_base_del_codigo_sin_tabla():
    dic.aplicar([])
    assert ce.ABREVIATURAS["sha"] == "shampoo"
    assert "his" not in ce.ABREVIATURAS
    assert ss.SINONIMOS["paracetamol"] == ["tafirol"]


def test_activa_agrega_descartada_saca_propuesta_no_se_usa():
    dic.aplicar([
        {"tipo": "abreviatura", "termino": "HIS", "equivale": "hisopos", "estado": "activa"},
        {"tipo": "abreviatura", "termino": "cr", "equivale": "crema", "estado": "descartada"},
        {"tipo": "abreviatura", "termino": "tin", "equivale": "tintura", "estado": "propuesta"},
        {"tipo": "sinonimo", "termino": "cotonetes", "equivale": "hisopos", "estado": "activa"},
    ])
    assert ce.ABREVIATURAS["his"] == "hisopos"
    assert "cr" not in ce.ABREVIATURAS and "tin" not in ce.ABREVIATURAS
    assert ss.SINONIMOS["cotonetes"] == ["hisopos"]


def test_hisopos_estrella_se_encuentra_con_la_sigla():
    sin = SKUService.from_rows(_filas())
    assert "hisopos" not in sin._search_index[0]
    dic.aplicar([{"tipo": "abreviatura", "termino": "his", "equivale": "hisopos",
                  "estado": "activa"}])
    con = SKUService.from_rows(_filas())          # el índice se arma al cargar
    assert "hisopos" in con._search_index[0]
    assert con.buscar("hisopos estrella")[0]["sku_id"] == "1"
    assert con.buscar("hisopos")[0]["sku_id"] == "1"


def test_sinonimo_de_cliente():
    dic.aplicar([{"tipo": "sinonimo", "termino": "suero fisiologico",
                  "equivale": "solucion fisiologica", "estado": "activa"}])
    svc = SKUService.from_rows(_filas())
    assert svc.buscar("suero fisiologico")[0]["sku_id"] == "3"


def test_sugerencias_lista_siglas_desconocidas():
    dic.aplicar([])
    svc = SKUService.from_rows(_filas())
    terms = {s["termino"] for s in dic.sugerencias(svc, minimo=1)}
    assert "his" in terms and "jab" not in terms        # jab ya está en la base


# ── Postgres ────────────────────────────────────────────────────────────────────
@pytest.fixture
async def db(pg_dsn):
    d = Database(pg_dsn)
    assert await d.connect()
    yield d
    await d.close()


async def test_migracion_siembra_base_y_propuestas(db):
    filas = await dic.listar(db)
    por = {(f["tipo"], f["termino"]): f for f in filas}
    assert por[("abreviatura", "sha")]["estado"] == "activa"
    assert por[("abreviatura", "his")]["estado"] == "propuesta"
    assert "ESTRELLA HIS" in por[("abreviatura", "his")]["nota"]
    assert por[("sinonimo", "cotonetes")]["estado"] == "propuesta"


async def test_guardar_actualizar_y_cargar(db):
    f = await dic.guardar(db, "abreviatura", " His. ", "Hisopos", autor="Lore")
    assert (f["termino"], f["equivale"], f["estado"]) == ("his", "hisopos", "activa")
    await dic.cargar(db)
    assert ce.ABREVIATURAS["his"] == "hisopos"
    f2 = await dic.actualizar(db, f["id"], estado="descartada")
    assert f2["estado"] == "descartada"
    await dic.cargar(db)
    assert "his" not in ce.ABREVIATURAS
    with pytest.raises(ValueError):
        await dic.guardar(db, "abreviatura", "dos palabras", "x")
    await dic.actualizar(db, f["id"], estado="propuesta")        # deja la semilla como estaba
