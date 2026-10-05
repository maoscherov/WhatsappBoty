"""
Las migraciones nombran el driver de Postgres. SQLAlchemy 2.1 cambió el driver
por defecto de `postgresql://` a psycopg (v3), que no está en requirements.txt:
sin el driver explícito, una imagen nueva no migra y Radar no arranca.
"""

import pytest
from sqlalchemy.engine import make_url

from app.radar.migrate import _url_psycopg, con_driver_psycopg2


@pytest.mark.parametrize("url", [
    "postgresql://u:p@h:5432/db",
    "postgres://u:p@h:5432/db",
    "postgresql+asyncpg://u:p@h:5432/db",
    "postgresql+psycopg://u:p@h:5432/db",
    "postgresql+psycopg2://u:p@h:5432/db",
])
def test_la_url_de_migracion_nombra_psycopg2(url):
    convertida = con_driver_psycopg2(url)
    assert convertida == "postgresql+psycopg2://u:p@h:5432/db"
    assert make_url(convertida).get_driver_name() == "psycopg2"


def test_otra_url_no_se_toca():
    assert con_driver_psycopg2("sqlite:///x.db") == "sqlite:///x.db"


def test_la_url_para_configparser_escapa_el_porcentaje():
    assert _url_psycopg("postgresql://u:p%40x@h/db") == "postgresql+psycopg2://u:p%%40x@h/db"
