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


# ── Fallbacks del código → perfil.textos ────────────────────────────────────────
@pytest.fixture
def perfil_con_textos():
    """Perfil farmacia (todas las capacidades prendidas) con textos propios:
    distingue un fallback que lee perfil.textos del literal de hoy. Igual que
    usar_perfil (Task 1), pisa atributos del Settings cacheado y nunca hace
    get_settings.cache_clear(): un Settings nuevo levantaría el DATABASE_URL
    que pg_dsn deja en el entorno."""
    from app.config import get_settings
    from app.services import perfil as perfil_mod
    mp = pytest.MonkeyPatch()

    def _usar(**textos):
        base = perfil_mod.perfil_por_clave("farmacia")
        propio = replace(base, textos=MappingProxyType({**base.textos, **textos}))
        mp.setattr(perfil_mod, "_registro", lambda: {"farmacia": propio})
        mp.setattr(get_settings(), "vertical", "farmacia")
        mp.setattr(get_settings(), "comercio_nombre", "")
        perfil_mod.get_perfil.cache_clear()
        return perfil_mod.get_perfil()

    yield _usar
    mp.undo()
    perfil_mod.get_perfil.cache_clear()


_ORDER = {"sku_nombre": "Piedras Sanicat 4 kg", "cantidad": 1, "total": 6200.0,
          "pickup_code": "445566", "tipo_entrega": "retiro", "direccion_envio": None}


def test_fallback_pedido_listo_lee_el_perfil(perfil_con_textos):
    from app.routers.orders_api import armar_mensaje_pedido_listo
    perfil_con_textos(pedido_listo_retiro_message="RETIRO {codigo}",
                      pedido_listo_envio_message="ENVIO {direccion}")
    assert armar_mensaje_pedido_listo(_ORDER, {}) == "RETIRO 445566"
    envio = dict(_ORDER, tipo_entrega="envio", direccion_envio="Mitre 100")
    assert armar_mensaje_pedido_listo(envio, {}) == "ENVIO Mitre 100"


@pytest.mark.parametrize("tipo", ["retiro", "envio"])
def test_pedido_listo_con_clave_vacia_petshop_y_farmacia(usar_perfil, tipo):
    from app.routers.orders_api import armar_mensaje_pedido_listo
    from app.services.config_service import DEFAULTS
    order = dict(_ORDER, tipo_entrega=tipo, direccion_envio="Mitre 100")
    clave = f"pedido_listo_{tipo}_message"

    usar_perfil("petshop")
    msg = armar_mensaje_pedido_listo(order, {clave: ""})
    assert msg.endswith("🐾") and "💊" not in msg

    usar_perfil("farmacia")                                   # igual que hoy
    assert armar_mensaje_pedido_listo(order, {clave: ""}) == \
        armar_mensaje_pedido_listo(order, {clave: DEFAULTS[clave]})


class _RedisOrdenes:
    def __init__(self):
        self.kv, self.idx = {}, []

    async def setex(self, k, ttl, v):
        self.kv[k] = v

    async def zadd(self, key, mapping):
        self.idx.extend(mapping.keys())

    async def zrevrange(self, key, a, b):
        return list(reversed(self.idx))[a:b + 1]

    async def mget(self, keys):
        return [self.kv.get(k) for k in keys]

    async def get(self, k):
        return self.kv.get(k)


async def _cerrar_efectivo(monkeypatch, tipo_entrega, direccion=None):
    import app.services.order_service as omod
    from app.services import checkout_helper as ch
    from app.services.session_service import SessionService
    ordenes = omod.OrderService("redis://127.0.0.1:1")
    ordenes._redis = _RedisOrdenes()
    monkeypatch.setattr(omod, "_instance", ordenes)
    ss = SessionService("redis://127.0.0.1:1")
    ph = "5493415550299"
    await ss.set_pending(ph, sku_id="S1", sku_nombre="Piedras Sanicat 4 kg", precio=6200.0,
                         cantidad=1, opciones=[])
    return await ch._cerrar_venta_efectivo(ss, ph, await ss.get(ph), tipo_entrega,
                                           direccion, 6200.0, cfg={})


async def test_fallback_efectivo_lee_el_perfil(perfil_con_textos, monkeypatch):
    perfil_con_textos(efectivo_retiro_message="EFECTIVO RETIRO {producto}",
                      efectivo_envio_message="EFECTIVO ENVIO {direccion}")
    assert await _cerrar_efectivo(monkeypatch, "retiro") == "EFECTIVO RETIRO Piedras Sanicat 4 kg"
    assert await _cerrar_efectivo(monkeypatch, "envio", "Mitre 100") == "EFECTIVO ENVIO Mitre 100"


@pytest.mark.parametrize("tipo,direccion", [("retiro", None), ("envio", "Mitre 100")])
async def test_efectivo_con_clave_vacia_petshop(usar_perfil, monkeypatch, tipo, direccion):
    usar_perfil("petshop")
    msg = await _cerrar_efectivo(monkeypatch, tipo, direccion)
    assert msg.endswith("¡Muchas gracias! 🐾") and "💊" not in msg


def test_fallback_sintoma_farmaceutico_lee_el_perfil(perfil_con_textos):
    from app.services.checkout_helper import agregar_oferta_farmaceutico
    perfil_con_textos(sintoma_farmaceutico_message="EXTRA DEL PERFIL")
    assert agregar_oferta_farmaceutico("Te ofrezco Tafirol $1.200", {}) == \
        "Te ofrezco Tafirol $1.200\n\nEXTRA DEL PERFIL"


def test_sintoma_farmaceutico_vacio_petshop_no_agrega_y_farmacia_como_hoy(usar_perfil):
    from app.services.checkout_helper import agregar_oferta_farmaceutico
    from app.services.config_service import DEFAULTS
    usar_perfil("petshop")
    assert agregar_oferta_farmaceutico(
        "Te ofrezco Pipeta X", {"sintoma_farmaceutico_message": ""}) == "Te ofrezco Pipeta X"
    usar_perfil("farmacia")
    assert agregar_oferta_farmaceutico(
        "Te ofrezco Pipeta X", {"sintoma_farmaceutico_message": ""}) == \
        "Te ofrezco Pipeta X\n\n" + DEFAULTS["sintoma_farmaceutico_message"]


def test_fallback_bonos_leen_el_perfil(perfil_con_textos):
    from app.services.checkout_helper import responder_bono
    perfil_con_textos(bono_recibido_message="RECIBIDO {laboratorio}",
                      bono_consulta_si_message="SI {laboratorio}",
                      bono_no_reconocido_message="NO RECONOCIDO")
    cfg = {"bonos_laboratorios": "Cassará"}
    assert responder_bono("cassara", cfg, por_foto=True) == ("RECIBIDO Cassará", True)
    assert responder_bono("cassara", cfg) == ("SI Cassará", True)
    assert responder_bono("Bagó", cfg, por_foto=True) == ("NO RECONOCIDO", False)


async def test_fallback_receta_recibida_lee_el_perfil(entorno, perfil_con_textos):
    perfil_con_textos(receta_recibida_message="RECETA DEL PERFIL")
    deps = entorno(img_tipo="receta", cfg={"receta_recibida_message": ""})
    await wh.procesar_mensajes([_msg("", tipo="image", media_url="https://kapso/r.jpg")])
    assert deps["wa"].enviados[-1] == "RECETA DEL PERFIL"


async def test_fallback_comprobante_lee_el_perfil(entorno, perfil_con_textos):
    perfil_con_textos(comprobante_recibido_message="COMPROBANTE DEL PERFIL")
    deps = entorno(img_tipo="comprobante", cfg={"comprobante_recibido_message": ""})
    await wh.procesar_mensajes([_msg("", tipo="image", media_url="https://kapso/c.jpg")])
    assert deps["wa"].enviados[-1] == "COMPROBANTE DEL PERFIL"


async def test_comprobante_petshop_sin_texto_guardado(entorno, usar_perfil):
    """Sin {nombre}: MO no tiene padrón y hoy saldría "¡Listo !"."""
    usar_perfil("petshop")
    deps = entorno(img_tipo="comprobante", cfg={"comprobante_recibido_message": ""})
    await wh.procesar_mensajes([_msg("", tipo="image", media_url="https://kapso/c.jpg")])
    assert deps["wa"].enviados[-1] == (
        "¡Listo! Recibimos tu comprobante 🙌 Lo verificamos y te confirmamos en un rato.")


async def test_fallback_descuentos_leen_el_perfil(entorno, perfil_con_textos):
    perfil_con_textos(socio_discount_off_message="OFF DEL PERFIL",
                      socio_discount_info_message="INFO {pct}% DEL PERFIL")
    deps = entorno(cfg={"socio_discount_off_message": ""})
    await wh.procesar_mensajes([_msg("tienen descuento?")])
    assert deps["wa"].enviados[-1] == "OFF DEL PERFIL"

    deps = entorno(cfg={"socio_discount_pct": "10", "socio_discount_info_message": ""})
    await wh.procesar_mensajes([_msg("tienen descuento?")])
    assert deps["wa"].enviados[-1] == "INFO 10% DEL PERFIL"
