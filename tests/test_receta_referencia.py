"""
Receta por código de barras (caso real 24/9: con el catálogo del ERP, que
solo dice "Medicamentos", el bot ofrecía Atenolol sin derivar).
"""
import hashlib

import pytest

from app.models.sync import CatalogItemIn
from app.services import receta_referencia as rr
from app.services.catalog_rules import derivar_requiere_receta
from app.services.catalog_store import CatalogStore
from app.services.db import Database

_CSV = (
    "SKU,Nombre,Precio,Marca,Laboratorio,Codigo_Barras_1,Codigo_Barras_2,Codigo_Barras_3,"
    "Codigo_Barras_4,Categoria,Es_Medicamento\n"
    "1,Atenolol Gador 50 Com X30,9000,Gador,Gador,7790001000011,,,,Medicamentos Bajo Receta,true\n"
    "2,Taural F Com 20X30,9758,Roemmers,Roemmers,7795345122427,0007795345122434,,,Medicamentos Bajo Receta,true\n"
    "3,Buscapina N Cto Gts X20,5000,Boehringer,Boehringer,7790002000028,,,,Venta Libre,true\n"
    "4,Ibuprofeno Fecofar X90,606,Fecofar,Fecofar,7790003000035,,,,Medicamentos Bajo Receta,true\n"
    "5,Shampoo Sedal 190 ml,3000,Sedal,Unilever,7790004000042,,,,Shampoo,false\n"
).encode("utf-8")


@pytest.fixture(autouse=True)
def _mapa_limpio(monkeypatch):
    monkeypatch.setattr(rr, "_MAPA", {})
    yield


def test_filas_desde_csv():
    filas = {b: f for b, _, f in rr.filas_desde_csv(_CSV)}
    assert filas["7790001000011"] == "si"             # Atenolol
    assert filas["7795345122427"] == "si"
    assert filas["7795345122434"] == "si"             # segundo código, sin ceros adelante
    assert filas["7790002000028"] == "no"             # Venta Libre
    assert filas["7790003000035"] == "no"             # ibuprofeno: lista blanca OTC
    assert filas["7790004000042"] == "no"             # rubro no medicinal


def test_derivar_con_referencia_y_criterio_conservador(monkeypatch):
    monkeypatch.setattr(rr, "_MAPA", {b: f for b, _, f in rr.filas_desde_csv(_CSV)})
    obs = ("Medicamentos", "Medicamentos", "Medicamentos")    # así viene de Observer
    # Cruza con la referencia por código de barras
    assert derivar_requiere_receta(*obs, "ATENOLOL GADOR 50 mg COM x 30", ["7790001000011"]) == "si"
    assert derivar_requiere_receta(*obs, "BUSCAPINA N GTS x 20", ["7790002000028"]) == "no"
    # Con ceros adelante en el ERP también cruza
    assert derivar_requiere_receta(*obs, "TAURAL F 20 mg COM x 30", ["07795345122427"]) == "si"
    # Medicamento SIN referencia: a validar (deriva en modo conservador)
    assert derivar_requiere_receta(*obs, "COLPURIL RETARD CAP x 50", ["7799999999999"]) == "ambiguo"
    assert derivar_requiere_receta(*obs, "COLPURIL RETARD CAP x 50", []) == "ambiguo"
    # Venta libre conocida gana aunque no esté en la referencia
    assert derivar_requiere_receta(*obs, "IBUPROFENO 400 mg x 10", ["7798888888888"]) == "no"
    # No medicinal
    assert derivar_requiere_receta("Perfumeria", "", "", "PERFUME X", ["7797777777777"]) == "no"
    # La categoría explícita del CSV sigue funcionando
    assert derivar_requiere_receta("Medicamentos Bajo Receta", "", "", "Lotrial 10", []) == "si"


def test_ambiguo_deriva_en_modo_conservador():
    from app.services.sku_service import requiere_derivacion
    assert requiere_derivacion("ambiguo", "conservador") is True


# ── Con Postgres: siembra, recálculo y estado ───────────────────────────────────
@pytest.fixture
async def db(pg_dsn):
    d = Database(pg_dsn)
    assert await d.connect()
    await d.execute("TRUNCATE catalog_extras, catalog_items, branches, receta_referencia")
    await d.execute("INSERT INTO branches (branch_id, nombre, token_hash) VALUES ('suc', 'Suc', 'x') "
                    "ON CONFLICT DO NOTHING")
    yield d
    await d.close()


def _it(eid, name, barcodes, category="Medicamentos"):
    return CatalogItemIn(
        external_id=eid, hash=hashlib.sha256(eid.encode()).hexdigest(), barcodes=barcodes,
        troquel=None, name=name, brand="", drug=None, form=None, category=category,
        rubro="Medicamentos", subrubro="Medicamentos", therapeutic_actions=[],
        price="1000", stock=2, visible=True, active=True)


async def test_inicializar_siembra_y_recalcula(db, tmp_path):
    # El catálogo ERP se escribió con la regla VIEJA: todo "no"
    store = CatalogStore(db)
    await store.upsert_items("suc", [
        _it("a", "ATENOLOL GADOR 50 mg COM x 30", ["7790001000011"]),
        _it("b", "BUSCAPINA N GTS x 20", ["7790002000028"]),
        _it("c", "COLPURIL RETARD CAP x 50", ["7799999999999"]),
        _it("d", "PERFUME X 100", ["7797777777777"], category="Perfumeria"),
    ])
    await db.execute("UPDATE catalog_items SET requiere_receta = 'no'")

    csv = tmp_path / "base.csv"
    csv.write_bytes(_CSV)
    res = await rr.inicializar(db, ruta_csv=str(csv))
    assert res["sembrado"] is True and res["referencia"] >= 5
    flags = {r["external_id"]: r["requiere_receta"]
             for r in await db.fetch("SELECT external_id, requiere_receta FROM catalog_items")}
    assert flags == {"a": "si", "b": "no", "c": "ambiguo", "d": "no"}
    assert res["totales"] == {"si": 1, "no": 2, "ambiguo": 1}

    # Segunda vez: no vuelve a sembrar ni cambia nada
    res2 = await rr.inicializar(db, ruta_csv=str(csv))
    assert res2["sembrado"] is False and res2["cambiados"] == 0

    est = await rr.estado(db)
    assert est["referencia"]["si"] >= 3
    assert est["catalogo"] == {"si": 1, "no": 2, "ambiguo": 1}
    assert est["a_validar_ejemplos"] == ["COLPURIL RETARD CAP x 50"]


async def test_upsert_nuevo_toma_la_referencia(db):
    await rr.reemplazar(db, rr.filas_desde_csv(_CSV), fuente="test")
    await CatalogStore(db).upsert_items("suc", [_it("a", "ATENOLOL GADOR 50 mg COM x 30",
                                                    ["7790001000011"])])
    r = await db.fetch("SELECT requiere_receta FROM catalog_items WHERE external_id = 'a'")
    assert r[0]["requiere_receta"] == "si"
