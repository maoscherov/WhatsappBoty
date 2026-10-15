"""
Estados por los que pasó cada conversación (15/10), con secuencias reales:
C-4917 (derivada fuera de horario, nadie la atendió), C-4232 (venta a cuenta
corriente armada por el operador), C-4660 (venta en efectivo).
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.services import estados_conversacion as ec
from app.services.db import Database

T0 = datetime(2026, 10, 12, 0, 11, tzinfo=timezone.utc)


def _ev(min_, tipo, dato=None, ref=None):
    return {"tipo": tipo, "dato": dato, "ref": ref, "created_at": T0 + timedelta(minutes=min_)}


def test_derivada_sin_atender():
    """C-4917: "pago con transferencia" a las 21:13, nadie la tomó."""
    r = ec.armar([_ev(0, "fuera_horario"), _ev(1, "fuera_horario"), _ev(1, "link_enviado"),
                  _ev(2, "derivacion", "transferencia_efectivo")], [], T0)
    assert r["estado_final"] == "derivada_sin_atender"
    cods = [e["estado"] for e in r["estados"]]
    assert cods == ["bot", "fuera_horario", "link", "derivada"]       # fuera de horario una vez
    assert r["estados"][-1]["detalle"] == "pago por transferencia"


def test_venta_a_cuenta_corriente_armada_por_el_operador():
    """C-4232: el bot cargó un pedido, después Claudia tomó y armó otro."""
    r = ec.armar([_ev(2, "link_enviado"), _ev(2, "pago_aprobado", "cuenta_corriente"),
                  _ev(5, "derivacion", "tomada_por_operador"),
                  _ev(5, "conversacion_tomada", "tomada_por_operador", "claudia"),
                  _ev(18, "pago_aprobado", "cuenta_corriente"),
                  _ev(18, "pedido_operador", "cuenta_corriente", "claudia")],
                 [T0 + timedelta(minutes=6)], T0)
    assert r["estado_final"] == "venta"
    ventas = [e for e in r["estados"] if e["estado"] == "venta"]
    assert ventas[0]["detalle"] == "a cuenta corriente"
    assert ventas[-1]["detalle"].startswith("armada por claudia") and ventas[-1]["agente"] == "claudia"
    tomadas = [e for e in r["estados"] if e["estado"] == "tomada"]
    assert len(tomadas) == 1 and tomadas[0]["etiqueta"] == "Tomada por claudia"


def test_venta_en_efectivo():
    r = ec.armar([_ev(1, "pedido_efectivo", "retiro")], [], T0)
    assert r["estado_final"] == "venta" and r["estados"][-1]["detalle"] == "en efectivo"


def test_derivada_y_atendida_por_mensaje_del_operador():
    """Escribió sin tomar: igual cuenta como atendida."""
    r = ec.armar([_ev(1, "derivacion", "receta")], [T0 + timedelta(minutes=3)], T0)
    assert r["estado_final"] == "atendida_devuelta"


def test_con_operador_y_devuelta():
    r = ec.armar([_ev(1, "derivacion", "receta"), _ev(2, "conversacion_tomada", None, "Lore")], [], T0)
    assert r["estado_final"] == "con_operador"
    r = ec.armar([_ev(1, "derivacion", "receta"), _ev(2, "conversacion_tomada", None, "Lore"),
                  _ev(9, "conversacion_devuelta", None, "Lore")], [], T0)
    assert r["estado_final"] == "atendida_devuelta"
    assert r["estados"][-1]["etiqueta"] == "Devuelta al bot por Lore"


def test_link_sin_pagar_y_cierres():
    assert ec.armar([_ev(1, "link_enviado")], [], T0)["estado_final"] == "link_sin_pagar"
    assert ec.armar([_ev(20, "conversacion_cerrada", "avisar")], [], T0)["estado_final"] \
        == "cerrada_sin_respuesta"
    assert ec.armar([_ev(20, "conversacion_cerrada", "silencio")], [], T0)["estado_final"] \
        == "resuelta_bot"
    devuelta_auto = ec.armar([_ev(1, "derivacion", "sin_stock"),
                              _ev(30, "conversacion_devuelta", "sin_atender", "automática")], [], T0)
    assert devuelta_auto["estados"][-1]["detalle"] == "automática (nadie la tomó)"


@pytest.fixture
async def db(pg_dsn):
    d = Database(pg_dsn)
    assert await d.connect()
    yield d
    await d.execute("DELETE FROM messages WHERE phone = '549000777'")
    await d.execute("DELETE FROM eventos WHERE phone = '549000777'")
    await d.close()


async def test_cada_evento_va_a_su_conversacion(db):
    """Dos conversaciones del mismo cliente: la derivación de la primera no
    contamina a la segunda (que terminó en venta)."""
    for cid, minutos, role in ((901, 0, "user"), (901, 1, "assistant"),
                               (902, 300, "user"), (902, 301, "assistant")):
        await db.execute("INSERT INTO messages (phone, role, content, created_at, conversacion_id) "
                         "VALUES ('549000777', $1, 'x', $2, $3)", role, T0 + timedelta(minutes=minutos), cid)
    for minutos, tipo, dato in ((1, "derivacion", "receta"), (302, "pago_aprobado", "mercadopago")):
        await db.execute("INSERT INTO eventos (tipo, phone, dato, created_at) VALUES ($1, '549000777', $2, $3)",
                         tipo, dato, T0 + timedelta(minutes=minutos))
    ult = await ec.ultima_conversacion_por_phone(db, ["549000777"])
    assert [u["id"] for u in ult] == [902]
    convs = [{"id": 901, "phone": "549000777", "desde": T0, "hasta": T0 + timedelta(minutes=1)},
             {"id": 902, "phone": "549000777", "desde": T0 + timedelta(minutes=300),
              "hasta": T0 + timedelta(minutes=301)}]
    est = await ec.por_conversaciones(db, ["549000777"], convs)
    assert est[901]["estado_final"] == "derivada_sin_atender"
    assert est[902]["estado_final"] == "venta"
