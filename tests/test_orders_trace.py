"""
Trazabilidad de operador en los pedidos: takeover (con y sin force), agente en
preparado/retirado, y compatibilidad con el backoffice viejo (PATCH sin body).

Usa un Redis falso en memoria — no hace falta un server.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import orders_api
from app.services.order_service import OrderService


class FakeRedis:
    """Lo mínimo que usa OrderService: strings con setex/get + un sorted set."""

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

    async def zrevrange(self, key, start, stop):
        items = sorted(self.z.get(key, {}).items(), key=lambda kv: kv[1], reverse=True)
        return [m for m, _ in items[start:stop + 1]]

    async def zrem(self, key, member):
        self.z.get(key, {}).pop(member, None)


@pytest.fixture
def svc():
    s = OrderService.__new__(OrderService)   # sin conectar a Redis
    s._redis = FakeRedis()
    return s


async def _nuevo(svc, phone="5491111111111", estado="pendiente"):
    order = await svc.create(
        phone=phone, sku_id="1", sku_nombre="Ibuprofeno", cantidad=1,
        total=1000.0, mp_payment_id="pay-1",
    )
    if estado != "pendiente":
        order["estado"] = estado
        await svc._save(order)
    return order


# ── Campos nuevos ─────────────────────────────────────────────────────────────

async def test_pedido_nuevo_trae_campos_de_traza_en_null(svc):
    order = await _nuevo(svc)
    for campo in ("agente", "tomado_at", "preparado_por", "preparado_at",
                  "retirado_por", "retirado_at"):
        assert campo in order and order[campo] is None


async def test_pedido_viejo_sin_campos_los_recibe_en_null(svc):
    """Un pedido guardado antes de esta versión no debe romper el listado."""
    viejo = {"order_id": "ORD-VIEJO", "phone": "549111", "estado": "pendiente"}
    await svc._redis.setex("order:ORD-VIEJO", 0, json.dumps(viejo))
    await svc._redis.zadd("orders:idx", {"ORD-VIEJO": 1.0})

    order = await svc.get("ORD-VIEJO")
    assert order["agente"] is None and order["preparado_at"] is None
    assert (await svc.list_all())[0]["tomado_at"] is None


# ── Takeover ──────────────────────────────────────────────────────────────────

async def test_takeover_asigna_agente_y_tomado_at(svc):
    order = await _nuevo(svc)
    tomado, ocupado = await svc.takeover(order["order_id"], "Sofía G.")
    assert ocupado is None
    assert tomado["agente"] == "Sofía G."
    assert isinstance(tomado["tomado_at"], int) and tomado["tomado_at"] > 0


async def test_takeover_de_pedido_ajeno_sin_force_reporta_el_dueno(svc):
    order = await _nuevo(svc)
    await svc.takeover(order["order_id"], "Sofía G.")

    tomado, ocupado = await svc.takeover(order["order_id"], "Juan P.")
    assert tomado is None and ocupado == "Sofía G."
    assert (await svc.get(order["order_id"]))["agente"] == "Sofía G."


async def test_takeover_con_force_reasigna(svc):
    order = await _nuevo(svc)
    await svc.takeover(order["order_id"], "Sofía G.")

    tomado, ocupado = await svc.takeover(order["order_id"], "Juan P.", force=True)
    assert ocupado is None and tomado["agente"] == "Juan P."


async def test_takeover_del_mismo_agente_no_pisa_tomado_at(svc):
    order = await _nuevo(svc)
    primero, _ = await svc.takeover(order["order_id"], "Sofía G.")
    otra_vez, ocupado = await svc.takeover(order["order_id"], "Sofía G.")
    assert ocupado is None
    assert otra_vez["tomado_at"] == primero["tomado_at"]


async def test_takeover_de_pedido_inexistente(svc):
    tomado, ocupado = await svc.takeover("ORD-NO-EXISTE", "Sofía G.")
    assert tomado is None and ocupado is None


# ── Agente en las acciones ────────────────────────────────────────────────────

async def test_preparado_guarda_preparado_por_y_toma_el_pedido(svc):
    order = await _nuevo(svc)
    upd = await svc.mark_preparado(order["order_id"], agente="Sofía G.")
    assert upd["estado"] == "preparado"
    assert upd["preparado_por"] == "Sofía G."
    assert isinstance(upd["preparado_at"], int)
    # Estaba sin dueño → el operador que prepara lo toma
    assert upd["agente"] == "Sofía G." and upd["tomado_at"] is not None


async def test_preparado_no_pisa_el_agente_existente(svc):
    order = await _nuevo(svc)
    await svc.takeover(order["order_id"], "Sofía G.")
    upd = await svc.mark_preparado(order["order_id"], agente="Juan P.")
    assert upd["agente"] == "Sofía G." and upd["preparado_por"] == "Juan P."


async def test_retirado_guarda_retirado_por(svc):
    order = await _nuevo(svc)
    upd = await svc.mark_retirado(order["order_id"], agente="Juan P.")
    assert upd["estado"] == "retirado"
    assert upd["retirado_por"] == "Juan P."
    assert isinstance(upd["retirado_at"], int)


async def test_acciones_sin_agente_siguen_funcionando(svc):
    """Compatibilidad: el backoffice actual llama sin agente."""
    order = await _nuevo(svc)
    upd = await svc.mark_retirado(order["order_id"])
    assert upd["estado"] == "retirado"
    assert upd["retirado_por"] is None and upd["agente"] is None
    assert isinstance(upd["retirado_at"], int)


# ── Cruce con conversaciones ──────────────────────────────────────────────────

async def test_pendientes_por_phone_cuenta_solo_pendientes(svc):
    await _nuevo(svc, phone="549111")
    await _nuevo(svc, phone="549111")
    await _nuevo(svc, phone="549222")
    await _nuevo(svc, phone="549222", estado="retirado")

    counts = await svc.pendientes_por_phone()
    assert counts == {"549111": 2, "549222": 1}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@pytest.fixture
def client(svc, monkeypatch):
    monkeypatch.setattr(orders_api, "get_order_service", lambda *_a, **_kw: svc)
    app = FastAPI()
    app.include_router(orders_api.router)
    return TestClient(app)


async def test_endpoint_takeover_ok_y_409(svc, client):
    order = await _nuevo(svc)
    oid = order["order_id"]

    r = client.post(f"/orders/api/{oid}/takeover", json={"agente": "Sofía G.", "force": False})
    assert r.status_code == 200 and r.json()["agente"] == "Sofía G."

    r = client.post(f"/orders/api/{oid}/takeover", json={"agente": "Juan P.", "force": False})
    assert r.status_code == 409
    assert r.json() == {"error": "ya_tomado", "agente": "Sofía G."}

    r = client.post(f"/orders/api/{oid}/takeover", json={"agente": "Juan P.", "force": True})
    assert r.status_code == 200 and r.json()["agente"] == "Juan P."


async def test_endpoint_takeover_404(client):
    r = client.post("/orders/api/ORD-NO-EXISTE/takeover", json={"agente": "Sofía G."})
    assert r.status_code == 404


async def test_endpoint_retirado_con_y_sin_body(svc, client):
    a = await _nuevo(svc)
    r = client.patch(f"/orders/api/{a['order_id']}/retirado", json={"agente": "Sofía G."})
    assert r.status_code == 200 and r.json()["retirado_por"] == "Sofía G."

    # Backoffice viejo: PATCH sin body ni content-type
    b = await _nuevo(svc)
    r = client.patch(f"/orders/api/{b['order_id']}/retirado")
    assert r.status_code == 200 and r.json()["retirado_por"] is None


async def test_endpoint_detalle_incluye_traza(svc, client):
    order = await _nuevo(svc)
    await svc.takeover(order["order_id"], "Sofía G.")
    r = client.get(f"/orders/api/{order['order_id']}")
    assert r.status_code == 200
    assert r.json()["agente"] == "Sofía G."
    assert r.json()["tomado_at"] is not None


async def test_endpoint_list_no_choca_con_el_detalle(svc, client):
    await _nuevo(svc)
    r = client.get("/orders/api/list")
    assert r.status_code == 200 and isinstance(r.json(), list) and len(r.json()) == 1
