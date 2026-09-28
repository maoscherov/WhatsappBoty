"""
Marcas manuales de receta (28/9): el operador marca un producto como venta
libre / con receta desde el ABM o desde la conversación derivada; queda
registrado quién y cuándo, el bot lo toma en el acto y, si el operador lo
elige en el modal, retoma la venta.
"""
import hashlib

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app.services.db as dbmod
import app.services.sku_service as skumod
from app.config import get_settings
from app.models.sync import CatalogItemIn
from app.services import receta_marcas
from app.services import receta_referencia as rr
from app.services.catalog_store import CatalogStore
from app.services.db import Database
from app.services.session_service import SessionService
from app.services.sku_service import SKUService

PHONE = "5493410000077"
BR = "suc"


@pytest.fixture(autouse=True)
def _sin_referencia(monkeypatch):
    monkeypatch.setattr(rr, "_MAPA", {})


@pytest.fixture
async def db(pg_dsn):
    d = Database(pg_dsn)
    assert await d.connect()
    await d.execute("TRUNCATE catalog_extras, catalog_items, branches, receta_cambios "
                    "RESTART IDENTITY")
    await d.execute("INSERT INTO branches (branch_id, nombre, token_hash) VALUES ($1, 'Suc', 'x') "
                    "ON CONFLICT DO NOTHING", BR)
    await CatalogStore(d).upsert_items(BR, [
        _it("1", "COLPURIL RETARD CAP x 50", ["7799000000011"], stock=4),
        _it("2", "LOPERAMIDA GENERICO 2 mg x 10", ["7799000000028"], stock=10),
        _it("3", "DROGA SIN STOCK x 20", ["7799000000035"], stock=0),
        _it("4", "PERFUME X 100", ["7799000000042"], stock=3, category="Perfumeria"),
    ])
    yield d
    await d.close()


def _it(eid, name, barcodes, stock=2, category="Medicamentos"):
    return CatalogItemIn(
        external_id=eid, hash=hashlib.sha256(eid.encode()).hexdigest(), barcodes=barcodes,
        troquel=None, name=name, brand="", drug=None, form=None, category=category,
        rubro="Medicamentos" if category == "Medicamentos" else "", subrubro="",
        therapeutic_actions=[], price="1500", stock=stock, visible=True, active=True)


@pytest.fixture
def bot_catalogo(monkeypatch):
    """Catálogo en memoria del bot con los mismos productos (a validar)."""
    filas = [{"external_id": eid, "hash": "a" * 64, "barcodes": [], "troquel": None,
              "name": n, "brand": "", "drug": None, "form": None, "category": "Medicamentos",
              "rubro": "", "subrubro": "", "therapeutic_actions": [], "price": 1500.0,
              "stock": 5, "visible": True, "active": True, "requiere_receta": "ambiguo",
              "source": "t"}
             for eid, n in (("1", "COLPURIL RETARD CAP x 50"),
                            ("2", "LOPERAMIDA GENERICO 2 mg x 10"))]
    svc = SKUService.from_rows(filas)
    monkeypatch.setattr(skumod, "_instance", svc)
    return svc


@pytest.fixture
async def cli(db, monkeypatch):
    from app.routers import backoffice_receta as br
    monkeypatch.setattr(get_settings(), "bo_key", "CLAVE")

    async def _branch(*a, **k):
        return BR
    monkeypatch.setattr(br, "resolver_branch_default", _branch)
    recargas = []
    monkeypatch.setattr(br, "_programar_recarga", lambda b, ids: recargas.append(set(ids)))
    ss = SessionService("redis://127.0.0.1:1")
    monkeypatch.setattr(br, "get_session_service", lambda *a, **k: ss)
    prev = dbmod._instance
    dbmod._instance = db
    app = FastAPI()
    app.include_router(br.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t",
                           headers={"x-bo-key": "CLAVE"}) as ac:
        yield ac, ss
    dbmod._instance = prev


# ── Marcar ──────────────────────────────────────────────────────────────────────
async def test_marcar_venta_libre_registra_y_aplica_en_el_bot(db, bot_catalogo):
    r = await receta_marcas.marcar(db, BR, "2", "no", "Belén", "abm")
    assert r["cambio"] and r["anterior"] == "ambiguo" and r["marca"] == "no"
    assert r["marca_manual"] == "no" and r["origen"] == "manual"
    assert r["ultimo_cambio"]["autor"] == "Belén"
    # El bot lo toma en el acto (sin recargar el catálogo)
    assert bot_catalogo.get_by_id("2").requiere_receta == "no"
    cambios = await receta_marcas.cambios(db, BR, "2")
    assert len(cambios) == 1
    assert (cambios[0]["anterior"], cambios[0]["nuevo"], cambios[0]["autor"],
            cambios[0]["origen"]) == ("ambiguo", "no", "Belén", "abm")


async def test_sacar_la_marca_vuelve_a_la_regla(db, bot_catalogo):
    await receta_marcas.marcar(db, BR, "2", "no", "Belén", "abm")
    r = await receta_marcas.marcar(db, BR, "2", None, "Belén", "abm")
    assert r["marca"] == "ambiguo" and r["marca_manual"] is None
    assert r["origen"] == "sin_referencia"
    assert bot_catalogo.get_by_id("2").requiere_receta == "ambiguo"
    assert len(await receta_marcas.cambios(db, BR, "2")) == 2


async def test_marcar_igual_no_registra_de_nuevo(db):
    await receta_marcas.marcar(db, BR, "2", "no", "Belén", "abm")
    r = await receta_marcas.marcar(db, BR, "2", "no", "Otro", "abm")
    assert r["cambio"] is False
    assert len(await receta_marcas.cambios(db, BR, "2")) == 1


async def test_marcar_valida(db):
    with pytest.raises(ValueError):
        await receta_marcas.marcar(db, BR, "2", "quizas", "x", "abm")
    with pytest.raises(LookupError):
        await receta_marcas.marcar(db, BR, "999", "no", "x", "abm")


# ── ABM ─────────────────────────────────────────────────────────────────────────
async def test_listado_por_defecto_a_validar_con_stock(db):
    total, items = await receta_marcas.listar(db, BR)
    assert total == 2 and [p["external_id"] for p in items] == ["1", "2"]
    assert all(p["marca"] == "ambiguo" and p["origen"] == "sin_referencia" for p in items)
    # Con o sin stock, por nombre y por código de barras
    total, _ = await receta_marcas.listar(db, BR, con_stock=False)
    assert total == 3
    _, items = await receta_marcas.listar(db, BR, q="lopera", marca="todas")
    assert [p["external_id"] for p in items] == ["2"]
    _, items = await receta_marcas.listar(db, BR, q="7799000000011", marca="todas")
    assert [p["external_id"] for p in items] == ["1"]
    # Manuales
    await receta_marcas.marcar(db, BR, "2", "no", "Belén", "abm")
    _, items = await receta_marcas.listar(db, BR, marca="manual")
    assert [p["external_id"] for p in items] == ["2"]
    assert items[0]["ultimo_cambio"]["autor"] == "Belén"
    with pytest.raises(ValueError):
        await receta_marcas.listar(db, BR, marca="cualquiera")


async def test_resumen(db):
    await receta_marcas.marcar(db, BR, "2", "no", "Belén", "abm")
    r = await receta_marcas.resumen(db, BR)
    assert r == {"si": 0, "no": 2, "ambiguo": 2, "a_validar_con_stock": 1, "manuales": 1}


async def test_endpoints_abm(cli, bot_catalogo):
    c, _ = cli
    r = await c.get("/bo/sku/receta")
    assert r.status_code == 200 and r.json()["total"] == 2
    r = await c.post("/bo/sku/receta/lote",
                     json={"external_ids": ["1", "2", "999"], "requiere_receta": "no",
                           "autor": "Belén"})
    assert r.json() == {"cambiados": 2, "sin_cambio": 0, "no_encontrados": ["999"]}
    assert bot_catalogo.get_by_id("1").requiere_receta == "no"
    assert (await c.get("/bo/sku/receta")).json()["total"] == 0
    csv = (await c.get("/bo/sku/receta/export.csv", params={"marca": "manual"})).text
    assert "COLPURIL" in csv and "Marca manual" in csv and "Belén" in csv
    cambios = (await c.get("/bo/receta/cambios")).json()["cambios"]
    assert len(cambios) == 2 and {x["origen"] for x in cambios} == {"lote"}
    assert (await c.post("/bo/sku/2/receta", json={"requiere_receta": "tal vez"})).status_code == 422
    assert (await c.post("/bo/sku/999/receta", json={"requiere_receta": "no"})).status_code == 404
    assert (await c.get("/bo/sku/receta/resumen")).json()["manuales"] == 2


# ── Desde la conversación ───────────────────────────────────────────────────────
async def test_al_derivar_por_receta_se_recuerda_el_producto(bot_catalogo):
    from app.services.checkout_helper import derivar_si_receta
    ss = SessionService("redis://127.0.0.1:1")
    await ss.set_pending(PHONE, "1", "COLPURIL RETARD CAP x 50", 1500.0, cantidad=2, opciones=[])
    msg = await derivar_si_receta(bot_catalogo, ss, {"receta_mode": "conservador"}, PHONE, "1")
    assert msg and "receta" in msg
    s = await ss.get(PHONE)
    assert s["estado"] == "operador" and not s.get("pending_sku_id")
    assert s["_productos_receta"] == [{"sku_id": "1", "nombre": "COLPURIL RETARD CAP x 50",
                                       "cantidad": 2, "ts": s["_productos_receta"][0]["ts"]}]


async def test_productos_de_la_conversacion(cli):
    c, ss = cli
    s = await ss.get(PHONE)
    s["_productos_receta"] = [{"sku_id": "2", "nombre": "LOPERAMIDA", "cantidad": 1}]
    await ss.save(PHONE, s)
    await ss.set_estado(PHONE, "operador", motivo="receta")
    r = (await c.get(f"/bo/session/{PHONE}/productos")).json()
    assert r["estado"] == "operador" and r["derivada_motivo"] == "receta"
    p = r["productos"][0]
    assert p["external_id"] == "2" and p["marca"] == "ambiguo" and p["marcable"] is True
    assert p["motivo"] == "frenado_por_receta"


class _Wa:
    def __init__(self):
        self.enviados = []

    async def send_text(self, phone, texto, **k):
        self.enviados.append(texto)
        return True


@pytest.fixture
def deps_bot(monkeypatch, bot_catalogo):
    """Dependencias del bot para retomar la venta, sin red ni ERP."""
    from app.routers import webhook as wh
    from app.services import checkout_helper as ch
    from app.services import message_store as ms
    from app.services.config_service import DEFAULTS

    class _Cfg:
        async def get_all(self):
            return dict(DEFAULTS)

    class _Socios:
        def find_by_phone(self, phone):
            return None

    deps = {"config": _Cfg(), "sku": bot_catalogo, "socios": _Socios(), "payment": object(),
            "wa": _Wa()}
    monkeypatch.setattr(wh, "_deps", lambda s=None: deps)

    async def _stock_ok(*a, **k):
        return None, None
    monkeypatch.setattr(ch, "_chequear_stock_vivo", _stock_ok)
    historial = []

    async def _hist(phone, role, content, *a, **k):
        historial.append((role, content))
    monkeypatch.setattr(ms, "guardar_historico", _hist)
    deps["historial"] = historial
    return deps


async def test_marcar_desde_la_conversacion_y_devolver_al_bot(cli, deps_bot):
    c, ss = cli
    deps_bot["session"] = ss
    s = await ss.get(PHONE)
    s["_productos_receta"] = [{"sku_id": "2", "nombre": "LOPERAMIDA", "cantidad": 2}]
    await ss.save(PHONE, s)
    await ss.set_estado(PHONE, "operador", motivo="receta")

    r = await c.post("/bo/sku/2/receta", json={"requiere_receta": "no", "autor": "Belén",
                                               "phone": PHONE, "devolver_al_bot": True})
    j = r.json()
    assert r.status_code == 200 and j["devuelto_al_bot"] is True and j["aviso"] is None
    enviado = deps_bot["wa"].enviados[-1]
    assert enviado == j["mensaje"]
    assert "se vende sin receta" in enviado and "x2" in enviado and "$3,000.00" in enviado
    assert "retiro en sucursal" in enviado
    s = await ss.get(PHONE)
    assert s["estado"] == "esperando_entrega" and s["pending_sku_id"] == "2"
    assert s["history"][-1]["content"] == enviado
    assert deps_bot["historial"][-1] == ("assistant", enviado)
    cambios = (await c.get("/bo/sku/2/receta/cambios")).json()["cambios"]
    assert cambios[0]["origen"] == "conversacion" and cambios[0]["phone"] == PHONE


async def test_devolver_al_bot_con_el_bot_apagado_avisa_y_la_marca_queda(cli, deps_bot):
    c, ss = cli
    deps_bot["session"] = ss
    from app.services.config_service import DEFAULTS

    class _CfgApagado:
        async def get_all(self):
            return {**DEFAULTS, "bot_enabled": "false"}
    deps_bot["config"] = _CfgApagado()
    await ss.set_estado(PHONE, "operador", motivo="receta")
    j = (await c.post("/bo/sku/2/receta", json={"requiere_receta": "no", "phone": PHONE,
                                                "devolver_al_bot": True})).json()
    assert j["devuelto_al_bot"] is False and "bot está apagado" in j["aviso"]
    assert j["marca"] == "no"
    assert deps_bot["wa"].enviados == []
    assert (await ss.get(PHONE))["estado"] == "operador"


async def test_devolver_al_bot_solo_con_venta_libre(cli, deps_bot):
    c, ss = cli
    deps_bot["session"] = ss
    j = (await c.post("/bo/sku/2/receta", json={"requiere_receta": "si", "phone": PHONE,
                                                "devolver_al_bot": True})).json()
    assert j["devuelto_al_bot"] is False and "venta libre" in j["aviso"]
    assert deps_bot["wa"].enviados == []
