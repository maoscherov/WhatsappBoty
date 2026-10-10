"""
Una imagen, dos modos. En modo bot no se monta nada de Radar; en modo radar
no se monta nada del bot (§6.2: "En ese despliegue el router del bot no se monta").
"""
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import crear_app


def _rutas(app) -> set[str]:
    return {r.path for r in app.routes if hasattr(r, "path")}


def test_modo_por_defecto_es_bot():
    assert Settings(_env_file=None).app_mode == "bot"


def test_modo_bot_no_monta_radar():
    rutas = _rutas(crear_app(Settings(_env_file=None, app_mode="bot")))
    assert {"/webhook", "/bo", "/health"} <= rutas
    assert not any(p.startswith("/radar") for p in rutas)


def test_modo_radar_no_monta_bot():
    rutas = _rutas(crear_app(Settings(_env_file=None, app_mode="radar")))
    assert "/health" in rutas
    assert "/webhook" not in rutas
    assert "/bo" not in rutas
    assert "/simulate" not in rutas


def test_modo_invalido_no_arranca():
    with pytest.raises(RuntimeError):
        crear_app(Settings(_env_file=None, app_mode="otro"))


async def test_health_radar_sin_contexto_dice_arrancando():
    app = crear_app(Settings(_env_file=None, app_mode="radar"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        r = await c.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "arrancando"
    assert r.json()["modo"] == "radar"
