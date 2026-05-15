import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import webhook, simulate, backoffice
from app.services.sku_service import get_sku_service
from app.services.session_service import get_session_service

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    logger = logging.getLogger(__name__)

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

    yield


app = FastAPI(
    title="Farma Bot — Farmacia Mutual Independencia",
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

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")

@app.get("/bo")
async def backoffice_ui():
    return FileResponse(STATIC_DIR / "backoffice.html")


@app.get("/health")
async def health():
    return {"status": "ok", "bot": "Farma"}
