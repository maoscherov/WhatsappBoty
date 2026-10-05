"""
App de Radar (APP_MODE=radar). Misma imagen que el bot, routers distintos,
sin CORS (la cookie de sesión es same-origin) y con una guarda de origen para
todo lo que cambia algo bajo /radar/ (solo_mismo_origen).

A diferencia del bot, una migración fallida o una base inaccesible IMPIDEN el
arranque: RLS depende del esquema y no hay modo degradado que valga.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.middleware import log_errores
from app.radar.bootstrap import asegurar_admins_iniciales, asegurar_roles, parsear_admins_iniciales
from app.radar.contexto import RadarContexto
from app.radar.db import RadarDB
from app.radar.fuente import FuenteStore
from app.radar.mailer import SEGURIDADES_SMTP, construir_mailer
from app.radar.migrate import migrar_fuente, migrar_resultados
from app.radar.routers import (admin, consola, cuenta, health, login, paginas, parametros, soporte, vinculo,
                               webhook_waha, workers_admin)
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
    if rs.mailer == "smtp":
        _validar_smtp(rs)
    # Un email mal escrito frena el arranque antes de migrar. El mensaje dice la posición, nunca el email.
    parsear_admins_iniciales(rs.admins_iniciales)


def _validar_smtp(rs: RadarSettings) -> None:
    """Los mensajes nombran variables, nunca valores: ni el host, ni el usuario, ni la contraseña."""
    if not rs.smtp_host:
        raise RuntimeError("RADAR_SMTP_HOST: obligatoria con RADAR_MAILER=smtp")
    if rs.smtp_seguridad not in SEGURIDADES_SMTP:
        raise RuntimeError("RADAR_SMTP_SEGURIDAD: tiene que ser " + "|".join(SEGURIDADES_SMTP))
    password = rs.smtp_password.get_secret_value()
    if rs.smtp_usuario and not password:
        raise RuntimeError("RADAR_SMTP_PASSWORD: obligatoria si hay RADAR_SMTP_USUARIO")
    # "ninguna" es para un relay sin autenticar de una red privada: con usuario, la contraseña iría en claro.
    if rs.smtp_usuario and rs.smtp_seguridad == "ninguna":
        raise RuntimeError("RADAR_SMTP_SEGURIDAD=ninguna no admite RADAR_SMTP_USUARIO: "
                           "la contraseña viajaría sin cifrar")
    # smtplib codifica las credenciales en ASCII; con otros caracteres lanza un UnicodeEncodeError cuyo
    # texto trae un pedazo de la contraseña. Mejor frenar acá, sin decir cuál.
    if not (rs.smtp_usuario + password).isascii():
        raise RuntimeError("RADAR_SMTP_USUARIO y RADAR_SMTP_PASSWORD: solo caracteres ASCII")


async def construir_contexto(rs: RadarSettings) -> RadarContexto:
    db = RadarDB(rs.database_url)
    await db.connect()
    fuente = FuenteStore(rs.fuente_database_url)
    await fuente.connect()
    return RadarContexto(settings=rs, db=db, fuente=fuente,
                         secretos=FileSecretStore(rs.secrets_dir), mailer=construir_mailer(rs.mailer, rs))


@asynccontextmanager
async def _lifespan_radar(app: FastAPI):
    rs: RadarSettings = app.state.radar_settings
    logging.basicConfig(level="INFO")
    propio = app.state.radar is None
    if propio:
        validar_settings(rs)
        if rs.bootstrap_roles:
            # Antes de migrar: r0001 aborta si faltan radar_app o radar_admin.
            await asyncio.wait_for(asyncio.to_thread(asegurar_roles, rs.migrator_database_url, rs.database_url),
                                   timeout=60)
        await asyncio.wait_for(asyncio.to_thread(migrar_resultados, rs.migrator_database_url), timeout=60)
        await asyncio.wait_for(asyncio.to_thread(migrar_fuente, rs.fuente_database_url), timeout=60)
        app.state.radar = await construir_contexto(rs)
        # Sin mandar nada (Decisión 8): cada admin entra después por /radar/login.
        await asegurar_admins_iniciales(app.state.radar, rs.admins_iniciales)
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


# La API devuelve datos de clientes (el estado del vínculo, el número de la línea, las listas): que ninguna caché,
# la del navegador o la de un intermediario, los guarde. Prefijos enteros, con la barra: /radar/administrador no entra.
PREFIJOS_SIN_CACHE = ("/radar/api/", "/radar/admin/")


async def sin_cache_en_la_api(request, call_next):
    """Cache-Control: no-store en toda respuesta de la API, también en sus errores (401, 403, 404, 409, 422), salvo
    que ya traiga el suyo: el QR y el código del vínculo se lo ponen ellos."""
    response = await call_next(request)
    if request.url.path.startswith(PREFIJOS_SIN_CACHE):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


# CSRF desde el mismo sitio. La cookie de sesión es SameSite=Lax: viaja en todo pedido same-site, y una página de
# cualquier subdominio de keepitsimple.com.ar es del mismo sitio. Con la versión fijada de FastAPI (0.115,
# requirements.txt) un cuerpo sin Content-Type se lee como JSON, así que ese pedido ni necesita preflight (un fetch
# no-cors con un Blob, navigator.sendBeacon): sin esta guarda, esa página podría registrar un WAHA propio con la sesión
# de un admin.
METODOS_QUE_CAMBIAN = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SITIOS_PERMITIDOS = frozenset({"same-origin", "none"})        # none: lo pidió la persona, no una página


def origen_permitido(request) -> bool:
    """Sec-Fetch-Site, si vino (lo pone el navegador y una página no lo puede cambiar): solo same-origin o none. Si no
    vino y vino Origin (un navegador viejo): su host[:puerto] tiene que ser el Host del pedido, sin mirar el esquema
    porque el proxy de Railway termina el TLS ("null" no tiene host: no pasa). Sin ninguno de los dos no es un
    navegador (curl, los scripts, los tests, servidor a servidor): pasa."""
    sitio = request.headers.get("sec-fetch-site")
    if sitio is not None:
        return sitio in SITIOS_PERMITIDOS
    origen = request.headers.get("origin")
    if origen is None:
        return True
    try:
        host = urlsplit(origen).netloc
    except ValueError:
        return False
    return bool(host) and host.lower() == request.headers.get("host", "").lower()


async def solo_mismo_origen(request, call_next):
    """403 sin llegar a la ruta para lo que cambia algo bajo /radar/ desde otro origen. No pasan por acá las lecturas
    (GET, HEAD, OPTIONS) ni /webhook/waha (fuera de /radar/ y firmado con HMAC, sin cookie)."""
    if (request.method in METODOS_QUE_CAMBIAN and request.url.path.startswith("/radar/")
            and not origen_permitido(request)):
        # %r: la ruta llega decodificada, y un separador de línea codificado en ella (%0B, %E2%80%A8) no puede partir
        # el renglón del log.
        logger.warning("pedido de otro origen rechazado: %s %r", request.method, request.url.path)
        return JSONResponse({"detail": {"error": "origen_no_permitido"}}, status_code=403)
    return await call_next(request)


def crear_app_radar(rs: RadarSettings | None = None, contexto: RadarContexto | None = None) -> FastAPI:
    rs = rs or get_radar_settings()
    # Sin /docs, /redoc ni /openapi.json: Radar tiene dominio público y describirían cada ruta de admin.
    app = FastAPI(title="Radar", version="0.1.0", lifespan=_lifespan_radar,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.radar_settings = rs
    app.state.radar = contexto
    app.middleware("http")(log_errores)
    # El que se agrega después envuelve al anterior: sin_cache_en_la_api, por fuera, le pone no-store también al 403
    # de la guarda.
    app.middleware("http")(solo_mismo_origen)
    app.middleware("http")(sin_cache_en_la_api)
    app.include_router(health.router)
    app.include_router(login.router)
    app.include_router(cuenta.router)
    app.include_router(admin.router)
    app.include_router(parametros.router)
    app.include_router(soporte.router)
    app.include_router(webhook_waha.router)
    app.include_router(consola.router)
    app.include_router(workers_admin.router)
    app.include_router(vinculo.router)
    app.include_router(paginas.router)
    return app
