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


def _montar_payway(monkeypatch, ent: _Entorno, pending: dict, payway_id: str):
    import app.routers.payway as pw
    import app.services.config_service as cs
    kv = {f"payway:pending:{pending['id']}": json.dumps(pending)}

    class _R:
        async def get(self, k):
            return kv.get(k)

        async def setex(self, k, ttl, v):
            kv[k] = v

    class _Pw:
        async def crear_pago(self, **k):
            return {"id": payway_id, "status": "approved", "card_brand": "Visa"}, None

    _montar_sesion(monkeypatch, ent)
    monkeypatch.setattr(pw, "_redis", lambda: _R())
    monkeypatch.setattr(pw, "get_payway_service", lambda *a, **k: _Pw())
    monkeypatch.setattr(pw, "get_order_service", lambda *a: _order_service(ent.redis))
    monkeypatch.setattr(pw, "get_whatsapp_service", lambda *a: ent.wa)
    monkeypatch.setattr(cs, "get_config_service", lambda *a, **k: _Cfg(ent.cfg))
    return pw, kv


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
    pw, _kv = _montar_payway(monkeypatch, ent, _pending_payway("PID-CORTE"), "PW-CORTE-1")

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
