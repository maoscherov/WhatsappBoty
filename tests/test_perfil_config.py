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


# ── Claves nuevas de DEFAULTS y ConfigUpdate ────────────────────────────────────
def test_defaults_claves_nuevas_dejan_todo_como_hoy():
    from app.services.config_service import DEFAULTS
    assert DEFAULTS["pago_mp_manual"] == "true"
    assert DEFAULTS["retiro_sucursal"] == ""
    assert DEFAULTS["retiro_info_message"] == "Lo retirás en *{sucursal}* 🏪"


def test_config_update_acepta_las_claves_nuevas():
    from app.routers.backoffice import ConfigUpdate
    body = ConfigUpdate(retiro_sucursal="Sucursal Piloto",
                        retiro_info_message="Lo retirás en *{sucursal}*, Calle Falsa 123",
                        pago_mp_manual="false")
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    assert updates == {"retiro_sucursal": "Sucursal Piloto",
                       "retiro_info_message": "Lo retirás en *{sucursal}*, Calle Falsa 123",
                       "pago_mp_manual": "false"}


# ── valores_base() y el merge de get_all / get ──────────────────────────────────
def test_valores_base_farmacia_es_defaults_y_petshop_suma_sus_textos(usar_perfil):
    from app.services.config_service import DEFAULTS, valores_base
    usar_perfil("farmacia")
    assert valores_base() == DEFAULTS

    p = usar_perfil("petshop")
    vb = valores_base()
    assert {k: vb[k] for k in p.textos} == dict(p.textos)
    assert {k: v for k, v in vb.items() if k not in p.textos} == \
        {k: v for k, v in DEFAULTS.items() if k not in p.textos}
    vb["send_images"] = "on_request"                 # copia: no ensucia DEFAULTS
    assert valores_base()["send_images"] == DEFAULTS["send_images"]


async def test_get_all_farmacia_es_defaults(usar_perfil):
    from app.services.config_service import DEFAULTS
    usar_perfil("farmacia")
    assert await _config().get_all() == DEFAULTS


async def test_get_all_petshop_textos_del_perfil_y_lo_guardado_gana(usar_perfil):
    from app.services.config_service import DEFAULTS
    p = usar_perfil("petshop")
    svc = _config()
    cfg = await svc.get_all()
    assert cfg["pedido_listo_retiro_message"].endswith("🐾")
    assert cfg["sintoma_farmaceutico_message"] == ""
    assert cfg["send_images"] == DEFAULTS["send_images"]
    assert cfg["consulta_salud_message"] == p.textos["consulta_salud_message"]
    assert not any("💊" in cfg[k] for k in p.textos)

    await svc.set_many({"pedido_listo_retiro_message": "X"})
    assert (await svc.get_all())["pedido_listo_retiro_message"] == "X"
    assert await svc.get("pedido_listo_retiro_message") == "X"


@pytest.mark.parametrize("origen", ["redis", "postgres"])
async def test_get_all_petshop_con_config_guardada(usar_perfil, origen):
    usar_perfil("petshop")
    guardado = {"send_images": "on_request"}
    svc = _config(redis_data=guardado) if origen == "redis" else _config(db_filas=guardado)
    cfg = await svc.get_all()
    assert cfg["send_images"] == "on_request"                 # lo guardado gana
    assert cfg["pedido_listo_envio_message"].endswith("🐾")   # el resto, del perfil
    assert "💊" not in cfg["efectivo_retiro_message"]


async def test_get_usa_valores_base_como_default(usar_perfil):
    usar_perfil("petshop")
    svc = _config()
    assert (await svc.get("pedido_listo_envio_message")).endswith("🐾")
    assert await svc.get("clave_que_no_existe") == ""
