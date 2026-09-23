"""
Fixtures de Radar.

Postgres embebido (pgserver) con los roles reales de RLS. El usuario por
defecto de pgserver es superusuario, y un superusuario ignora RLS: los tests
de aislamiento solo valen conectando como `radar_app`. Acá se crean, igual
que scripts/radar_bootstrap_roles.sql en producción:

- radar_migrator: dueño de las tablas, corre Alembic (INHERIT, para tener
  CREATE en public como dueño de la base).
- radar_app: la app. NOSUPERUSER, NOBYPASSRLS, NOINHERIT, no dueño.
- radar_admin: NOLOGIN, dueño de las funciones SECURITY DEFINER.
"""

import os
import tempfile

import asyncpg
import psycopg2
import pytest

from app.radar.constantes import TENANT_KIS
from app.radar.db import RadarDB
from app.radar.migrate import migrar_resultados

ROLES_SQL = """
    CREATE ROLE radar_migrator LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    CREATE ROLE radar_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
    CREATE ROLE radar_admin NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
    GRANT radar_admin TO radar_migrator;
"""

# Orden de TRUNCATE: hijas antes que padres. Se amplía en la Task 6.
TABLAS = ["tenants"]


@pytest.fixture(scope="session")
def radar_urls():
    try:
        import pgserver
    except ImportError:
        pytest.skip("pgserver no instalado")

    tmp = tempfile.mkdtemp(prefix="pgtest_radar_")
    srv = pgserver.get_server(tmp)
    info = srv.get_postmaster_info()

    su = psycopg2.connect(info.get_uri())
    su.autocommit = True
    cur = su.cursor()
    cur.execute(ROLES_SQL)
    cur.execute("CREATE DATABASE radar_test OWNER radar_migrator")
    su.close()

    urls = {
        "super": info.get_uri(database="radar_test"),
        "migrator": info.get_uri(user="radar_migrator", database="radar_test"),
        "app": info.get_uri(user="radar_app", database="radar_test"),
    }
    migrar_resultados(urls["migrator"])
    yield urls
    try:
        srv.cleanup()
    except Exception:
        pass


async def _limpiar(url_super: str) -> None:
    """Vacía las tablas y vuelve a sembrar el tenant KIS. Como superusuario:
    con FORCE RLS ni el dueño puede insertar sin política."""
    con = await asyncpg.connect(url_super)
    try:
        await con.execute("TRUNCATE " + ", ".join(TABLAS) + " CASCADE")
        await con.execute(
            "INSERT INTO tenants (id, nombre, rubro, es_kis) VALUES ($1, 'Keep IT Simple', 'kis', TRUE)",
            TENANT_KIS,
        )
    finally:
        await con.close()


@pytest.fixture
async def radar_db(radar_urls):
    await _limpiar(radar_urls["super"])
    db = RadarDB(radar_urls["app"], max_size=2)
    await db.connect()
    yield db
    await db.close()
