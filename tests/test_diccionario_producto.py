"""
Tinturas y diccionario por producto (15/10, Claudia): las tinturas las atiende
una persona, y cada producto tiene un panel para ver/alimentar con qué
palabras lo encuentra el bot y marcarlo "siempre derivar".
"""
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.routers import backoffice_producto_dic as bpd
from app.routers import webhook as wh
from app.services import checkout_helper as ch
from app.services.config_service import DEFAULTS
from app.services.db import Database
from app.services.sku_service import SKUService
from tests.test_webhook_secuencias import PHONE, _Cfg, _Img, _Intent, _Rag, _Rec, _Socios, _Wa, _msg


# ── Regla por palabras ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("txt", ["tenés tintura koleston 7.1?", "necesito un tinte rubio",
                                 "¿tienen coloración igora?"])
def test_tinturas_derivan(txt):
    assert ch.termino_que_deriva(txt, DEFAULTS)


def test_lo_demas_no_deriva():
    assert ch.termino_que_deriva("un shampoo dove", DEFAULTS) is None
    assert ch.termino_que_deriva("tenés tintura?", {**DEFAULTS, "derivar_terminos": ""}) is None


def _deps(monkeypatch, filas, extras=None):
    from app.services.session_service import SessionService
    deps = {"wa": _Wa(), "sku": SKUService.from_rows(filas, extras or {}),
            "session": SessionService("redis://127.0.0.1:1"),
            "intent": _Intent({"tenés koleston?": {"intencion": "consulta_stock",
                                                    "entidad_producto": "koleston",
                                                    "respuesta": "Tengo el Koleston a $9.000."},
                               "tenés el tono rubio ceniza de koleston?": {
                                   "intencion": "consulta_stock", "entidad_producto": "koleston rubio",
                                   "respuesta": "Tengo el Koleston a $9.000."}}),
            "payment": _Rec(), "audio": _Rec(), "image": _Img(), "perf": _Rec(),
            "config": _Cfg({"derivar_terminos": "tintura"}), "socios": _Socios(), "msgs": _Rec(),
            "metrics": _Rec(), "rag": _Rag()}
    monkeypatch.setattr(wh, "_deps", lambda s=None: deps)
    return deps


async def test_tintura_deriva_en_el_chat(monkeypatch):
    deps = _deps(monkeypatch, [])
    await wh.procesar_mensajes([_msg("hola, tenés tintura rubio ceniza?")])
    assert (await deps["session"].get(PHONE))["estado"] == "operador"
    assert "tono" in deps["wa"].enviados[-1]


async def test_producto_marcado_siempre_derivar(monkeypatch):
    """Un producto marcado desde su diccionario deriva aunque la palabra no
    esté en la lista ("Koleston" no está: el producto sí está marcado)."""
    filas = [{"external_id": "9", "name": "KOLESTON 7.1 RUBIO CENIZA ENV x 1", "price": 9000,
              "brand": "Koleston", "stock": 3}]
    deps = _deps(monkeypatch, filas, {"9": {"siempre_derivar": True}})
    await wh.procesar_mensajes([_msg("tenés el tono rubio ceniza de koleston?")])
    assert (await deps["session"].get(PHONE))["estado"] == "operador"
    assert "$9.000" not in deps["wa"].enviados[-1]


# ── Panel del diccionario ───────────────────────────────────────────────────────
@pytest.fixture
async def cli(pg_dsn, monkeypatch):
    db = Database(pg_dsn)
    assert await db.connect()
    await db.execute("INSERT INTO branches (branch_id, nombre, token_hash) VALUES ('b1', 'Test', 'th-b1') "
                     "ON CONFLICT DO NOTHING")
    await db.execute(
        """INSERT INTO catalog_items (branch_id, external_id, hash, name, brand, price, stock)
           VALUES ('b1', '77', 'h', 'KOLESTON 71 RUB.CEN. ENV x 1', 'Koleston', 9000, 3)
           ON CONFLICT (branch_id, external_id) DO UPDATE SET name = EXCLUDED.name""")
    monkeypatch.setattr(bpd, "_db", lambda: db)

    async def _b():
        return "b1"
    monkeypatch.setattr(bpd, "_branch", _b)
    monkeypatch.setattr(bpd, "_programar_recarga", lambda *a, **k: None)
    sku = SKUService.from_rows([{"external_id": "77", "name": "KOLESTON 71 RUB.CEN. ENV x 1",
                                 "price": 9000, "brand": "Koleston", "stock": 3}])
    monkeypatch.setattr(bpd, "get_sku_service", lambda *a, **k: sku)
    monkeypatch.setattr(get_settings(), "bo_key", "CLAVE")
    app = FastAPI()
    app.include_router(bpd.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t",
                           headers={"x-bo-key": "CLAVE"}) as ac:
        yield ac
    await db.execute("DELETE FROM sku_alias WHERE branch_id = 'b1'")
    await db.execute("DELETE FROM catalog_extras WHERE branch_id = 'b1'")
    await db.execute("DELETE FROM catalog_items WHERE branch_id = 'b1'")
    await db.close()


async def test_ver_y_editar_el_diccionario(cli):
    r = await cli.get("/bo/sku/77/diccionario")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["nombre_erp"].startswith("KOLESTON") and d["terminos"] == [] and not d["siempre_derivar"]

    r = await cli.put("/bo/sku/77/diccionario", json={
        "nombre_legible": "Tintura Koleston 7.1 rubio ceniza",
        "terminos": "tintura, koleston 7.1, rubio ceniza, Tintura",
        "siempre_derivar": True, "autor": "claudia"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["nombre_legible"] == "Tintura Koleston 7.1 rubio ceniza"
    assert d["terminos"] == ["tintura", "koleston 7.1", "rubio ceniza"]       # sin repetidos
    assert d["siempre_derivar"] and d["alias_vigente"] and d["alias_autor"] == "claudia"


async def test_probar_busqueda(cli):
    r = await cli.get("/bo/sku/77/probar", params={"q": "koleston"})
    assert r.status_code == 200, r.text
    assert r.json()["puesto"] == 1


async def test_producto_inexistente(cli):
    assert (await cli.get("/bo/sku/999/diccionario")).status_code == 404
