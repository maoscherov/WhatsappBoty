"""
Tipo de cliente en el backoffice (29/9): el bot atendía a un empleado con su
20% pero el backoffice lo mostraba sin nombre y como "No socio", porque solo
miraba el padrón de socios.
"""
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.routers import backoffice as bo
from app.services.session_service import SessionService

EMPLEADA = "5493415969693"
SOCIA = "5493415807742"
OTRO = "5493471603090"


@pytest.fixture
def padrones(monkeypatch):
    mod = types.ModuleType("app.services.empleado_service")

    class _Emp:
        def find_by_phone(self, phone):
            if phone == EMPLEADA:
                return {"nombre": "María", "apellido": "Gómez", "nombre_pila": "María",
                        "celular": "3415969693"}
            return None

        def listar(self, q="", page=1, page_size=50):
            return ([{"celular": "3415969693"}], 1) if "gomez" in q.lower() else ([], 0)
    emp = _Emp()
    mod.get_empleado_service = lambda: emp
    monkeypatch.setitem(sys.modules, "app.services.empleado_service", mod)

    class _Socios:
        def find_by_phone(self, phone):
            return {"nombre": "Muff Claudia Beatriz"} if phone in (SOCIA, EMPLEADA) else None
    monkeypatch.setattr(bo, "get_socio_service", lambda *a, **k: _Socios())


def test_datos_cliente(padrones):
    # Empleada que además es socia: gana empleado (como el descuento).
    assert bo._datos_cliente(EMPLEADA) == {"nombre": "Gómez María", "tipo_cliente": "empleado"}
    assert bo._datos_cliente(SOCIA) == {"nombre": "Muff Claudia Beatriz", "tipo_cliente": "socio"}
    assert bo._datos_cliente(OTRO) == {"nombre": None, "tipo_cliente": "no_socio"}


def test_listados_en_vivo_traen_tipo_cliente(padrones, monkeypatch):
    ss = SessionService("redis://127.0.0.1:1")
    monkeypatch.setattr(bo, "get_session_service", lambda *a, **k: ss)

    async def _sin_pedidos():
        return {}
    monkeypatch.setattr(bo, "_pedidos_pendientes", _sin_pedidos)
    monkeypatch.setattr(get_settings(), "bo_key", "CLAVE")
    app = FastAPI()
    app.include_router(bo.router)
    c = TestClient(app, headers={"x-bo-key": "CLAVE"})

    import asyncio
    asyncio.run(ss.add_message(EMPLEADA, "user", "hola"))
    asyncio.run(ss.add_message(OTRO, "user", "hola"))
    lista = {x["phone"]: x for x in c.get("/bo/sessions").json()}
    assert lista[EMPLEADA]["tipo_cliente"] == "empleado" and lista[EMPLEADA]["nombre"] == "Gómez María"
    assert lista[OTRO]["tipo_cliente"] == "no_socio"
    det = c.get(f"/bo/session/{EMPLEADA}").json()
    assert det["tipo_cliente"] == "empleado"
