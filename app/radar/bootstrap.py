"""
Arranque de Radar sin shell en el servidor.

asegurar_roles (RADAR_BOOTSTRAP_ROLES=true, Decisión 7): crea radar_admin y radar_app si faltan, con los
atributos de scripts/radar_bootstrap_roles.sql, le cede radar_admin al rol de migración y le pone a radar_app
la contraseña de RADAR_DATABASE_URL, solo si hace falta. Corre en el lifespan antes de las migraciones (r0001
aborta sin los dos roles). Usa psycopg2 como Alembic: es bloqueante y el lifespan la corre en un hilo.

La contraseña de radar_app es un secreto: no va a ningún log ni a ningún mensaje de error, y la sentencia que
la lleva la arma el servidor (format ... %L), nunca Python. Los nombres de rol son constantes; el del rol de
migración lo dice el servidor (CURRENT_USER), no la URL.
"""

import logging
from urllib.parse import unquote, urlsplit

import psycopg2

from app.radar.db import _normalizar_dsn

logger = logging.getLogger("app.radar.bootstrap")

MIN_PASSWORD_APP = 16
TIMEOUT_CONEXION_S = 10

# Los atributos de scripts/radar_bootstrap_roles.sql (hay test). Solo se crean si faltan: los atributos de un
# rol que ya existe no se tocan.
CREAR_ROL = {
    "radar_app": "CREATE ROLE radar_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT",
    "radar_admin": "CREATE ROLE radar_admin NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT",
}


def _password_de_app(app_url: str) -> str:
    """La contraseña de radar_app como la mandan asyncpg y libpq al conectar con esa URL: decodificada (%40
    es @). Se valida antes de conectarse a nada; los errores nombran la variable, nunca el valor."""
    try:
        partes = urlsplit(_normalizar_dsn(app_url))
    except ValueError:
        # El mensaje de urlsplit puede citar el netloc entero, contraseña incluida.
        raise RuntimeError("RADAR_DATABASE_URL: no es una URL válida") from None
    # Las migraciones otorgan los permisos a radar_app por nombre: con otro usuario la app no tendría ninguno.
    if unquote(partes.username or "") != "radar_app":
        raise RuntimeError("RADAR_DATABASE_URL: con RADAR_BOOTSTRAP_ROLES el usuario tiene que ser radar_app")
    password = unquote(partes.password or "")
    if len(password) < MIN_PASSWORD_APP:
        raise RuntimeError("RADAR_DATABASE_URL: con RADAR_BOOTSTRAP_ROLES la contraseña de radar_app es "
                           f"obligatoria, de {MIN_PASSWORD_APP} caracteres o más")
    return password


def _conectar(url: str, variable: str):
    try:
        return psycopg2.connect(_normalizar_dsn(url), connect_timeout=TIMEOUT_CONEXION_S)
    except psycopg2.Error as e:
        # Solo el tipo: el texto de libpq puede citar la URL (una contraseña mal codificada, entera).
        raise RuntimeError(f"{variable}: no se pudo conectar ({type(e).__name__})") from None


def puede_entrar(app_url: str) -> bool:
    """¿radar_app entra con RADAR_DATABASE_URL? Función aparte para que un test fije la respuesta: con trust
    (lo que deja pgserver) entraría con cualquier contraseña."""
    try:
        psycopg2.connect(_normalizar_dsn(app_url), connect_timeout=TIMEOUT_CONEXION_S).close()
    except psycopg2.Error:
        return False
    return True


def _exigir_admin_de_radar_app(cur) -> None:
    # Desde Postgres 16, CREATEROLE solo cambia la contraseña de un rol sobre el que tiene ADMIN (quien lo creó
    # lo tiene; el superusuario, siempre). Se pregunta antes: un ALTER ROLE fallido queda en el log del servidor
    # con la contraseña (log_min_error_statement).
    cur.execute("SELECT pg_has_role(current_user, 'radar_app', 'MEMBER WITH ADMIN OPTION')")
    if not cur.fetchone()[0]:
        raise RuntimeError("RADAR_DATABASE_URL: radar_app no entra con esa contraseña y el rol de "
                           "RADAR_MIGRATOR_DATABASE_URL no puede cambiarla (hace falta superusuario o ADMIN sobre "
                           "radar_app): fijarla a mano con ALTER ROLE radar_app PASSWORD")


def _fijar_password(cur, password: str) -> None:
    # El servidor arma la sentencia (%L cita el literal) y se ejecuta lo que devuelve. Si falla, el texto del
    # error no sale: podría citar la sentencia.
    try:
        cur.execute("SELECT format('ALTER ROLE radar_app PASSWORD %%L', %s)", (password,))
        cur.execute(cur.fetchone()[0])
    except psycopg2.Error as e:
        raise RuntimeError("RADAR_BOOTSTRAP_ROLES: no se pudo fijar la contraseña de radar_app "
                           f"({type(e).__name__})") from None


def asegurar_roles(migrator_url: str, app_url: str) -> None:
    """Idempotente. Sin superusuario ni CREATEROLE en el rol de migración no hace nada (solo avisa): las
    migraciones fallan después, con su mensaje, si los roles no se crearon a mano."""
    password = _password_de_app(app_url)
    con = _conectar(migrator_url, "RADAR_MIGRATOR_DATABASE_URL")
    try:
        con.autocommit = True
        with con.cursor() as cur:
            cur.execute("SELECT rolsuper, rolcreaterole FROM pg_roles WHERE rolname = current_user")
            superusuario, createrole = cur.fetchone()
            if not (superusuario or createrole):
                logger.warning("RADAR_BOOTSTRAP_ROLES: el rol de RADAR_MIGRATOR_DATABASE_URL no es superusuario "
                               "ni tiene CREATEROLE; no se crea ningún rol (ver scripts/radar_bootstrap_roles.sql)")
                return
            cur.execute("SELECT rolname FROM pg_roles WHERE rolname IN ('radar_app', 'radar_admin')")
            existentes = {nombre for (nombre,) in cur.fetchall()}
            creados = [nombre for nombre in CREAR_ROL if nombre not in existentes]
            for nombre in creados:
                cur.execute(CREAR_ROL[nombre])
            # Para el ALTER ... OWNER TO radar_admin de las migraciones. Explícito también con CREATEROLE: desde
            # Postgres 16 quien crea un rol recibe ADMIN sobre él, pero sin SET ni INHERIT.
            cur.execute("GRANT radar_admin TO CURRENT_USER")
            if "radar_app" in creados:
                _fijar_password(cur, password)
                contrasena = "fijada"
            elif puede_entrar(app_url):
                contrasena = "sin cambios (ya entra con RADAR_DATABASE_URL)"
            else:
                _exigir_admin_de_radar_app(cur)
                _fijar_password(cur, password)
                contrasena = "fijada (no entraba con RADAR_DATABASE_URL)"
    finally:
        con.close()
    logger.info("RADAR_BOOTSTRAP_ROLES: roles creados: %s; contraseña de radar_app: %s",
                ", ".join(creados) or "ninguno", contrasena)
