import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import webhook, simulate, backoffice, mp_webhook, orders_api, media, payway
from app.services.sku_service import get_sku_service
from app.services.session_service import get_session_service
from app.services.blob_store import get_blob_store
from app.services.db import get_db

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    logger = logging.getLogger(__name__)

    # Restaurar archivos subidos (catálogo/padrón) desde Redis — el filesystem
    # de Railway es efímero y se borra en cada deploy.
    try:
        blob = get_blob_store(settings.redis_url)
        cat = await blob.load("catalogo")
        if cat:
            p = Path(settings.sku_csv_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(cat[0])
            logger.info(f"Catálogo restaurado desde Redis ({len(cat[0])} bytes)")
        soc = await blob.load("socios")
        if soc:
            data, ext = soc
            dest = Path(settings.socios_path).with_suffix(ext or ".csv")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            settings.socios_path = str(dest)
            logger.info(f"Padrón restaurado desde Redis ({len(data)} bytes → {dest.name})")
    except Exception as e:
        logger.warning(f"No se pudieron restaurar archivos desde Redis: {e}")

    # Carga del catálogo (síncrona, pero rápida desde disco)
    try:
        sku_svc = get_sku_service(settings.sku_csv_path)
        logger.info(f"Catálogo cargado: {sku_svc.total} SKUs")
    except FileNotFoundError as e:
        logger.warning(f"Catálogo SKU no encontrado: {e}")

    # Verificación Redis con timeout para no bloquear el arranque
    try:
        session_svc = get_session_service(settings.redis_url)
        ok = await asyncio.wait_for(session_svc.ping(), timeout=3.0)
        logger.info("Redis conectado" if ok else "Redis no disponible — sesiones en memoria")
    except Exception:
        logger.warning("Redis no disponible — sesiones no persistirán")

    # PostgreSQL (opcional): historial permanente + RAG con pgvector
    if settings.database_url:
        # Migraciones Alembic — aplican el esquema al arrancar
        try:
            from alembic.config import Config
            from alembic import command
            root = Path(__file__).resolve().parent.parent
            cfg = Config(str(root / "alembic.ini"))
            cfg.set_main_option("script_location", str(root / "migrations"))
            # Timeout: nunca colgar el arranque por una DB lenta/inaccesible.
            await asyncio.wait_for(asyncio.to_thread(command.upgrade, cfg, "head"), timeout=20.0)
            logger.info("Migraciones Alembic aplicadas (head)")
        except Exception as e:
            logger.warning(f"No se pudieron aplicar migraciones Alembic: {e}")
        try:
            db = get_db(settings.database_url)
            await asyncio.wait_for(db.connect(), timeout=10.0)
        except Exception as e:
            logger.warning(f"Postgres init falló: {e} — se usa solo Redis")

    # Job de cierre por inactividad: avisa y cierra sesiones sin actividad
    # (minuta 2026-07-31: 15 min). Solo con WhatsApp configurado (no en tests).
    cierre_task = None
    if settings.whatsapp_token:
        cierre_task = asyncio.create_task(_cerrar_sesiones_inactivas())

    yield

    if cierre_task:
        cierre_task.cancel()
    try:
        await get_db(settings.database_url).close()
    except Exception:
        pass


async def _cerrar_sesiones_inactivas():
    """Cada 60s cierra (con mensaje de despedida) las sesiones inactivas."""
    from app.services.config_service import get_config_service
    from app.services.whatsapp_service import get_whatsapp_service

    logger = logging.getLogger("app.inactividad")
    settings = get_settings()
    session_svc = get_session_service(settings.redis_url)
    cfg_svc = get_config_service(settings.redis_url)
    wa = get_whatsapp_service(settings.whatsapp_token, settings.whatsapp_phone_number_id)

    while True:
        try:
            await asyncio.sleep(60)
            cfg = await cfg_svc.get_all()
            minutos = int(cfg.get("inactivity_minutes") or 15)
            mensaje = cfg.get("inactivity_close_message") or ""
            for phone, session in await session_svc.inactivas(minutos * 60):
                if mensaje and session.get("history"):
                    try:
                        await wa.send_text(phone, mensaje)
                    except Exception as e:
                        logger.warning(f"No se pudo avisar cierre a {phone}: {e}")
                await session_svc.delete(phone)
                logger.info(f"Sesión cerrada por inactividad ({minutos} min): {phone}")
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"Job de inactividad: {e}")


app = FastAPI(
    title="Remedia Bot",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _log_errores(request, call_next):
    """Loguea cualquier 5xx con el método + endpoint para poder rastrearlo en Railway."""
    import logging as _logging
    _log = _logging.getLogger("app.errors")
    try:
        response = await call_next(request)
    except Exception as e:
        _log.exception(f"💥 500 en {request.method} {request.url.path} — {type(e).__name__}: {e}")
        raise
    if response.status_code >= 500:
        _log.error(f"💥 {response.status_code} en {request.method} {request.url.path}")
    return response


app.include_router(webhook.router)
app.include_router(simulate.router)
app.include_router(backoffice.router)
app.include_router(mp_webhook.router)
app.include_router(orders_api.router)
app.include_router(media.router)
app.include_router(payway.router)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")

@app.get("/bo")
async def backoffice_ui():
    return FileResponse(STATIC_DIR / "backoffice.html")

@app.get("/backoffice")
async def orders_ui():
    return FileResponse(STATIC_DIR / "orders.html")


@app.get("/health")
async def health():
    return {"status": "ok", "bot": "Remedia"}
