"""
Websocket /v1/agent/ws: handshake, ping/pong de texto, lookup round-trip y
reemplazo de conexión. La auth se fakea (resolver_branch) porque el TestClient
corre la app en otro event loop que el pool de asyncpg del fixture.
"""

import asyncio
import threading

import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.routers.agent_ws as ws_mod
from app.services.agent_registry import AgentRegistry, get_agent_registry
from app.services.branch_auth import Branch

BRANCH = "farmacia-ws"
TOKEN = "token-valido-de-test"


@pytest.fixture
def client(monkeypatch):
    async def _fake_resolver(token: str) -> Branch:
        if token != TOKEN:
            raise HTTPException(status_code=401, detail="token inválido")
        return Branch(branch_id=BRANCH, nombre="WS Test", activa=True)

    monkeypatch.setattr(ws_mod, "resolver_branch", _fake_resolver)
    # Registry limpio por test
    import app.services.agent_registry as reg_mod
    reg_mod._instance = AgentRegistry()
    from app.main import app
    return TestClient(app)


def _hello(ws):
    ws.send_json({"op": "hello", "branch_id": BRANCH, "agent_version": "0.1.0"})


class TestHandshake:
    def test_sin_bearer_rechaza(self, client):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/v1/agent/ws"):
                pass

    def test_token_invalido_rechaza(self, client):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                    "/v1/agent/ws", headers={"Authorization": "Bearer malo"}):
                pass

    def test_hello_con_branch_ajeno_cierra(self, client):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                    "/v1/agent/ws", headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
                ws.send_json({"op": "hello", "branch_id": "otra", "agent_version": "x"})
                ws.receive_json()   # fuerza a leer el close

    def test_hello_y_ping_pong(self, client):
        with client.websocket_connect(
                "/v1/agent/ws", headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
            _hello(ws)
            ws.send_json({"op": "ping"})
            assert ws.receive_json() == {"op": "pong"}
            assert get_agent_registry().connected(BRANCH)


class TestLookup:
    def test_round_trip(self, client):
        with client.websocket_connect(
                "/v1/agent/ws", headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
            _hello(ws)
            ws.send_json({"op": "ping"})
            assert ws.receive_json()["op"] == "pong"   # conexión registrada

            registry = get_agent_registry()
            resultado = {}

            # El lookup corre en el event loop de la app (portal del TestClient);
            # este hilo hace de "agente": lee el pedido y responde.
            def _agente():
                req = ws.receive_json()
                resultado["req"] = req
                ws.send_json({"op": "lookup_result", "req_id": req["req_id"],
                              "items": [{"external_id": "77", "stock": 4,
                                         "price": "900.00"}],
                              "missing": ["999"]})

            hilo = threading.Thread(target=_agente)
            hilo.start()
            res = ws.portal.call(
                lambda: registry.lookup(BRANCH, ids=["77", "999"], timeout=5.0))
            hilo.join(timeout=5)

            assert resultado["req"]["op"] == "lookup"
            assert resultado["req"]["ids"] == ["77", "999"]
            assert res is not None
            assert res.items[0]["external_id"] == "77"
            assert res.missing == ["999"]

    def test_timeout_devuelve_none(self, client):
        with client.websocket_connect(
                "/v1/agent/ws", headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
            _hello(ws)
            ws.send_json({"op": "ping"})
            assert ws.receive_json()["op"] == "pong"
            registry = get_agent_registry()
            res = ws.portal.call(
                lambda: registry.lookup(BRANCH, ids=["1"], timeout=0.2))
            assert res is None   # nadie respondió

    def test_sin_conexion_devuelve_none(self, client):
        registry = get_agent_registry()
        assert asyncio.run(registry.lookup("sucursal-inexistente", ids=["1"])) is None
        assert asyncio.run(registry.sync_now("sucursal-inexistente")) is False


class TestReemplazo:
    def test_segunda_conexion_reemplaza(self, client):
        with client.websocket_connect(
                "/v1/agent/ws", headers={"Authorization": f"Bearer {TOKEN}"}) as ws1:
            _hello(ws1)
            ws1.send_json({"op": "ping"})
            assert ws1.receive_json()["op"] == "pong"
            with client.websocket_connect(
                    "/v1/agent/ws", headers={"Authorization": f"Bearer {TOKEN}"}) as ws2:
                _hello(ws2)
                ws2.send_json({"op": "ping"})
                assert ws2.receive_json()["op"] == "pong"
                assert get_agent_registry().connected(BRANCH)
                # la primera fue cerrada por el server (1000)
                msg = ws1.receive()
                assert msg["type"] == "websocket.close"
