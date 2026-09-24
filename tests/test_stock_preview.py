"""Vista previa de stock del backoffice ("¿qué ve el bot?", 22/9)."""
import csv
import os
import tempfile

import pytest

from app.services import stock_preview as sp
from app.services.sku_service import SKUService

_ROWS = [
    ("1", "Aveno Protector Solar Infantil F65 X175", "32409", "Aveno", "Aveno", "111", "Dermocosmética", "false"),
    ("2", "Aveno Protector Solar F50 X175", "0", "Aveno", "Aveno", "222", "Dermocosmética", "false"),
    ("3", "Aveno Acondicionador X300", "9000", "Aveno", "Aveno", "333", "Shampoo", "false"),
    ("4", "Ibuprofeno Fecofar Sus X90", "606", "Fecofar", "Fecofar", "444", "Medicamentos Bajo Receta", "true"),
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


@pytest.fixture(autouse=True)
def _sin_erp(monkeypatch):
    """Sin canal en vivo ni Postgres: el preview tiene que funcionar igual."""
    import app.services.catalog_live as cl

    async def _none(*a, **k):
        return None
    monkeypatch.setattr(cl, "lookup_y_aplicar", _none)

    async def _estado():
        return {"fuente": "csv", "branch": None}
    import app.services.catalog_source as cs
    monkeypatch.setattr(cs, "estado", _estado)


async def test_estructura_y_resumen(sku_svc):
    out = await sp.vista_previa_stock("aveno solar", sku_svc, {})
    assert out["query"] == "aveno solar" and out["total_catalogo"] == 4
    assert out["fuente"]["fuente"] == "csv"
    nombres = [p["nombre"] for p in out["productos"]]
    assert any("F65" in n for n in nombres)
    p65 = next(p for p in out["productos"] if "F65" in p["nombre"])
    assert set(p65) >= {"sku_id", "precio", "stock_cache", "stock_vivo", "verificado_en_vivo",
                        "ofrecible", "motivo", "requiere_receta", "precio_socio"}
    assert p65["ofrecible"] is True and p65["motivo"] is None
    assert p65["stock_cache"]["precio"] == 32409.0
    assert out["respuesta_esperada"].startswith("Ofrecería")
    assert "$ 32.409,00" in out["respuesta_esperada"]


async def test_sin_precio_no_es_ofrecible_y_se_marca_dudoso(sku_svc):
    out = await sp.vista_previa_stock("aveno solar", sku_svc, {})
    p50 = next(p for p in out["productos"] if "F50" in p["nombre"])
    assert p50["ofrecible"] is False and p50["motivo"] == "sin_precio"
    # Era dudoso (precio 0) → se intentó verificar; sin canal, queda sin dato vivo
    assert "2" in out["verificacion_vivo"]["ids_consultados"]
    assert out["verificacion_vivo"]["modo"] == "dudosos"
    assert p50["stock_vivo"] is None and p50["verificado_en_vivo"] is False


async def test_vivo_forzado_sin_canal(sku_svc):
    out = await sp.vista_previa_stock("aveno", sku_svc, {}, vivo=True)
    assert out["verificacion_vivo"]["modo"] == "todos"
    assert out["verificacion_vivo"]["canal_disponible"] is False
    assert all(p["stock_vivo"] is None for p in out["productos"])


async def test_vivo_forzado_con_canal(sku_svc, monkeypatch):
    import app.services.catalog_live as cl

    async def _vivos(ids, timeout=None, sku_svc=None):
        return {"1": {"external_id": "1", "stock": 2, "precio": 32409.0},
                "2": {"external_id": "2", "stock": 0, "missing": True}}
    monkeypatch.setattr(cl, "lookup_y_aplicar", _vivos)
    out = await sp.vista_previa_stock("aveno solar", sku_svc, {}, vivo=True)
    assert out["verificacion_vivo"]["canal_disponible"] is True
    p65 = next(p for p in out["productos"] if p["sku_id"] == "1")
    assert p65["stock_vivo"] == {"consultado": True, "encontrado": True, "unidades": 2, "precio": 32409.0}
    p50 = next(p for p in out["productos"] if p["sku_id"] == "2")
    assert p50["stock_vivo"]["encontrado"] is False


async def test_receta_no_ofrecible(sku_svc):
    out = await sp.vista_previa_stock("platsul", sku_svc, {"receta_mode": "estricto"})
    # No hay Platsul en el fixture: sin resultados → resumen de "no lo tiene"
    assert out["productos"] == [] and "No encuentra" in out["respuesta_esperada"]


async def test_descuento_socio(sku_svc, tmp_path):
    from app.services.socio_service import SocioService
    p = tmp_path / "padron.csv"
    p.write_text("APELLIDO,NOMBRE,DNI,SOCIO,CELULAR,DOMICILIO\n"
                 "Muff,Claudia,20111222,4001,3415550001,Mitre 100\n", encoding="utf-8")
    socios = SocioService(str(p))
    cfg = {"socio_discount_pct": "10", "socio_discount_en_catalogo": "true"}
    out = await sp.vista_previa_stock("aveno acondicionador", sku_svc, cfg,
                                      phone="5493415550001", socio_svc=socios)
    assert out["descuento_socio_pct"] == 10.0
    acond = next(p for p in out["productos"] if "Acondicionador" in p["nombre"])
    assert acond["precio_socio"] == pytest.approx(8100.0)
    assert acond["stock_cache"]["precio"] == 9000.0     # el cache no se toca


async def test_erp_responde_lo_mismo_que_el_cache(sku_svc, monkeypatch):
    """Caso real 23/9: el ERP confirma el mismo dato (stock 0) y la pantalla
    decía 'no se pudo consultar el ERP' y mostraba '—' en stock en el ERP."""
    import app.services.catalog_live as cl
    llamados = []

    async def _igual(ids, timeout=None, sku_svc=None):
        llamados.append(list(ids))
        return {i: {"external_id": i, "stock": 0, "precio": 0} for i in ids}
    monkeypatch.setattr(cl, "lookup_y_aplicar", _igual)
    out = await sp.vista_previa_stock("aveno solar", sku_svc, {})
    assert out["verificacion_vivo"]["canal_disponible"] is True
    assert llamados and "2" in llamados[0]          # el dudoso se consultó
    p50 = next(p for p in out["productos"] if p["sku_id"] == "2")
    assert p50["stock_vivo"] == {"consultado": True, "encontrado": True, "unidades": 0, "precio": 0}


def test_formato_pesos():
    assert sp.pesos(13415.38) == "$ 13.415,38"
    assert sp.pesos(0) == "$ 0,00"
    assert sp.pesos(None) == "$ 0,00"
