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


@pytest.fixture(autouse=True)
def _customer_id_por_default(monkeypatch):
    """fix-C2 (hallazgo 10): sin customer_id no hay POST. Los tests del alta
    corren con uno por default; los que prueban su falta lo vacían."""
    monkeypatch.setattr(get_settings(), "mercurio_customer_id_default", "20111111112")


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

    def test_sin_codigo_conocido_no_arma_el_pedido(self):
        """Hallazgo 7: nunca se cae al external_id (es el id_articulo_mercurio,
        no el codigo: 422 para siempre, o peor, el codigo de OTRO artículo)."""
        from app.services.mercurio_pedidos import CodigoMercurioFaltante
        with pytest.raises(CodigoMercurioFaltante,
                           match="sin código Mercurio para 424242; se reintenta "
                                 "después del próximo sync"):
            pedido_desde_orden(_orden(sku_id="424242"), {}, state="complete",
                               customer_id_default="x")

    def test_codigo_vacio_cuenta_como_faltante(self):
        from app.services.mercurio_pedidos import CodigoMercurioFaltante
        with pytest.raises(CodigoMercurioFaltante, match="7508"):
            pedido_desde_orden(_orden(), {"7508": {"codigo": "", "codigo_padre": "X"}},
                               state="complete", customer_id_default="x")


# ── Cliente: POST /pedidos ────────────────────────────────────────────────────

def _transport_pedidos(respuestas: list, capturados: list) -> httpx.MockTransport:
    """Cada elemento de `respuestas` es (status, body, headers) y se consume
    en orden; los requests quedan en `capturados` para inspección. Un `body`
    de tipo bytes va crudo (cuerpo que no es JSON)."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/pedidos") and request.method == "POST"
        capturados.append(request)
        st, body, headers = respuestas.pop(0)
        if isinstance(body, bytes):
            return httpx.Response(st, content=body, headers=headers or {})
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


# ── Clasificación de las respuestas del POST /pedidos (hallazgo 8) ───────────

@pytest.fixture
def esperas(monkeypatch):
    """asyncio.sleep sin esperar: devuelve la lista de segundos pedidos."""
    import app.services.mercurio_service as m
    pedidas: list = []

    async def _sleep(s):
        pedidas.append(s)

    monkeypatch.setattr(m.asyncio, "sleep", _sleep)
    return pedidas


class TestClasificacionDeRespuestas:
    """422, 400, 404, 409 y 413: rechazo definitivo (sin reintento). 401 y
    403: credencial. 429, 5xx, red y un 2xx sin id_comprobante: reintento."""

    @pytest.mark.parametrize("status", [400, 404, 409, 413, 422])
    async def test_rechazo_definitivo_sin_reintento(self, status, esperas):
        reqs: list = []
        c = _cliente_pedidos([(status, {"error": True, "message": "no va"}, None)] * 3, reqs)
        with pytest.raises(MercurioPedidoRechazado, match="no va"):
            await c.crear_pedido({"number": "ORD-1"}, idempotency_key="ORD-1")
        assert len(reqs) == 1

    @pytest.mark.parametrize("status", [401, 403])
    async def test_401_y_403_son_error_de_credencial(self, status, esperas):
        from app.services.mercurio_service import MercurioCredencialError
        reqs: list = []
        c = _cliente_pedidos([(status, {"error": True, "message": "clave"}, None)] * 3, reqs)
        with pytest.raises(MercurioCredencialError, match=str(status)):
            await c.crear_pedido({"number": "ORD-1"}, idempotency_key="ORD-1")
        assert len(reqs) == 1

    @pytest.mark.parametrize("cuerpo", [["lista", "no objeto"], "texto JSON", b"<html>boom</html>",
                                        b""])
    async def test_422_con_cuerpo_que_no_es_un_objeto_json(self, cuerpo, esperas):
        reqs: list = []
        c = _cliente_pedidos([(422, cuerpo, None)], reqs)
        with pytest.raises(MercurioPedidoRechazado, match="ORD-1"):
            await c.crear_pedido({"number": "ORD-1"}, idempotency_key="ORD-1")

    @pytest.mark.parametrize("respuesta", [
        (201, {"ok": True, "numero": "1"}, None),
        (200, {"ok": False}, None),
        (201, {"ok": True, "id_comprobante": "", "numero": "1"}, None),
        (201, ["FC-1"], None),
        (201, b"no es json", None),
    ])
    async def test_2xx_sin_id_comprobante_es_reintentable(self, respuesta, esperas):
        reqs: list = []
        c = _cliente_pedidos([respuesta], reqs)
        with pytest.raises(MercurioError) as e:
            await c.crear_pedido({"number": "ORD-1"}, idempotency_key="ORD-1")
        assert not isinstance(e.value, MercurioPedidoRechazado)

    @pytest.mark.parametrize("retry_after, espera", [
        ("5", 5),
        ("Wed, 21 Oct 2015 07:28:00 GMT", 0),     # fecha HTTP ya pasada
        ("pasado manana", 60),                     # no se entiende: el default
        ("", 60),
        ("-3", 0),
        ("999", 120),                             # tope
    ])
    async def test_retry_after_tolerante(self, retry_after, espera, esperas):
        reqs: list = []
        c = _cliente_pedidos([(429, {"error": True, "message": "rate"},
                               {"Retry-After": retry_after}), OK_201], reqs)
        r = await c.crear_pedido({"number": "ORD-1"}, idempotency_key="ORD-1")
        assert r["id_comprobante"] == "FC-0001-00012345"
        assert esperas == [espera]

    async def test_retry_after_con_fecha_futura(self, esperas):
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime
        futura = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
        reqs: list = []
        c = _cliente_pedidos([(429, {}, {"Retry-After": futura}), OK_201], reqs)
        await c.crear_pedido({"number": "ORD-1"}, idempotency_key="ORD-1")
        assert len(esperas) == 1 and 25 <= esperas[0] <= 30

    async def test_retry_after_con_fecha_en_el_get_del_sync(self, esperas):
        respuestas = [httpx.Response(429, json={}, headers={"Retry-After": "Wed, 21 Oct 2015 "
                                                                         "07:28:00 GMT"}),
                      httpx.Response(200, json={"ok": True})]
        c = MercurioClient("mrc_test", "https://api.mercurio.test/v1", timeout=5,
                           transport=httpx.MockTransport(lambda req: respuestas.pop(0)))
        assert await c.estado() == {"ok": True}
        assert esperas == [0]

    async def test_429_agotado_dice_429(self, esperas):
        reqs: list = []
        c = _cliente_pedidos([(429, {}, {"Retry-After": "1"})] * 3, reqs)
        with pytest.raises(MercurioError, match="429"):
            await c.crear_pedido({"number": "ORD-1"}, idempotency_key="ORD-1")
        assert len(reqs) == 3


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


# ── Errores de Postgres que se propagan (hallazgo 6) ─────────────────────────

class _PoolQueFalla:
    """Pool cuya próxima adquisición falla (corte transitorio de Postgres)."""

    def __init__(self, real, fallas: int = 1):
        self.real = real
        self.fallas = fallas

    def acquire(self):
        if self.fallas > 0:
            self.fallas -= 1
            raise ConnectionError("conexion reseteada")
        return self.real.acquire()

    async def close(self):
        await self.real.close()


class TestDbRaiseErrors:
    async def test_sin_raise_errors_se_sigue_tragando_el_error(self, db):
        real = db._pool
        db._pool = _PoolQueFalla(real, fallas=3)
        try:
            assert await db.execute("SELECT 1") is None
            assert await db.fetch("SELECT 1") == []
            assert await db.fetchrow("SELECT 1") is None
        finally:
            db._pool = real

    async def test_con_raise_errors_el_error_se_propaga(self, db):
        real = db._pool
        db._pool = _PoolQueFalla(real, fallas=3)
        try:
            with pytest.raises(ConnectionError):
                await db.execute("SELECT 1", raise_errors=True)
            with pytest.raises(ConnectionError):
                await db.fetch("SELECT 1", raise_errors=True)
            with pytest.raises(ConnectionError):
                await db.fetchrow("SELECT 1", raise_errors=True)
        finally:
            db._pool = real
        assert [dict(r) for r in await db.fetch("SELECT 1 AS x", raise_errors=True)] == [{"x": 1}]

    async def test_sin_postgres_raise_errors_lanza(self):
        d = Database("")
        assert await d.connect() is False
        assert await d.execute("SELECT 1") is None           # igual que siempre
        with pytest.raises(RuntimeError, match="no disponible"):
            await d.execute("SELECT 1", raise_errors=True)
        with pytest.raises(RuntimeError, match="no disponible"):
            await d.fetch("SELECT 1", raise_errors=True)

    async def test_order_store_propaga_los_errores(self, db):
        from app.services.order_store import get_order_store
        store = get_order_store(db)
        real = db._pool
        db._pool = _PoolQueFalla(real, fallas=3)
        try:
            with pytest.raises(ConnectionError):
                await store.upsert(_orden())
            with pytest.raises(ConnectionError):
                await store.marcar_erp("ORD-20261005-120000-AB12C", "pendiente")
            with pytest.raises(ConnectionError):
                await store.pendientes_erp()
        finally:
            db._pool = real


# ── Encolado temprano: la orden cobrada nace 'pendiente' (hallazgos 6 y 14) ──

def _order_service_falso():
    from app.services.order_service import OrderService
    svc = OrderService.__new__(OrderService)
    svc._redis = _FakeRedis()
    return svc


async def _erp_estado(db, order_id):
    row = await db.fetchrow("SELECT erp_estado FROM orders WHERE order_id = $1", order_id)
    return row["erp_estado"] if row else "SIN FILA"


class TestEncoladoTemprano:
    async def test_cobrada_online_nace_pendiente_con_el_flag(self, db, monkeypatch):
        monkeypatch.setattr(get_settings(), "mercurio_pedidos_enabled", True)
        order = await _order_service_falso().create(
            phone="549341999", sku_id="7508", sku_nombre="ROYAL", cantidad=1,
            total=1728.42, mp_payment_id="mp-1")
        assert await _erp_estado(db, order["order_id"]) == "pendiente"

    async def test_sin_flag_nace_sin_estado_erp(self, db, monkeypatch):
        monkeypatch.setattr(get_settings(), "mercurio_pedidos_enabled", False)
        order = await _order_service_falso().create(
            phone="549341999", sku_id="7508", sku_nombre="ROYAL", cantidad=1,
            total=1728.42, mp_payment_id="mp-1")
        assert await _erp_estado(db, order["order_id"]) is None

    @pytest.mark.parametrize("pago, payment_id", [("cuenta_corriente", ""), ("efectivo", ""),
                                                  ("online", "")])
    async def test_sin_cobro_online_no_se_encola(self, db, monkeypatch, pago, payment_id):
        monkeypatch.setattr(get_settings(), "mercurio_pedidos_enabled", True)
        order = await _order_service_falso().create(
            phone="549341999", sku_id="7508", sku_nombre="ROYAL", cantidad=1,
            total=1728.42, mp_payment_id=payment_id, pago=pago)
        assert await _erp_estado(db, order["order_id"]) is None

    async def test_el_upsert_no_pisa_un_estado_posterior(self, db):
        from app.services.order_store import get_order_store
        store = get_order_store(db)
        oid = "ORD-20261005-120000-AB12C"
        await store.upsert(_orden(), erp_estado="pendiente")
        assert await _erp_estado(db, oid) == "pendiente"
        await store.marcar_erp(oid, "enviado", id_comprobante="FC-1", numero="1")
        await store.upsert(_orden(estado="preparado"), erp_estado="pendiente")
        await store.upsert(_orden(estado="retirado"))          # un _save del backoffice
        row = await db.fetchrow("SELECT estado, erp_estado FROM orders WHERE order_id = $1", oid)
        assert (row["estado"], row["erp_estado"]) == ("retirado", "enviado")

    async def test_save_no_encola_una_orden_vieja(self, db, monkeypatch):
        """Una orden creada con el flag apagado no entra a la cola porque el
        operador la marque preparada después de encender el flag."""
        monkeypatch.setattr(get_settings(), "mercurio_pedidos_enabled", False)
        svc = _order_service_falso()
        order = await svc.create(phone="549341999", sku_id="7508", sku_nombre="ROYAL",
                                 cantidad=1, total=1728.42, mp_payment_id="mp-1")
        monkeypatch.setattr(get_settings(), "mercurio_pedidos_enabled", True)
        await svc._save(dict(order, estado="preparado"))
        assert await _erp_estado(db, order["order_id"]) is None

    async def test_marcar_erp_sin_fila_inserta_la_orden(self, db):
        from app.services.order_store import get_order_store
        await get_order_store(db).marcar_erp("ORD-20261005-120000-AB12C", "pendiente",
                                             error="x", incrementar_intento=True,
                                             order=_orden())
        row = await db.fetchrow("SELECT * FROM orders WHERE order_id = $1",
                                "ORD-20261005-120000-AB12C")
        assert row["erp_estado"] == "pendiente"
        assert row["erp_intentos"] == 1 and row["erp_ultimo_error"] == "x"
        assert row["payment_id"] == "mp-777"
        assert json.loads(row["data"])["sku_id"] == "7508"

    async def test_marcar_erp_sin_fila_y_sin_orden_lanza(self, db):
        from app.services.order_store import get_order_store
        with pytest.raises(LookupError, match="ORD-NO-EXISTE"):
            await get_order_store(db).marcar_erp("ORD-NO-EXISTE", "pendiente")

    async def test_despachar_sin_fila_y_erp_caido_queda_en_la_cola(self, db, monkeypatch):
        """El write-through falló al crear la orden: el hook la inserta y,
        con el ERP caído, queda 'pendiente' para el job (antes: 0 filas)."""
        import app.services.mercurio_service as m
        from app.services.mercurio_pedidos import (despachar_alta_erp,
                                                   reintentar_pedidos_pendientes)

        async def _sin_espera(_s):
            return None

        monkeypatch.setattr(m.asyncio, "sleep", _sin_espera)
        s = get_settings()
        monkeypatch.setattr(s, "mercurio_pedidos_enabled", True)
        monkeypatch.setattr(s, "mercurio_branch_id", "mascotas-oeste")
        await _preparar_codigos(db)

        reqs: list = []
        c = _cliente_pedidos([(500, {"error": True, "message": "x"}, None)] * 3, reqs)
        assert await despachar_alta_erp(_orden(), client=c, db=db) is None
        assert await _erp_estado(db, "ORD-20261005-120000-AB12C") == "pendiente"
        # fix-C2 (backoff por pedido): la falla lo aplazó; pasa el tiempo.
        await db.execute("UPDATE orders SET erp_proximo_intento = now() - interval '1 second'")

        n = await reintentar_pedidos_pendientes(client=_cliente_pedidos([OK_201], reqs), db=db)
        assert n == 1
        assert await _erp_estado(db, "ORD-20261005-120000-AB12C") == "enviado"


# ── Códigos de Mercurio: sin código no hay POST (hallazgo 7) ──────────────────

class TestCodigosMercurio:
    @pytest.fixture(autouse=True)
    def _flag(self, monkeypatch):
        s = get_settings()
        monkeypatch.setattr(s, "mercurio_pedidos_enabled", True)
        monkeypatch.setattr(s, "mercurio_branch_id", "mascotas-oeste")

    async def _fila(self, db):
        return await db.fetchrow("SELECT erp_estado, erp_ultimo_error, erp_intentos FROM orders "
                                 "WHERE order_id = $1", "ORD-20261005-120000-AB12C")

    async def test_sin_codigo_no_hay_post_y_queda_pendiente(self, db):
        """El artículo todavía no tiene código (p. ej. antes del primer sync):
        no se manda nada y se reintenta después del próximo sync."""
        from app.services.order_store import get_order_store
        await get_order_store(db).upsert(_orden(), erp_estado="pendiente")   # sin códigos
        reqs: list = []
        assert await enviar_pedido_erp(_orden(), client=_cliente_pedidos([OK_201], reqs),
                                       db=db) is None
        assert reqs == []
        row = await self._fila(db)
        assert row["erp_estado"] == "pendiente"
        assert row["erp_ultimo_error"] == ("sin código Mercurio para 7508; se reintenta "
                                           "después del próximo sync")

        # El sync trae el código: la pasada siguiente lo manda.
        await _preparar_codigos(db)
        assert await enviar_pedido_erp(_orden(), client=_cliente_pedidos([OK_201], reqs),
                                       db=db) is not None
        assert json.loads(reqs[0].content)["line_items"][0]["variant_id"] == "2209004"

    async def test_falla_al_leer_los_codigos_no_hay_post(self, db):
        """Un corte transitorio de Postgres al leer mercurio_codigos ya no se
        confunde con "sin códigos" ni sale con el external_id."""
        from app.services.order_store import get_order_store
        await _preparar_codigos(db)
        await get_order_store(db).upsert(_orden(), erp_estado="pendiente")
        real = db._pool
        db._pool = _PoolQueFalla(real, fallas=1)            # solo la lectura de códigos
        reqs: list = []
        try:
            assert await enviar_pedido_erp(_orden(), client=_cliente_pedidos([OK_201], reqs),
                                           db=db) is None
        finally:
            db._pool = real
        assert reqs == []
        row = await self._fila(db)
        assert row["erp_estado"] == "pendiente"
        assert "códigos Mercurio" in row["erp_ultimo_error"]
        assert "conexion reseteada" in row["erp_ultimo_error"]


# ── Renglones del pedido: carrito y envío (hallazgo 5) ────────────────────────

CODIGOS_2 = {**CODIGOS, "15181": {"codigo": "10955", "codigo_padre": "PET1304801"}}


def _orden_con_items(costo_envio=0.0, total=None, **extra):
    items = [
        {"sku_id": "7508", "nombre": "Royal 400 g", "cantidad": 2,
         "precio_unitario": 1000.0, "total": 2000.0},
        {"sku_id": "15181", "nombre": "Collar rosa", "cantidad": 1,
         "precio_unitario": 500.0, "total": 500.0},
    ]
    return _orden(sku_id="MULTI", sku_nombre="2 productos", cantidad=1,
                  total=2500.0 + costo_envio if total is None else total,
                  items=items, costo_envio=costo_envio, **extra)


class TestRenglones:
    def test_un_line_item_por_renglon_y_el_envio_en_shipping_total(self):
        p = pedido_desde_orden(_orden_con_items(costo_envio=2000.0), CODIGOS_2,
                               state="complete", customer_id_default="x")
        assert [(li["variant_id"], li["product_id"], li["quantity"], li["subtotal"],
                 li["total"]) for li in p["line_items"]] == [
            ("2209004", "ROY1051701", 2, 2000.0, 2000.0),
            ("10955", "PET1304801", 1, 500.0, 500.0)]
        assert p["shipping_total"] == 2000.0
        assert p["total"] == 4500.0

    def test_sin_envio_no_manda_shipping_total(self):
        p = pedido_desde_orden(_orden_con_items(), CODIGOS_2, state="complete",
                               customer_id_default="x")
        assert "shipping_total" not in p
        assert p["total"] == 2500.0

    def test_el_renglon_sale_de_precio_unitario_por_cantidad(self):
        o = _orden_con_items()
        o["items"][0]["total"] = 1.0              # el total guardado no manda
        p = pedido_desde_orden(o, CODIGOS_2, state="complete", customer_id_default="x")
        assert p["line_items"][0]["subtotal"] == 2000.0

    def test_un_centavo_de_diferencia_se_tolera(self):
        p = pedido_desde_orden(_orden_con_items(total=2500.01), CODIGOS_2,
                               state="complete", customer_id_default="x")
        assert p["total"] == 2500.01

    def test_renglones_que_no_cuadran_no_arman_el_pedido(self):
        from app.services.mercurio_pedidos import PedidoInconsistente
        with pytest.raises(PedidoInconsistente, match="renglones no cuadran con el total"):
            pedido_desde_orden(_orden_con_items(total=3000.0), CODIGOS_2, state="complete",
                               customer_id_default="x")

    def test_orden_vieja_de_carrito_sin_renglones(self):
        from app.services.mercurio_pedidos import PedidoInconsistente
        with pytest.raises(PedidoInconsistente, match="pedido sin renglones"):
            pedido_desde_orden(_orden(sku_id="MULTI", cantidad=1), CODIGOS_2,
                               state="complete", customer_id_default="x")

    @pytest.fixture
    def _flag(self, monkeypatch):
        s = get_settings()
        monkeypatch.setattr(s, "mercurio_pedidos_enabled", True)
        monkeypatch.setattr(s, "mercurio_branch_id", "mascotas-oeste")

    @pytest.mark.parametrize("orden, motivo", [
        (_orden_con_items(total=3000.0), "renglones no cuadran con el total"),
        (_orden(sku_id="MULTI", cantidad=1), "pedido sin renglones"),
    ])
    async def test_sin_cuadrar_no_hay_post_y_queda_rechazado(self, db, _flag, orden, motivo):
        from app.services.order_store import get_order_store
        await _preparar_codigos(db)
        await get_order_store(db).upsert(orden, erp_estado="pendiente")
        reqs: list = []
        assert await enviar_pedido_erp(orden, client=_cliente_pedidos([OK_201], reqs),
                                       db=db) is None
        assert reqs == []
        row = await db.fetchrow("SELECT erp_estado, erp_ultimo_error FROM orders "
                                "WHERE order_id = $1", orden["order_id"])
        assert row["erp_estado"] == "rechazado"
        assert motivo in row["erp_ultimo_error"]


# ── Un pago, una orden: índice único de payment_id (hallazgo 11) ─────────────

class TestPagoDuplicado:
    async def test_create_con_un_pago_que_ya_tiene_orden_es_duplicado(self, db, caplog):
        from app.services.order_store import PedidoDuplicado
        primera = await _order_service_falso().create(
            phone="549341999", sku_id="7508", sku_nombre="ROYAL", cantidad=1,
            total=1728.42, mp_payment_id="mp-dup")
        otra = _order_service_falso()                  # otro Redis (reiniciado)
        with pytest.raises(PedidoDuplicado):
            await otra.create(phone="549341999", sku_id="7508", sku_nombre="ROYAL",
                              cantidad=1, total=1728.42, mp_payment_id="mp-dup")
        assert otra._redis.kv == {} and otra._redis.z == {}     # no queda otra orden
        rows = await db.fetch("SELECT order_id FROM orders WHERE payment_id = 'mp-dup'")
        assert [r["order_id"] for r in rows] == [primera["order_id"]]
        assert not [r for r in caplog.records if r.levelno >= 40]
        assert any(r.levelname == "WARNING" and "mp-dup" in r.getMessage()
                   for r in caplog.records)

    async def test_sin_id_de_pago_no_hay_duplicado(self, db):
        """Efectivo y cuenta corriente no tienen payment_id: no chocan."""
        svc = _order_service_falso()
        for _ in range(2):
            await svc.create(phone="549341999", sku_id="7508", sku_nombre="ROYAL",
                             cantidad=1, total=10.0, mp_payment_id="", pago="cuenta_corriente")
        assert (await db.fetchrow("SELECT count(*) AS n FROM orders"))["n"] == 2

    async def test_despachar_una_orden_de_un_pago_que_ya_tiene_otra_no_la_manda(self, db,
                                                                               monkeypatch):
        """El write-through de B falló y el pago ya tiene la orden A en la tabla:
        el hook no inserta B ni la manda (sería otro pedido con otra clave)."""
        from app.services.mercurio_pedidos import despachar_alta_erp
        from app.services.order_store import get_order_store
        s = get_settings()
        monkeypatch.setattr(s, "mercurio_pedidos_enabled", True)
        monkeypatch.setattr(s, "mercurio_branch_id", "mascotas-oeste")
        await _preparar_codigos(db)
        await get_order_store(db).upsert(_orden(order_id="ORD-A", mp_payment_id="mp-x"),
                                         erp_estado="pendiente")
        reqs: list = []
        r = await despachar_alta_erp(_orden(order_id="ORD-B", mp_payment_id="mp-x"),
                                     client=_cliente_pedidos([OK_201], reqs), db=db)
        assert r is None and reqs == []
        rows = await db.fetch("SELECT order_id FROM orders")
        assert [x["order_id"] for x in rows] == ["ORD-A"]


# ══════════════════════════════════════════════════════════════════════════════
# Reintentos del alta (hallazgo 8, fix-C2): clasificación de respuestas,
# enviar_pedido_erp nunca lanza y el job atrapa por pedido.
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def alta(monkeypatch):
    """Alta habilitada para la sucursal de MO, con customer_id por default."""
    s = get_settings()
    monkeypatch.setattr(s, "mercurio_pedidos_enabled", True)
    monkeypatch.setattr(s, "mercurio_branch_id", "mascotas-oeste")
    monkeypatch.setattr(s, "mercurio_customer_id_default", "20111111112")
    monkeypatch.setattr(s, "mercurio_pedidos_retry_secs", 300)


async def _fila_erp(db, order_id="ORD-20261005-120000-AB12C"):
    row = await db.fetchrow(
        "SELECT erp_estado, erp_ultimo_error, erp_intentos, erp_proximo_intento "
        "FROM orders WHERE order_id = $1", order_id)
    return dict(row) if row else None


async def _encolar(db, *ordenes):
    from app.services.order_store import get_order_store
    await _preparar_codigos(db)
    for o in ordenes:
        await get_order_store(db).upsert(o, erp_estado="pendiente")


class _ClienteQueRevienta:
    """Un cliente que lanza algo que no es MercurioError (un bug, una
    respuesta inesperada): enviar_pedido_erp igual no lanza."""

    def __init__(self):
        self.llamadas = 0

    async def crear_pedido(self, pedido, idempotency_key):
        self.llamadas += 1
        raise RuntimeError("explotó algo inesperado")


class TestEnviarClasifica:
    @pytest.mark.parametrize("status", [400, 404, 409, 413])
    async def test_4xx_definitivo_queda_rechazado(self, db, alta, esperas, status):
        await _encolar(db, _orden())
        reqs: list = []
        c = _cliente_pedidos([(status, {"error": True, "message": "malo"}, None)] * 3, reqs)
        assert await enviar_pedido_erp(_orden(), client=c, db=db) is None
        assert len(reqs) == 1
        fila = await _fila_erp(db)
        assert fila["erp_estado"] == "rechazado"
        assert "malo" in fila["erp_ultimo_error"]

    @pytest.mark.parametrize("status", [401, 403])
    async def test_credencial_no_consume_intentos(self, db, alta, esperas, caplog, status):
        await _encolar(db, _orden())
        reqs: list = []
        c = _cliente_pedidos([(status, {"error": True, "message": "clave"}, None)], reqs)
        assert await enviar_pedido_erp(_orden(), client=c, db=db) is None
        fila = await _fila_erp(db)
        assert fila["erp_estado"] == "pendiente"
        assert fila["erp_intentos"] == 0
        assert str(status) in fila["erp_ultimo_error"]
        assert any(r.levelname == "ERROR" and "credencial" in r.getMessage().lower()
                   for r in caplog.records)

    async def test_2xx_sin_id_comprobante_queda_pendiente(self, db, alta, esperas):
        await _encolar(db, _orden())
        reqs: list = []
        c = _cliente_pedidos([(200, {"ok": False}, None)], reqs)
        assert await enviar_pedido_erp(_orden(), client=c, db=db) is None
        fila = await _fila_erp(db)
        assert (fila["erp_estado"], fila["erp_intentos"]) == ("pendiente", 1)
        assert "id_comprobante" in fila["erp_ultimo_error"]

    async def test_422_con_cuerpo_no_json_queda_rechazado(self, db, alta, esperas):
        await _encolar(db, _orden())
        reqs: list = []
        c = _cliente_pedidos([(422, ["no", "objeto"], None)], reqs)
        assert await enviar_pedido_erp(_orden(), client=c, db=db) is None
        assert (await _fila_erp(db))["erp_estado"] == "rechazado"

    async def test_retry_after_con_fecha_no_rompe_el_alta(self, db, alta, esperas):
        await _encolar(db, _orden())
        reqs: list = []
        c = _cliente_pedidos([(429, {}, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}),
                              OK_201], reqs)
        assert (await enviar_pedido_erp(_orden(), client=c, db=db))["numero"] == "12345"
        assert (await _fila_erp(db))["erp_estado"] == "enviado"


class TestEnviarNuncaLanza:
    async def test_excepcion_inesperada_del_cliente(self, db, alta, caplog):
        await _encolar(db, _orden())
        c = _ClienteQueRevienta()
        assert await enviar_pedido_erp(_orden(), client=c, db=db) is None
        fila = await _fila_erp(db)
        assert fila["erp_estado"] == "pendiente"
        assert "explotó algo inesperado" in fila["erp_ultimo_error"]
        assert any(r.levelname == "ERROR" and "ORD-20261005-120000-AB12C" in r.getMessage()
                   for r in caplog.records)

    async def test_orden_rota(self, db, alta):
        """Un renglón con una cantidad que no es número (dato roto): no se
        arregla reintentando, queda rechazado con el motivo a la vista."""
        rota = _orden(sku_id="MULTI", items=[{"sku_id": "7508", "cantidad": "dos",
                                              "precio_unitario": 1000.0}])
        await _encolar(db, rota)
        reqs: list = []
        assert await enviar_pedido_erp(rota, client=_cliente_pedidos([OK_201], reqs),
                                       db=db) is None
        assert reqs == []
        fila = await _fila_erp(db)
        assert fila["erp_estado"] == "rechazado"
        assert "renglones ilegibles" in fila["erp_ultimo_error"]


class TestJobPorPedido:
    async def test_un_pedido_roto_no_corta_la_pasada(self, db, alta):
        from app.services.mercurio_pedidos import reintentar_pedidos_pendientes
        rota = _orden(order_id="ORD-A-ROTA", mp_payment_id="mp-a", sku_id="MULTI",
                      items=[{"sku_id": "7508", "cantidad": "dos", "precio_unitario": 1.0}])
        buena = _orden(order_id="ORD-B-BUENA", mp_payment_id="mp-b")
        await _encolar(db, rota, buena)
        reqs: list = []
        n = await reintentar_pedidos_pendientes(client=_cliente_pedidos([OK_201], reqs), db=db)
        assert n == 1
        assert (await _fila_erp(db, "ORD-B-BUENA"))["erp_estado"] == "enviado"

    async def test_si_enviar_lanza_igual_sigue_con_el_proximo(self, db, alta, monkeypatch):
        """Defensa en profundidad: aunque el alta de un pedido lance, el job
        atrapa por pedido y sigue."""
        import app.services.mercurio_pedidos as mp
        from app.services.mercurio_pedidos import reintentar_pedidos_pendientes
        original = mp._intentar_alta

        async def _falla_la_primera(order, **kw):
            if order["order_id"] == "ORD-A":
                raise RuntimeError("boom")
            return await original(order, **kw)

        monkeypatch.setattr(mp, "_intentar_alta", _falla_la_primera)
        await _encolar(db, _orden(order_id="ORD-A", mp_payment_id="mp-a"),
                       _orden(order_id="ORD-B", mp_payment_id="mp-b"))
        reqs: list = []
        n = await reintentar_pedidos_pendientes(client=_cliente_pedidos([OK_201], reqs), db=db)
        assert n == 1
        assert (await _fila_erp(db, "ORD-B"))["erp_estado"] == "enviado"

    async def test_credencial_corta_la_pasada_sin_consumir_intentos(self, db, alta, esperas,
                                                                    caplog):
        from app.services.mercurio_pedidos import reintentar_pedidos_pendientes
        await _encolar(db, _orden(order_id="ORD-A", mp_payment_id="mp-a"),
                       _orden(order_id="ORD-B", mp_payment_id="mp-b"))
        reqs: list = []
        c = _cliente_pedidos([(401, {"error": True, "message": "clave inválida"}, None)] * 2,
                             reqs)
        assert await reintentar_pedidos_pendientes(client=c, db=db) == 0
        assert len(reqs) == 1                                  # cortó la pasada
        for oid in ("ORD-A", "ORD-B"):
            fila = await _fila_erp(db, oid)
            assert (fila["erp_estado"], fila["erp_intentos"]) == ("pendiente", 0)
        assert any(r.levelname == "ERROR" and "credencial" in r.getMessage().lower()
                   for r in caplog.records)


# ── Backoff por pedido y cola de vencidos (hallazgo 8, fix-C2) ───────────────

async def _segundos_hasta_el_proximo(db, order_id="ORD-20261005-120000-AB12C"):
    row = await db.fetchrow(
        "SELECT EXTRACT(EPOCH FROM erp_proximo_intento - now())::float8 AS s "
        "FROM orders WHERE order_id = $1", order_id)
    return row["s"]


def _cliente_500(reqs):
    return _cliente_pedidos([(500, {"error": True, "message": "caído"}, None)] * 3, reqs)


class TestBackoff:
    async def test_cada_falla_reintentable_duplica_la_espera(self, db, alta, esperas):
        await _encolar(db, _orden())
        reqs: list = []
        assert await enviar_pedido_erp(_orden(), client=_cliente_500(reqs), db=db) is None
        fila = await _fila_erp(db)
        assert (fila["erp_estado"], fila["erp_intentos"]) == ("pendiente", 1)
        assert 295 <= await _segundos_hasta_el_proximo(db) <= 300      # 300 * 2^0

        assert await enviar_pedido_erp(_orden(), client=_cliente_500(reqs), db=db) is None
        assert (await _fila_erp(db))["erp_intentos"] == 2
        assert 595 <= await _segundos_hasta_el_proximo(db) <= 600      # 300 * 2^1

    async def test_la_espera_no_pasa_de_6_horas(self, db, alta, esperas):
        await _encolar(db, _orden())
        await db.execute("UPDATE orders SET erp_intentos = 9")
        reqs: list = []
        await enviar_pedido_erp(_orden(), client=_cliente_500(reqs), db=db)
        assert (await _fila_erp(db))["erp_intentos"] == 10
        assert 21595 <= await _segundos_hasta_el_proximo(db) <= 21600

    async def test_un_intento_enorme_no_rompe_el_calculo(self, db, alta, esperas):
        await _encolar(db, _orden())
        await db.execute("UPDATE orders SET erp_intentos = 5000")
        reqs: list = []
        await enviar_pedido_erp(_orden(), client=_cliente_500(reqs), db=db)
        assert 21595 <= await _segundos_hasta_el_proximo(db) <= 21600

    async def test_sin_codigo_tambien_espera(self, db, alta):
        from app.services.order_store import get_order_store
        await get_order_store(db).upsert(_orden(), erp_estado="pendiente")   # sin códigos
        reqs: list = []
        await enviar_pedido_erp(_orden(), client=_cliente_pedidos([OK_201], reqs), db=db)
        assert reqs == []
        assert 295 <= await _segundos_hasta_el_proximo(db) <= 300

    async def test_la_credencial_no_aplaza_el_pedido(self, db, alta, esperas):
        await _encolar(db, _orden())
        reqs: list = []
        c = _cliente_pedidos([(401, {"error": True, "message": "clave"}, None)], reqs)
        await enviar_pedido_erp(_orden(), client=c, db=db)
        assert (await _fila_erp(db))["erp_proximo_intento"] is None

    async def test_la_cola_toma_solo_los_vencidos_en_orden(self, db):
        from app.services.order_store import get_order_store
        store = get_order_store(db)
        for oid, pid in (("ORD-NULO", "mp-1"), ("ORD-FUTURO", "mp-2"), ("ORD-PASADO", "mp-3"),
                         ("ORD-ENVIADO", "mp-4")):
            await store.upsert(_orden(order_id=oid, mp_payment_id=pid), erp_estado="pendiente")
        await db.execute("UPDATE orders SET created_at = now() - interval '2 hours' "
                         "WHERE order_id = 'ORD-NULO'")
        await db.execute("UPDATE orders SET erp_proximo_intento = now() + interval '1 hour' "
                         "WHERE order_id = 'ORD-FUTURO'")
        await db.execute("UPDATE orders SET erp_proximo_intento = now() - interval '3 hours' "
                         "WHERE order_id = 'ORD-PASADO'")
        await store.marcar_erp("ORD-ENVIADO", "enviado", id_comprobante="FC-9", numero="9")
        assert [p["order_id"] for p in await store.pendientes_erp()] == ["ORD-PASADO",
                                                                         "ORD-NULO"]

    async def test_la_cabeza_de_la_cola_no_bloquea_al_resto(self, db, alta, esperas):
        """El R6 de la revisión: con un error permanente reintentable en los
        primeros de la cola, el de atrás igual se manda en la pasada siguiente."""
        from app.services.mercurio_pedidos import reintentar_pedidos_pendientes
        await _encolar(db, _orden(order_id="ORD-1-MALO", mp_payment_id="mp-1"),
                       _orden(order_id="ORD-2-MALO", mp_payment_id="mp-2"),
                       _orden(order_id="ORD-3-BUENO", mp_payment_id="mp-3"))
        await db.execute("UPDATE orders SET created_at = now() - make_interval(mins => "
                         "CASE order_id WHEN 'ORD-1-MALO' THEN 30 WHEN 'ORD-2-MALO' THEN 20 "
                         "ELSE 10 END)")

        def _handler(request):
            if json.loads(request.content)["number"].endswith("MALO"):
                return httpx.Response(500, json={"error": True, "message": "x"})
            return httpx.Response(201, json=OK_201[1])

        c = MercurioClient("mrc_test", "https://api.mercurio.test/v1", timeout=5,
                           transport=httpx.MockTransport(_handler))
        assert await reintentar_pedidos_pendientes(client=c, db=db, limit=2) == 0
        assert await reintentar_pedidos_pendientes(client=c, db=db, limit=2) == 1
        assert (await _fila_erp(db, "ORD-3-BUENO"))["erp_estado"] == "enviado"


class TestVencido:
    """Un pedido 'pendiente' con más de MERCURIO_PEDIDOS_MAX_DIAS (6; la
    Idempotency-Key dura 7) pasa a 'vencido' y no se reintenta más:
    reintentarlo después de 7 días podría duplicarlo en el ERP."""

    def test_el_tope_por_defecto_es_6_dias(self):
        from app.config import Settings
        assert Settings.model_fields["mercurio_pedidos_max_dias"].default == 6

    async def test_pendiente_viejo_pasa_a_vencido_sin_post(self, db, alta, caplog):
        from app.services.mercurio_pedidos import reintentar_pedidos_pendientes
        await _encolar(db, _orden(order_id="ORD-VIEJO", mp_payment_id="mp-v"),
                       _orden(order_id="ORD-NUEVO", mp_payment_id="mp-n"))
        await db.execute("UPDATE orders SET created_at = now() - interval '6 days 1 hour', "
                         "erp_ultimo_error = '/pedidos: HTTP 503' WHERE order_id = 'ORD-VIEJO'")
        await db.execute("UPDATE orders SET created_at = now() - interval '5 days 23 hours' "
                         "WHERE order_id = 'ORD-NUEVO'")
        reqs: list = []
        n = await reintentar_pedidos_pendientes(
            client=_cliente_pedidos([OK_201, OK_201], reqs), db=db)
        assert n == 1
        assert [json.loads(r.content)["number"] for r in reqs] == ["ORD-NUEVO"]
        viejo = await _fila_erp(db, "ORD-VIEJO")
        assert viejo["erp_estado"] == "vencido"
        assert "6 días" in viejo["erp_ultimo_error"]
        assert "HTTP 503" in viejo["erp_ultimo_error"]           # conserva el último error
        assert any(r.levelname == "ERROR" and "ORD-VIEJO" in r.getMessage()
                   for r in caplog.records)

        # Y no se reintenta más
        assert await reintentar_pedidos_pendientes(
            client=_cliente_pedidos([OK_201], reqs), db=db) == 0
        assert (await _fila_erp(db, "ORD-VIEJO"))["erp_estado"] == "vencido"

    async def test_el_tope_sale_del_setting(self, db, alta, monkeypatch):
        from app.services.mercurio_pedidos import reintentar_pedidos_pendientes
        monkeypatch.setattr(get_settings(), "mercurio_pedidos_max_dias", 2)
        await _encolar(db, _orden())
        await db.execute("UPDATE orders SET created_at = now() - interval '3 days'")
        reqs: list = []
        assert await reintentar_pedidos_pendientes(
            client=_cliente_pedidos([OK_201], reqs), db=db) == 0
        assert reqs == []
        fila = await _fila_erp(db)
        assert fila["erp_estado"] == "vencido" and "2 días" in fila["erp_ultimo_error"]

    async def test_solo_vencen_los_pendientes(self, db, alta):
        from app.services.mercurio_pedidos import reintentar_pedidos_pendientes
        from app.services.order_store import get_order_store
        store = get_order_store(db)
        await store.upsert(_orden(order_id="ORD-E", mp_payment_id="mp-e"), erp_estado="pendiente")
        await store.marcar_erp("ORD-E", "enviado", id_comprobante="FC-1", numero="1")
        await store.upsert(_orden(order_id="ORD-R", mp_payment_id="mp-r"), erp_estado="pendiente")
        await store.marcar_erp("ORD-R", "rechazado", error="422")
        await store.upsert(_orden(order_id="ORD-N", mp_payment_id="mp-x"))   # NULL: no aplica
        await db.execute("UPDATE orders SET created_at = now() - interval '30 days'")
        await reintentar_pedidos_pendientes(client=_cliente_pedidos([], []), db=db)
        rows = await db.fetch("SELECT order_id, erp_estado FROM orders ORDER BY order_id")
        assert [(r["order_id"], r["erp_estado"]) for r in rows] == [
            ("ORD-E", "enviado"), ("ORD-N", None), ("ORD-R", "rechazado")]


# ── customer_id (hallazgo 10, fix-C2) ────────────────────────────────────────

class TestCustomerId:
    async def test_sin_customer_id_no_hay_post_y_queda_pendiente(self, db, alta, monkeypatch):
        monkeypatch.setattr(get_settings(), "mercurio_customer_id_default", "  ")
        await _encolar(db, _orden())
        reqs: list = []
        assert await enviar_pedido_erp(_orden(), client=_cliente_pedidos([OK_201], reqs),
                                       db=db) is None
        assert reqs == []
        fila = await _fila_erp(db)
        assert fila["erp_estado"] == "pendiente"
        assert fila["erp_ultimo_error"] == "customer_id sin configurar"
        assert fila["erp_intentos"] == 1 and fila["erp_proximo_intento"] is not None

    async def test_con_customer_id_en_la_orden_alcanza(self, db, alta, monkeypatch):
        monkeypatch.setattr(get_settings(), "mercurio_customer_id_default", "")
        orden = _orden(customer_id="20304050")
        await _encolar(db, orden)
        reqs: list = []
        assert await enviar_pedido_erp(orden, client=_cliente_pedidos([OK_201], reqs),
                                       db=db) is not None
        assert json.loads(reqs[0].content)["customer_id"] == "20304050"


class TestConfiguracionAlArrancar:
    def _settings(self, monkeypatch, **valores):
        s = get_settings()
        base = {"mercurio_pedidos_enabled": True, "mercurio_api_key": "mrc_x",
                "mercurio_customer_id_default": "20111111112", "mercurio_pedidos_max_dias": 6}
        for k, v in {**base, **valores}.items():
            monkeypatch.setattr(s, k, v)

    def test_flag_apagado_no_hay_problemas(self, monkeypatch):
        from app.services.mercurio_pedidos import problemas_de_configuracion
        self._settings(monkeypatch, mercurio_pedidos_enabled=False, mercurio_api_key="",
                       mercurio_customer_id_default="")
        assert problemas_de_configuracion() == []

    def test_completo_no_hay_problemas(self, monkeypatch):
        from app.services.mercurio_pedidos import problemas_de_configuracion
        self._settings(monkeypatch)
        assert problemas_de_configuracion() == []

    def test_sin_customer_id_ni_clave(self, monkeypatch):
        from app.services.mercurio_pedidos import problemas_de_configuracion
        self._settings(monkeypatch, mercurio_api_key="", mercurio_customer_id_default=" ")
        problemas = problemas_de_configuracion()
        assert len(problemas) == 2
        assert any("MERCURIO_CUSTOMER_ID_DEFAULT" in p for p in problemas)
        assert any("MERCURIO_API_KEY" in p for p in problemas)

    def test_tope_de_dias_que_alcanza_a_la_clave(self, monkeypatch):
        from app.services.mercurio_pedidos import problemas_de_configuracion
        self._settings(monkeypatch, mercurio_pedidos_max_dias=7)
        [p] = problemas_de_configuracion()
        assert "MERCURIO_PEDIDOS_MAX_DIAS" in p

    def test_el_arranque_lo_loguea_como_error(self, monkeypatch, usar_perfil, caplog):
        """Antes de tocar Redis (mismo corte que tests/test_perfil.py)."""
        import logging

        from fastapi.testclient import TestClient

        import app.main as main

        class _Corte(BaseException):
            pass

        def _cortar(*a, **k):
            raise _Corte()

        monkeypatch.setattr(main, "get_blob_store", _cortar)
        usar_perfil("petshop")
        self._settings(monkeypatch, mercurio_api_key="", mercurio_customer_id_default="")
        with caplog.at_level(logging.INFO, logger="app.main"):
            with pytest.raises(_Corte):
                with TestClient(main.app):
                    pass
        errores = [r.getMessage() for r in caplog.records
                   if r.levelname == "ERROR" and r.name == "app.main"]
        assert any("MERCURIO_CUSTOMER_ID_DEFAULT" in m for m in errores)
        assert any("MERCURIO_API_KEY" in m for m in errores)


# ── Alta en segundo plano (hallazgo 12) y pedidos en vuelo (M2), fix-C2 ──────

class _ClienteColgado:
    """Mercurio que no contesta hasta que el test lo suelta."""

    def __init__(self):
        import asyncio
        self.soltar = asyncio.Event()
        self.llamado = asyncio.Event()
        self.reqs: list = []

    async def crear_pedido(self, pedido, idempotency_key):
        self.reqs.append(idempotency_key)
        self.llamado.set()
        await self.soltar.wait()
        return {"id_comprobante": "FC-9", "numero": "9", "replay": False}


@pytest.fixture
async def sin_altas_colgadas():
    """Al terminar el test, cancela las altas en segundo plano que queden."""
    yield
    from app.services.mercurio_pedidos import esperar_altas_en_curso
    await esperar_altas_en_curso(timeout=0)


class TestAltaEnSegundoPlano:
    async def test_flag_apagado_no_programa_nada(self, db, monkeypatch, sin_altas_colgadas):
        import app.services.mercurio_pedidos as mp
        monkeypatch.setattr(get_settings(), "mercurio_pedidos_enabled", False)
        c = _ClienteColgado()
        assert mp.programar_alta_erp(_orden(), client=c, db=db) is None
        assert mp._tareas == set() and mp._en_vuelo == set()

    async def test_programa_y_vuelve_enseguida(self, db, alta, sin_altas_colgadas):
        import app.services.mercurio_pedidos as mp
        await _encolar(db, _orden())
        c = _ClienteColgado()
        tarea = mp.programar_alta_erp(_orden(), client=c, db=db)
        assert tarea is not None and not tarea.done()
        assert "ORD-20261005-120000-AB12C" in mp._en_vuelo
        await c.llamado.wait()                         # el POST salió y está colgado
        assert (await _fila_erp(db))["erp_estado"] == "pendiente"
        c.soltar.set()
        assert await mp.esperar_altas_en_curso() == 0
        assert (await _fila_erp(db))["erp_estado"] == "enviado"
        assert mp._tareas == set() and mp._en_vuelo == set()

    def test_sin_loop_no_lanza(self, monkeypatch):
        """Llamado fuera de un event loop: no lanza ni deja la orden en vuelo."""
        import app.services.mercurio_pedidos as mp
        monkeypatch.setattr(get_settings(), "mercurio_pedidos_enabled", True)
        assert mp.programar_alta_erp(_orden()) is None
        assert "ORD-20261005-120000-AB12C" not in mp._en_vuelo

    async def test_orden_sin_id_no_lanza(self, alta, sin_altas_colgadas):
        import app.services.mercurio_pedidos as mp
        assert mp.programar_alta_erp({"total": 1}) is None
        assert mp.programar_alta_erp(None) is None

    async def test_el_mismo_pedido_no_se_programa_dos_veces(self, db, alta, sin_altas_colgadas):
        import app.services.mercurio_pedidos as mp
        await _encolar(db, _orden())
        c = _ClienteColgado()
        assert mp.programar_alta_erp(_orden(), client=c, db=db) is not None
        assert mp.programar_alta_erp(_orden(), client=c, db=db) is None
        await c.llamado.wait()
        c.soltar.set()
        await mp.esperar_altas_en_curso()
        assert c.reqs == ["ORD-20261005-120000-AB12C"]

    async def test_el_job_saltea_un_pedido_en_vuelo(self, db, alta, sin_altas_colgadas):
        import app.services.mercurio_pedidos as mp
        await _encolar(db, _orden())
        c = _ClienteColgado()
        mp.programar_alta_erp(_orden(), client=c, db=db)
        await c.llamado.wait()
        reqs: list = []
        assert await mp.reintentar_pedidos_pendientes(
            client=_cliente_pedidos([OK_201], reqs), db=db) == 0
        assert reqs == []                               # no hubo un segundo POST en paralelo
        c.soltar.set()
        await mp.esperar_altas_en_curso()
        assert (await _fila_erp(db))["erp_estado"] == "enviado"

    async def test_el_hook_saltea_un_pedido_que_manda_el_job(self, db, alta, sin_altas_colgadas):
        import asyncio

        import app.services.mercurio_pedidos as mp
        await _encolar(db, _orden())
        c = _ClienteColgado()
        job = asyncio.create_task(mp.reintentar_pedidos_pendientes(client=c, db=db))
        await c.llamado.wait()
        assert mp.programar_alta_erp(_orden(), client=c, db=db) is None
        c.soltar.set()
        assert await job == 1
        assert c.reqs == ["ORD-20261005-120000-AB12C"]
        assert mp._en_vuelo == set()

    async def test_al_apagar_se_cancelan_y_quedan_pendientes(self, db, alta, sin_altas_colgadas):
        import app.services.mercurio_pedidos as mp
        await _encolar(db, _orden())
        c = _ClienteColgado()
        mp.programar_alta_erp(_orden(), client=c, db=db)
        await c.llamado.wait()
        assert await mp.esperar_altas_en_curso(timeout=0.05) == 1
        assert mp._tareas == set() and mp._en_vuelo == set()
        assert (await _fila_erp(db))["erp_estado"] == "pendiente"


# ── Visibilidad (hallazgo 9, fix-C2) ─────────────────────────────────────────

class _RedisPedidos:
    """Redis mínimo de OrderService para la consola de pedidos."""

    def __init__(self):
        self.kv: dict[str, str] = {}
        self.z: dict[str, dict[str, float]] = {}

    async def setex(self, key, ttl, value):
        self.kv[key] = value

    async def get(self, key):
        return self.kv.get(key)

    async def mget(self, keys):
        return [self.kv.get(k) for k in keys]

    async def zadd(self, key, mapping):
        self.z.setdefault(key, {}).update(mapping)

    async def zrevrange(self, key, a, b):
        return sorted(self.z.get(key, {}), key=lambda k: -self.z[key][k])[a:b + 1]


def _cliente_bo(monkeypatch, svc=None):
    """Consola de pedidos + /bo/mercurio/* en el MISMO event loop del test
    (el pool de asyncpg del fixture db no cruza de loop)."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.routers import backoffice_branches, orders_api
    monkeypatch.setattr(get_settings(), "bo_key", "")
    if svc is not None:
        monkeypatch.setattr(orders_api, "get_order_service", lambda *a, **k: svc)
    app = FastAPI()
    app.include_router(orders_api.router)
    app.include_router(backoffice_branches.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


CAMPOS_ERP = ("erp_estado", "erp_ultimo_error", "erp_intentos", "erp_numero",
              "erp_id_comprobante")


class TestConsolaDePedidos:
    async def _tres_pedidos(self, db):
        """Uno rechazado, uno enviado (en Redis y en Postgres) y uno solo en Redis."""
        from app.services.order_store import get_order_store
        svc = _order_service_falso()
        svc._redis = _RedisPedidos()
        store = get_order_store(db)
        rech = await svc.create(phone="549341111", sku_id="7508", sku_nombre="ROYAL",
                                cantidad=1, total=10.0, mp_payment_id="mp-r")
        env = await svc.create(phone="549341222", sku_id="7508", sku_nombre="ROYAL",
                               cantidad=1, total=10.0, mp_payment_id="mp-e")
        await store.marcar_erp(rech["order_id"], "rechazado", error="HTTP 422: state inválido",
                               incrementar_intento=True)
        await store.marcar_erp(env["order_id"], "enviado", id_comprobante="FC-7", numero="77",
                               incrementar_intento=True)
        solo_redis = {"order_id": "ORD-SOLO-REDIS", "phone": "549341333", "estado": "pendiente"}
        await svc._redis.setex("order:ORD-SOLO-REDIS", 0, json.dumps(solo_redis))
        await svc._redis.zadd("orders:idx", {"ORD-SOLO-REDIS": 0.5})
        return svc, rech["order_id"], env["order_id"]

    async def test_lista_y_detalle_traen_el_estado_del_erp(self, db, monkeypatch):
        svc, rech, env = await self._tres_pedidos(db)
        async with _cliente_bo(monkeypatch, svc) as ac:
            r = await ac.get("/orders/api/list")
            assert r.status_code == 200
            por_id = {o["order_id"]: o for o in r.json()}
            assert {k: por_id[rech][k] for k in CAMPOS_ERP} == {
                "erp_estado": "rechazado", "erp_ultimo_error": "HTTP 422: state inválido",
                "erp_intentos": 1, "erp_numero": None, "erp_id_comprobante": None}
            assert {k: por_id[env][k] for k in CAMPOS_ERP} == {
                "erp_estado": "enviado", "erp_ultimo_error": None, "erp_intentos": 1,
                "erp_numero": "77", "erp_id_comprobante": "FC-7"}
            assert {k: por_id["ORD-SOLO-REDIS"][k] for k in CAMPOS_ERP} == dict.fromkeys(
                CAMPOS_ERP)

            d = await ac.get(f"/orders/api/{rech}")
            assert d.status_code == 200
            assert d.json()["erp_estado"] == "rechazado"
            assert d.json()["erp_ultimo_error"] == "HTTP 422: state inválido"
            d = await ac.get("/orders/api/ORD-SOLO-REDIS")
            assert d.status_code == 200 and d.json()["erp_estado"] is None

    async def test_con_postgres_caido_los_campos_van_en_null(self, db, monkeypatch):
        svc, rech, _ = await self._tres_pedidos(db)
        monkeypatch.setattr(dbmod, "_instance", Database(""))      # sin Postgres
        async with _cliente_bo(monkeypatch, svc) as ac:
            r = await ac.get("/orders/api/list")
            assert r.status_code == 200 and len(r.json()) == 3
            for o in r.json():
                assert {k: o[k] for k in CAMPOS_ERP} == dict.fromkeys(CAMPOS_ERP)
            d = await ac.get(f"/orders/api/{rech}")
            assert d.status_code == 200 and d.json()["erp_estado"] is None


class TestEstadoMercurio:
    async def _cola(self, db):
        from app.services.order_store import get_order_store
        store = get_order_store(db)
        for oid, pid in (("ORD-P1", "mp-1"), ("ORD-P2-VIEJO", "mp-2"), ("ORD-R", "mp-3"),
                         ("ORD-V", "mp-4"), ("ORD-E", "mp-5")):
            await store.upsert(_orden(order_id=oid, mp_payment_id=pid), erp_estado="pendiente")
        await store.upsert(_orden(order_id="ORD-NULL", mp_payment_id="mp-6"))   # farmacia
        await db.execute("UPDATE orders SET created_at = now() - interval '2 hours' "
                         "WHERE order_id = 'ORD-P2-VIEJO'")
        await store.marcar_erp("ORD-R", "rechazado", error="HTTP 400: viejo")
        await store.marcar_erp("ORD-V", "vencido", error="vencido: más de 6 días")
        await store.marcar_erp("ORD-E", "enviado", id_comprobante="FC-1", numero="1")
        await store.marcar_erp("ORD-P1", "pendiente", error="HTTP 503", incrementar_intento=True)
        await db.execute("UPDATE orders SET updated_at = now() - interval '1 minute' "
                         "WHERE order_id <> 'ORD-P1'")

    async def test_bloque_pedidos_sin_clave(self, db, alta, monkeypatch):
        monkeypatch.setattr(get_settings(), "mercurio_api_key", "")
        await self._cola(db)
        async with _cliente_bo(monkeypatch) as ac:
            r = await ac.get("/bo/mercurio/estado")
        assert r.status_code == 200
        body = r.json()
        assert body["configurado"] is False
        p = body["pedidos"]
        assert (p["habilitado"], p["customer_id_default"]) == (True, True)
        assert (p["pendientes"], p["pendientes_mas_1h"], p["rechazados"], p["vencidos"]) == \
            (2, 1, 1, 1)
        assert p["ultimo_error"]["order_id"] == "ORD-P1"
        assert p["ultimo_error"]["erp_estado"] == "pendiente"
        assert p["ultimo_error"]["error"] == "HTTP 503"

    async def test_bloque_pedidos_con_clave_y_sin_customer_id(self, db, alta, monkeypatch):
        import app.services.mercurio_service as msvc

        class _Cli:
            async def estado(self):
                return {"ok": True}

        class _Sync:
            ultimo = {"variantes": 3}

        s = get_settings()
        monkeypatch.setattr(s, "mercurio_api_key", "mrc_x")
        monkeypatch.setattr(s, "mercurio_customer_id_default", "")
        monkeypatch.setattr(msvc, "get_mercurio_client", lambda: _Cli())
        monkeypatch.setattr(msvc, "get_mercurio_sync", lambda: _Sync())
        async with _cliente_bo(monkeypatch) as ac:
            r = await ac.get("/bo/mercurio/estado")
        body = r.json()
        assert body["configurado"] is True and body["servicio"] == {"ok": True}
        assert body["ultimo_sync"] == {"variantes": 3}
        p = body["pedidos"]
        assert (p["habilitado"], p["customer_id_default"]) == (True, False)
        assert (p["pendientes"], p["rechazados"], p["vencidos"]) == (0, 0, 0)
        assert p["ultimo_error"] is None

    async def test_bloque_pedidos_con_postgres_caido(self, db, alta, monkeypatch):
        monkeypatch.setattr(get_settings(), "mercurio_api_key", "")
        monkeypatch.setattr(dbmod, "_instance", Database(""))
        async with _cliente_bo(monkeypatch) as ac:
            r = await ac.get("/bo/mercurio/estado")
        assert r.status_code == 200
        p = r.json()["pedidos"]
        assert p["habilitado"] is True and p["pendientes"] is None
        assert "error" in p


@pytest.fixture
def eventos(db, monkeypatch):
    """metrics_store apuntando a la base del test (es un singleton)."""
    import app.services.metrics_store as ms
    monkeypatch.setattr(ms, "_instance", None)

    async def _leer():
        rows = await db.fetch("SELECT tipo, ref, phone, dato FROM eventos "
                              "WHERE tipo LIKE 'erp_pedido_%' ORDER BY id")
        return [dict(r) for r in rows]

    return _leer


class TestAvisos:
    async def test_rechazo_del_erp_deja_error_y_evento(self, db, alta, eventos, caplog):
        await db.execute("DELETE FROM eventos WHERE tipo LIKE 'erp_pedido_%'")
        await _encolar(db, _orden())
        reqs: list = []
        c = _cliente_pedidos([(422, {"error": True, "message": "state inválido"}, None)], reqs)
        await enviar_pedido_erp(_orden(), client=c, db=db)
        [ev] = await eventos()
        assert (ev["tipo"], ev["ref"], ev["phone"]) == (
            "erp_pedido_rechazado", "ORD-20261005-120000-AB12C", "5493415551234")
        assert "state inválido" in ev["dato"]
        assert any(r.levelname == "ERROR" and "ORD-20261005-120000-AB12C" in r.getMessage()
                   for r in caplog.records)

    async def test_renglones_que_no_cuadran_dejan_evento(self, db, alta, eventos):
        await db.execute("DELETE FROM eventos WHERE tipo LIKE 'erp_pedido_%'")
        orden = _orden_con_items(total=3000.0)
        await _encolar(db, orden)
        await enviar_pedido_erp(orden, client=_cliente_pedidos([], []), db=db)
        [ev] = await eventos()
        assert ev["tipo"] == "erp_pedido_rechazado" and ev["ref"] == orden["order_id"]

    async def test_vencido_deja_error_y_evento(self, db, alta, eventos, caplog):
        from app.services.mercurio_pedidos import reintentar_pedidos_pendientes
        await db.execute("DELETE FROM eventos WHERE tipo LIKE 'erp_pedido_%'")
        await _encolar(db, _orden(order_id="ORD-VIEJO", mp_payment_id="mp-v"))
        await db.execute("UPDATE orders SET created_at = now() - interval '7 days'")
        await reintentar_pedidos_pendientes(client=_cliente_pedidos([], []), db=db)
        [ev] = await eventos()
        assert (ev["tipo"], ev["ref"]) == ("erp_pedido_vencido", "ORD-VIEJO")
        assert "vencido" in ev["dato"]
        assert any(r.levelname == "ERROR" and "ORD-VIEJO" in r.getMessage()
                   for r in caplog.records)

    async def test_un_pendiente_no_deja_evento(self, db, alta, eventos, esperas):
        await db.execute("DELETE FROM eventos WHERE tipo LIKE 'erp_pedido_%'")
        await _encolar(db, _orden())
        await enviar_pedido_erp(_orden(), client=_cliente_500([]), db=db)
        assert await eventos() == []
