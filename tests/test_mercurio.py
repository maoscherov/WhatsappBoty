"""
Mercurio (Mascotas del Oeste): parseo de artículos reales de preproducción
(fixtures del 14/9), cliente REST con reintentos y sync completo contra el
Postgres embebido usando un transporte HTTP simulado que sirve los fixtures.
"""

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

import app.services.db as dbmod
from app.services.db import Database
from app.services.mercurio_service import (
    CATALOGOS, MercurioClient, MercurioError, MercurioSync, articulo_a_item,
    es_padre, parsear_stock_x_deposito,
)

FIX = Path(__file__).parent / "fixtures" / "mercurio"


def _fixture(nombre: str) -> dict:
    return json.loads((FIX / nombre).read_text(encoding="utf-8"))


def _taxonomias() -> dict[str, dict[str, str]]:
    out = {}
    for cat, clave in CATALOGOS.items():
        items = _fixture(f"catalogo_{cat}.json")["items"]
        out[cat] = {str(i[clave]): i["descripcion"] for i in items}
    return out


def _articulos() -> list[dict]:
    return _fixture("articulos_pagina1.json")["articulos"]


class TestParseo:
    def test_stock_x_deposito(self):
        assert parsear_stock_x_deposito("1|8.00ç4|0.00ç27|1.00") == {"1": 8.0, "4": 0.0, "27": 1.0}
        assert parsear_stock_x_deposito("1|17.00") == {"1": 17.0}
        assert parsear_stock_x_deposito(None) == {}
        assert parsear_stock_x_deposito("basura") == {}

    def test_padres_y_variantes_del_fixture(self):
        arts = _articulos()
        padres = [a for a in arts if es_padre(a)]
        variantes = [a for a in arts if not es_padre(a)]
        assert len(arts) == 27
        assert len(padres) == 5
        assert {p["codigo"] for p in padres} == {"ROY1051701", "PET1304801", "KIP1295301",
                                                 "BON1645401", "10196750P"}
        assert all(a["codigo"] != a["codigo_padre"] for a in variantes)

    def test_mapeo_de_una_variante_real(self):
        tax = _taxonomias()
        royal = next(a for a in _articulos() if a["id_articulo_mercurio"] == "7508")
        item = articulo_a_item(royal, tax)
        assert item.external_id == "7508"
        assert item.name == "ROYAL URINARY CAT S/O HIGH DILUTION 400GRS"
        assert item.brand == "ROYAL"
        assert item.rubro == "GATOS" and item.subrubro == "SECOS" and item.category == "ALIMENTOS"
        assert item.form == "400 gr"
        assert item.price == Decimal("1728.42")
        assert item.stock == 9
        assert set(item.barcodes) == {"7790187338852", "7790187340237"}
        # etapa/edad/tamaño: solo los ids que existen en su catálogo
        esperado = [x for x in (tax["materiales"].get(royal["id_material"]),
                                tax["edades-mascota"].get(royal["id_edad_mascota"]),
                                tax["tamanios-mascota"].get(str(royal["id_tamanio_mascota"]))) if x]
        assert item.therapeutic_actions == esperado
        assert len(item.hash) == 64

    def test_barcodes_internos_se_filtran(self):
        tax = _taxonomias()
        correa = next(a for a in _articulos() if a["id_articulo_mercurio"] == "15181")
        # "15181000" tiene 8 dígitos: pasa el filtro de forma (indistinguible);
        # el pretal "680320" (6 dígitos) no.
        pretal = next(a for a in _articulos() if a["id_articulo_mercurio"] == "9809")
        assert articulo_a_item(pretal, tax).barcodes == []
        assert articulo_a_item(correa, tax).barcodes == ["15181000"]

    def test_hash_estable_y_sensible_a_precio_y_stock(self):
        tax = _taxonomias()
        a = next(x for x in _articulos() if x["id_articulo_mercurio"] == "7508")
        h1 = articulo_a_item(a, tax).hash
        assert articulo_a_item(dict(a), tax).hash == h1
        assert articulo_a_item(dict(a, stock="8.00"), tax).hash != h1
        assert articulo_a_item(dict(a, precio="1800"), tax).hash != h1
        # el RowNum/orden (paginación) NO cambia el hash
        assert articulo_a_item(dict(a, RowNum="99", orden="1"), tax).hash == h1


# ── Transporte simulado que sirve los fixtures ───────────────────────────────

def _transport(fallas: dict | None = None):
    """Responde como Mercurio preprod. `fallas` = {path: [status, status, ...]}
    para simular 429/500 antes de responder bien."""
    fallas = dict(fallas or {})

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/v1", "", 1)
        if path in fallas and fallas[path]:
            st = fallas[path].pop(0)
            return httpx.Response(st, headers={"Retry-After": "0"} if st == 429 else {},
                                  json={"error": True, "message": "simulado"})
        if request.headers.get("Authorization") != "Bearer mrc_test":
            return httpx.Response(401, json={"error": True, "message": "clave"})
        if path == "/estado":
            return httpx.Response(200, json={"ok": True, "hora": "2026-09-14T08:31:25-03:00"})
        for cat in CATALOGOS:
            if path == f"/{cat}":
                return httpx.Response(200, json=_fixture(f"catalogo_{cat}.json"))
        if path == "/articulos/paginas":
            return httpx.Response(200, json=_fixture("articulos_paginas.json"))
        if path == "/articulos":
            return httpx.Response(200, json=_fixture("articulos_pagina1.json"))
        if path == "/articulos/7508/stock":
            return httpx.Response(200, json=_fixture("stock_primer_articulo.json"))
        if path.startswith("/articulos/") and path.endswith("/stock"):
            return httpx.Response(404, json={"error": True, "message": "no existe"})
        return httpx.Response(404, json={"error": True, "message": "ruta"})

    return httpx.MockTransport(handler)


def _cliente(fallas=None) -> MercurioClient:
    return MercurioClient("mrc_test", "https://api.mercurio.test/v1", timeout=5,
                          transport=_transport(fallas))


class TestCliente:
    async def test_catalogo_y_paginas(self):
        c = _cliente()
        assert (await c.estado())["ok"] is True
        marcas = await c.catalogo("marcas")
        assert marcas["112"] == "ROYAL"
        assert await c.paginas() == 1
        assert len(await c.articulos(1)) == 27

    async def test_stock_en_vivo(self):
        c = _cliente()
        assert await c.stock("7508") == {"1": 8.0, "4": 0.0, "27": 1.0}
        assert await c.stock("999999") is None            # 404 → no existe

    async def test_reintenta_429_y_500(self, monkeypatch):
        import app.services.mercurio_service as m
        async def _sin_espera(_s):
            return None
        monkeypatch.setattr(m.asyncio, "sleep", _sin_espera)
        c = _cliente({"/articulos/paginas": [429, 500]})
        assert await c.paginas() == 1                     # tercer intento OK

    async def test_401_no_reintenta(self):
        c = MercurioClient("mrc_mala", "https://api.mercurio.test/v1", transport=_transport())
        with pytest.raises(MercurioError):
            await c.paginas()


# ── Sync completo contra Postgres embebido ───────────────────────────────────

@pytest.fixture
async def db(pg_dsn):
    import app.services.catalog_store as csmod
    import app.services.branch_store as bsmod
    d = Database(pg_dsn)
    assert await d.connect()
    await d.execute("TRUNCATE catalog_extras, catalog_items, branches")
    prev = dbmod._instance
    dbmod._instance = d
    csmod._instance = None
    bsmod._instance = None
    yield d
    dbmod._instance = prev
    csmod._instance = None
    bsmod._instance = None
    await d.close()


class TestSync:
    async def test_sync_carga_solo_variantes(self, db, monkeypatch):
        import app.services.catalog_source as cs
        async def _csv():
            return "csv"          # que no recargue el catálogo en memoria del proceso
        monkeypatch.setattr(cs, "fuente_configurada", _csv)

        sync = MercurioSync(_cliente(), "mascotas-test", db)
        rep = await sync.sincronizar()
        assert rep["paginas"] == 1
        assert rep["variantes"] == 22 and rep["padres_omitidos"] == 5
        assert rep["upserted"] == 22 and rep["deactivated"] == 0

        rows = await db.fetch("SELECT * FROM catalog_items WHERE branch_id = 'mascotas-test'")
        assert len(rows) == 22
        royal = next(r for r in rows if r["external_id"] == "7508")
        assert royal["source"] == "mercurio"
        assert str(royal["price"]) == "1728.42" and royal["stock"] == 9
        assert royal["requiere_receta"] == "no"
        assert royal["rubro"] == "GATOS"

        # la sucursal se creó sola y tiene heartbeat
        b = await db.fetchrow("SELECT * FROM branches WHERE branch_id = 'mascotas-test'")
        assert b and b["agent_version"] == "mercurio-rest" and b["catalog_count"] == 22

        # segundo ciclo: nada cambió → 0 upserted
        rep2 = await sync.sincronizar()
        assert rep2["upserted"] == 0 and rep2["unchanged"] == 22

    async def test_sync_desactiva_lo_que_desaparece(self, db, monkeypatch):
        import app.services.catalog_source as cs
        async def _csv():
            return "csv"
        monkeypatch.setattr(cs, "fuente_configurada", _csv)
        from app.services.catalog_store import get_catalog_store
        from app.models.sync import CatalogItemIn
        # Un producto viejo que Mercurio ya no lista
        sync = MercurioSync(_cliente(), "mascotas-test", db)
        await sync.asegurar_sucursal()
        await get_catalog_store(db).upsert_items("mascotas-test", [CatalogItemIn(
            external_id="viejo", hash="f" * 64, name="Producto viejo", price=None, stock=0)],
            source="mercurio")
        rep = await sync.sincronizar()
        assert rep["deactivated"] == 1
        row = await db.fetchrow("SELECT active FROM catalog_items WHERE external_id = 'viejo'")
        assert row["active"] is False


class TestStockVivoMercurio:
    async def test_lookup_vivo_usa_mercurio_sin_agente(self, monkeypatch):
        import app.services.mercurio_service as m
        from app.config import get_settings
        from app.services.catalog_live import lookup_vivo
        s = get_settings()
        monkeypatch.setattr(s, "mercurio_api_key", "mrc_test")
        monkeypatch.setattr(s, "mercurio_branch_id", "mascotas-test")
        monkeypatch.setattr(m, "_client", _cliente())
        res = await lookup_vivo("mascotas-test", ["7508", "999999"], timeout=5)
        assert res is not None
        assert res.items == [{"external_id": "7508", "stock": 9, "price": None}]
        assert res.missing == ["999999"]

    async def test_otra_sucursal_sin_agente_devuelve_none(self, monkeypatch):
        from app.config import get_settings
        from app.services.catalog_live import lookup_vivo
        monkeypatch.setattr(get_settings(), "mercurio_api_key", "mrc_test")
        assert await lookup_vivo("farmacia-mutual", ["1"], timeout=1) is None
