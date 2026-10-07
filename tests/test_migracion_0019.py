"""
Migración 0019 (pedidos durables y códigos de Mercurio) sobre la 0018 de
develop (operadores). Hallazgos 1 y 13 de la revisión final del 6/10.

La rama traía su propia 0018 (orders + mercurio_codigos) y develop agregó otra
0018 (operadores): con dos heads, el `upgrade head` del arranque fallaba en los
tres deploys. Al mergear develop, la de la rama pasó a 0019 sobre la 0018 de
develop. La base de MO ya estaba estampada en 0018 con la tabla de pedidos y
sin operadores: la 0019 se auto-repara y corre el upgrade() de la 0018 de
develop si falta la tabla operadores.

Las simulaciones crean una base aparte en el mismo server de pg_dsn y corren
alembic en un subproceso con su DATABASE_URL (env.py la toma del entorno): no
se toca el entorno del proceso de tests ni el Settings cacheado. Cada base se
borra al terminar el test.
"""
import contextlib
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg2
import pytest

ROOT = Path(__file__).resolve().parent.parent

# SQL original de la vieja 0018 de la rama, el que ya corrió en la base de MO
# (git show 80595d4:migrations/versions/0018_orders_y_mercurio_codigos.py).
_SQL_VIEJA_0018 = (
    """
        CREATE TABLE IF NOT EXISTS orders (
            order_id            TEXT PRIMARY KEY,
            phone               TEXT NOT NULL,
            estado              TEXT NOT NULL DEFAULT 'pendiente',
            total               NUMERIC(12,2),
            pago                TEXT,
            payment_id          TEXT,
            data                JSONB NOT NULL,
            erp_estado          TEXT,            -- NULL = no aplica | pendiente | enviado | rechazado
            erp_id_comprobante  TEXT,
            erp_numero          TEXT,
            erp_intentos        INT NOT NULL DEFAULT 0,
            erp_ultimo_error    TEXT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """,
    "CREATE INDEX IF NOT EXISTS ix_orders_erp_pendiente ON orders (created_at) "
    "WHERE erp_estado = 'pendiente'",
    "CREATE INDEX IF NOT EXISTS ix_orders_payment ON orders (payment_id)",
    """
        CREATE TABLE IF NOT EXISTS mercurio_codigos (
            branch_id     TEXT NOT NULL,
            external_id   TEXT NOT NULL,
            codigo        TEXT NOT NULL,
            codigo_padre  TEXT NOT NULL,
            PRIMARY KEY (branch_id, external_id)
        )
    """,
)


def _alembic(dsn: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": dsn, "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini"), *args],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=300,
    )
    assert r.returncode == 0, f"alembic {' '.join(args)} falló:\n{r.stdout}\n{r.stderr}"


def _ejecutar(dsn: str, *sentencias: str) -> None:
    with contextlib.closing(psycopg2.connect(dsn)) as conn:
        with conn, conn.cursor() as cur:
            for s in sentencias:
                cur.execute(s)


def _consulta(dsn: str, sql: str) -> list[tuple]:
    with contextlib.closing(psycopg2.connect(dsn)) as conn:
        with conn, conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()


def _tablas(dsn: str) -> set[str]:
    return {r[0] for r in _consulta(
        dsn, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}


def _indices(dsn: str) -> set[str]:
    return {r[0] for r in _consulta(
        dsn, "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")}


def _version(dsn: str) -> list[tuple]:
    return _consulta(dsn, "SELECT version_num FROM alembic_version")


@pytest.fixture
def base_aparte(pg_dsn):
    """Base nueva y vacía en el server de pg_dsn; se borra al final."""
    nombre = f"migr_{uuid.uuid4().hex[:12]}"
    admin = psycopg2.connect(pg_dsn)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{nombre}"')
        yield urlunsplit(urlsplit(pg_dsn)._replace(path=f"/{nombre}"))
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{nombre}" WITH (FORCE)')
        admin.close()


# ── (a) el árbol de alembic ───────────────────────────────────────────────────
def test_un_solo_head_y_es_0019_sobre_operadores():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    sd = ScriptDirectory.from_config(cfg)
    assert sd.get_heads() == ["0019"]
    r19 = sd.get_revision("0019")
    assert r19.down_revision == "0018"
    assert Path(r19.path).name == "0019_orders_y_mercurio_codigos.py"
    assert Path(sd.get_revision("0018").path).name == "0018_operadores.py"


# ── (b) base recién migrada (la de pg_dsn) ───────────────────────────────────
def test_base_recien_migrada_tiene_operadores_y_pedidos(pg_dsn):
    assert {"operadores", "orders", "mercurio_codigos"} <= _tablas(pg_dsn)
    assert _version(pg_dsn) == [("0019",)]


# ── (c) MO: estampada en 0018 por la vieja migración de la rama ──────────────
def test_base_de_mo_estampada_en_la_vieja_0018_se_repara_sola(base_aparte):
    dsn = base_aparte
    _alembic(dsn, "upgrade", "0017")
    _ejecutar(dsn, *_SQL_VIEJA_0018)
    _ejecutar(
        dsn,
        "INSERT INTO orders (order_id, phone, total, pago, payment_id, data, erp_estado) "
        "VALUES ('MO-0001', '5491100000000', 15000, 'online', 'pay-1', "
        "'{\"order_id\": \"MO-0001\"}', 'pendiente')",
        # un operador que ya escribió en MO: la siembra de la 0018 de develop lo levanta
        "INSERT INTO messages (phone, role, content, autor) "
        "VALUES ('5491100000000', 'operator', 'hola', 'Lore')",
    )
    _alembic(dsn, "stamp", "0018")
    assert "operadores" not in _tablas(dsn)     # el estado real de la base de MO

    _alembic(dsn, "upgrade", "head")

    assert {"operadores", "orders", "mercurio_codigos"} <= _tablas(dsn)
    assert _version(dsn) == [("0019",)]
    assert _consulta(dsn, "SELECT order_id, erp_estado, data->>'order_id' FROM orders") == [
        ("MO-0001", "pendiente", "MO-0001")]
    assert _consulta(dsn, "SELECT nombre FROM operadores") == [("Lore",)]


# ── (d) farmacia/mutual: en 0018 = operadores de develop ─────────────────────
def test_base_de_farmacia_en_0018_operadores_recibe_pedidos(base_aparte):
    dsn = base_aparte
    _alembic(dsn, "upgrade", "0018")
    tablas = _tablas(dsn)
    assert "operadores" in tablas and "orders" not in tablas
    _ejecutar(
        dsn,
        "INSERT INTO operadores (nombre, aliases) VALUES ('María', ARRAY['02'])",
        # si la 0019 volviera a sembrar operadores, 'carcarana' aparecería
        "INSERT INTO messages (phone, role, content, autor) "
        "VALUES ('5493410000000', 'operator', 'hola', 'carcarana')",
    )

    _alembic(dsn, "upgrade", "head")

    assert {"operadores", "orders", "mercurio_codigos"} <= _tablas(dsn)
    # fix-C1 (hallazgo 11): el índice de payment_id es UNIQUE (ux_orders_payment)
    # y el no único (ix_orders_payment) ya no queda.
    assert {"ix_orders_erp_pendiente", "ux_orders_payment"} <= _indices(dsn)
    assert "ix_orders_payment" not in _indices(dsn)
    assert _version(dsn) == [("0019",)]
    assert _consulta(dsn, "SELECT nombre, aliases FROM operadores") == [("María", ["02"])]

    # downgrade de la 0019: borra solo lo suyo, nunca operadores
    _alembic(dsn, "downgrade", "0018")
    tablas = _tablas(dsn)
    assert "orders" not in tablas and "mercurio_codigos" not in tablas
    assert _consulta(dsn, "SELECT nombre FROM operadores") == [("María",)]
    assert _version(dsn) == [("0018",)]


# ── fix-C1: índice único de payment_id y erp_proximo_intento ─────────────────
# Hallazgo 11 (un mismo pago no crea dos órdenes aunque Redis se pierda) y la
# columna que usa el backoff de los reintentos (fix-C2). Van al final de la
# 0019 con IF NOT EXISTS: tienen que llegar también a la orders que ya existe
# en MO (creada por la vieja 0018 de la rama).

def _indice(dsn: str, nombre: str) -> list[tuple]:
    return _consulta(dsn, f"SELECT indexdef FROM pg_indexes WHERE indexname = '{nombre}'")


def _columna(dsn: str, tabla: str, columna: str) -> list[tuple]:
    return _consulta(
        dsn, "SELECT data_type, is_nullable FROM information_schema.columns "
             f"WHERE table_name = '{tabla}' AND column_name = '{columna}'")


def _assert_unico_y_proximo_intento(dsn: str) -> None:
    [(definicion,)] = _indice(dsn, "ux_orders_payment")
    assert "UNIQUE" in definicion and "(payment_id)" in definicion
    assert "payment_id IS NOT NULL" in definicion
    assert "ix_orders_payment" not in _indices(dsn)
    assert _columna(dsn, "orders", "erp_proximo_intento") == [
        ("timestamp with time zone", "YES")]


def test_base_recien_migrada_tiene_indice_unico_y_proximo_intento(pg_dsn):
    _assert_unico_y_proximo_intento(pg_dsn)


def _base_de_mo(dsn: str, *filas: str) -> None:
    """La base de MO: 0017 + el SQL de la vieja 0018 de la rama + sus filas,
    estampada en 0018 (como fix-A, test (c))."""
    _alembic(dsn, "upgrade", "0017")
    _ejecutar(dsn, *_SQL_VIEJA_0018, *filas)
    _alembic(dsn, "stamp", "0018")


def test_mo_sin_pagos_repetidos_recibe_indice_unico_y_proximo_intento(base_aparte):
    dsn = base_aparte
    _base_de_mo(
        dsn,
        "INSERT INTO orders (order_id, phone, total, pago, payment_id, data, erp_estado) VALUES "
        "('MO-0001', '549', 15000, 'online', 'pay-1', '{}', 'pendiente'),"
        "('MO-0002', '549', 9000, 'online', 'pay-2', '{}', NULL),"
        # efectivo / cuenta corriente: sin payment_id, pueden ser muchas
        "('MO-0003', '549', 100, 'cuenta_corriente', NULL, '{}', NULL),"
        "('MO-0004', '549', 200, 'efectivo', NULL, '{}', NULL)")
    assert "ix_orders_payment" in _indices(dsn)

    _alembic(dsn, "upgrade", "head")

    assert _version(dsn) == [("0019",)]
    _assert_unico_y_proximo_intento(dsn)
    assert _consulta(dsn, "SELECT order_id, payment_id, erp_estado, erp_proximo_intento "
                          "FROM orders ORDER BY order_id") == [
        ("MO-0001", "pay-1", "pendiente", None), ("MO-0002", "pay-2", None, None),
        ("MO-0003", None, None, None), ("MO-0004", None, None, None)]
    # el índice muerde: otro pedido con el mismo pago no entra
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _ejecutar(dsn, "INSERT INTO orders (order_id, phone, payment_id, data) "
                       "VALUES ('MO-0005', '549', 'pay-1', '{}')")


def test_mo_con_pagos_repetidos_migra_igual_sin_indice_unico(base_aparte):
    """Si la base ya tiene dos órdenes con el mismo pago, la migración no
    falla (el arranque corre `upgrade head`): avisa y deja el índice común."""
    dsn = base_aparte
    _base_de_mo(
        dsn,
        "INSERT INTO orders (order_id, phone, total, pago, payment_id, data) VALUES "
        "('MO-0001', '549', 15000, 'online', 'pay-dup', '{}'),"
        "('MO-0002', '549', 15000, 'online', 'pay-dup', '{}')")

    _alembic(dsn, "upgrade", "head")

    assert _version(dsn) == [("0019",)]
    assert _indice(dsn, "ux_orders_payment") == []
    assert "ix_orders_payment" in _indices(dsn)
    assert _columna(dsn, "orders", "erp_proximo_intento") == [
        ("timestamp with time zone", "YES")]
    assert _consulta(dsn, "SELECT count(*) FROM orders") == [(2,)]


# ── Ronda de arreglo 2 (i): erp_actualizado_at ───────────────────────────────
# El "último error" de /bo/mercurio/estado se ordenaba por updated_at, que
# también se mueve con las acciones del operador (preparado, retirado). La
# fecha del último cambio del alta en el ERP va en su propia columna, también
# en la orders que ya existe en MO.

def test_base_recien_migrada_tiene_erp_actualizado_at(pg_dsn):
    assert _columna(pg_dsn, "orders", "erp_actualizado_at") == [
        ("timestamp with time zone", "YES")]


def test_mo_recibe_erp_actualizado_at(base_aparte):
    dsn = base_aparte
    _base_de_mo(
        dsn,
        "INSERT INTO orders (order_id, phone, total, pago, payment_id, data, erp_estado) "
        "VALUES ('MO-0001', '549', 15000, 'online', 'pay-1', '{}', 'pendiente')")

    _alembic(dsn, "upgrade", "head")

    assert _columna(dsn, "orders", "erp_actualizado_at") == [
        ("timestamp with time zone", "YES")]
    assert _consulta(dsn, "SELECT order_id, erp_actualizado_at FROM orders") == [
        ("MO-0001", None)]
