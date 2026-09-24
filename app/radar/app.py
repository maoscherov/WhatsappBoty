"""
App de Radar (APP_MODE=radar). Misma imagen que el bot, routers distintos,
sin CORS (la cookie de sesión es same-origin).

A diferencia del bot, una migración fallida o una base inaccesible IMPIDEN el
arranque: RLS depende del esquema y no hay modo degradado que valga.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.middleware import log_errores
from app.radar.contexto import RadarContexto
from app.radar.db import RadarDB
from app.radar.fuente import FuenteStore
from app.radar.mailer import construir_mailer
from app.radar.migrate import migrar_fuente, migrar_resultados
from app.radar.routers import admin, consola, cuenta, health, login, parametros, soporte, webhook_waha
from app.radar.secrets import FileSecretStore
from app.radar.settings import RadarSettings, get_radar_settings

logger = logging.getLogger("app.radar")

OBLIGATORIAS = ("database_url", "migrator_database_url", "fuente_database_url", "cookie_secret")
MIN_COOKIE_SECRET = 32
MIN_WEBHOOK_HMAC = 32


def validar_settings(rs: RadarSettings) -> None:
    faltantes = [n for n in OBLIGATORIAS if not getattr(rs, n)]
    if faltantes:
        raise RuntimeError("Faltan variables RADAR_: " + ", ".join(faltantes))
    if len(rs.cookie_secret) < MIN_COOKIE_SECRET:
        raise RuntimeError(f"RADAR_COOKIE_SECRET: mínimo {MIN_COOKIE_SECRET} caracteres aleatorios")
    # Vacía se admite (el receptor responde 401 a todo); corta no: sería una firma débil.
    if rs.waha_webhook_hmac_key and len(rs.waha_webhook_hmac_key) < MIN_WEBHOOK_HMAC:
        raise RuntimeError(f"RADAR_WAHA_WEBHOOK_HMAC_KEY: mínimo {MIN_WEBHOOK_HMAC} caracteres aleatorios")


async def construir_contexto(rs: RadarSettings) -> RadarContexto:
    db = RadarDB(rs.database_url)
    await db.connect()
    fuente = FuenteStore(rs.fuente_database_url)
    await fuente.connect()
    return RadarContexto(settings=rs, db=db, fuente=fuente,
                         secretos=FileSecretStore(rs.secrets_dir), mailer=construir_mailer(rs.mailer))


@asynccontextmanager
async def _lifespan_radar(app: FastAPI):
    rs: RadarSettings = app.state.radar_settings
    logging.basicConfig(level="INFO")
    propio = app.state.radar is None
    if propio:
        validar_settings(rs)
        await asyncio.wait_for(asyncio.to_thread(migrar_resultados, rs.migrator_database_url), timeout=60)
        await asyncio.wait_for(asyncio.to_thread(migrar_fuente, rs.fuente_database_url), timeout=60)
        app.state.radar = await construir_contexto(rs)
    logger.info("Radar arrancó: almacén de fuente %s", app.state.radar.fuente.almacen)
    parar = asyncio.Event()
    tarea = None
    if propio and rs.worker_embebido:
        from app.radar.worker import bucle   # import local: app.radar.worker importa este módulo en _main
        tarea = asyncio.create_task(bucle(app.state.radar, parar=parar))
    app.state.radar_worker = tarea
    yield
    if tarea is not None:
        parar.set()
        try:
            await asyncio.wait_for(tarea, timeout=15)
        except asyncio.TimeoutError:
            tarea.cancel()
    if propio:
        await app.state.radar.cerrar()


def crear_app_radar(rs: RadarSettings | None = None, contexto: RadarContexto | None = None) -> FastAPI:
    rs = rs or get_radar_settings()
    app = FastAPI(title="Radar", version="0.1.0", lifespan=_lifespan_radar)
    app.state.radar_settings = rs
    app.state.radar = contexto
    app.middleware("http")(log_errores)
    app.include_router(health.router)
    app.include_router(login.router)
    app.include_router(cuenta.router)
    app.include_router(admin.router)
    app.include_router(parametros.router)
    app.include_router(soporte.router)
    app.include_router(webhook_waha.router)
    app.include_router(consola.router)
    return app
