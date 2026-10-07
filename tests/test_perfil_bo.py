"""
GET /bo/perfil: el portal de cada instancia lee el rubro (identidad y
capacidades) para ocultar las secciones que no aplican. Autenticado con la
BO_KEY como el resto de /bo, y sin el prompt ni los textos del perfil.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.routers import backoffice

CLAVE = "clave-de-test"
CAMPOS = {"clave", "comercio", "emoji", "capacidades"}
CAPACIDADES = {"venta", "recetas", "obras_sociales", "socios",
               "cuenta_corriente", "links_como_receta", "sintomas"}


def _get(monkeypatch, headers=None):
    monkeypatch.setattr(get_settings(), "bo_key", CLAVE)
    app = FastAPI()
    app.include_router(backoffice.router)
    return TestClient(app).get("/bo/perfil", headers=headers if headers is not None
                               else {"x-bo-key": CLAVE})


def test_bo_perfil_petshop(usar_perfil, monkeypatch):
    usar_perfil("petshop")
    r = _get(monkeypatch)
    assert r.status_code == 200
    body = r.json()
    assert body["clave"] == "petshop"
    assert body["comercio"] == "Mascotas del Oeste"
    assert body["emoji"] == "🐾"
    assert body["capacidades"] == {
        "venta": True, "recetas": False, "obras_sociales": False, "socios": False,
        "cuenta_corriente": False, "links_como_receta": False, "sintomas": "derivar",
    }


def test_bo_perfil_farmacia(usar_perfil, monkeypatch):
    usar_perfil("farmacia")
    r = _get(monkeypatch)
    assert r.status_code == 200
    body = r.json()
    assert body["clave"] == "farmacia"
    assert body["comercio"] == "Remedia"
    assert body["emoji"] == "💊"
    assert body["capacidades"] == {
        "venta": True, "recetas": True, "obras_sociales": True, "socios": True,
        "cuenta_corriente": True, "links_como_receta": True, "sintomas": "farmaceutico",
    }


def test_bo_perfil_mutual_no_vende(usar_perfil, monkeypatch):
    usar_perfil("mutual")
    r = _get(monkeypatch)
    assert r.status_code == 200
    body = r.json()
    assert body["clave"] == "mutual"
    assert body["capacidades"]["venta"] is False
    assert body["capacidades"]["recetas"] is True        # igual que hoy (spec §3.3)


def test_bo_perfil_con_comercio_nombre(usar_perfil, monkeypatch):
    usar_perfil("petshop", comercio="MO Prueba")
    r = _get(monkeypatch)
    assert r.status_code == 200
    assert r.json()["comercio"] == "MO Prueba"


def test_bo_perfil_no_expone_prompt_ni_textos(usar_perfil, monkeypatch):
    usar_perfil("petshop")
    r = _get(monkeypatch)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == CAMPOS
    assert set(body["capacidades"]) == CAPACIDADES
    assert "Soy el asistente virtual" not in r.text
    assert "consulta_salud_message" not in r.text


def test_bo_perfil_exige_clave(usar_perfil, monkeypatch):
    usar_perfil("petshop")
    assert _get(monkeypatch, headers={}).status_code == 403
    assert _get(monkeypatch, headers={"x-bo-key": "otra"}).status_code == 403
    assert _get(monkeypatch).status_code == 200
