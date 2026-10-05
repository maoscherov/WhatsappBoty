"""
Arranque de Radar sin shell en el servidor.

asegurar_roles (RADAR_BOOTSTRAP_ROLES=true, Decisión 7): crea radar_admin y radar_app si faltan, con los
atributos de scripts/radar_bootstrap_roles.sql, le cede radar_admin al rol de migración y le pone a radar_app
la contraseña de RADAR_DATABASE_URL, solo si hace falta. Corre en el lifespan antes de las migraciones (r0001
aborta sin los dos roles). Usa psycopg2 como Alembic: es bloqueante y el lifespan la corre en un hilo.

La contraseña de radar_app es un secreto: no va a ningún log, a ningún mensaje de error ni al servidor. El
ALTER ROLE lleva solo su verificador, calculado en el cliente, y esa sentencia la arma el servidor (format ...
%L), nunca Python. Los nombres de rol son constantes; el del rol de migración lo dice el servidor
(CURRENT_USER), no la URL.

asegurar_admins_iniciales (RADAR_ADMINS_INICIALES, Decisión 8): crea los admins de KIS que faltan. Corre en el
lifespan, después de armar el contexto. No manda nada (ni un mail ni un link): cada admin entra después por
/radar/login. Los emails son dato de personas: ni el log ni un error los repiten, solo cuántos se crearon y la
posición, en la lista, del que no es válido.
"""

import logging
from urllib.parse import unquote, urlsplit

import psycopg2
from psycopg2.errors import InsufficientPrivilege
from psycopg2.extensions import encrypt_password

from app.radar import auditoria
from app.radar.constantes import TENANT_KIS
from app.radar.contexto import RadarContexto
from app.radar.db import _normalizar_dsn
from app.radar.links import EmailInvalido, normalizar_email

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


def _fijar_password(cur, password: str) -> None:
    # Al servidor va solo el verificador: encrypt_password lo calcula en el cliente (libpq), con el
    # password_encryption del servidor y una sal al azar, como \password de psql. Así ni log_statement, ni
    # pg_stat_statements, ni pgaudit, ni el log de una sentencia fallida pueden guardar la contraseña. El
    # servidor arma el ALTER ROLE (%L cita el literal) y se ejecuta lo que devuelve. Los errores no citan nada.
    try:
        verificador = encrypt_password(password, "radar_app", cur)
        cur.execute("SELECT format('ALTER ROLE radar_app PASSWORD %%L', %s)", (verificador,))
        cur.execute(cur.fetchone()[0])
    except InsufficientPrivilege:
        raise RuntimeError("RADAR_BOOTSTRAP_ROLES: el rol de RADAR_MIGRATOR_DATABASE_URL no tiene permiso para "
                           "cambiar la contraseña de radar_app (desde Postgres 16, CREATEROLE necesita ADMIN sobre "
                           "radar_app): fijarla a mano con ALTER ROLE radar_app PASSWORD") from None
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
                _fijar_password(cur, password)
                contrasena = "fijada (no entraba con RADAR_DATABASE_URL)"
    finally:
        con.close()
    logger.info("RADAR_BOOTSTRAP_ROLES: roles creados: %s; contraseña de radar_app: %s",
                ", ".join(creados) or "ninguno", contrasena)


def parsear_admins_iniciales(valor: str) -> list[str]:
    """Los emails de RADAR_ADMINS_INICIALES: separados por coma, normalizados y sin repetidos, en el orden en que
    vienen. Una entrada vacía (una coma de más) se ignora. Lo usan validar_settings y asegurar_admins_iniciales.

    Una entrada inválida sale como RuntimeError que dice su posición en la lista como se escribió (la 1 es la
    primera; las vacías también cuentan) y nunca el email, ni un pedazo."""
    emails: list[str] = []
    for posicion, entrada in enumerate(valor.split(","), start=1):
        if not entrada.strip():
            continue
        try:
            email = normalizar_email(entrada)
        except EmailInvalido:
            raise RuntimeError(f"RADAR_ADMINS_INICIALES: el email en la posición {posicion} de la lista no es "
                               "válido") from None
        if email not in emails:
            emails.append(email)
    return emails


async def asegurar_admins_iniciales(ctx: RadarContexto, valor: str) -> None:
    """Idempotente. Deja como admin del tenant KIS a cada email de `valor`, sin mandar nada: ni un mail ni un
    login_token. Todo va en una transacción, y la lista entera se valida antes de abrirla: no queda ninguno a medias.

    Un usuario de KIS que ya existe no se duplica ni pierde su nombre. Si no tenía membresía, queda admin
    (admin_inicial_creado, como uno nuevo); si tenía otro rol, también, y como es un cambio de privilegios queda
    su rastro (rol_cambiado). Uno que ya es admin no cambia ni deja fila. Quitar un email de la variable no
    borra al admin ni le baja el rol."""
    emails = parsear_admins_iniciales(valor)
    if not emails:
        return
    comun = dict(tenant_id=TENANT_KIS, actor_user_id=None, actor_rol="sistema")
    creados = promovidos = existentes = 0
    async with ctx.db.tenant_tx(TENANT_KIS) as con:
        for email in emails:
            # ON CONFLICT: si dos instancias arrancan a la vez, la segunda espera a la primera y ve lo que creó
            # (un error de clave duplicada de Postgres trae el email en su DETAIL).
            uid = await con.fetchval(
                "INSERT INTO users (email) VALUES ($1) ON CONFLICT (tenant_id, email) DO NOTHING RETURNING id", email)
            if uid is None:
                uid = await con.fetchval("SELECT id FROM users WHERE email = $1", email)
            rol = await con.fetchval("SELECT rol FROM memberships WHERE user_id = $1", uid)
            if rol == "admin":
                existentes += 1
            elif rol is None:
                await con.execute("INSERT INTO memberships (user_id, rol) VALUES ($1, 'admin')", uid)
                await auditoria.registrar(con, accion="admin_inicial_creado", tipo_objeto="user", objeto_id=uid,
                                          **comun)
                creados += 1
            else:
                await con.execute("UPDATE memberships SET rol = 'admin' WHERE user_id = $1", uid)
                await auditoria.registrar(con, accion="rol_cambiado", tipo_objeto="membership", objeto_id=uid,
                                          detalle={"rol_anterior": rol, "rol_nuevo": "admin"}, **comun)
                promovidos += 1
    logger.info("RADAR_ADMINS_INICIALES: creados: %d, promovidos: %d, ya existían: %d",
                creados, promovidos, existentes)
