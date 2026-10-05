"""
Arranque de Radar sin shell en el servidor: los roles de Postgres (Decisión 7, RADAR_BOOTSTRAP_ROLES=true) y los
admins iniciales de KIS (Decisión 8, RADAR_ADMINS_INICIALES).

Los roles son de todo el cluster y conftest.py ya los crea en el pgserver de la sesión: estos tests levantan su
propio pgserver (fixture de módulo) y cada uno parte sin radar_app ni radar_admin (`cluster_vacio`). En ese
cluster radar_app entra solo con contraseña (scram-sha-256 en pg_hba.conf; pgserver deja todo en trust): así
"radar_app entra con RADAR_DATABASE_URL" se prueba de verdad, con asyncpg como la app. Y el servidor escribe en
su log toda sentencia que recibe (log_statement = 'all'): así se prueba que la contraseña no llega al servidor.
El rol de migración es el superusuario del cluster, como `postgres` en Railway, salvo donde el test dice otra cosa.

La contraseña de radar_app es un secreto: ni en un log, ni en un mensaje de error, ni en un repr, ni en una
sentencia que llegue al servidor (va solo su verificador).

Los admins iniciales no necesitan ese cluster: usan la base de la sesión (`radar_ctx`, con los roles de conftest.py).
Sus emails son dato de personas: ni un log ni un error los repiten.
"""
import asyncio
import json
import logging
import pathlib
import re
import time
import traceback
from dataclasses import dataclass
from urllib.parse import quote

import asyncpg
import psycopg2
import pytest
from starlette.testclient import TestClient

import app.radar.app as app_radar
import app.radar.worker as worker_radar
from app.radar import auditoria, bootstrap
from app.radar.admin_kis import crear_admin_kis
from app.radar.app import crear_app_radar, validar_settings
from app.radar.bootstrap import asegurar_admins_iniciales, asegurar_roles, parsear_admins_iniciales
from app.radar.constantes import TENANT_KIS
from app.radar.contexto import RadarContexto
from app.radar.migrate import migrar_resultados
from app.radar.settings import RadarSettings

from .helpers import crear_tenant_directo, crear_usuario

RAIZ = pathlib.Path(__file__).resolve().parents[2]

# En la URL va codificada (%40); el rol tiene que quedar con la decodificada, la que mandan asyncpg y libpq.
CLAVE = "clave@de-radar-app-que-no-sale"
CLAVE_EN_URL = "clave%40de-radar-app-que-no-sale"
OTRA_CLAVE = "otra-clave-de-radar-app-que-no-sale"

MIGRADOR = "postgres"                          # superusuario de pgserver
MIGRADOR_CREATEROLE = "migrador_createrole"
MIGRADOR_SIN_PERMISOS = "migrador_sin_permisos"

# Primera regla que coincide gana: radar_app con contraseña, el resto como lo deja pgserver (trust).
HBA_RADAR_APP = "host all radar_app all scram-sha-256\nlocal all radar_app scram-sha-256\n"

ATRIBUTOS = ("rolsuper", "rolinherit", "rolcreaterole", "rolcreatedb", "rolcanlogin", "rolreplication",
             "rolbypassrls", "rolconnlimit")

# URLs que nunca se conectan: la validación de RADAR_DATABASE_URL va antes que cualquier conexión.
MIGRADOR_FALSO = "postgresql://postgres:clave-del-migrador@127.0.0.1:1/radar"
APP_FALSA = f"postgresql://radar_app:{CLAVE_EN_URL}@127.0.0.1:1/radar"
FUENTE_FALSA = "postgresql://postgres:clave-de-la-fuente@127.0.0.1:1/fuente"


# ── cluster propio ─────────────────────────────────────────────────────────────

@dataclass
class Cluster:
    info: object             # PostmasterInfo de pgserver
    log: pathlib.Path        # log del servidor (pg_ctl -l)

    def url(self, usuario: str = MIGRADOR, base: str = "postgres", clave_en_url: str | None = None) -> str:
        url = self.info.get_uri(user=usuario, database=base)
        if clave_en_url is not None:
            url = url.replace(f"{usuario}:@", f"{usuario}:{clave_en_url}@", 1)
        return url

    def url_app(self, clave_en_url: str = CLAVE_EN_URL, base: str = "postgres") -> str:
        return self.url("radar_app", base, clave_en_url)

    def sql(self, sentencia: str, *args, base: str = "postgres"):
        """Como superusuario y en autocommit; devuelve las filas si la sentencia devuelve alguna."""
        con = psycopg2.connect(self.url(base=base))
        try:
            con.autocommit = True
            with con.cursor() as cur:
                cur.execute(sentencia, args or None)
                return cur.fetchall() if cur.description else None
        finally:
            con.close()

    def fin_del_log(self) -> int:
        return len(self.log.read_bytes())

    def log_desde(self, inicio: int) -> str:
        """Lo que escribió el servidor desde `inicio` (con log_statement = 'all', toda sentencia recibida)."""
        return self.log.read_bytes()[inicio:].decode("utf-8", errors="replace")


def _configurar(cluster: Cluster, pgdata: pathlib.Path) -> None:
    hba = pgdata / "pg_hba.conf"
    hba.write_text(HBA_RADAR_APP + hba.read_text())
    cluster.sql("ALTER SYSTEM SET log_statement = 'all'")
    antes = cluster.sql("SELECT pg_conf_load_time()")[0][0]
    cluster.sql("SELECT pg_reload_conf()")
    # La recarga es asíncrona: una sesión nueva ve la hora de carga del postmaster.
    for _ in range(100):
        if cluster.sql("SELECT pg_conf_load_time()")[0][0] > antes:
            assert cluster.sql("SHOW log_statement") == [("all",)]
            return
        time.sleep(0.05)
    raise RuntimeError("el cluster de test no recargó su configuración")


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    try:
        import pgserver
    except ImportError:
        pytest.skip("pgserver no instalado")
    pgdata = tmp_path_factory.mktemp("pg_bootstrap")
    srv = pgserver.get_server(pgdata)
    try:
        c = Cluster(info=srv.get_postmaster_info(), log=srv.log)
        _configurar(c, pgdata)
        c.sql(f"CREATE ROLE {MIGRADOR_CREATEROLE} LOGIN NOSUPERUSER CREATEROLE;"
              f"CREATE ROLE {MIGRADOR_SIN_PERMISOS} LOGIN NOSUPERUSER NOCREATEROLE;")
        yield c
    finally:
        srv.cleanup()            # para el postgres de este módulo: no queda ningún proceso


@pytest.fixture
def cluster_vacio(cluster):
    """Sin radar_app ni radar_admin y sin las bases que crearon otros tests (los roles son del cluster)."""
    for (base,) in cluster.sql("SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres'"):
        cluster.sql(f'DROP DATABASE "{base}" WITH (FORCE)')
    cluster.sql("DROP ROLE IF EXISTS radar_app, radar_admin")
    return cluster


def _roles(cluster: Cluster) -> dict:
    filas = cluster.sql("SELECT rolname, " + ", ".join(ATRIBUTOS) + ", rolpassword FROM pg_authid "
                        "WHERE rolname IN ('radar_app', 'radar_admin')")
    return {f[0]: dict(zip(ATRIBUTOS + ("rolpassword",), f[1:])) for f in filas}


def _membresias(cluster: Cluster) -> set:
    return set(cluster.sql(
        "SELECT r.rolname, m.rolname, g.rolname, am.admin_option FROM pg_auth_members am "
        "JOIN pg_roles r ON r.oid = am.roleid JOIN pg_roles m ON m.oid = am.member "
        "JOIN pg_roles g ON g.oid = am.grantor WHERE r.rolname IN ('radar_app', 'radar_admin')"))


def _miembros_de_radar_admin(cluster: Cluster) -> set:
    return {miembro for rol, miembro, _, _ in _membresias(cluster) if rol == "radar_admin"}


def _fijar_contrasena(cluster: Cluster, clave: str) -> None:
    """Como lo haría un admin a mano (la contraseña sin decodificar nada: la que se pasa)."""
    cluster.sql(cluster.sql("SELECT format('ALTER ROLE radar_app PASSWORD %%L', %s)", clave)[0][0])


def _entra_con(url: str) -> bool:
    """¿Entra radar_app con esa URL, conectando con asyncpg como la app?"""
    async def probar():
        con = await asyncpg.connect(url, timeout=10)
        try:
            return await con.fetchval("SELECT current_user")
        finally:
            await con.close()
    try:
        return asyncio.run(probar()) == "radar_app"
    except asyncpg.InvalidAuthorizationSpecificationError:      # incluye la contraseña incorrecta
        return False


def _sin_secretos(error: BaseException, *secretos: str) -> None:
    """Ni el mensaje ni el traceback completo, que es lo que escribiría un log, traen los secretos."""
    texto = "".join(traceback.format_exception(error))
    for secreto in secretos:
        assert secreto not in texto


# ── crear, idempotencia y atributos ────────────────────────────────────────────

def test_en_un_cluster_vacio_crea_los_dos_roles_y_radar_app_entra_con_la_url(cluster_vacio):
    asegurar_roles(cluster_vacio.url(), cluster_vacio.url_app())
    roles = _roles(cluster_vacio)
    apagados = dict.fromkeys(ATRIBUTOS[:-1], False)
    assert {a: roles["radar_admin"][a] for a in ATRIBUTOS[:-1]} == apagados                  # NOLOGIN
    assert {a: roles["radar_app"][a] for a in ATRIBUTOS[:-1]} == {**apagados, "rolcanlogin": True}
    assert roles["radar_app"]["rolpassword"] is not None
    assert roles["radar_admin"]["rolpassword"] is None
    assert _miembros_de_radar_admin(cluster_vacio) == {MIGRADOR}     # para el ALTER ... OWNER TO de las migraciones
    assert _entra_con(cluster_vacio.url_app())


def test_correrlo_dos_veces_no_falla_ni_cambia_nada(cluster_vacio):
    asegurar_roles(cluster_vacio.url(), cluster_vacio.url_app())
    antes = (_roles(cluster_vacio), _membresias(cluster_vacio))
    asegurar_roles(cluster_vacio.url(), cluster_vacio.url_app())
    # rolpassword igual: un ALTER ROLE ... PASSWORD, aun con la misma contraseña, cambia la sal.
    assert (_roles(cluster_vacio), _membresias(cluster_vacio)) == antes


def test_crea_los_roles_con_los_atributos_del_script_manual():
    """Mismos roles por los dos caminos: scripts/radar_bootstrap_roles.sql (a mano) y el arranque."""
    script = (RAIZ / "scripts" / "radar_bootstrap_roles.sql").read_text(encoding="utf-8")
    assert set(bootstrap.CREAR_ROL) == {"radar_app", "radar_admin"}
    for sentencia in bootstrap.CREAR_ROL.values():
        assert sentencia + ";" in script


@pytest.mark.parametrize("esquema", ["postgres://", "postgresql+asyncpg://"])
def test_acepta_los_esquemas_que_aceptan_las_otras_urls(cluster_vacio, esquema):
    asegurar_roles(cluster_vacio.url().replace("postgresql://", esquema, 1),
                   cluster_vacio.url_app().replace("postgresql://", esquema, 1))
    assert _entra_con(cluster_vacio.url_app())


# ── la contraseña ──────────────────────────────────────────────────────────────

def test_la_contrasena_es_la_de_la_url_decodificada(cluster_vacio):
    """%40 en la URL es @ en el rol: lo que mandan asyncpg (la app) y libpq (Alembic y la sonda)."""
    asegurar_roles(cluster_vacio.url(), cluster_vacio.url_app())
    assert _entra_con(cluster_vacio.url_app())
    psycopg2.connect(cluster_vacio.url_app()).close()
    # Con la contraseña literal de la URL (%40 sin decodificar) no entra: no es la que quedó en el rol.
    assert not _entra_con(cluster_vacio.url_app(quote(CLAVE_EN_URL, safe="")))


@pytest.mark.parametrize("entra", [True, False])
def test_la_contrasena_se_fija_solo_si_radar_app_no_entra_con_la_url(cluster_vacio, monkeypatch, entra):
    """radar_app creado a mano, con otros atributos y otra contraseña. La sonda decide; los atributos de un
    rol que ya existe no se tocan nunca."""
    cluster_vacio.sql("CREATE ROLE radar_app LOGIN INHERIT CONNECTION LIMIT 7")
    _fijar_contrasena(cluster_vacio, OTRA_CLAVE)
    antes = _roles(cluster_vacio)["radar_app"]
    sondeadas = []
    monkeypatch.setattr(bootstrap, "puede_entrar", lambda url: sondeadas.append(url) or entra)
    asegurar_roles(cluster_vacio.url(), cluster_vacio.url_app())
    despues = _roles(cluster_vacio)["radar_app"]
    assert sondeadas == [cluster_vacio.url_app()]
    assert {a: despues[a] for a in ATRIBUTOS} == {a: antes[a] for a in ATRIBUTOS}
    if entra:
        assert despues["rolpassword"] == antes["rolpassword"]           # ningún ALTER ROLE: la sal cambiaría
    else:
        assert despues["rolpassword"] != antes["rolpassword"]
        assert _entra_con(cluster_vacio.url_app())


def test_radar_app_creado_a_mano_con_otra_contrasena_pasa_a_entrar_con_la_url(cluster_vacio):
    """El camino real, sin reemplazar la sonda: la URL no entra, se fija su contraseña y la vieja deja de andar."""
    cluster_vacio.sql("CREATE ROLE radar_app LOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT")
    _fijar_contrasena(cluster_vacio, OTRA_CLAVE)
    asegurar_roles(cluster_vacio.url(), cluster_vacio.url_app())
    assert _entra_con(cluster_vacio.url_app())
    assert not _entra_con(cluster_vacio.url_app(OTRA_CLAVE))


# ── RADAR_DATABASE_URL inválida ────────────────────────────────────────────────

@pytest.mark.parametrize("usuario", ["postgres", "radar_migrator", "radar_app2", "RADAR_APP"])
def test_un_usuario_que_no_es_radar_app_no_arranca(usuario):
    """Las migraciones otorgan los permisos a radar_app por nombre. Falla antes de conectarse a nada."""
    url = APP_FALSA.replace("//radar_app:", f"//{usuario}:", 1)
    with pytest.raises(RuntimeError, match="RADAR_DATABASE_URL.*radar_app") as exc:
        asegurar_roles(MIGRADOR_FALSO, url)
    _sin_secretos(exc.value, url, CLAVE, CLAVE_EN_URL, MIGRADOR_FALSO)
    assert usuario not in str(exc.value)                               # nombra la variable, no el valor


@pytest.mark.parametrize("clave_en_url", [
    pytest.param(None, id="sin-contrasena"),
    pytest.param("", id="vacia"),
    pytest.param("c" * 15, id="15-caracteres"),
    pytest.param("c" * 13 + "%40", id="16-en-la-url-14-decodificados"),
])
def test_sin_contrasena_o_con_menos_de_16_caracteres_no_arranca(clave_en_url):
    userinfo = "radar_app" if clave_en_url is None else f"radar_app:{clave_en_url}"
    url = f"postgresql://{userinfo}@127.0.0.1:1/radar"
    with pytest.raises(RuntimeError, match="RADAR_DATABASE_URL.*16") as exc:
        asegurar_roles(MIGRADOR_FALSO, url)
    _sin_secretos(exc.value, url, MIGRADOR_FALSO, *([clave_en_url] if clave_en_url else []))


def test_16_caracteres_decodificados_alcanzan():
    """Pasa la validación y recién ahí intenta conectarse al rol de migración (que acá no existe)."""
    with pytest.raises(RuntimeError, match="RADAR_MIGRATOR_DATABASE_URL"):
        asegurar_roles(MIGRADOR_FALSO, "postgresql://radar_app:" + "c" * 15 + "%40@127.0.0.1:1/radar")


@pytest.mark.parametrize("migrador", [
    pytest.param(MIGRADOR_FALSO, id="no-responde"),
    # libpq cita el pedazo mal codificado (la contraseña entera) en su error: "invalid percent-encoded token".
    pytest.param("postgresql://postgres:clave%zz-del-migrador@127.0.0.1:1/radar", id="codificacion-invalida"),
])
def test_si_no_conecta_con_el_rol_de_migracion_el_error_no_trae_la_url(migrador):
    with pytest.raises(RuntimeError, match="RADAR_MIGRATOR_DATABASE_URL") as exc:
        asegurar_roles(migrador, APP_FALSA)
    _sin_secretos(exc.value, migrador, "clave-del-migrador", "zz-del-migrador", APP_FALSA, CLAVE)


# ── qué puede el rol de migración ──────────────────────────────────────────────

def test_un_rol_de_migracion_sin_createrole_ni_superusuario_no_hace_nada_y_avisa(cluster_vacio, caplog):
    caplog.set_level(logging.DEBUG)
    asegurar_roles(cluster_vacio.url(MIGRADOR_SIN_PERMISOS), cluster_vacio.url_app())
    assert _roles(cluster_vacio) == {}
    avisos = [r for r in caplog.records if r.name == "app.radar.bootstrap" and r.levelno == logging.WARNING]
    assert len(avisos) == 1 and "CREATEROLE" in avisos[0].getMessage()


def test_un_rol_de_migracion_con_createrole_queda_con_una_membresia_que_sirve_para_migrar(cluster_vacio):
    """Postgres 16+: quien crea un rol con CREATEROLE recibe ADMIN sobre él, pero sin SET ni INHERIT. El GRANT
    explícito es lo que deja migrar: ALTER FUNCTION ... OWNER TO radar_admin y los REVOKE sobre sus funciones."""
    cluster_vacio.sql(f"CREATE DATABASE radar_createrole OWNER {MIGRADOR_CREATEROLE}")
    migrador = cluster_vacio.url(MIGRADOR_CREATEROLE, "radar_createrole")
    asegurar_roles(migrador, cluster_vacio.url_app(base="radar_createrole"))
    assert MIGRADOR_CREATEROLE in _miembros_de_radar_admin(cluster_vacio)
    migrar_resultados(migrador)
    dueno = cluster_vacio.sql("SELECT pg_get_userbyid(proowner) FROM pg_proc "
                              "WHERE proname = 'radar_admin_crear_tenant'", base="radar_createrole")
    assert dueno == [("radar_admin",)]
    assert _entra_con(cluster_vacio.url_app(base="radar_createrole"))


def test_con_createrole_sin_admin_sobre_radar_app_no_arranca_con_un_error_que_lo_dice(cluster_vacio):
    """Postgres 16+: con CREATEROLE solo se cambia la contraseña de un rol sobre el que se tiene ADMIN. Si
    radar_app lo creó otro y no entra con la URL, el ALTER ROLE falla: el error nombra el problema sin secretos, y
    la sentencia fallida que Postgres guarda en su log lleva el verificador, no la contraseña."""
    cluster_vacio.sql("CREATE ROLE radar_app LOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT")
    _fijar_contrasena(cluster_vacio, OTRA_CLAVE)
    antes = _roles(cluster_vacio)["radar_app"]
    inicio = cluster_vacio.fin_del_log()
    with pytest.raises(RuntimeError, match="no tiene permiso.*ADMIN") as exc:
        asegurar_roles(cluster_vacio.url(MIGRADOR_CREATEROLE), cluster_vacio.url_app())
    _sin_secretos(exc.value, CLAVE, CLAVE_EN_URL, cluster_vacio.url_app(), "SCRAM-SHA-256$")
    assert _roles(cluster_vacio)["radar_app"] == antes
    log = cluster_vacio.log_desde(inicio)
    assert "ALTER ROLE radar_app PASSWORD 'SCRAM-SHA-256$" in log and CLAVE not in log


# ── secretos ───────────────────────────────────────────────────────────────────

def test_al_servidor_llega_el_verificador_nunca_la_contrasena(cluster_vacio):
    """Al crear radar_app y al rotar su contraseña, la sentencia que recibe el servidor lleva el verificador SCRAM
    calculado en el cliente: ni log_statement, ni pg_stat_statements, ni pgaudit pueden guardar la contraseña. Que
    después entre con la URL prueba que el verificador es el de esa contraseña."""
    inicio = cluster_vacio.fin_del_log()
    asegurar_roles(cluster_vacio.url(), cluster_vacio.url_app())               # crea radar_app y fija
    _fijar_contrasena(cluster_vacio, OTRA_CLAVE)                               # a mano, en claro: deja de entrar
    asegurar_roles(cluster_vacio.url(), cluster_vacio.url_app())               # no entra: la rota
    log = cluster_vacio.log_desde(inicio)
    assert log.count("ALTER ROLE radar_app PASSWORD 'SCRAM-SHA-256$") == 2
    assert CLAVE not in log and CLAVE_EN_URL not in log
    assert _entra_con(cluster_vacio.url_app())


def test_ni_la_contrasena_ni_las_urls_van_al_log(cluster_vacio, caplog):
    caplog.set_level(logging.DEBUG)
    asegurar_roles(cluster_vacio.url(), cluster_vacio.url_app())              # crea los roles y fija
    _fijar_contrasena(cluster_vacio, OTRA_CLAVE)
    asegurar_roles(cluster_vacio.url(), cluster_vacio.url_app())              # la sonda falla: vuelve a fijar
    asegurar_roles(cluster_vacio.url(), cluster_vacio.url_app())              # ya entra: sin cambios
    mensajes = [r.getMessage() for r in caplog.records if r.name == "app.radar.bootstrap"]
    assert len(mensajes) == 3 and all("radar_app" in m for m in mensajes)    # dice qué hizo cada vez
    for secreto in (CLAVE, CLAVE_EN_URL, OTRA_CLAVE, cluster_vacio.url_app(), cluster_vacio.url()):
        assert secreto not in caplog.text


def test_las_urls_no_salen_en_el_repr_de_los_settings_ni_del_contexto():
    rs = RadarSettings(_env_file=None, database_url=APP_FALSA, migrator_database_url=MIGRADOR_FALSO,
                       fuente_database_url=FUENTE_FALSA)
    ctx = RadarContexto(settings=rs, db=None, fuente=None, secretos=None, mailer=None)
    for texto in (repr(rs), str(rs), repr(ctx)):
        for secreto in (APP_FALSA, MIGRADOR_FALSO, FUENTE_FALSA, CLAVE_EN_URL, "clave-del-migrador",
                        "clave-de-la-fuente"):
            assert secreto not in texto
    assert (rs.database_url, rs.migrator_database_url, rs.fuente_database_url) == \
           (APP_FALSA, MIGRADOR_FALSO, FUENTE_FALSA)          # los valores siguen ahí para quien los usa


# ── settings, arranque y docs ──────────────────────────────────────────────────

def test_bootstrap_roles_viene_apagado_y_lo_prende_la_variable(monkeypatch):
    monkeypatch.delenv("RADAR_BOOTSTRAP_ROLES", raising=False)
    assert RadarSettings(_env_file=None).bootstrap_roles is False
    monkeypatch.setenv("RADAR_BOOTSTRAP_ROLES", "true")
    assert RadarSettings(_env_file=None).bootstrap_roles is True


def test_el_arranque_con_bootstrap_roles_en_un_cluster_vacio_crea_los_roles_migra_y_responde(
        cluster_vacio, tmp_path, caplog):
    """Railway sin psql: el rol de migración es el superusuario, radar_app no existe y la app entra con la
    contraseña de su URL (scram de verdad en este cluster). Sin los roles, la migración r0001 aborta: que
    arranque prueba que asegurar_roles corrió antes de migrar."""
    caplog.set_level(logging.DEBUG)
    cluster_vacio.sql("CREATE DATABASE radar")
    cluster_vacio.sql("CREATE DATABASE radar_fuente")
    rs = RadarSettings(_env_file=None, bootstrap_roles=True, database_url=cluster_vacio.url_app(base="radar"),
                       migrator_database_url=cluster_vacio.url(base="radar"),
                       fuente_database_url=cluster_vacio.url(base="radar_fuente"),
                       cookie_secret="secreto-de-test-de-32-caracteres!", secrets_dir=str(tmp_path / "secretos"),
                       mailer="memoria", worker_embebido=False)
    with TestClient(crear_app_radar(rs)) as cliente:
        r = cliente.get("/health")
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["resultados"]) == ("ok", {"ok": True})
    assert set(_roles(cluster_vacio)) == {"radar_app", "radar_admin"}
    for secreto in (CLAVE, CLAVE_EN_URL):
        assert secreto not in caplog.text


@pytest.mark.parametrize("bootstrap_roles, inyectado", [(False, False), (True, True)],
                         ids=["sin-la-variable", "con-contexto-inyectado"])
async def test_el_arranque_no_toca_los_roles_sin_la_variable_ni_con_un_contexto_inyectado(
        radar_ctx, monkeypatch, bootstrap_roles, inyectado):
    llamadas = []
    monkeypatch.setattr(app_radar, "asegurar_roles", lambda *args: llamadas.append(args))
    rs = radar_ctx.settings.model_copy(update={"bootstrap_roles": bootstrap_roles, "worker_embebido": False})
    app = crear_app_radar(rs, contexto=radar_ctx if inyectado else None)
    async with app.router.lifespan_context(app):
        pass
    assert llamadas == []


def test_la_tabla_de_despliegue_documenta_radar_bootstrap_roles():
    doc = (RAIZ / "docs" / "radar-despliegue.md").read_text(encoding="utf-8")
    assert "| `RADAR_BOOTSTRAP_ROLES` |" in doc
    assert "postgresql://radar_app:<contraseña>@${{Postgres.PGHOST}}:${{Postgres.PGPORT}}/${{Postgres.PGDATABASE}}" \
        in doc


# ── admins iniciales (Decisión 8) ──────────────────────────────────────────────

LINK = re.compile(r"/radar/login/canjear\?t=([0-9a-f-]+)#k=([A-Za-z0-9_-]+)")

# Con un fragmento reconocible ("zorro", "plateado") se prueba que ni el email ni un pedazo salen en un error.
ENTRADAS_INVALIDAS = [
    pytest.param("zorro-plateado", id="sin-arroba"),
    pytest.param("zorro-plateado@sinpunto", id="dominio-sin-punto"),
    pytest.param("zorro plateado@kis.com", id="con-espacio"),
    pytest.param("zorro@@plateado.com", id="dos-arrobas"),
    pytest.param("zorro-" + "p" * 250 + "@kis.com", id="mas-de-254-caracteres"),
]


def _settings(**campos) -> RadarSettings:
    """Settings que pasan validar_settings (las URLs no se conectan nunca)."""
    return RadarSettings(_env_file=None, database_url=APP_FALSA, migrator_database_url=MIGRADOR_FALSO,
                         fuente_database_url=FUENTE_FALSA, cookie_secret="secreto-de-test-de-32-caracteres!",
                         mailer="memoria", **campos)


async def _usuarios_de_kis(ctx) -> dict:
    """{email: (rol, nombre)} del tenant KIS; rol None si el usuario no tiene membresía."""
    async with ctx.db.tenant_tx(TENANT_KIS) as con:
        filas = await con.fetch("SELECT u.email, u.nombre, m.rol FROM users u "
                                "LEFT JOIN memberships m ON m.user_id = u.id")
    return {f["email"]: (f["rol"], f["nombre"]) for f in filas}


async def _ids_de_kis(ctx) -> dict:
    async with ctx.db.tenant_tx(TENANT_KIS) as con:
        return {f["email"]: f["id"] for f in await con.fetch("SELECT id, email FROM users")}


async def _auditoria_de_kis(ctx) -> list:
    async with ctx.db.tenant_tx(TENANT_KIS) as con:
        filas = await con.fetch("SELECT accion, actor_user_id, actor_rol, tipo_objeto, objeto_id, detalle "
                                "FROM access_audit_log ORDER BY id")
    return [(f["accion"], f["actor_user_id"], f["actor_rol"], f["tipo_objeto"], f["objeto_id"],
             json.loads(f["detalle"])) for f in filas]


async def _foto_de_kis(ctx) -> tuple:
    """Todo lo que el arranque puede tocar en el tenant KIS, con sus ids: dos fotos iguales, nada cambió."""
    consultas = ("SELECT id, email, nombre FROM users ORDER BY email",
                 "SELECT id, user_id, rol, lineas_permitidas FROM memberships ORDER BY user_id",
                 "SELECT id, accion, actor_user_id, actor_rol, tipo_objeto, objeto_id, detalle "
                 "FROM access_audit_log ORDER BY id",
                 "SELECT id FROM login_tokens ORDER BY id")
    foto = []
    async with ctx.db.tenant_tx(TENANT_KIS) as con:
        for consulta in consultas:
            foto.append([tuple(f) for f in await con.fetch(consulta)])
    return tuple(foto)


async def _hasta(parar: asyncio.Event) -> None:
    await parar.wait()


async def test_crea_los_admins_de_kis_con_el_email_normalizado_sin_mandar_nada(radar_ctx):
    await asegurar_admins_iniciales(radar_ctx, "a@kis.com, B@KIS.com")
    assert await _usuarios_de_kis(radar_ctx) == {"a@kis.com": ("admin", ""), "b@kis.com": ("admin", "")}
    assert radar_ctx.mailer.enviados == []
    async with radar_ctx.db.tenant_tx(TENANT_KIS) as con:
        assert await con.fetchval("SELECT count(*) FROM login_tokens") == 0      # ni un link: entran por /radar/login
    ids = await _ids_de_kis(radar_ctx)
    assert await _auditoria_de_kis(radar_ctx) == [
        ("admin_inicial_creado", None, "sistema", "user", ids["a@kis.com"], {}),
        ("admin_inicial_creado", None, "sistema", "user", ids["b@kis.com"], {})]


async def test_el_admin_creado_entra_por_login_como_admin_de_kis(cliente, radar_ctx):
    """Para eso se crea: nadie le manda nada, él pide su link y entra a Radar con rol admin en el tenant KIS."""
    await asegurar_admins_iniciales(radar_ctx, "Mariano@KIS.com.ar")
    assert radar_ctx.mailer.enviados == []
    r = await cliente.post("/radar/login", json={"email": "mariano@kis.com.ar"})
    assert r.status_code == 202
    assert [m.para for m in radar_ctx.mailer.enviados] == ["mariano@kis.com.ar"]
    t, k = LINK.search(radar_ctx.mailer.enviados[0].texto).groups()
    r = await cliente.post("/radar/login/canjear", data={"t": t, "k": k})
    assert r.status_code == 303 and r.headers["location"] == "/radar/inicio"
    r = await cliente.get("/radar/api/yo")
    assert r.status_code == 200
    assert (r.json()["rol"], r.json()["es_kis"], r.json()["tenant_id"], r.json()["email"]) == \
           ("admin", True, str(TENANT_KIS), "mariano@kis.com.ar")


async def test_sin_smtp_crear_admin_le_da_su_link_a_un_admin_que_creo_el_arranque(radar_ctx):
    """El camino sin SMTP de la documentación: el mismo usuario, no uno nuevo, y el link sale del script."""
    await asegurar_admins_iniciales(radar_ctx, "a@kis.com")
    ids = await _ids_de_kis(radar_ctx)
    assert await crear_admin_kis(radar_ctx, email="A@kis.com", nombre="Ana") == ids["a@kis.com"]
    assert await _ids_de_kis(radar_ctx) == ids
    assert [m.para for m in radar_ctx.mailer.enviados] == ["a@kis.com"]


async def test_la_segunda_corrida_no_crea_ni_audita_ni_cambia_el_nombre(radar_ctx):
    await asegurar_admins_iniciales(radar_ctx, "a@kis.com, b@kis.com")
    async with radar_ctx.db.tenant_tx(TENANT_KIS) as con:
        await con.execute("UPDATE users SET nombre = 'Mariano' WHERE email = 'a@kis.com'")
    antes = await _foto_de_kis(radar_ctx)
    await asegurar_admins_iniciales(radar_ctx, "A@kis.com,b@kis.com, ")       # el mismo conjunto, escrito distinto
    assert await _foto_de_kis(radar_ctx) == antes
    assert (await _usuarios_de_kis(radar_ctx))["a@kis.com"] == ("admin", "Mariano")
    assert radar_ctx.mailer.enviados == []


@pytest.mark.parametrize("rol", ["dueno", "gestor", "lector"])
async def test_un_usuario_de_kis_con_otro_rol_queda_admin_y_se_audita_el_cambio(radar_ctx, rol):
    """Subir un rol es un cambio de privilegios: deja su fila (rol_cambiado, como PUT /radar/api/usuarios/.../rol).
    Es la misma persona, con su nombre."""
    uid = await crear_usuario(radar_ctx.db, TENANT_KIS, "otro@kis.com", rol)
    await asegurar_admins_iniciales(radar_ctx, "otro@kis.com")
    assert await _usuarios_de_kis(radar_ctx) == {"otro@kis.com": ("admin", "otro")}
    assert await _ids_de_kis(radar_ctx) == {"otro@kis.com": uid}
    assert await _auditoria_de_kis(radar_ctx) == [
        ("rol_cambiado", None, "sistema", "membership", uid, {"rol_anterior": rol, "rol_nuevo": "admin"})]
    assert radar_ctx.mailer.enviados == []


async def test_un_usuario_de_kis_sin_membresia_queda_admin_sin_duplicarse(radar_ctx):
    async with radar_ctx.db.tenant_tx(TENANT_KIS) as con:
        uid = await con.fetchval(
            "INSERT INTO users (email, nombre) VALUES ('sin-membresia@kis.com', 'Ana') RETURNING id")
    await asegurar_admins_iniciales(radar_ctx, "sin-membresia@kis.com")
    assert await _usuarios_de_kis(radar_ctx) == {"sin-membresia@kis.com": ("admin", "Ana")}
    assert await _auditoria_de_kis(radar_ctx) == [("admin_inicial_creado", None, "sistema", "user", uid, {})]


async def test_el_mismo_email_en_una_cuenta_de_cliente_no_se_toca(radar_ctx):
    """Los usuarios son por tenant: el admin de KIS es otro usuario que el dueño de la farmacia con ese email."""
    farmacia = await crear_tenant_directo(radar_ctx.db, "Farmacia A")
    dueno = await crear_usuario(radar_ctx.db, farmacia, "mariano@kis.com", "dueno")
    await asegurar_admins_iniciales(radar_ctx, "mariano@kis.com")
    assert await _usuarios_de_kis(radar_ctx) == {"mariano@kis.com": ("admin", "")}
    assert await _ids_de_kis(radar_ctx) != {"mariano@kis.com": dueno}
    async with radar_ctx.db.tenant_tx(farmacia) as con:
        filas = await con.fetch("SELECT u.id, m.rol FROM users u JOIN memberships m ON m.user_id = u.id")
        assert [(f["id"], f["rol"]) for f in filas] == [(dueno, "dueno")]
        assert await con.fetchval("SELECT count(*) FROM access_audit_log") == 0


async def test_dos_arranques_a_la_vez_no_se_pisan(radar_ctx):
    """Dos instancias que arrancan juntas (un redeploy que se solapa): ninguna falla (una clave duplicada de Postgres
    trae el email en su DETAIL) y no se duplica ni se audita dos veces."""
    assert radar_ctx.db.pool.get_max_size() >= 2    # conftest lo arma con 2; con 1 la espera de abajo no terminaría
    async with radar_ctx.db.pool.acquire(), radar_ctx.db.pool.acquire():
        pass                                        # el pool ya tiene sus dos conexiones: las transacciones se cruzan
    lista = "a@kis.com, b@kis.com"
    await asyncio.gather(asegurar_admins_iniciales(radar_ctx, lista), asegurar_admins_iniciales(radar_ctx, lista))
    assert await _usuarios_de_kis(radar_ctx) == {"a@kis.com": ("admin", ""), "b@kis.com": ("admin", "")}
    assert [f[0] for f in await _auditoria_de_kis(radar_ctx)] == ["admin_inicial_creado"] * 2


@pytest.mark.parametrize("valor", ["", "   ", ",", " , ,"])
async def test_con_la_variable_vacia_no_hace_nada(radar_ctx, caplog, valor):
    caplog.set_level(logging.DEBUG)
    await asegurar_admins_iniciales(radar_ctx, valor)
    assert await _foto_de_kis(radar_ctx) == ([], [], [], [])
    assert [r for r in caplog.records if r.name == "app.radar.bootstrap"] == []
    assert radar_ctx.mailer.enviados == []


def test_la_lista_se_normaliza_sin_entradas_vacias_ni_repetidas():
    assert parsear_admins_iniciales(" A@kis.com ,, b@KIS.com,a@kis.com, ") == ["a@kis.com", "b@kis.com"]
    assert parsear_admins_iniciales("") == []


def test_una_lista_valida_pasa_validar_settings():
    validar_settings(_settings(admins_iniciales="a@kis.com, B@kis.com,"))


@pytest.mark.parametrize("entrada", ENTRADAS_INVALIDAS)
def test_un_email_invalido_frena_el_arranque_con_su_posicion_y_sin_repetirlo(entrada):
    with pytest.raises(RuntimeError, match=r"RADAR_ADMINS_INICIALES.*posición 2\b") as exc:
        validar_settings(_settings(admins_iniciales=f"a@kis.com, {entrada}"))
    _sin_secretos(exc.value, "zorro", "plateado")           # un email no es un secreto, pero tampoco va a un log


@pytest.mark.parametrize("valor, posicion", [
    ("zorro-plateado", 1),
    ("a@kis.com,b@kis.com,c@kis.com,zorro-plateado", 4),
    # La posición es la de la entrada tal como se escribió: las vacías también ocupan un lugar.
    ("a@kis.com,, zorro-plateado , b@kis.com", 3),
])
def test_la_posicion_es_la_de_la_entrada_dentro_de_la_lista_escrita(valor, posicion):
    with pytest.raises(RuntimeError, match=rf"RADAR_ADMINS_INICIALES.*posición {posicion}\b") as exc:
        parsear_admins_iniciales(valor)
    _sin_secretos(exc.value, "zorro", "plateado")


async def test_con_un_email_invalido_no_crea_ninguno_y_no_lo_repite(radar_ctx):
    """La lista entera se valida antes de tocar la base: no queda la mitad hecha."""
    lista = "valido@kis.com, zorro-plateado"      # en una variable: el traceback cita la línea de la llamada
    with pytest.raises(RuntimeError, match=r"RADAR_ADMINS_INICIALES.*posición 2\b") as exc:
        await asegurar_admins_iniciales(radar_ctx, lista)
    _sin_secretos(exc.value, "zorro", "plateado", "valido@kis.com")
    assert await _foto_de_kis(radar_ctx) == ([], [], [], [])


async def test_el_log_dice_cuantos_se_crearon_y_cuantos_ya_existian_sin_emails(radar_ctx, caplog):
    caplog.set_level(logging.DEBUG)
    await crear_usuario(radar_ctx.db, TENANT_KIS, "tero@kis.com", "admin")
    await crear_usuario(radar_ctx.db, TENANT_KIS, "puma@kis.com", "lector")
    lista = "tero@kis.com, puma@kis.com, zorro@kis.com"
    await asegurar_admins_iniciales(radar_ctx, lista)
    await asegurar_admins_iniciales(radar_ctx, lista)
    mensajes = [r.getMessage() for r in caplog.records if r.name == "app.radar.bootstrap"]
    assert mensajes == ["RADAR_ADMINS_INICIALES: creados: 1, promovidos: 1, ya existían: 1",
                        "RADAR_ADMINS_INICIALES: creados: 0, promovidos: 0, ya existían: 3"]
    for fragmento in ("tero", "puma", "zorro", "kis.com", "@"):
        assert fragmento not in caplog.text


async def test_si_algo_falla_a_mitad_no_queda_ningun_admin_a_medias(radar_ctx, monkeypatch):
    """Todo en una transacción del tenant KIS: el segundo falla y el primero tampoco queda."""
    original = auditoria.registrar
    llamadas = []

    async def falla_la_segunda(con, **campos):
        llamadas.append(campos["accion"])
        if len(llamadas) == 2:
            raise RuntimeError("falla simulada")
        return await original(con, **campos)

    monkeypatch.setattr(auditoria, "registrar", falla_la_segunda)
    with pytest.raises(RuntimeError, match="falla simulada"):
        await asegurar_admins_iniciales(radar_ctx, "a@kis.com, b@kis.com")
    assert llamadas == ["admin_inicial_creado", "admin_inicial_creado"]
    assert await _foto_de_kis(radar_ctx) == ([], [], [], [])


# ── lifespan, settings y docs ──────────────────────────────────────────────────

async def test_el_arranque_crea_los_admins_antes_de_levantar_el_worker(radar_ctx, monkeypatch):
    """Con el contexto que arma el propio lifespan y el valor de la variable tal cual."""
    eventos = []

    async def asegurar_falso(ctx, valor):
        eventos.append(("admins", ctx, valor))

    def bucle_falso(ctx, *, parar):
        eventos.append(("worker", ctx))
        return _hasta(parar)

    monkeypatch.setattr(app_radar, "asegurar_admins_iniciales", asegurar_falso)
    monkeypatch.setattr(worker_radar, "bucle", bucle_falso)
    rs = radar_ctx.settings.model_copy(update={"admins_iniciales": "a@kis.com,B@kis.com", "worker_embebido": True})
    app = crear_app_radar(rs)
    async with app.router.lifespan_context(app):
        pass
    assert [e[0] for e in eventos] == ["admins", "worker"]
    _, ctx, valor = eventos[0]
    assert ctx is not radar_ctx and valor == "a@kis.com,B@kis.com"
    assert eventos[1][1] is ctx


async def test_con_un_email_invalido_el_arranque_falla_antes_de_migrar(monkeypatch):
    """Un deploy con la variable mal escrita falla enseguida y dice cuál es la entrada, sin tocar ninguna base."""
    migraciones = []
    monkeypatch.setattr(app_radar, "migrar_resultados", lambda url: migraciones.append(url))
    app = crear_app_radar(_settings(admins_iniciales="a@kis.com, zorro-plateado"))
    with pytest.raises(RuntimeError, match=r"RADAR_ADMINS_INICIALES.*posición 2\b"):
        async with app.router.lifespan_context(app):
            pass
    assert migraciones == []


async def test_con_un_contexto_inyectado_el_arranque_no_crea_admins(radar_ctx, monkeypatch):
    llamadas = []

    async def espia(*args):
        llamadas.append(args)

    monkeypatch.setattr(app_radar, "asegurar_admins_iniciales", espia)
    rs = radar_ctx.settings.model_copy(update={"admins_iniciales": "a@kis.com", "worker_embebido": False})
    app = crear_app_radar(rs, contexto=radar_ctx)
    async with app.router.lifespan_context(app):
        pass
    assert llamadas == []


async def test_el_arranque_crea_los_admins_de_la_variable(radar_ctx):
    """Por el lifespan de verdad, sin espías: migra, arma su contexto y crea los admins sin mandar nada."""
    rs = radar_ctx.settings.model_copy(update={"admins_iniciales": "Mariano@KIS.com.ar", "worker_embebido": False})
    app = crear_app_radar(rs)
    async with app.router.lifespan_context(app):
        assert app.state.radar is not radar_ctx
        assert app.state.radar.mailer.enviados == []
    assert await _usuarios_de_kis(radar_ctx) == {"mariano@kis.com.ar": ("admin", "")}


def test_admins_iniciales_viene_vacio_y_lo_llena_la_variable(monkeypatch):
    monkeypatch.delenv("RADAR_ADMINS_INICIALES", raising=False)
    assert RadarSettings(_env_file=None).admins_iniciales == ""
    monkeypatch.setenv("RADAR_ADMINS_INICIALES", "a@kis.com,b@kis.com")
    assert RadarSettings(_env_file=None).admins_iniciales == "a@kis.com,b@kis.com"


def test_la_tabla_de_despliegue_documenta_radar_admins_iniciales():
    doc = (RAIZ / "docs" / "radar-despliegue.md").read_text(encoding="utf-8")
    assert "| `RADAR_ADMINS_INICIALES` |" in doc
