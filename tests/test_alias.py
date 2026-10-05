"""
Alias de productos (capa B, 3/10): nombre legible + términos de cliente
generados con un prompt e importados como CSV.
"""
import pytest

from app.services import alias_service as al
from app.services.catalog_store import CatalogStore
from app.services.db import Database
from app.services.sku_service import SKUService

SALIDA = """```
id;nombre_legible;tipo;terminos;seguro
1;Hisopos Estrella pote x 125;hisopos;cotonetes, palitos para los oídos;SI
2;Algodón Estrella x 140;algodón;;SI
3;Producto XYZ;;;NO
1;repetido;;;SI
;sin id;;;SI
```"""


def _filas():
    base = {"hash": "a" * 64, "barcodes": [], "troquel": None, "brand": "", "drug": None,
            "form": None, "category": "Perfumeria", "rubro": "", "subrubro": "",
            "therapeutic_actions": [], "stock": 5, "visible": True, "active": True,
            "requiere_receta": "no", "source": "t"}
    return [{**base, "external_id": "1", "name": "ESTRELLA HIS POT x 125", "price": 2500.0},
            {**base, "external_id": "2", "name": "ESTRELLA ALGODON ENV x 140", "price": 3000.0},
            {**base, "external_id": "9", "name": "DOVE ORIGINAL JAB x 90", "price": 1800.0}]


def test_parsear_tolera_bloque_de_codigo_y_reporta_errores():
    filas, errores = al.parsear_csv(SALIDA.encode("utf-8-sig"))
    assert [f["external_id"] for f in filas] == ["1", "2", "3"]
    assert filas[0]["terminos"] == "cotonetes, palitos para los oídos"
    assert filas[2]["seguro"] is False
    assert len(errores) == 2                       # id repetido + fila sin id


def test_encabezado_equivocado():
    filas, errores = al.parsear_csv("sku;nombre\n1;x")
    assert not filas and "Encabezado" in errores[0]


def test_alias_vigente_entra_al_indice_y_el_viejo_no():
    alias = {"nombre_base": "ESTRELLA HIS POT x 125", "nombre_legible": "Hisopos Estrella pote x 125",
             "tipo": "hisopos", "terminos": "cotonetes, palitos", "seguro": True}
    assert "cotonetes" in al.texto_indice(alias, "ESTRELLA HIS POT x 125")
    assert al.texto_indice(alias, "ESTRELLA HIS POT x 150") == ""       # el ERP lo renombró
    dudoso = dict(alias, seguro=False)
    assert "cotonetes" not in al.texto_indice(dudoso, "ESTRELLA HIS POT x 125")

    extras = {"1": {"_alias": alias}}
    svc = SKUService.from_rows(_filas(), extras)
    assert svc.buscar("cotonetes")[0]["sku_id"] == "1"
    assert svc.buscar("hisopos")[0]["sku_id"] == "1"


# ── Postgres ────────────────────────────────────────────────────────────────────
@pytest.fixture
async def db(pg_dsn):
    d = Database(pg_dsn)
    assert await d.connect()
    await d.execute("DELETE FROM sku_alias")
    yield d
    await d.execute("DELETE FROM sku_alias")
    await d.close()


async def test_importar_y_cargar_con_el_catalogo(db):
    filas, _ = al.parsear_csv(SALIDA)
    nombres = {"1": "ESTRELLA HIS POT x 125", "2": "ESTRELLA ALGODON ENV x 140"}
    r = await al.importar(db, "suc", filas, nombres, autor="Mariano")
    assert (r["importados"], r["nuevos"], r["actualizados"], r["total_desconocidos"]) == (2, 2, 0, 1)
    r2 = await al.importar(db, "suc", filas[:1], nombres)
    assert (r2["nuevos"], r2["actualizados"]) == (0, 1)

    lista = await al.listar(db, "suc", q="cotonetes")
    assert lista["total"] == 1 and lista["items"][0]["external_id"] == "1"

    # load_rows trae los alias en extras["_alias"]
    _, extras = await CatalogStore(db).load_rows("suc")
    assert extras["1"]["_alias"]["nombre_legible"] == "Hisopos Estrella pote x 125"
