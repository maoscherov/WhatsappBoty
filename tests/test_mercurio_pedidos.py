"""
Alta de pedidos en Mercurio (F5): armado del JSON desde nuestra orden,
POST /pedidos con Idempotency-Key y reintentos, códigos de variante
persistidos por el sync, persistencia durable de pedidos en Postgres y el
flujo completo enviar_pedido_erp detrás de MERCURIO_PEDIDOS_ENABLED.

Contrato: docs/superpowers/specs/2026-09-14-mercurio-api-v1.md — en el pedido
`variant_id` = codigo de la variante y `product_id` = codigo_padre; 201 →
{ok, id_comprobante, numero}; reintento con la misma Idempotency-Key →
header `Idempotent-Replay: true`; 422 = no se registró (no reintentar).
"""

import json

import httpx
import pytest

import app.services.db as dbmod
from app.config import get_settings
from app.services.db import Database
from app.services.mercurio_service import (
    MercurioClient, MercurioError, MercurioPedidoRechazado, MercurioSync,
)
from app.services.mercurio_pedidos import enviar_pedido_erp, pedido_desde_orden


# ── Orden de ejemplo (formato de OrderService) ────────────────────────────────

def _orden(**extra) -> dict:
    base = {
        "order_id": "ORD-20261005-120000-AB12C",
        "phone": "5493415551234",
        "sku_id": "7508",                      # external_id = id_articulo_mercurio
        "sku_nombre": "ROYAL URINARY CAT S/O HIGH DILUTION 400GRS",
        "cantidad": 2,
        "total": 3456.84,
        "mp_payment_id": "mp-777",
        "pago": "online",
        "estado": "pendiente",
        "tipo_entrega": "retiro",
        "created_at": "2026-10-05T12:00:00+00:00",
    }
    base.update(extra)
    return base


CODIGOS = {"7508": {"codigo": "2209004", "codigo_padre": "ROY1051701"}}


# ── Armado del pedido ─────────────────────────────────────────────────────────

class TestPedidoDesdeOrden:
    def test_mapea_los_campos_obligatorios(self):
        p = pedido_desde_orden(_orden(), CODIGOS, state="complete",
                               customer_id_default="99999999")
        assert p["number"] == "ORD-20261005-120000-AB12C"
        assert p["state"] == "complete"
        assert p["customer_id"] == "99999999"
        assert p["total"] == 3456.84
        assert isinstance(p["id"], int) and p["id"] > 0
        [li] = p["line_items"]
        assert li["variant_id"] == "2209004"       # codigo de la variante
        assert li["product_id"] == "ROY1051701"    # codigo_padre
        assert li["quantity"] == 2
        assert li["subtotal"] == 3456.84 and li["total"] == 3456.84

    def test_id_es_estable_entre_reintentos(self):
        a = pedido_desde_orden(_orden(), CODIGOS, state="complete", customer_id_default="x")
        b = pedido_desde_orden(_orden(), CODIGOS, state="complete", customer_id_default="x")
        assert a["id"] == b["id"]

    def test_customer_id_de_la_orden_gana_al_default(self):
        p = pedido_desde_orden(_orden(customer_id="20304050"), CODIGOS,
                               state="complete", customer_id_default="99999999")
        assert p["customer_id"] == "20304050"

    def test_sin_codigo_conocido_cae_al_external_id(self):
        p = pedido_desde_orden(_orden(sku_id="424242"), {}, state="complete",
                               customer_id_default="x")
        [li] = p["line_items"]
        assert li["variant_id"] == "424242" and li["product_id"] == "424242"


# ── Cliente: POST /pedidos ────────────────────────────────────────────────────

def _transport_pedidos(respuestas: list, capturados: list) -> httpx.MockTransport:
    """Cada elemento de `respuestas` es (status, body, headers) y se consume
    en orden; los requests quedan en `capturados` para inspección."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/pedidos") and request.method == "POST"
        capturados.append(request)
        st, body, headers = respuestas.pop(0)
        return httpx.Response(st, json=body, headers=headers or {})

    return httpx.MockTransport(handler)


def _cliente_pedidos(respuestas, capturados) -> MercurioClient:
    return MercurioClient("mrc_test", "https://api.mercurio.test/v1", timeout=5,
                          transport=_transport_pedidos(respuestas, capturados))


OK_201 = (201, {"ok": True, "id_comprobante": "FC-0001-00012345", "numero": "12345"}, None)


class TestCrearPedido:
    async def test_201_devuelve_comprobante(self):
        reqs: list = []
        c = _cliente_pedidos([OK_201], reqs)
        r = await c.crear_pedido({"number": "ORD-1"}, idempotency_key="ORD-1")
        assert r == {"id_comprobante": "FC-0001-00012345", "numero": "12345", "replay": False}
        assert reqs[0].headers["Idempotency-Key"] == "ORD-1"
        assert json.loads(reqs[0].content)["number"] == "ORD-1"

    async def test_replay_del_mismo_pedido(self):
        reqs: list = []
        c = _cliente_pedidos([(201, OK_201[1], {"Idempotent-Replay": "true"})], reqs)
        r = await c.crear_pedido({"number": "ORD-1"}, idempotency_key="ORD-1")
        assert r["replay"] is True

    async def test_422_no_reintenta_y_es_rechazo(self):
        reqs: list = []
        c = _cliente_pedidos([(422, {"error": True, "message": "customer_id inválido"}, None)], reqs)
        with pytest.raises(MercurioPedidoRechazado, match="customer_id"):
            await c.crear_pedido({"number": "ORD-1"}, idempotency_key="ORD-1")
        assert len(reqs) == 1

    async def test_429_y_500_reintentan_con_la_misma_clave(self, monkeypatch):
        import app.services.mercurio_service as m

        async def _sin_espera(_s):
            return None

        monkeypatch.setattr(m.asyncio, "sleep", _sin_espera)
        reqs: list = []
        c = _cliente_pedidos([
            (429, {"error": True, "message": "rate"}, {"Retry-After": "0"}),
            (500, {"error": True, "message": "boom"}, None),
            OK_201,
        ], reqs)
        r = await c.crear_pedido({"number": "ORD-1"}, idempotency_key="ORD-1")
        assert r["id_comprobante"] == "FC-0001-00012345"
        assert len(reqs) == 3
        assert {q.headers["Idempotency-Key"] for q in reqs} == {"ORD-1"}

    async def test_500_persistente_agota_y_falla(self, monkeypatch):
        import app.services.mercurio_service as m

        async def _sin_espera(_s):
            return None

        monkeypatch.setattr(m.asyncio, "sleep", _sin_espera)
        reqs: list = []
        c = _cliente_pedidos([(500, {"error": True, "message": "x"}, None)] * 3, reqs)
        with pytest.raises(MercurioError):
            await c.crear_pedido({"number": "ORD-1"}, idempotency_key="ORD-1")


# ── Postgres embebido ─────────────────────────────────────────────────────────

@pytest.fixture
async def db(pg_dsn):
    import app.services.branch_store as bsmod
    import app.services.catalog_store as csmod
    import app.services.order_store as osmod
    d = Database(pg_dsn)
    assert await d.connect()
    await d.execute("TRUNCATE catalog_extras, catalog_items, branches, "
                    "orders, mercurio_codigos")
    prev = dbmod._instance
    dbmod._instance = d
    csmod._instance = None
    bsmod._instance = None
    osmod._instance = None
    yield d
    dbmod._instance = prev
    csmod._instance = None
    bsmod._instance = None
    osmod._instance = None
    await d.close()


# ── El sync persiste los códigos de pedido ────────────────────────────────────

def _transport_catalogo() -> httpx.MockTransport:
    from pathlib import Path
    from app.services.mercurio_service import CATALOGOS

    fix = Path(__file__).parent / "fixtures" / "mercurio"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/v1", "", 1)
        if path == "/articulos/paginas":
            return httpx.Response(200, json=json.loads((fix / "articulos_paginas.json").read_text(encoding="utf-8")))
        if path == "/articulos":
            return httpx.Response(200, json=json.loads((fix / "articulos_pagina1.json").read_text(encoding="utf-8")))
        for cat in CATALOGOS:
            if path == f"/{cat}":
                return httpx.Response(200, json=json.loads((fix / f"catalogo_{cat}.json").read_text(encoding="utf-8")))
        return httpx.Response(404, json={"error": True, "message": "ruta"})

    return httpx.MockTransport(handler)


class TestSyncCodigos:
    async def test_sync_guarda_codigo_y_codigo_padre(self, db, monkeypatch):
        import app.services.catalog_source as cs

        async def _csv():
            return "csv"

        monkeypatch.setattr(cs, "fuente_configurada", _csv)
        cliente = MercurioClient("mrc_test", "https://api.mercurio.test/v1",
                                 timeout=5, transport=_transport_catalogo())
        await MercurioSync(cliente, "mascotas-test", db).sincronizar()

        rows = await db.fetch("SELECT * FROM mercurio_codigos WHERE branch_id = 'mascotas-test'")
        assert len(rows) == 22                      # solo variantes
        royal = next(r for r in rows if r["external_id"] == "7508")
        assert royal["codigo"] == "2209004"
        assert royal["codigo_padre"] == "ROY1051701"


# ── Persistencia durable de pedidos ───────────────────────────────────────────

class TestOrderStore:
    async def test_upsert_y_actualizacion(self, db):
        from app.services.order_store import get_order_store
        store = get_order_store(db)
        await store.upsert(_orden())
        await store.upsert(_orden(estado="preparado"))

        row = await db.fetchrow("SELECT * FROM orders WHERE order_id = $1",
                                "ORD-20261005-120000-AB12C")
        assert row["phone"] == "5493415551234"
        assert row["estado"] == "preparado"         # el upsert actualiza
        assert json.loads(row["data"])["sku_nombre"].startswith("ROYAL")

    async def test_cola_erp(self, db):
        from app.services.order_store import get_order_store
        store = get_order_store(db)
        await store.upsert(_orden())
        await store.marcar_erp("ORD-20261005-120000-AB12C", "pendiente")
        pendientes = await store.pendientes_erp()
        assert [p["order_id"] for p in pendientes] == ["ORD-20261005-120000-AB12C"]

        await store.marcar_erp("ORD-20261005-120000-AB12C", "enviado",
                               id_comprobante="FC-0001-00012345", numero="12345")
        row = await db.fetchrow("SELECT * FROM orders WHERE order_id = $1",
                                "ORD-20261005-120000-AB12C")
        assert row["erp_estado"] == "enviado"
        assert row["erp_id_comprobante"] == "FC-0001-00012345"
        assert await store.pendientes_erp() == []


# ── Write-through: OrderService → tabla durable ──────────────────────────────

class _FakeRedis:
    """Lo mínimo que usa OrderService.create (mismo patrón que test_orders_trace)."""

    def __init__(self):
        self.kv: dict[str, str] = {}
        self.z: dict[str, dict[str, float]] = {}

    async def setex(self, key, ttl, value):
        self.kv[key] = value

    async def zadd(self, key, mapping):
        self.z.setdefault(key, {}).update(mapping)


class TestWriteThrough:
    async def test_create_persiste_en_postgres(self, db):
        """Un pedido creado (cobro confirmado) queda en la tabla durable:
        si Redis lo pierde (TTL 7 días, reinicio), la venta no desaparece."""
        from app.services.order_service import OrderService
        svc = OrderService.__new__(OrderService)
        svc._redis = _FakeRedis()
        order = await svc.create(phone="549341999", sku_id="7508", sku_nombre="ROYAL",
                                 cantidad=1, total=1728.42, mp_payment_id="mp-1")
        row = await db.fetchrow("SELECT * FROM orders WHERE order_id = $1",
                                order["order_id"])
        assert row is not None
        assert row["phone"] == "549341999"
        assert str(row["total"]) == "1728.42"


# ── Flujo completo: enviar_pedido_erp ─────────────────────────────────────────

async def _preparar_codigos(db):
    await db.execute(
        "INSERT INTO mercurio_codigos (branch_id, external_id, codigo, codigo_padre) "
        "VALUES ($1, $2, $3, $4)", "mascotas-oeste", "7508", "2209004", "ROY1051701")


class TestEnviarPedidoERP:
    async def test_flag_apagado_no_envia(self, db, monkeypatch):
        reqs: list = []
        monkeypatch.setattr(get_settings(), "mercurio_pedidos_enabled", False)
        r = await enviar_pedido_erp(_orden(), client=_cliente_pedidos([OK_201], reqs), db=db)
        assert r is None
        assert reqs == []

    async def test_exito_marca_enviado(self, db, monkeypatch):
        from app.services.order_store import get_order_store
        s = get_settings()
        monkeypatch.setattr(s, "mercurio_pedidos_enabled", True)
        monkeypatch.setattr(s, "mercurio_branch_id", "mascotas-oeste")
        await _preparar_codigos(db)
        await get_order_store(db).upsert(_orden())

        reqs: list = []
        r = await enviar_pedido_erp(_orden(), client=_cliente_pedidos([OK_201], reqs), db=db)
        assert r["id_comprobante"] == "FC-0001-00012345"
        assert reqs[0].headers["Idempotency-Key"] == "ORD-20261005-120000-AB12C"
        body = json.loads(reqs[0].content)
        assert body["line_items"][0]["variant_id"] == "2209004"

        row = await db.fetchrow("SELECT * FROM orders WHERE order_id = $1",
                                "ORD-20261005-120000-AB12C")
        assert row["erp_estado"] == "enviado"
        assert row["erp_numero"] == "12345"

    async def test_422_marca_rechazado_sin_reintentos(self, db, monkeypatch):
        from app.services.order_store import get_order_store
        s = get_settings()
        monkeypatch.setattr(s, "mercurio_pedidos_enabled", True)
        monkeypatch.setattr(s, "mercurio_branch_id", "mascotas-oeste")
        await _preparar_codigos(db)
        await get_order_store(db).upsert(_orden())

        reqs: list = []
        c = _cliente_pedidos([(422, {"error": True, "message": "state inválido"}, None)], reqs)
        r = await enviar_pedido_erp(_orden(), client=c, db=db)
        assert r is None
        row = await db.fetchrow("SELECT erp_estado, erp_ultimo_error FROM orders "
                                "WHERE order_id = $1", "ORD-20261005-120000-AB12C")
        assert row["erp_estado"] == "rechazado"
        assert "state" in row["erp_ultimo_error"]

    async def test_despachar_marca_pendiente_y_envia(self, db, monkeypatch):
        """despachar_alta_erp: deja la fila durable en 'pendiente' ANTES de
        intentar el POST (crash-safe) y termina en 'enviado' si el ERP responde."""
        from app.services.mercurio_pedidos import despachar_alta_erp
        from app.services.order_store import get_order_store
        s = get_settings()
        monkeypatch.setattr(s, "mercurio_pedidos_enabled", True)
        monkeypatch.setattr(s, "mercurio_branch_id", "mascotas-oeste")
        await _preparar_codigos(db)
        await get_order_store(db).upsert(_orden())

        reqs: list = []
        await despachar_alta_erp(_orden(), client=_cliente_pedidos([OK_201], reqs), db=db)
        row = await db.fetchrow("SELECT erp_estado FROM orders WHERE order_id = $1",
                                "ORD-20261005-120000-AB12C")
        assert row["erp_estado"] == "enviado"
        assert len(reqs) == 1

    async def test_despachar_con_flag_apagado_no_toca_nada(self, db, monkeypatch):
        from app.services.mercurio_pedidos import despachar_alta_erp
        from app.services.order_store import get_order_store
        monkeypatch.setattr(get_settings(), "mercurio_pedidos_enabled", False)
        await get_order_store(db).upsert(_orden())

        reqs: list = []
        await despachar_alta_erp(_orden(), client=_cliente_pedidos([OK_201], reqs), db=db)
        row = await db.fetchrow("SELECT erp_estado FROM orders WHERE order_id = $1",
                                "ORD-20261005-120000-AB12C")
        assert row["erp_estado"] is None
        assert reqs == []

    async def test_reintento_levanta_los_pendientes(self, db, monkeypatch):
        """El job de fondo toma lo que quedó 'pendiente' (ERP caído en el
        momento del cobro) y lo termina de registrar."""
        from app.services.mercurio_pedidos import reintentar_pedidos_pendientes
        from app.services.order_store import get_order_store
        s = get_settings()
        monkeypatch.setattr(s, "mercurio_pedidos_enabled", True)
        monkeypatch.setattr(s, "mercurio_branch_id", "mascotas-oeste")
        await _preparar_codigos(db)
        store = get_order_store(db)
        await store.upsert(_orden())
        await store.marcar_erp("ORD-20261005-120000-AB12C", "pendiente")

        reqs: list = []
        n = await reintentar_pedidos_pendientes(client=_cliente_pedidos([OK_201], reqs), db=db)
        assert n == 1
        row = await db.fetchrow("SELECT erp_estado, erp_numero FROM orders "
                                "WHERE order_id = $1", "ORD-20261005-120000-AB12C")
        assert row["erp_estado"] == "enviado" and row["erp_numero"] == "12345"

    async def test_error_de_red_queda_pendiente_para_reintento(self, db, monkeypatch):
        from app.services.order_store import get_order_store
        import app.services.mercurio_service as m

        async def _sin_espera(_s):
            return None

        monkeypatch.setattr(m.asyncio, "sleep", _sin_espera)
        s = get_settings()
        monkeypatch.setattr(s, "mercurio_pedidos_enabled", True)
        monkeypatch.setattr(s, "mercurio_branch_id", "mascotas-oeste")
        await _preparar_codigos(db)
        await get_order_store(db).upsert(_orden())

        reqs: list = []
        c = _cliente_pedidos([(500, {"error": True, "message": "x"}, None)] * 3, reqs)
        r = await enviar_pedido_erp(_orden(), client=c, db=db)
        assert r is None
        row = await db.fetchrow("SELECT erp_estado, erp_intentos FROM orders "
                                "WHERE order_id = $1", "ORD-20261005-120000-AB12C")
        assert row["erp_estado"] == "pendiente"
        assert row["erp_intentos"] == 1
