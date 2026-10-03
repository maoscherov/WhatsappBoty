"""
Auditoría de chats 2/10, puntos 6 a 8: el bot no se mete cuando atiende un
operador, "encargalo" no cobra un sustituto, obras sociales y precios
absurdos del ERP.
"""
import time

import pytest

from app.services import checkout_helper as ch
from app.services.session_service import SessionService

PHONE = "5493410000077"


# ── 6. Operador ────────────────────────────────────────────────────────────────
async def test_operador_escribe_y_el_bot_calla():
    ss = SessionService("redis://127.0.0.1:1")
    await ss.add_message(PHONE, "user", "hola")
    await ss.operador_escribio(PHONE, "Lore")
    s = await ss.get(PHONE)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "operador_escribio"
    assert s["agente"] == "Lore" and s["_operador_at"]


async def test_mucha_demanda_no_sale_si_el_operador_ya_escribio():
    ss = SessionService("redis://127.0.0.1:1")
    await ss.set_estado(PHONE, "operador", motivo="receta")
    s = await ss.get(PHONE)
    s["derivada_at"] = time.time() - 3600
    await ss.save(PHONE, s)
    assert PHONE in await ss.derivadas_para_aviso(60)
    await ss.operador_escribio(PHONE)
    assert PHONE not in await ss.derivadas_para_aviso(60)
    assert PHONE not in await ss.derivadas_sin_atender(60)


@pytest.mark.parametrize("history,accion", [
    ([{"role": "assistant", "content": "¿Retiro o envío?"}], "avisar"),
    ([{"role": "assistant", "content": "¿Querés que lo consulte?"},
      {"role": "user", "content": "Si, necesito saber cuánto sale!"}], "derivar"),
    ([{"role": "user", "content": "ok gracias!"}], "silencio"),
    ([{"role": "user", "content": "lo paso a buscar mañana"},
      {"role": "assistant", "content": "¡Te esperamos!"}], "silencio"),
    ([{"role": "operator", "content": "Listo, te lo preparo"}], "silencio"),
])
def test_cierre_por_inactividad(history, accion):
    assert ch.cierre_por_inactividad(history) == accion


@pytest.mark.parametrize("txt,esperado", [
    ("¡Hola Florencia! Qué bueno verte de nuevo 😊 Te cuento que tengo el Skala.",
     "Te cuento que tengo el Skala."),
    ("¡Hola Laura! Perfecto, en un momento te contactamos.",
     "Perfecto, en un momento te contactamos."),
    ("Hola María tengo curitas", "Hola María tengo curitas"),
    ("¡Hola!", "¡Hola!"),
])
def test_saludo_repetido(txt, esperado):
    assert ch.quitar_saludo_repetido(txt, [{"role": "assistant", "content": "x"}]) == esperado


def test_primer_saludo_se_mantiene():
    t = "¡Hola Laura! Qué bueno verte de nuevo 😊 ¿En qué te ayudo?"
    assert ch.quitar_saludo_repetido(t, [{"role": "user", "content": "hola"}]) == t


# ── 7. Encargo ─────────────────────────────────────────────────────────────────
_NO_LO_TENGO = [{"role": "assistant", "content":
                 "No me figura disponible el protector facial Eximia, puedo encargártelo. "
                 "Tengo el Bagovit Solar FPS 50 a $25.000."}]


@pytest.mark.parametrize("txt", ["sí, encargalo", "dale pedilo", "encargámelo porfa",
                                 "que lo traigan"])
def test_pide_encargo(txt):
    assert ch.pide_encargo(txt, _NO_LO_TENGO)


def test_encargo_sin_falta_de_stock_no_cuenta():
    ofrece = [{"role": "assistant", "content": "Tengo el Bagovit Solar a $25.000, ¿lo querés?"}]
    assert not ch.pide_encargo("encargalo", ofrece)
    assert not ch.pide_encargo("no lo encargues", _NO_LO_TENGO)


# ── 8. Obras sociales y precios absurdos ───────────────────────────────────────
def test_precio_dudoso_no_se_vende_ni_se_cita():
    res = [{"sku_id": "D", "nombre": "DOVE SHA X 200", "precio": 56.9, "vendible": True},
           {"sku_id": "N", "nombre": "NIVEA", "precio": 5000.0, "vendible": True}]
    out = ch.marcar_precio_dudoso(res, {"precio_minimo_venta": "300"})
    assert out[0]["vendible"] is False and out[0]["precio_dudoso"]
    assert out[1]["vendible"] is True and not out[1].get("precio_dudoso")
    assert res[0]["vendible"] is True                       # no muta el catálogo
    unit, tot = ch.referencias_de_precio(out, {}, {})
    assert ch.precios_inventados("El Dove sale $56,90", unit, tot) == [56.9]
    assert ch.marcar_precio_dudoso(res, {"precio_minimo_venta": "0"}) == res
