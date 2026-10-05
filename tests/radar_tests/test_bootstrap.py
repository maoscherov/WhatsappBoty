"""
Roles de Postgres al arrancar (Decisión 7, RADAR_BOOTSTRAP_ROLES=true).

Los roles son de todo el cluster y conftest.py ya los crea en el pgserver de la sesión: estos tests levantan su
propio pgserver (fixture de módulo) y cada uno parte sin radar_app ni radar_admin (`cluster_vacio`). En ese
cluster radar_app entra solo con contraseña (scram-sha-256 en pg_hba.conf; pgserver deja todo en trust): así
"radar_app entra con RADAR_DATABASE_URL" se prueba de verdad, con asyncpg como la app. El rol de migración es
el superusuario del cluster, como `postgres` en Railway, salvo donde el test dice otra cosa.

La contraseña de radar_app es un secreto: ni en un log, ni en un mensaje de error, ni en un repr.
"""
import asyncio
import logging
import pathlib
import time
import traceback
from dataclasses import dataclass
from urllib.parse import quote

import asyncpg
import psycopg2
import pytest
from starlette.testclient import TestClient

import app.radar.app as app_radar
from app.radar import bootstrap
from app.radar.app import crear_app_radar
from app.radar.bootstrap import asegurar_roles
from app.radar.contexto import RadarContexto
from app.radar.migrate import migrar_resultados
from app.radar.settings import RadarSettings

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


def _exigir_contrasena_a_radar_app(cluster: Cluster, pgdata: pathlib.Path) -> None:
    hba = pgdata / "pg_hba.conf"
    hba.write_text(HBA_RADAR_APP + hba.read_text())
    antes = cluster.sql("SELECT pg_conf_load_time()")[0][0]
    cluster.sql("SELECT pg_reload_conf()")
    # La recarga es asíncrona: una sesión nueva ve la hora de carga del postmaster.
    for _ in range(100):
        if cluster.sql("SELECT pg_conf_load_time()")[0][0] > antes:
            return
        time.sleep(0.05)
    raise RuntimeError("el cluster de test no recargó pg_hba.conf")


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
        _exigir_contrasena_a_radar_app(c, pgdata)
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


def test_con_createrole_sin_admin_sobre_radar_app_no_intenta_cambiar_la_contrasena(cluster_vacio):
    """Postgres 16+: con CREATEROLE solo se cambia la contraseña de un rol sobre el que se tiene ADMIN. Si
    radar_app lo creó otro y no entra con la URL, no se intenta el ALTER ROLE: fallido, Postgres lo escribiría
    en su log con la contraseña (log_min_error_statement)."""
    cluster_vacio.sql("CREATE ROLE radar_app LOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT")
    _fijar_contrasena(cluster_vacio, OTRA_CLAVE)
    antes = _roles(cluster_vacio)["radar_app"]
    with pytest.raises(RuntimeError, match="ADMIN") as exc:
        asegurar_roles(cluster_vacio.url(MIGRADOR_CREATEROLE), cluster_vacio.url_app())
    _sin_secretos(exc.value, CLAVE, CLAVE_EN_URL, cluster_vacio.url_app())
    assert _roles(cluster_vacio)["radar_app"] == antes
    assert CLAVE not in cluster_vacio.log.read_text(encoding="utf-8", errors="replace")


# ── secretos ───────────────────────────────────────────────────────────────────

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
