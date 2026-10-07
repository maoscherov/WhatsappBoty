"""
Arranque, catálogo y venta por perfil de rubro (spec 4.8 y 4.2, fila main.py):
restauración de archivos, padrón, referencia de recetas, aviso de horario, el
CSV de la farmacia que petshop nunca carga, el tablero y el desvío sin venta.
"""
import logging
from types import SimpleNamespace

import pytest

from app.routers import webhook as wh
from test_webhook_secuencias import PHONE, _msg, entorno  # noqa: F401  (fixture reusada)


# ── _restaurar_archivos ─────────────────────────────────────────────────────────
class _BlobFalso:
    def __init__(self, archivos):
        self.archivos = archivos
        self.pedidos = []

    async def load(self, nombre):
        self.pedidos.append(nombre)
        return self.archivos.get(nombre)


async def test_restaurar_sin_sku_csv_path_no_tira_y_restaura_el_padron(tmp_path, usar_perfil):
    from app.main import _restaurar_archivos
    perfil = usar_perfil("farmacia")
    settings = SimpleNamespace(sku_csv_path="", socios_path=str(tmp_path / "socios.csv"))
    blob = _BlobFalso({"catalogo": (b"SKU,Nombre\n1,X\n", ""),
                       "socios": (b"PK\x03\x04padron", ".xlsx")})
    await _restaurar_archivos(settings, perfil, blob)
    destino = tmp_path / "socios.xlsx"
    assert destino.read_bytes() == b"PK\x03\x04padron"
    assert settings.socios_path == str(destino)


async def test_restaurar_escribe_el_catalogo_si_hay_ruta(tmp_path, usar_perfil):
    from app.main import _restaurar_archivos
    perfil = usar_perfil("farmacia")
    ruta = tmp_path / "cat" / "catalogo.csv"
    settings = SimpleNamespace(sku_csv_path=str(ruta), socios_path=str(tmp_path / "socios.csv"))
    await _restaurar_archivos(settings, perfil, _BlobFalso({"catalogo": (b"SKU,Nombre\n1,X\n", "")}))
    assert ruta.read_bytes() == b"SKU,Nombre\n1,X\n"


async def test_restaurar_petshop_no_toca_el_padron(tmp_path, usar_perfil, caplog):
    from app.main import _restaurar_archivos
    perfil = usar_perfil("petshop")
    settings = SimpleNamespace(sku_csv_path="", socios_path=str(tmp_path / "socios.csv"))
    blob = _BlobFalso({"socios": (b"PK\x03\x04padron", ".xlsx")})
    with caplog.at_level(logging.INFO, logger="app.main"):
        await _restaurar_archivos(settings, perfil, blob)
    assert "socios" not in blob.pedidos
    assert list(tmp_path.iterdir()) == []
    assert settings.socios_path == str(tmp_path / "socios.csv")
    assert "Perfil sin socios: no se carga el padrón" in caplog.text


# ── Padrón en Postgres ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("clave,llamadas", [("farmacia", 1), ("mutual", 1), ("petshop", 0)])
async def test_hidratar_padron_segun_perfil(usar_perfil, monkeypatch, clave, llamadas):
    import app.services.socio_service as socio_mod
    from app.main import _hidratar_padron
    vistos = []

    async def _cargar(db, svc):
        vistos.append(db)
        return 5

    monkeypatch.setattr(socio_mod, "cargar_desde_db", _cargar)
    monkeypatch.setattr(socio_mod, "get_socio_service", lambda path: SimpleNamespace(total=0))

    class _DB:
        def available(self):
            return True

    perfil = usar_perfil(clave)
    await _hidratar_padron(SimpleNamespace(socios_path="data/socios.csv"), perfil, _DB())
    assert len(vistos) == llamadas


# ── Referencia de recetas (spec 4.2, fila main.py) ──────────────────────────────
@pytest.mark.parametrize("clave,llamadas", [("farmacia", 1), ("mutual", 1), ("petshop", 0)])
async def test_init_referencia_receta_segun_perfil(usar_perfil, monkeypatch, caplog,
                                                   clave, llamadas):
    import app.services.receta_referencia as rr
    from app.main import _init_referencia_receta
    vistos = []

    async def _inicializar(db):
        vistos.append(db)
        return {"referencia": 3}

    monkeypatch.setattr(rr, "inicializar", _inicializar)
    usar_perfil(clave)
    with caplog.at_level(logging.INFO, logger="app.main"):
        r = await _init_referencia_receta("DB")
    assert vistos == ["DB"] * llamadas
    if llamadas:
        assert r == {"referencia": 3}
    else:
        assert r is None
        assert "Perfil sin recetas" in caplog.text


# ── Aviso de horario por defecto ────────────────────────────────────────────────
class _CfgHoras:
    def __init__(self, horas):
        self.horas = horas

    async def get_hours(self):
        return self.horas


async def test_avisa_si_el_horario_es_el_de_defecto(caplog):
    from app.main import _avisar_horario_por_defecto
    from app.services.config_service import DEFAULT_HOURS
    with caplog.at_level(logging.WARNING, logger="app.main"):
        assert await _avisar_horario_por_defecto(_CfgHoras(dict(DEFAULT_HOURS))) is True
    assert "Horario no cargado: se usa DEFAULT_HOURS" in caplog.text

    caplog.clear()
    cargado = {**DEFAULT_HOURS, "enabled": True}
    with caplog.at_level(logging.WARNING, logger="app.main"):
        assert await _avisar_horario_por_defecto(_CfgHoras(cargado)) is False
    assert "DEFAULT_HOURS" not in caplog.text
