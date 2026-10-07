"""
Config del perfil de rubro (spec 3.4): DEFAULTS + perfil.textos, lo guardado
gana, y los fallbacks del código que pasan a perfil.textos. La farmacia queda
idéntica: sus textos de perfil SON los de DEFAULTS.
"""
import hashlib
import json
from dataclasses import replace
from types import MappingProxyType

import pytest

from app.routers import webhook as wh
from test_webhook_secuencias import PHONE, _msg, entorno  # noqa: F401  (fixture reusada)


# ── Golden de DEFAULTS (tomado sobre 07a1d7a, ANTES del cambio) ─────────────────
def test_defaults_farmacia_golden():
    """Los 90 DEFAULTS de hoy no cambian: solo se suman las tres claves nuevas."""
    from app.services.config_service import DEFAULTS
    nuevas = {"pago_mp_manual", "retiro_sucursal", "retiro_info_message"}
    hoy = {k: v for k, v in DEFAULTS.items() if k not in nuevas}
    assert len(hoy) == 90
    h = hashlib.sha256(json.dumps(hoy, sort_keys=True, ensure_ascii=False)
                       .encode("utf-8")).hexdigest()
    assert h == "655cbe78de891191bb660b42cc1d4b3b9e325bf93205f139a6f4bfcefa910e1d"


# ── Fakes de Redis y Postgres para ConfigService ────────────────────────────────
class _RedisFalso:
    def __init__(self, data):
        self.data = dict(data)

    async def hgetall(self, key):
        return dict(self.data)

    async def hset(self, key, field=None, value=None, mapping=None):
        self.data.update(mapping or {field: value})


class _DBFalsa:
    def __init__(self, filas=None, disponible=True):
        self.filas = dict(filas or {})
        self._disponible = disponible

    def available(self):
        return self._disponible

    async def fetch(self, query, *args):
        return [{"clave": k, "valor": v} for k, v in self.filas.items()]

    async def execute(self, query, *args):
        self.filas[args[0]] = args[1]
        return "OK"


def _config(redis_data=None, db_filas=None):
    """ConfigService con Redis caído (o con datos) y sin Postgres (o con filas)."""
    from app.services.config_service import ConfigService
    svc = ConfigService("redis://127.0.0.1:1")
    svc._ok = redis_data is not None
    if redis_data is not None:
        svc._redis = _RedisFalso(redis_data)
    svc._db = _DBFalsa(db_filas, disponible=db_filas is not None)
    return svc
