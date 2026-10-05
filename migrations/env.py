"""
Entorno de Alembic. Toma la URL de conexión de DATABASE_URL (o del .env vía
pydantic settings) y corre las migraciones con psycopg2 (sync).
"""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    if not url:
        try:
            from app.config import get_settings
            url = get_settings().database_url
        except Exception:
            url = ""
    if not url:
        raise RuntimeError("DATABASE_URL no configurada — no se puede migrar")
    # Alembic/SQLAlchemy usan psycopg2, y la URL lo nombra: SQLAlchemy 2.1 cambió
    # el driver por defecto de postgresql:// a psycopg (v3), que no está instalado.
    for esquema in ("postgres://", "postgresql://", "postgresql+asyncpg://", "postgresql+psycopg://"):
        if url.startswith(esquema):
            url = "postgresql+psycopg2://" + url[len(esquema):]
            break
    return url


# Sin modelos SQLAlchemy: las migraciones son manuales (raw SQL con pgvector).
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
