"""
Aplica las migraciones de Radar. Se llama desde el lifespan de modo radar
(fallar acá impide el arranque) y desde la fixture de tests.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config

RAIZ = Path(__file__).resolve().parent.parent.parent


def _url_psycopg(url: str) -> str:
    """Alembic usa psycopg2: normaliza el esquema y escapa % para configparser."""
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql+asyncpg://"):
        url = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    return url.replace("%", "%%")


def _config(ini: str, url: str) -> Config:
    if not url:
        raise RuntimeError(f"URL vacía para {ini}")
    cfg = Config(str(RAIZ / ini))
    cfg.set_main_option("script_location", str(RAIZ / cfg.get_main_option("script_location")))
    cfg.set_main_option("sqlalchemy.url", _url_psycopg(url))
    return cfg


def migrar_resultados(migrator_url: str) -> None:
    command.upgrade(_config("alembic_radar.ini", migrator_url), "head")
