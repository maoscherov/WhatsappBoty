import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import webhook, simulate, backoffice, mp_webhook, orders_api, media
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
    try:
        db = get_db(settings.database_url)
        await asyncio.wait_for(db.connect(), timeout=10.0)
    except Exception as e:
        logger.warning(f"Postgres init falló: {e} — se usa solo Redis")

    yield

    try:
        await get_db(settings.database_url).close()
    except Exception:
        pass


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

app.include_router(webhook.router)
app.include_router(simulate.router)
app.include_router(backoffice.router)
app.include_router(mp_webhook.router)
app.include_router(orders_api.router)
app.include_router(media.router)

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
