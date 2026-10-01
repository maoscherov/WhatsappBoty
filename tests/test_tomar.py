"""
"Tomar" una conversación (28/9): la asigna al operador Y calla al bot. Antes
solo asignaba: la charla seguía en "idle" y el bot contestaba encima.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.services.session_service import SessionService

PHONE = "5493410000009"


@pytest.fixture
def cli(monkeypatch):
    from app.routers import backoffice as bo
    ss = SessionService("redis://127.0.0.1:1")
    eventos = []

    class _Metrics:
        async def evento(self, tipo, **k):
            eventos.append((tipo, k))
    monkeypatch.setattr(bo, "get_session_service", lambda *a, **k: ss)
    monkeypatch.setattr(bo, "get_metrics_store", lambda *a, **k: _Metrics())
    monkeypatch.setattr(get_settings(), "bo_key", "CLAVE")
    app = FastAPI()
    app.include_router(bo.router)
    return TestClient(app), ss, eventos


def _tomar(c, agente="Belén"):
    return c.post(f"/bo/session/{PHONE}/take", params={"agente": agente},
                  headers={"x-bo-key": "CLAVE"})


async def test_tomar_una_charla_idle_calla_al_bot(cli):
    c, ss, eventos = cli
    await ss.add_message(PHONE, "user", "hola")
    r = _tomar(c)
    assert r.status_code == 200 and r.json()["estado"] == "operador"
    s = await ss.get(PHONE)
    assert s["estado"] == "operador" and s["agente"] == "Belén"
    assert s["derivada_motivo"] == "tomada_por_operador"
    # No cuenta para el SLA de derivaciones (no estaba derivada)
    assert not [e for e in eventos if e[0] == "derivacion_atendida"]
    # Ya tiene agente: no vuelve sola al bot por "derivada sin atender"
    assert PHONE not in await ss.derivadas_sin_atender(0)


async def test_tomar_una_derivada_mide_el_sla_y_conserva_el_motivo(cli):
    c, ss, eventos = cli
    await ss.set_estado(PHONE, "operador", motivo="receta")
    _tomar(c)
    s = await ss.get(PHONE)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "receta"
    assert [e for e in eventos if e[0] == "derivacion_atendida"]


async def test_tomar_registra_quien_la_tomo(cli):
    c, ss, eventos = cli
    await ss.add_message(PHONE, "user", "hola")
    _tomar(c, "Lore")
    tomadas = [k for t, k in eventos if t == "conversacion_tomada"]
    assert tomadas and tomadas[-1]["ref"] == "Lore"


async def test_devolver_al_bot_limpia_operador_y_derivacion(cli):
    """Caso 1/10: tras "Devolver al bot" quedaba Idle con "Atiende: Lore" y
    "No entendido"; ahora queda limpia y se registra quién la devolvió."""
    c, ss, eventos = cli
    await ss.set_estado(PHONE, "operador", motivo="no_entendido")
    _tomar(c, "Lore")
    r = c.post(f"/bo/session/{PHONE}/release", params={"agente": "Lore"},
               headers={"x-bo-key": "CLAVE"})
    assert r.status_code == 200 and r.json()["estado"] == "idle"
    s = await ss.get(PHONE)
    assert s["estado"] == "idle"
    assert not s.get("agente") and not s.get("derivada_motivo") and not s.get("atendida_at")
    dev = [k for t, k in eventos if t == "conversacion_devuelta"]
    assert dev and dev[-1]["ref"] == "Lore" and dev[-1]["dato"] == "no_entendido"
