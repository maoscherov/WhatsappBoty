import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import (webhook, simulate, backoffice, mp_webhook, orders_api,
                         media, payway, sync_api, agent_ws, backoffice_branches)
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

        # Config: Postgres es la fuente de verdad, Redis el cache. Se lee acá
        # para repoblar el cache si Redis se reinició y quedó vacío — si no,
        # los valores editados desde el backoffice vuelven a los defaults.
        try:
            from app.services.config_service import get_config_service
            _cfg_svc = get_config_service(settings.redis_url)
            # Rescate: lo configurado antes de esta versión vive solo en Redis.
            await _cfg_svc.sincronizar_durable()
            cfg_actual = await _cfg_svc.get_all()
            logger.info(f"Config cargada ({len(cfg_actual)} claves, "
                        f"descuento socio: {cfg_actual.get('socio_discount_pct')}%)")
        except Exception as e:
            logger.warning(f"No se pudo hidratar la config: {e}")

        # Padrón de socios y empleados (24/9): Postgres pasa a ser la fuente de
        # verdad. Si la tabla socios tiene filas, el singleton se carga desde
        # ahí (pisa lo leído del archivo arriba); si está vacía y el archivo
        # cargó socios, se siembra la tabla desde el archivo. Best-effort: un
        # padrón mal cargado en Postgres nunca debe tumbar el arranque.
        try:
            from app.services.socio_service import (get_socio_service, guardar_en_db,
                                                     cargar_desde_db as cargar_socios_db)
            _db_socios = get_db(settings.database_url)
            if _db_socios.available():
                _socio_svc = get_socio_service(settings.socios_path)
                _n_db = await cargar_socios_db(_db_socios, _socio_svc)
                if _n_db:
                    logger.info(f"Padrón de socios cargado desde Postgres: {_n_db} socios")
                elif _socio_svc.total:
                    await guardar_en_db(_db_socios, _socio_svc)
                    logger.info(f"Padrón de socios sembrado en Postgres desde el archivo: "
                                f"{_socio_svc.total} socios")
        except Exception as e:
            logger.warning(f"No se pudo hidratar/sembrar el padrón de socios en Postgres: {e}")

        try:
            from app.services.empleado_service import get_empleado_service, cargar_desde_db as cargar_empleados_db
            _db_emp = get_db(settings.database_url)
            if _db_emp.available():
                _n_emp = await cargar_empleados_db(_db_emp, get_empleado_service())
                logger.info(f"Listado de empleados cargado desde Postgres: {_n_emp} empleados")
        except Exception as e:
            logger.warning(f"No se pudo cargar el listado de empleados desde Postgres: {e}")

        # Receta por código de barras (24/9): carga la referencia (la siembra
        # con el catálogo de la farmacia si está vacía) y recalcula el flag de
        # todo el catálogo ERP ANTES de cargarlo en el bot.
        try:
            from app.services.receta_referencia import inicializar as _init_receta
            _r = await asyncio.wait_for(_init_receta(get_db(settings.database_url)), timeout=60.0)
            logger.info(f"Referencia de receta: {_r}")
        except Exception as e:
            logger.error(f"No se pudo inicializar la referencia de receta: {e}")

        # Catálogo ERP: si hay una sucursal sincronizada por el agente (o la
        # que fija DEFAULT_BRANCH_ID), gana Postgres sobre el CSV, que ya
        # quedó cargado arriba como fallback (el blob de Redis no se toca).
        try:
            from app.services.catalog_refresher import get_catalog_refresher
            from app.services.catalog_source import resolver_branch_default
            _branch = await resolver_branch_default(forzar=True)
            if _branch:
                _refresher = get_catalog_refresher()
                _refresher._branch_id = _branch
                await _refresher.recargar()
            else:
                logger.info("Sin sucursal ERP sincronizada (o catalogo_fuente=csv) — "
                            "el bot usa el CSV")
        except Exception as e:
            logger.warning(f"No se pudo cargar el catálogo ERP: {e} — se usa el CSV")

    # Job periódico: cierre por inactividad, aviso de demora y devolución al bot
    # de las derivaciones sin atender. Arranca con cualquier proveedor de
    # WhatsApp configurado (con Kapso, WHATSAPP_TOKEN va vacío).
    cierre_task = None
    if settings.whatsapp_token or settings.kapso_api_key:
        cierre_task = asyncio.create_task(_cerrar_sesiones_inactivas())
        logger.info("Job de sesiones activo (inactividad, avisos y auto-liberación)")
    else:
        logger.warning("Sin credenciales de WhatsApp: el job de sesiones NO arranca "
                       "(no habrá cierre por inactividad ni auto-liberación)")

    # Sync del catálogo de Mercurio (Mascotas del Oeste): API REST en la nube,
    # sin agente. Solo si hay clave y Postgres.
    mercurio_task = None
    if settings.mercurio_api_key and settings.database_url:
        mercurio_task = asyncio.create_task(_sync_mercurio_periodico())
        logger.info(f"Sync de Mercurio activo cada {settings.mercurio_sync_interval_secs}s "
                    f"(sucursal {settings.mercurio_branch_id})")

    yield

    if cierre_task:
        cierre_task.cancel()
    if mercurio_task:
        mercurio_task.cancel()
    try:
        await get_db(settings.database_url).close()
    except Exception:
        pass


async def _sync_mercurio_periodico():
    """Primer sync al arrancar (tras 10 s) y después cada intervalo. Nunca
    tumba el proceso: los errores quedan en el log y se reintenta al próximo."""
    from app.services.mercurio_service import MercurioConvivenciaError, get_mercurio_sync
    logger = logging.getLogger("app.mercurio")
    settings = get_settings()
    await asyncio.sleep(10)
    while True:
        try:
            await get_mercurio_sync().sincronizar()
        except asyncio.CancelledError:
            return
        except MercurioConvivenciaError as e:
            # No reintentar: esta base es de otra sucursal. Queda el error en
            # el log y en /bo/mercurio/estado; el operador saca la clave.
            logger.error(f"Sync de Mercurio DESACTIVADO en este deploy: {e}")
            get_mercurio_sync().ultimo = {"error": str(e)}
            return
        except Exception as e:
            logger.error(f"Sync de Mercurio falló: {e}")
        await asyncio.sleep(max(60, settings.mercurio_sync_interval_secs))


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
            minutos_pago = int(cfg.get("inactivity_minutes_pago") or 1440)
            mensaje = cfg.get("inactivity_close_message") or ""
            # Vacío = cerrar SIN avisar (no cae al mensaje general: el aviso
            # de vencimiento del link se sacó a pedido — 20/8).
            mensaje_pago = cfg.get("inactivity_close_message_pago") or ""
            # Bot apagado desde el backoffice: ningún aviso automático sale
            # (ni cierre, ni reanudación, ni demora). Las sesiones se cierran
            # igual por inactividad, en silencio.
            from app.services.checkout_helper import bot_encendido
            _bot_on = bot_encendido(cfg)
            if not _bot_on:
                mensaje = mensaje_pago = ""
            for phone, session in await session_svc.inactivas(minutos * 60, minutos_pago * 60):
                con_link = session.get("estado") == "esperando_pago"
                texto_cierre = mensaje_pago if con_link else mensaje
                if texto_cierre and session.get("history"):
                    # Una sola vez por cliente: si la sesión reaparece, no se
                    # le repite el mismo aviso de cierre.
                    if await session_svc.cierre_ya_avisado(phone):
                        logger.info(f"Cierre ya avisado a {phone}, no se repite")
                    else:
                        try:
                            await wa.send_text(phone, texto_cierre)
                            from app.services.message_store import guardar_historico
                            await guardar_historico(phone, "assistant", texto_cierre)
                        except Exception as e:
                            logger.warning(f"No se pudo avisar cierre a {phone}: {e}")
                await session_svc.delete(phone)
                logger.info(f"Sesión cerrada por inactividad "
                            f"({minutos_pago if con_link else minutos} min, "
                            f"{'con link de pago' if con_link else 'normal'}): {phone}")

            # Devolver al bot las derivaciones que nadie tomó (0 = desactivado).
            # Sin gente atendiendo, una conversación derivada queda muda: es
            # preferible que el bot siga ayudando.
            libre_min = int(cfg.get("auto_liberar_minutos") or 0) if _bot_on else 0
            if libre_min > 0:
                aviso = cfg.get("auto_liberar_message") or ""
                for phone in await session_svc.derivadas_sin_atender(libre_min * 60):
                    await session_svc.liberar(phone)
                    if aviso:
                        try:
                            await wa.send_text(phone, aviso)
                            await session_svc.add_message(phone, "assistant", aviso)
                            from app.services.message_store import guardar_historico
                            await guardar_historico(phone, "assistant", aviso)
                        except Exception as e:
                            logger.warning(f"No se pudo avisar la reanudación a {phone}: {e}")
                    logger.info(f"Conversación devuelta al bot tras {libre_min} min sin atender: {phone}")

            # Aviso de demora en derivaciones (0 = desactivado)
            hr_min = int(cfg.get("handoff_reminder_minutes") or 0) if _bot_on else 0
            hr_msg = cfg.get("handoff_reminder_message") or ""
            if hr_min > 0 and hr_msg:
                for phone in await session_svc.derivadas_para_aviso(hr_min * 60):
                    try:
                        await wa.send_text(phone, hr_msg)
                        await session_svc.add_message(phone, "assistant", hr_msg)
                        from app.services.message_store import guardar_historico
                        await guardar_historico(phone, "assistant", hr_msg)
                        logger.info(f"Aviso de demora enviado a {phone} ({hr_min} min derivada)")
                    except Exception as e:
                        logger.warning(f"No se pudo avisar demora a {phone}: {e}")
                    await session_svc.marcar_handoff_avisado(phone)
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
app.include_router(sync_api.router)
app.include_router(agent_ws.router)
app.include_router(backoffice_branches.router)

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

@app.get("/dashboard")
async def dashboard_ui():
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/tablero")
async def tablero_ui():
    """Tablero CERCA: indicadores por vertical (diseño 'Tableros CERCA')."""
    return FileResponse(STATIC_DIR / "tablero.html")


@app.get("/health")
async def health():
    import os
    return {
        "status": "ok",
        "bot": "Remedia",
        # Railway inyecta el SHA del commit deployado — permite verificar qué
        # versión está corriendo sin mirar los logs.
        "commit": (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "")[:9] or None,
    }
