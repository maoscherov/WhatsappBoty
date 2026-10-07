"""
F5 de punta a punta: link de pago -> cobro (Mercado Pago / Payway) -> orden ->
POST /pedidos de Mercurio. Arreglos de la revisión final del 6/10 (fix-C1):

- hallazgos 6 y 14: la orden cobrada nace 'pendiente' en el mismo INSERT, así
  un corte del post-cobro (WhatsApp, config, sesión) no la deja fuera de la
  cola del job;
- hallazgo 5: el pedido llega con sus renglones (carrito y envío);
- hallazgo 11: un mismo pago crea una sola orden aunque Redis se pierda.

Corre contra el Postgres embebido (pg_dsn) con un Mercurio falso
(httpx.MockTransport) que contesta 422 si un variant_id no existe.
"""
import json

import httpx
import pytest

import app.services.db as dbmod
import app.services.mercurio_service as msvc
from app.config import get_settings
from app.services.db import Database
from app.services.mercurio_service import MercurioClient

PHONE = "5491155550000"
# codigo de variante -> codigo_padre que el ERP falso conoce
CODIGOS_ERP = {"2209004": "ROY1051701", "10955": "PET1304801"}


@pytest.fixture
async def db(pg_dsn):
    import app.services.order_store as osmod
    d = Database(pg_dsn)
    assert await d.connect()
    await d.execute("TRUNCATE orders, mercurio_codigos")
    await d.execute(
        "INSERT INTO mercurio_codigos (branch_id, external_id, codigo, codigo_padre) VALUES "
        "('mascotas-oeste', '7508', '2209004', 'ROY1051701'),"
        "('mascotas-oeste', '15181', '10955', 'PET1304801')")
    prev = dbmod._instance
    dbmod._instance = d
    osmod._instance = None
    yield d
    dbmod._instance = prev
    osmod._instance = None
    await d.close()


class _ERP:
    """Mercurio falso: 422 si un variant_id no existe o falta customer_id."""

    def __init__(self):
        self.reqs: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.reqs.append(body)
        if not body.get("customer_id"):
            return httpx.Response(422, json={"error": True, "message": "customer_id obligatorio"})
        for li in body["line_items"]:
            if CODIGOS_ERP.get(li["variant_id"]) != li["product_id"]:
                return httpx.Response(422, json={
                    "error": True, "message": f"variant_id {li['variant_id']} inexistente"})
        return httpx.Response(201, json={"ok": True, "id_comprobante": "FC-1", "numero": "1"})

    def cliente(self) -> MercurioClient:
        return MercurioClient("mrc_test", "https://api.mercurio.test/v1", timeout=5,
                              transport=httpx.MockTransport(self.handler))


@pytest.fixture
def erp(monkeypatch, usar_perfil, db):
    usar_perfil("petshop")
    s = get_settings()
    monkeypatch.setattr(s, "mercurio_pedidos_enabled", True)
    monkeypatch.setattr(s, "mercurio_branch_id", "mascotas-oeste")
    monkeypatch.setattr(s, "mercurio_api_key", "mrc_test")
    monkeypatch.setattr(s, "mercurio_customer_id_default", "20111111112")

    async def _sin_espera(_s):
        return None

    monkeypatch.setattr(msvc.asyncio, "sleep", _sin_espera)
    e = _ERP()
    monkeypatch.setattr(msvc, "get_mercurio_client", lambda: e.cliente())
    return e


# ── Fakes de la cadena posterior al cobro ─────────────────────────────────────

class _FakeRedis:
    """Lo que usa OrderService: pedidos (setex/mget) y su índice (zadd/zrevrange)."""

    def __init__(self):
        self.kv: dict[str, str] = {}
        self.z: dict[str, dict[str, float]] = {}

    async def setex(self, key, ttl, value):
        self.kv[key] = value

    async def zadd(self, key, mapping):
        self.z.setdefault(key, {}).update(mapping)

    async def zrevrange(self, key, a, b):
        return sorted(self.z.get(key, {}), key=lambda k: -self.z[key][k])[a:b + 1]

    async def mget(self, keys):
        return [self.kv.get(k) for k in keys]


class _WA:
    def __init__(self, falla: bool = False):
        self.falla = falla
        self.enviados: list[str] = []

    async def send_text(self, to, text, **kw):
        if self.falla:
            raise RuntimeError("WhatsApp caído")
        self.enviados.append(text)
        return True


class _Cfg:
    def __init__(self, cfg=None):
        self.cfg = cfg or {}

    async def get_all(self):
        return dict(self.cfg)

    async def get_hours(self):
        return {}

    def get_pickup_text(self, hours, minutes):
        return ""


def _order_service(redis):
    from app.services.order_service import OrderService
    o = OrderService.__new__(OrderService)
    o._redis = redis
    return o


class _Entorno:
    """Redis, sesión y WhatsApp de la cadena; `redis` y `ss` se pueden
    reemplazar a mitad de un test (Redis que se reinicia sin persistencia)."""

    def __init__(self, wa=None, cfg=None):
        from app.services.session_service import SessionService
        self.redis = _FakeRedis()
        self.ss = SessionService("redis://127.0.0.1:1")
        self.wa = wa or _WA()
        self.cfg = cfg or {}

    def perder_redis(self):
        from app.services.session_service import SessionService
        self.redis = _FakeRedis()
        self.ss = SessionService("redis://127.0.0.1:1")


def _montar_sesion(monkeypatch, ent: _Entorno):
    import app.services.session_service as sesmod
    monkeypatch.setattr(sesmod, "get_session_service", lambda *a, **k: ent.ss)


def _montar_mp(monkeypatch, ent: _Entorno, payment: dict):
    from app.routers import mp_webhook

    class _Pay:
        async def get_payment_info(self, pid):
            return json.loads(json.dumps(payment))

    _montar_sesion(monkeypatch, ent)
    monkeypatch.setattr(mp_webhook, "get_payment_service", lambda *a, **k: _Pay())
    monkeypatch.setattr(mp_webhook, "get_order_service", lambda *a: _order_service(ent.redis))
    monkeypatch.setattr(mp_webhook, "get_whatsapp_service", lambda *a: ent.wa)
    monkeypatch.setattr(mp_webhook, "get_config_service", lambda *a: _Cfg(ent.cfg))
    monkeypatch.setattr(mp_webhook, "get_session_service", lambda *a: ent.ss)
    return mp_webhook


class _RedisKV:
    """Redis mínimo de Payway (get/setex sobre un dict compartido)."""

    def __init__(self, kv: dict):
        self.kv = kv

    async def get(self, k):
        return self.kv.get(k)

    async def setex(self, k, ttl, v):
        self.kv[k] = v


def _kv_con(pending: dict) -> dict:
    return {f"payway:pending:{pending['id']}": json.dumps(pending)}


def _montar_payway(monkeypatch, ent: _Entorno, kv: dict, payway_id: str):
    import app.routers.payway as pw
    import app.services.config_service as cs

    class _Pw:
        async def crear_pago(self, **k):
            return {"id": payway_id, "status": "approved", "card_brand": "Visa"}, None

    _montar_sesion(monkeypatch, ent)
    monkeypatch.setattr(pw, "_redis", lambda: _RedisKV(kv))
    monkeypatch.setattr(pw, "get_payway_service", lambda *a, **k: _Pw())
    monkeypatch.setattr(pw, "get_order_service", lambda *a: _order_service(ent.redis))
    monkeypatch.setattr(pw, "get_whatsapp_service", lambda *a: ent.wa)
    monkeypatch.setattr(cs, "get_config_service", lambda *a, **k: _Cfg(ent.cfg))
    return pw


def _pago_mp(sku="7508", total=1000.0, titulo="Royal 400 g", cantidad=1, metadata=None):
    p = {"status": "approved", "external_reference": f"{PHONE}_{sku}",
         "transaction_amount": total, "payment_method_id": "visa",
         "additional_info": {"items": [{"title": titulo, "quantity": cantidad}]}}
    if metadata is not None:
        p["metadata"] = metadata
    return p


def _pending_payway(pid="PID-1", sku="7508", total=1000.0, cantidad=1, extra=None):
    return {"id": pid, "phone": PHONE, "sku_id": sku, "sku_nombre": "Royal 400 g",
            "cantidad": cantidad, "total": total, "estado": "pendiente", **(extra or {})}


async def _filas(db):
    return [dict(r) for r in await db.fetch(
        "SELECT order_id, payment_id, erp_estado, erp_ultimo_error FROM orders "
        "ORDER BY created_at")]


# ══════════════════════════════════════════════════════════════════════════════
# Hallazgos 6 y 14: un corte después de crear la orden no la saca de la cola
# ══════════════════════════════════════════════════════════════════════════════

async def test_mp_corte_despues_de_crear_la_orden_queda_pendiente(erp, db, monkeypatch):
    from app.services.mercurio_pedidos import reintentar_pedidos_pendientes
    ent = _Entorno(wa=_WA(falla=True))
    mpw = _montar_mp(monkeypatch, ent, _pago_mp())

    r = await mpw.procesar_pago("mp-corte-1")
    assert r["status"] == "error"                       # el WhatsApp lanzó
    [fila] = await _filas(db)
    assert (fila["payment_id"], fila["erp_estado"]) == ("mp-corte-1", "pendiente")
    assert erp.reqs == []

    assert await reintentar_pedidos_pendientes(db=db) == 1
    assert [q["number"] for q in erp.reqs] == [fila["order_id"]]
    assert (await _filas(db))[0]["erp_estado"] == "enviado"


async def test_payway_corte_despues_de_crear_la_orden_queda_pendiente(erp, db, monkeypatch):
    from app.services.mercurio_pedidos import reintentar_pedidos_pendientes
    ent = _Entorno(wa=_WA(falla=True))
    pw = _montar_payway(monkeypatch, ent, _kv_con(_pending_payway("PID-CORTE")), "PW-CORTE-1")

    r = await pw.payway_charge(pw.ChargeIn(pid="PID-CORTE", token="tok", bin="450799"))
    assert r["status"] == "approved"                    # el cobro ya ocurrió
    [fila] = await _filas(db)
    assert (fila["payment_id"], fila["erp_estado"]) == ("PW-CORTE-1", "pendiente")
    assert erp.reqs == []

    assert await reintentar_pedidos_pendientes(db=db) == 1
    assert (await _filas(db))[0]["erp_estado"] == "enviado"


async def test_mp_con_el_flag_apagado_no_encola(erp, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "mercurio_pedidos_enabled", False)
    ent = _Entorno()
    mpw = _montar_mp(monkeypatch, ent, _pago_mp())
    r = await mpw.procesar_pago("mp-sin-flag-1")
    assert r["status"] == "ok"
    [fila] = await _filas(db)
    assert fila["erp_estado"] is None
    assert erp.reqs == []


# ══════════════════════════════════════════════════════════════════════════════
# Hallazgo 5: el pedido llega con sus renglones (carrito y envío)
# ══════════════════════════════════════════════════════════════════════════════

class _PagoQueCaptura:
    """Proveedor de cobro falso: guarda el link pedido y su snapshot."""

    def __init__(self):
        self.links: list[dict] = []

    async def crear_link(self, sku_id, nombre, precio, phone, cantidad=1, snapshot=None):
        self.links.append({"sku_id": sku_id, "nombre": nombre, "precio": precio,
                           "cantidad": cantidad, "snapshot": snapshot})
        return f"https://pago.test/{len(self.links)}", None


async def _link(monkeypatch, ent: _Entorno, tipo="retiro", direccion=None, pago=None):
    """El link de pago que arma el bot (crear_link_y_responder)."""
    import time

    import app.services.config_service as cs
    from app.services import checkout_helper as ch
    monkeypatch.setattr(cs, "get_config_service", lambda *a, **k: _Cfg(ent.cfg))
    s = await ent.ss.get(PHONE)
    s["_stock_ok_at"] = time.time()                 # sin chequeo de stock en vivo
    s["_stock_ok_para"] = ch._clave_stock(s)
    await ent.ss.save(PHONE, s)
    pago = pago or _PagoQueCaptura()
    _resp, url = await ch.crear_link_y_responder(pago, ent.ss, PHONE, await ent.ss.get(PHONE),
                                                 tipo, direccion)
    assert url, _resp
    return pago.links[-1] if isinstance(pago, _PagoQueCaptura) else url


def _pago_mp_del_link(link: dict, con_metadata: bool = True) -> dict:
    """Lo que devuelve MP al consultar el pago de ese link."""
    return _pago_mp(sku=link["sku_id"], total=round(link["precio"] * link["cantidad"], 2),
                    titulo=link["nombre"], cantidad=link["cantidad"],
                    metadata=link["snapshot"] if con_metadata else None)


async def _carrito(ss):
    await ss.set_pending(PHONE, sku_id="7508", sku_nombre="Royal 400 g", precio=1000.0,
                         cantidad=1)
    await ss.agregar_item(PHONE, "15181", "Collar rosa", 500.0, 2)


SNAP_CARRITO = {
    "items": [
        {"sku_id": "7508", "nombre": "Royal 400 g", "cantidad": 1,
         "precio_unitario": 1000.0, "total": 1000.0},
        {"sku_id": "15181", "nombre": "Collar rosa", "cantidad": 2,
         "precio_unitario": 500.0, "total": 1000.0},
    ],
    "costo_envio": 0.0,
    "total": 2000.0,
}


def _renglones(body):
    return [(li["variant_id"], li["product_id"], li["quantity"], li["subtotal"], li["total"])
            for li in body["line_items"]]


async def test_link_de_carrito_guarda_los_renglones(monkeypatch):
    ent = _Entorno()
    await _carrito(ent.ss)
    link = await _link(monkeypatch, ent)
    assert (link["sku_id"], link["precio"], link["cantidad"]) == ("MULTI", 2000.0, 1)  # igual que hoy
    assert link["snapshot"] == SNAP_CARRITO


async def test_link_con_envio_guarda_la_cantidad_y_el_envio_aparte(monkeypatch):
    ent = _Entorno(cfg={"envio_costo": "2000"})
    await ent.ss.set_pending(PHONE, sku_id="7508", sku_nombre="Royal 400 g", precio=1000.0,
                             cantidad=3)
    link = await _link(monkeypatch, ent, "envio", "San Martín 123")
    assert (link["precio"], link["cantidad"]) == (5000.0, 1)       # el link, igual que hoy
    assert link["snapshot"] == {
        "items": [{"sku_id": "7508", "nombre": "Royal 400 g", "cantidad": 3,
                   "precio_unitario": 1000.0, "total": 3000.0}],
        "costo_envio": 2000.0, "total": 5000.0}


def _http_falso(monkeypatch, modulo, status, data):
    """Reemplaza httpx.AsyncClient del módulo y devuelve los payloads posteados."""
    capturados = []

    class _Resp:
        status_code = status
        text = json.dumps(data)

        def json(self):
            return data

    class _Cliente:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None, timeout=None):
            capturados.append(json)
            return _Resp()

    monkeypatch.setattr(modulo.httpx, "AsyncClient", _Cliente)
    return capturados


async def test_mp_la_preferencia_lleva_el_snapshot_en_metadata(monkeypatch):
    from app.services import payment_service as ps
    cap = _http_falso(monkeypatch, ps, 201, {"init_point": "https://mp.test/x"})
    svc = ps.PaymentService("TEST-TOKEN", "https://bot.test/mp/notification")
    link, err = await svc.crear_link(sku_id="MULTI", nombre="2 productos", precio=2000.0,
                                     phone="549", cantidad=1, snapshot=SNAP_CARRITO)
    assert (link, err) == ("https://mp.test/x", None)
    assert cap[-1]["metadata"] == SNAP_CARRITO
    assert cap[-1]["items"][0]["unit_price"] == 2000.0               # el cobro no cambia
    await svc.crear_link(sku_id="S1", nombre="X", precio=1.0, phone="549")
    assert "metadata" not in cap[-1]


async def test_payway_el_pago_pendiente_lleva_el_snapshot(monkeypatch):
    import app.services.payway_link as pl
    kv: dict = {}
    monkeypatch.setattr(pl, "_redis", lambda: _RedisKV(kv))
    monkeypatch.setattr(get_settings(), "public_base_url", "https://bot.test")
    url, err = await pl.PaywayLinkService().crear_link(
        sku_id="MULTI", nombre="2 productos", precio=2000.0, phone=PHONE, cantidad=1,
        snapshot=SNAP_CARRITO)
    assert err is None
    pend = json.loads(kv[f"payway:pending:{url.rsplit('/', 1)[-1]}"])
    assert pend["items"] == SNAP_CARRITO["items"]
    assert pend["costo_envio"] == 0.0 and pend["total"] == 2000.0
    assert (pend["sku_id"], pend["cantidad"]) == ("MULTI", 1)        # igual que hoy


async def test_mp_carrito_llega_al_erp_con_sus_renglones(erp, db, monkeypatch):
    ent = _Entorno()
    await _carrito(ent.ss)
    link = await _link(monkeypatch, ent)
    # El cliente sigue chateando antes de pagar: la sesión cambia.
    await ent.ss.set_pending(PHONE, sku_id="9999", sku_nombre="Otra cosa", precio=50.0)
    mpw = _montar_mp(monkeypatch, ent, _pago_mp_del_link(link))

    r = await mpw.procesar_pago("mp-carrito-1")
    assert r["status"] == "ok"
    [body] = erp.reqs
    assert _renglones(body) == [("2209004", "ROY1051701", 1, 1000.0, 1000.0),
                                ("10955", "PET1304801", 2, 1000.0, 1000.0)]
    assert body["total"] == 2000.0 and "shipping_total" not in body
    [fila] = await _filas(db)
    assert fila["erp_estado"] == "enviado"
    data = json.loads((await db.fetchrow("SELECT data FROM orders"))["data"])
    assert data["items"] == SNAP_CARRITO["items"] and data["costo_envio"] == 0.0


async def test_mp_envio_llega_con_la_cantidad_y_shipping_total(erp, db, monkeypatch):
    ent = _Entorno(cfg={"envio_costo": "2000"})
    await ent.ss.set_pending(PHONE, sku_id="7508", sku_nombre="Royal 400 g", precio=1000.0,
                             cantidad=3)
    link = await _link(monkeypatch, ent, "envio", "San Martín 123")
    mpw = _montar_mp(monkeypatch, ent, _pago_mp_del_link(link))

    assert (await mpw.procesar_pago("mp-envio-1"))["status"] == "ok"
    [body] = erp.reqs
    assert _renglones(body) == [("2209004", "ROY1051701", 3, 3000.0, 3000.0)]
    assert body["shipping_total"] == 2000.0 and body["total"] == 5000.0
    assert (await _filas(db))[0]["erp_estado"] == "enviado"


async def test_payway_carrito_llega_al_erp_con_sus_renglones(erp, db, monkeypatch):
    import app.services.payway_link as pl
    ent = _Entorno(cfg={"envio_costo": "2000"})
    await _carrito(ent.ss)
    kv: dict = {}
    monkeypatch.setattr(pl, "_redis", lambda: _RedisKV(kv))
    monkeypatch.setattr(get_settings(), "public_base_url", "https://bot.test")
    url = await _link(monkeypatch, ent, "envio", "San Martín 123", pago=pl.PaywayLinkService())
    pid = url.rsplit("/", 1)[-1]
    await ent.ss.set_pending(PHONE, sku_id="9999", sku_nombre="Otra cosa", precio=50.0)
    pw = _montar_payway(monkeypatch, ent, kv, "PW-CARRITO-1")

    r = await pw.payway_charge(pw.ChargeIn(pid=pid, token="tok", bin="450799"))
    assert r == {"status": "approved"}
    [body] = erp.reqs
    assert _renglones(body) == [("2209004", "ROY1051701", 1, 1000.0, 1000.0),
                                ("10955", "PET1304801", 2, 1000.0, 1000.0)]
    assert body["shipping_total"] == 2000.0 and body["total"] == 4000.0
    assert (await _filas(db))[0]["erp_estado"] == "enviado"


async def test_mp_sin_metadata_usa_el_carrito_de_la_sesion(erp, db, monkeypatch):
    """Link creado antes de este cambio (sin metadata): los renglones salen
    del carrito de la sesión, como hoy, si todavía es el del cobro."""
    ent = _Entorno()
    await _carrito(ent.ss)
    link = await _link(monkeypatch, ent)
    mpw = _montar_mp(monkeypatch, ent, _pago_mp_del_link(link, con_metadata=False))

    assert (await mpw.procesar_pago("mp-sin-meta-1"))["status"] == "ok"
    [body] = erp.reqs
    assert _renglones(body) == [("2209004", "ROY1051701", 1, 1000.0, 1000.0),
                                ("10955", "PET1304801", 2, 1000.0, 1000.0)]
    assert (await _filas(db))[0]["erp_estado"] == "enviado"


async def test_mp_sin_metadata_y_otra_sesion_no_inventa_renglones(erp, db, monkeypatch):
    """Sin metadata y con la sesión en otro producto: la orden queda con su
    único SKU cobrado (no se toma el producto nuevo de la sesión)."""
    ent = _Entorno()
    await ent.ss.set_pending(PHONE, sku_id="7508", sku_nombre="Royal 400 g", precio=1000.0,
                             cantidad=2)
    link = await _link(monkeypatch, ent)
    await ent.ss.set_pending(PHONE, sku_id="15181", sku_nombre="Collar rosa", precio=1000.0,
                             cantidad=2)
    mpw = _montar_mp(monkeypatch, ent, _pago_mp_del_link(link, con_metadata=False))

    assert (await mpw.procesar_pago("mp-sin-meta-2"))["status"] == "ok"
    [body] = erp.reqs
    assert _renglones(body) == [("2209004", "ROY1051701", 2, 2000.0, 2000.0)]
    data = json.loads((await db.fetchrow("SELECT data FROM orders"))["data"])
    assert "items" not in data


async def test_mp_carrito_sin_metadata_ni_sesion_queda_rechazado(erp, db, monkeypatch):
    ent = _Entorno()
    await _carrito(ent.ss)
    link = await _link(monkeypatch, ent)
    await ent.ss.clear_pending(PHONE)
    mpw = _montar_mp(monkeypatch, ent, _pago_mp_del_link(link, con_metadata=False))

    assert (await mpw.procesar_pago("mp-sin-meta-3"))["status"] == "ok"
    assert erp.reqs == []
    [fila] = await _filas(db)
    assert fila["erp_estado"] == "rechazado"
    assert "pedido sin renglones" in fila["erp_ultimo_error"]


@pytest.mark.parametrize("body, items, total", [
    ({"detalle": "Bolsa 15 kg", "monto": 1000, "cantidad": 3},
     [("MANUAL", "Bolsa 15 kg", 3, 1000.0, 3000.0)], 3000.0),
    ({"items": [{"detalle": "A", "monto": 1000}, {"detalle": "B", "monto": 500, "cantidad": 3}]},
     [("LIBRE1", "A", 1, 1000.0, 1000.0), ("LIBRE2", "B", 3, 500.0, 1500.0)], 2500.0),
])
def test_link_del_backoffice_guarda_los_renglones(monkeypatch, body, items, total):
    from fastapi.testclient import TestClient

    import app.routers.webhook as wh
    from app.main import app
    pago = _PagoQueCaptura()
    monkeypatch.setattr(wh, "payment_svc_para", lambda *a, **k: pago)
    monkeypatch.setattr(get_settings(), "bo_key", "")
    r = TestClient(app).post("/bo/paylink", json={"phone": "5490000000444", "enviar": False,
                                                  **body})
    assert r.status_code == 200, r.text
    snap = pago.links[-1]["snapshot"]
    assert [(i["sku_id"], i["nombre"], i["cantidad"], i["precio_unitario"], i["total"])
            for i in snap["items"]] == items
    assert (snap["costo_envio"], snap["total"]) == (0.0, total)


# ══════════════════════════════════════════════════════════════════════════════
# Hallazgo 11: un mismo pago crea una sola orden (y un solo pedido en el ERP)
# aunque Redis se pierda o dos cierres corran a la vez
# ══════════════════════════════════════════════════════════════════════════════

def _orden_guardada(order_id, payment_id):
    return {"order_id": order_id, "phone": PHONE, "sku_id": "7508", "cantidad": 1,
            "total": 1000.0, "mp_payment_id": payment_id, "pago": "online",
            "pickup_code": "123456"}


async def test_find_by_payment_cae_a_postgres(db):
    from app.services.order_store import get_order_store
    await get_order_store(db).upsert(_orden_guardada("ORD-PG-1", "mp-pg-1"))
    svc = _order_service(_FakeRedis())                 # Redis reiniciado: vacío
    o = await svc.find_by_payment("mp-pg-1")
    assert o is not None and o["order_id"] == "ORD-PG-1"
    assert o["pickup_code"] == "123456"
    assert await svc.find_by_payment("mp-otro") is None


async def test_find_by_payment_con_redis_caido_cae_a_postgres(db):
    from app.services.order_store import get_order_store

    class _RedisCaido:
        async def zrevrange(self, *a, **k):
            raise ConnectionError("redis caído")

    await get_order_store(db).upsert(_orden_guardada("ORD-PG-2", "mp-pg-2"))
    o = await _order_service(_RedisCaido()).find_by_payment("mp-pg-2")
    assert o is not None and o["order_id"] == "ORD-PG-2"


async def test_mp_redis_perdido_y_renotificacion_no_duplica(erp, db, monkeypatch):
    ent = _Entorno()
    mpw = _montar_mp(monkeypatch, ent, _pago_mp())
    r1 = await mpw.procesar_pago("mp-555")
    assert r1["status"] == "ok"
    ent.perder_redis()               # sin persistencia: se van el pedido, el índice y el candado
    r2 = await mpw.procesar_pago("mp-555")             # MP renotifica días después
    assert r2["status"] == "duplicado" and r2["order_id"] == r1["order_id"]
    assert len(await _filas(db)) == 1
    assert [q["number"] for q in erp.reqs] == [r1["order_id"]]
    assert len(ent.wa.enviados) == 1


def _order_service_ciego(redis):
    """Dos cierres del mismo pago a la vez: ninguno ve todavía la orden del otro."""
    o = _order_service(redis)

    async def _nada(pid):
        return None

    o.find_by_payment = _nada
    return o


def _sin_errores_de_duplicado(caplog, payment_id):
    errores = [r for r in caplog.records if r.levelno >= 40 and (
        r.name in ("app.services.order_service", "app.services.order_store")
        or payment_id in r.getMessage())]
    assert errores == []
    assert any(r.levelname == "WARNING" and payment_id in r.getMessage()
               for r in caplog.records)


async def test_mp_dos_cierres_del_mismo_pago_crean_una_sola_orden(erp, db, monkeypatch, caplog):
    ent = _Entorno()
    mpw = _montar_mp(monkeypatch, ent, _pago_mp())
    monkeypatch.setattr(mpw, "get_order_service", lambda *a: _order_service_ciego(ent.redis))
    r1 = await mpw.procesar_pago("mp-carrera-1")
    ent.perder_redis()                     # tampoco lo frena el candado (otra réplica)
    r2 = await mpw.procesar_pago("mp-carrera-1")
    assert (r1["status"], r2["status"]) == ("ok", "duplicado")
    assert len(await _filas(db)) == 1
    assert len(ent.wa.enviados) == 1 and len(erp.reqs) == 1
    _sin_errores_de_duplicado(caplog, "mp-carrera-1")


async def test_payway_redis_perdido_no_duplica(erp, db, monkeypatch):
    ent = _Entorno()
    pending = _pending_payway("PID-DUP")
    kv = _kv_con(pending)
    pw = _montar_payway(monkeypatch, ent, kv, "PW-DUP-1")
    assert await pw.payway_charge(pw.ChargeIn(pid="PID-DUP", token="t1", bin="450799")) == \
        {"status": "approved"}
    # Redis pierde el pedido y el candado, y el pendiente vuelve a estar sin aprobar
    ent.perder_redis()
    kv.update(_kv_con(pending))
    r2 = await pw.payway_charge(pw.ChargeIn(pid="PID-DUP", token="t2", bin="450799"))
    assert r2["status"] == "approved" and r2.get("duplicado") is True
    assert len(await _filas(db)) == 1
    assert len(erp.reqs) == 1 and len(ent.wa.enviados) == 1


async def test_payway_dos_cobros_del_mismo_pago_crean_una_sola_orden(erp, db, monkeypatch, caplog):
    import app.routers.payway as pwmod
    ent = _Entorno()
    pending = _pending_payway("PID-CARRERA")
    kv = _kv_con(pending)
    pw = _montar_payway(monkeypatch, ent, kv, "PW-CARRERA-1")
    monkeypatch.setattr(pwmod, "get_order_service", lambda *a: _order_service_ciego(ent.redis))
    assert (await pw.payway_charge(pw.ChargeIn(pid="PID-CARRERA", token="t1", bin="450799"))
            )["status"] == "approved"
    ent.perder_redis()
    kv.update(_kv_con(pending))
    r2 = await pw.payway_charge(pw.ChargeIn(pid="PID-CARRERA", token="t2", bin="450799"))
    assert r2["status"] == "approved" and r2.get("duplicado") is True
    assert len(await _filas(db)) == 1
    assert len(erp.reqs) == 1 and len(ent.wa.enviados) == 1
    _sin_errores_de_duplicado(caplog, "PW-CARRERA-1")
