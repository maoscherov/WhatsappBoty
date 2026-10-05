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
    # Alembic/SQLAlchemy usan psycopg2; normalizar el esquema de la URL.
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql+asyncpg://"):
        url = url.replace("postgresql+asyncpg://", "postgresql://", 1)
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
