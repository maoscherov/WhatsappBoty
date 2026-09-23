"""
App de Radar (APP_MODE=radar). Misma imagen que el bot, routers distintos,
sin CORS (la cookie de sesión es same-origin).
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.middleware import log_errores
from app.radar.routers import health
from app.radar.settings import RadarSettings, get_radar_settings


@asynccontextmanager
async def _lifespan_radar(app: FastAPI):
    yield


def crear_app_radar(rs: RadarSettings | None = None, contexto=None) -> FastAPI:
    rs = rs or get_radar_settings()
    app = FastAPI(title="Radar", version="0.1.0", lifespan=_lifespan_radar)
    app.state.radar_settings = rs
    app.state.radar = contexto
    app.middleware("http")(log_errores)
    app.include_router(health.router)
    return app
