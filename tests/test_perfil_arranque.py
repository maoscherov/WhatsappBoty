"""
Arranque, catálogo y venta por perfil de rubro (spec 4.8 y 4.2, fila main.py):
restauración de archivos, padrón, referencia de recetas, aviso de horario, el
CSV de la farmacia que petshop nunca carga, el tablero y el desvío sin venta.
"""
import logging
from types import SimpleNamespace

import pytest

from app.routers import webhook as wh
from test_webhook_secuencias import PHONE, _msg, entorno  # noqa: F401  (fixture reusada)


# ── _restaurar_archivos ─────────────────────────────────────────────────────────
class _BlobFalso:
    def __init__(self, archivos):
        self.archivos = archivos
        self.pedidos = []

    async def load(self, nombre):
        self.pedidos.append(nombre)
        return self.archivos.get(nombre)


async def test_restaurar_sin_sku_csv_path_no_tira_y_restaura_el_padron(tmp_path, usar_perfil):
    from app.main import _restaurar_archivos
    perfil = usar_perfil("farmacia")
    settings = SimpleNamespace(sku_csv_path="", socios_path=str(tmp_path / "socios.csv"))
    blob = _BlobFalso({"catalogo": (b"SKU,Nombre\n1,X\n", ""),
                       "socios": (b"PK\x03\x04padron", ".xlsx")})
    await _restaurar_archivos(settings, perfil, blob)
    destino = tmp_path / "socios.xlsx"
    assert destino.read_bytes() == b"PK\x03\x04padron"
    assert settings.socios_path == str(destino)


async def test_restaurar_escribe_el_catalogo_si_hay_ruta(tmp_path, usar_perfil):
    from app.main import _restaurar_archivos
    perfil = usar_perfil("farmacia")
    ruta = tmp_path / "cat" / "catalogo.csv"
    settings = SimpleNamespace(sku_csv_path=str(ruta), socios_path=str(tmp_path / "socios.csv"))
    await _restaurar_archivos(settings, perfil, _BlobFalso({"catalogo": (b"SKU,Nombre\n1,X\n", "")}))
    assert ruta.read_bytes() == b"SKU,Nombre\n1,X\n"


async def test_restaurar_petshop_no_toca_el_padron(tmp_path, usar_perfil, caplog):
    from app.main import _restaurar_archivos
    perfil = usar_perfil("petshop")
    settings = SimpleNamespace(sku_csv_path="", socios_path=str(tmp_path / "socios.csv"))
    blob = _BlobFalso({"socios": (b"PK\x03\x04padron", ".xlsx")})
    with caplog.at_level(logging.INFO, logger="app.main"):
        await _restaurar_archivos(settings, perfil, blob)
    assert "socios" not in blob.pedidos
    assert list(tmp_path.iterdir()) == []
    assert settings.socios_path == str(tmp_path / "socios.csv")
    assert "Perfil sin socios: no se carga el padrón" in caplog.text


# ── Padrón en Postgres ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("clave,llamadas", [("farmacia", 1), ("mutual", 1), ("petshop", 0)])
async def test_hidratar_padron_segun_perfil(usar_perfil, monkeypatch, clave, llamadas):
    import app.services.socio_service as socio_mod
    from app.main import _hidratar_padron
    vistos = []

    async def _cargar(db, svc):
        vistos.append(db)
        return 5

    monkeypatch.setattr(socio_mod, "cargar_desde_db", _cargar)
    monkeypatch.setattr(socio_mod, "get_socio_service", lambda path: SimpleNamespace(total=0))

    class _DB:
        def available(self):
            return True

    perfil = usar_perfil(clave)
    await _hidratar_padron(SimpleNamespace(socios_path="data/socios.csv"), perfil, _DB())
    assert len(vistos) == llamadas


# ── Referencia de recetas (spec 4.2, fila main.py) ──────────────────────────────
@pytest.mark.parametrize("clave,llamadas", [("farmacia", 1), ("mutual", 1), ("petshop", 0)])
async def test_init_referencia_receta_segun_perfil(usar_perfil, monkeypatch, caplog,
                                                   clave, llamadas):
    import app.services.receta_referencia as rr
    from app.main import _init_referencia_receta
    vistos = []

    async def _inicializar(db):
        vistos.append(db)
        return {"referencia": 3}

    monkeypatch.setattr(rr, "inicializar", _inicializar)
    usar_perfil(clave)
    with caplog.at_level(logging.INFO, logger="app.main"):
        r = await _init_referencia_receta("DB")
    assert vistos == ["DB"] * llamadas
    if llamadas:
        assert r == {"referencia": 3}
    else:
        assert r is None
        assert "Perfil sin recetas" in caplog.text


# ── Aviso de horario por defecto ────────────────────────────────────────────────
class _CfgHoras:
    def __init__(self, horas):
        self.horas = horas

    async def get_hours(self):
        return self.horas


async def test_avisa_si_el_horario_es_el_de_defecto(caplog):
    from app.main import _avisar_horario_por_defecto
    from app.services.config_service import DEFAULT_HOURS
    with caplog.at_level(logging.WARNING, logger="app.main"):
        assert await _avisar_horario_por_defecto(_CfgHoras(dict(DEFAULT_HOURS))) is True
    assert "Horario no cargado: se usa DEFAULT_HOURS" in caplog.text

    caplog.clear()
    cargado = {**DEFAULT_HOURS, "enabled": True}
    with caplog.at_level(logging.WARNING, logger="app.main"):
        assert await _avisar_horario_por_defecto(_CfgHoras(cargado)) is False
    assert "DEFAULT_HOURS" not in caplog.text


# ── Catálogo: el CSV de la farmacia nunca entra en petshop ──────────────────────
@pytest.fixture
def sku_singleton(monkeypatch):
    """reload_sku_service pisa el singleton: se restaura al terminar el test."""
    from app.services import sku_service as sk
    monkeypatch.setattr(sk, "_instance", None)
    return sk


def test_csv_de_arranque_petshop_rechaza_el_csv_de_la_farmacia(usar_perfil, caplog):
    from app.services.catalog_source import CSV_FARMACIA, csv_de_arranque
    usar_perfil("petshop")
    with caplog.at_level(logging.ERROR, logger="app.services.catalog_source"):
        assert csv_de_arranque("data/catalogo_base.csv") == ""
    assert any(r.levelno == logging.ERROR and "catálogo de la farmacia" in r.getMessage()
               for r in caplog.records)
    assert csv_de_arranque(str(CSV_FARMACIA)) == ""
    assert csv_de_arranque("") == ""
    assert csv_de_arranque("data/catalogo_mo.csv") == "data/catalogo_mo.csv"


def test_csv_de_arranque_farmacia_y_mutual_igual_que_hoy(usar_perfil):
    from app.services.catalog_source import csv_de_arranque
    for clave in ("farmacia", "mutual"):
        usar_perfil(clave)
        assert csv_de_arranque("data/catalogo_base.csv") == "data/catalogo_base.csv"


def test_reload_petshop_no_carga_el_csv_de_la_farmacia(usar_perfil, sku_singleton, tmp_path):
    usar_perfil("petshop")
    assert sku_singleton.reload_sku_service("data/catalogo_base.csv").total == 0

    mo = tmp_path / "catalogo_mo.csv"
    mo.write_text(
        "SKU,Nombre,Precio,Marca,Laboratorio,Codigo_Barras_1,Categoria,Es_Medicamento\n"
        "1,Royal Canin Medium Adult 15 kg,98000,ROYAL CANIN,,7790187000011,ALIMENTOS,false\n"
        "2,Piedras Sanicat 4 kg,6200,SANICAT,,7798043000022,PIEDRAS SANITARIAS,false\n",
        encoding="utf-8")
    assert sku_singleton.reload_sku_service(str(mo)).total == 2


def test_get_sku_service_petshop_arranca_vacio(usar_perfil, sku_singleton):
    usar_perfil("petshop")
    assert sku_singleton.get_sku_service().total == 0       # default: el CSV de la farmacia


def test_farmacia_carga_el_csv_como_hoy(usar_perfil, sku_singleton):
    usar_perfil("farmacia")
    svc = sku_singleton.reload_sku_service("data/catalogo_base.csv")
    assert svc.total == 17192
    assert svc.buscar("ibuprofeno")


def _catalogo_erp_mo():
    """Catálogo que dejó el sync de Mercurio (set_sku_service), un producto."""
    from app.services.sku_service import SKUService
    return SKUService.from_rows([{
        "external_id": "31", "name": "ROYAL CANIN MEDIUM ADULT 15KG", "price": 98000.0,
        "hash": "e" * 64, "barcodes": [], "troquel": None, "brand": "", "drug": None,
        "form": None, "category": "ALIMENTOS", "rubro": "PERROS", "subrubro": "",
        "therapeutic_actions": [], "stock": 5, "visible": True, "active": True,
        "requiere_receta": "no", "source": "mercurio"}])


async def test_aplicar_fuente_csv_en_petshop_no_toca_el_catalogo(usar_perfil, sku_singleton,
                                                                 monkeypatch, caplog):
    """Revisión final, hallazgo 17: catalogo_fuente=csv (guardado antes de la
    guarda del panel) no aplica a un perfil sin catalogo_csv_base. Antes
    vaciaba el catálogo del ERP en memoria; ahora no lo toca y loguea ERROR."""
    from app.config import get_settings
    from app.services import catalog_source as cs
    usar_perfil("petshop")
    # usar_perfil no recrea Settings: un delenv no llegaría. Se pisan los atributos.
    monkeypatch.setattr(get_settings(), "default_branch_id", "")
    monkeypatch.setattr(get_settings(), "sku_csv_path", "data/catalogo_base.csv")
    erp = sku_singleton.set_sku_service(_catalogo_erp_mo())

    async def _fuente_csv():
        return "csv"

    monkeypatch.setattr(cs, "fuente_configurada", _fuente_csv)
    monkeypatch.setattr(cs, "_cache", {"branch_id": None, "at": 0.0})
    monkeypatch.setattr(cs, "estado_recarga", dict(cs.estado_recarga))
    with caplog.at_level(logging.ERROR, logger="app.services.catalog_source"):
        est = await cs.aplicar_fuente()
    assert sku_singleton.get_sku_service() is erp                 # el catálogo del ERP sigue
    assert est["total_productos"] == 1
    assert any(r.levelno == logging.ERROR and "catalogo_fuente=csv" in r.getMessage()
               for r in caplog.records)


def _fuente_erp_sin_sucursal(monkeypatch, cs):
    """catalogo_fuente=erp y resolver_branch_default sin sucursal (Postgres
    caído o sin catalog_items)."""
    async def _fuente_erp():
        return "erp"

    async def _sin_sucursal(forzar=False):
        return None

    monkeypatch.setattr(cs, "fuente_configurada", _fuente_erp)
    monkeypatch.setattr(cs, "resolver_branch_default", _sin_sucursal)
    monkeypatch.setattr(cs, "_cache", {"branch_id": None, "at": 0.0})
    monkeypatch.setattr(cs, "estado_recarga", dict(cs.estado_recarga))


async def test_aplicar_fuente_erp_sin_sucursal_en_petshop_conserva_el_catalogo(
        usar_perfil, sku_singleton, monkeypatch, caplog):
    """Ronda de arreglo 2 (residuo del hallazgo 17): con la fuente erp y sin
    sucursal resuelta, aplicar_fuente caía a reload_sku_service(sku_csv_path)
    y en petshop dejaba el catálogo vacío. Ahora no recarga nada, conserva el
    catálogo del ERP en memoria y loguea ERROR."""
    from app.config import get_settings
    from app.services import catalog_source as cs
    usar_perfil("petshop")
    monkeypatch.setattr(get_settings(), "default_branch_id", "")
    monkeypatch.setattr(get_settings(), "sku_csv_path", "")
    erp = sku_singleton.set_sku_service(_catalogo_erp_mo())
    _fuente_erp_sin_sucursal(monkeypatch, cs)
    with caplog.at_level(logging.ERROR, logger="app.services.catalog_source"):
        est = await cs.aplicar_fuente()
    assert sku_singleton.get_sku_service() is erp                 # el catálogo del ERP sigue
    assert est["total_productos"] == 1
    assert any(r.levelno == logging.ERROR and "sin sucursal" in r.getMessage()
               for r in caplog.records)


async def test_aplicar_fuente_erp_sin_sucursal_en_farmacia_recarga_el_csv(
        usar_perfil, sku_singleton, monkeypatch, tmp_path):
    """Guarda: la farmacia (catalogo_csv_base) cae al CSV como hoy."""
    from app.config import get_settings
    from app.services import catalog_source as cs
    usar_perfil("farmacia")
    csv = tmp_path / "catalogo.csv"
    csv.write_bytes(_CSV_CHICO_FARMACIA)
    monkeypatch.setattr(get_settings(), "default_branch_id", "")
    monkeypatch.setattr(get_settings(), "sku_csv_path", str(csv))
    erp = sku_singleton.set_sku_service(_catalogo_erp_mo())
    _fuente_erp_sin_sucursal(monkeypatch, cs)
    est = await cs.aplicar_fuente()
    assert sku_singleton.get_sku_service() is not erp
    assert est["total_productos"] == 2 and est["fuente"] == "csv"


_CSV_CHICO_FARMACIA = (
    "SKU,Nombre,Precio,Marca,Laboratorio,Codigo_Barras_1,Categoria,Es_Medicamento\n"
    "1,Ibuprofeno 600 mg x 10,3500,IBUPIRAC,PFIZER,7790000000011,ANALGESICOS,true\n"
    "2,Shampoo Dove 400 ml,4200,DOVE,UNILEVER,7790000000022,PERFUMERIA,false\n"
).encode("utf-8")


# ── Revisión final, hallazgo 17: el panel no importa CSV en un perfil del ERP ───
class _BlobGuardado:
    def __init__(self):
        self.guardados = []

    async def save(self, nombre, data, ext):
        self.guardados.append(nombre)


class _CfgBo:
    def __init__(self):
        self.v = {"catalogo_fuente": "erp"}
        self.sets = []

    async def set_many(self, updates):
        self.sets.append(dict(updates))
        self.v.update(updates)

    async def get_all(self):
        return dict(self.v)


_ERP_409 = {"detail": "Este comercio toma el catálogo del ERP"}
_CSV_CHICO = (
    "SKU,Nombre,Precio,Marca,Laboratorio,Codigo_Barras_1,Categoria,Es_Medicamento\n"
    "1,Royal Canin Medium Adult 15 kg,98000,ROYAL CANIN,,7790187000011,ALIMENTOS,false\n"
    "2,Piedras Sanicat 4 kg,6200,SANICAT,,7798043000022,PIEDRAS SANITARIAS,false\n"
).encode("utf-8")


@pytest.fixture
def panel(monkeypatch, tmp_path, sku_singleton):
    """Router /bo con TestClient: SKU_CSV_PATH en tmp, blob y config falsos y
    aplicar_fuente registrada (sin Redis ni Postgres)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.config import get_settings
    from app.routers import backoffice
    from app.services import catalog_source as cs
    ruta = tmp_path / "data" / "catalogo.csv"
    monkeypatch.setattr(get_settings(), "bo_key", "")
    monkeypatch.setattr(get_settings(), "sku_csv_path", str(ruta))
    blob, cfg, aplicadas = _BlobGuardado(), _CfgBo(), []
    monkeypatch.setattr(backoffice, "get_blob_store", lambda url: blob)
    monkeypatch.setattr(backoffice, "get_config_service", lambda url: cfg)

    async def _aplicar():
        aplicadas.append(True)
        return {}

    monkeypatch.setattr(cs, "aplicar_fuente", _aplicar)
    app = FastAPI()
    app.include_router(backoffice.router)
    return SimpleNamespace(client=TestClient(app), ruta=ruta, blob=blob, cfg=cfg,
                           aplicadas=aplicadas, sku=sku_singleton)


def test_petshop_importar_csv_responde_409_sin_tocar_nada(usar_perfil, panel):
    usar_perfil("petshop")
    erp = panel.sku.set_sku_service(_catalogo_erp_mo())
    r = panel.client.post("/bo/sku/import", files={"file": ("cat.csv", _CSV_CHICO, "text/csv")})
    assert r.status_code == 409
    assert r.json() == _ERP_409
    assert not panel.ruta.exists()                        # no escribió el archivo
    assert panel.blob.guardados == []                     # ni guardó el blob
    assert panel.sku.get_sku_service() is erp             # ni recargó el catálogo


def test_petshop_importar_pdf_responde_409_sin_tocar_nada(usar_perfil, panel):
    usar_perfil("petshop")
    erp = panel.sku.set_sku_service(_catalogo_erp_mo())
    r = panel.client.post("/bo/sku/import-pdf",
                          files=[("files", ("existencias.pdf", b"%PDF-1.4 informe", "application/pdf"))])
    assert r.status_code == 409
    assert r.json() == _ERP_409
    assert not panel.ruta.exists()
    assert panel.blob.guardados == []
    assert panel.sku.get_sku_service() is erp


@pytest.mark.parametrize("valor", ["csv", " CSV "])
def test_petshop_config_rechaza_fuente_csv(usar_perfil, panel, valor):
    usar_perfil("petshop")
    r = panel.client.patch("/bo/config", json={"catalogo_fuente": valor})
    assert r.status_code == 422
    assert "ERP" in r.json()["detail"]
    assert panel.cfg.sets == []                           # no se guardó nada
    assert panel.aplicadas == []                          # ni se aplicó


def test_petshop_config_fuente_erp_sigue_andando(usar_perfil, panel):
    """Guarda: volver a la fuente ERP no se bloquea."""
    usar_perfil("petshop")
    r = panel.client.patch("/bo/config", json={"catalogo_fuente": "erp"})
    assert r.status_code == 200
    assert panel.cfg.sets == [{"catalogo_fuente": "erp"}]
    assert panel.aplicadas == [True]


def test_farmacia_importa_el_catalogo_igual_que_hoy(usar_perfil, panel):
    """Guarda: en la farmacia (catalogo_csv_base) la importación y el botón de
    pánico no cambian."""
    usar_perfil("farmacia")
    r = panel.client.post("/bo/sku/import", files={"file": ("cat.csv", _CSV_CHICO, "text/csv")})
    assert r.status_code == 200
    assert r.json()["total"] == 2
    assert panel.ruta.read_bytes() == _CSV_CHICO
    assert panel.blob.guardados == ["catalogo"]
    assert panel.sku.get_sku_service().total == 2

    # El PDF pasa la guarda y lo rechaza su propia validación (no es un informe real)
    r = panel.client.post("/bo/sku/import-pdf",
                          files=[("files", ("existencias.pdf", b"%PDF-1.4 informe", "application/pdf"))])
    assert r.status_code == 422

    r = panel.client.patch("/bo/config", json={"catalogo_fuente": "csv"})
    assert r.status_code == 200
    assert panel.cfg.sets == [{"catalogo_fuente": "csv"}]
    assert panel.aplicadas == [True]


# ── Tablero ─────────────────────────────────────────────────────────────────────
def test_tablero_usa_la_clave_del_perfil(usar_perfil, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.config import get_settings
    usar_perfil(" Petshop ")                       # se normaliza: strip + lower
    monkeypatch.setattr(get_settings(), "bo_key", "")   # sin clave del panel
    r = TestClient(app).get("/bo/tablero?mes=2026-09")
    assert r.status_code == 200
    body = r.json()
    assert body["vertical"] == "petshop"
    assert "producto" in body                      # tablero de venta


# ── Desvío sin venta: se pregunta por la capacidad, no por el nombre ────────────
@pytest.mark.parametrize("vertical,llamadas", [
    ("farmacia", 0), ("petshop", 0), ("mutual", 1), ("Mutual", 1)])
async def test_flujo_mutual_solo_sin_venta(entorno, usar_perfil, monkeypatch, vertical, llamadas):
    usar_perfil(vertical)
    vistos = []

    async def _flujo(deps, phone, session, texto, *a, **k):
        vistos.append(texto)
        return "respuesta de la mutual", "mutual_info"

    monkeypatch.setattr(wh, "_flujo_mutual", _flujo)
    deps = entorno()
    await wh.procesar_mensajes([_msg("hola")])
    assert vistos == ["hola"] * llamadas
    if llamadas:
        assert deps["wa"].enviados[-1] == "respuesta de la mutual"


# ── Review Focus: primer arranque de MO, con el catálogo vacío ──────────────────
async def test_petshop_con_catalogo_vacio_no_inventa_ni_deja_pendiente(entorno, usar_perfil,
                                                                       sku_singleton):
    """Hasta el primer sync de Mercurio el catálogo está vacío (spec 9.2): el
    bot no puede ofrecer ni cobrar lo que no tiene, aunque el modelo lo invente."""
    usar_perfil("petshop")
    txt = "hola, tenés royal canin medium adult 15 kg?"
    deps = entorno({txt: {"intencion": "consulta_stock",
                          "entidad_producto": "royal canin medium adult 15 kg",
                          "respuesta": "¡Sí! Tengo Royal Canin Medium Adult 15 kg a $98.000 🐾"}})
    deps["sku"] = sku_singleton.get_sku_service()          # lo que hay antes del primer sync
    assert deps["sku"].total == 0
    await wh.procesar_mensajes([_msg(txt)])
    enviado = deps["wa"].enviados[-1]
    assert enviado.startswith("No lo encuentro en nuestro catálogo")
    assert "98.000" not in enviado and "farmac" not in enviado.lower()
    s = await deps["session"].get(PHONE)
    assert not s.get("pending_sku_id")
