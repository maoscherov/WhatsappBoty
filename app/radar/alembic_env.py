"""
Entorno compartido por los dos árboles de migraciones de Radar
(migrations_radar/ y migrations_fuente/). Cada uno tiene su .ini con su
`script_location` y su `version_table`; la URL la fija quien invoca
(app/radar/migrate.py) en `sqlalchemy.url`, o `-x url=...` desde la línea
de comandos. Nunca lee DATABASE_URL: esa es la base de un cliente del bot y
Radar no debe tocarla.
"""

from alembic import context
from sqlalchemy import engine_from_config, pool


def correr() -> None:
    config = context.config
    url = config.get_main_option("sqlalchemy.url") or ""
    if not url:
        url = context.get_x_argument(as_dictionary=True).get("url", "")
    if not url:
        raise RuntimeError("Falta la URL: usar app.radar.migrate o `alembic -c <ini> -x url=... upgrade head`")
    version_table = config.get_main_option("version_table") or "alembic_version_radar"

    if context.is_offline_mode():
        context.configure(url=url, target_metadata=None, literal_binds=True,
                          version_table=version_table)
        with context.begin_transaction():
            context.run_migrations()
        return

    section = dict(config.get_section(config.config_ini_section) or {})
    section["sqlalchemy.url"] = url
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=None,
                          version_table=version_table)
        with context.begin_transaction():
            context.run_migrations()
