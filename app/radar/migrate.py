"""
Aplica las migraciones de Radar. Se llama desde el lifespan de modo radar
(fallar acá impide el arranque) y desde la fixture de tests.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config

RAIZ = Path(__file__).resolve().parent.parent.parent


_ESQUEMAS_POSTGRES = ("postgres://", "postgresql://", "postgresql+asyncpg://", "postgresql+psycopg://",
                      "postgresql+psycopg2://")


def con_driver_psycopg2(url: str) -> str:
    """Alembic usa psycopg2, y la URL lo nombra: SQLAlchemy 2.1 cambió el driver
    por defecto de `postgresql://` a psycopg (v3), que requirements.txt no trae."""
    for esquema in _ESQUEMAS_POSTGRES:
        if url.startswith(esquema):
            return "postgresql+psycopg2://" + url[len(esquema):]
    return url


def _url_psycopg(url: str) -> str:
    """La URL de con_driver_psycopg2, con % escapado para configparser."""
    return con_driver_psycopg2(url).replace("%", "%%")


def _config(ini: str, url: str) -> Config:
    if not url:
        raise RuntimeError(f"URL vacía para {ini}")
    cfg = Config(str(RAIZ / ini))
    cfg.set_main_option("script_location", str(RAIZ / cfg.get_main_option("script_location")))
    cfg.set_main_option("sqlalchemy.url", _url_psycopg(url))
    return cfg


def migrar_resultados(migrator_url: str) -> None:
    command.upgrade(_config("alembic_radar.ini", migrator_url), "head")


def migrar_fuente(fuente_url: str) -> None:
    command.upgrade(_config("alembic_fuente.ini", fuente_url), "head")
