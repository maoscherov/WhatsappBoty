"""
Stock comprometido por pedidos del bot (C-5596, 16/10): había 1 Roby
Antifrizz y salieron dos pedidos de 1; "quiero 2" eligiendo la entrega no
cambiaba la cantidad; "y quiero otro igual" con el pedido cerrado.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.routers import webhook as wh
from app.services import checkout_helper as ch
from app.services import reservas
from app.services.sku_service import SKUService
from tests.test_webhook_secuencias import PHONE, _Cfg, _Img, _Intent, _Rag, _Rec, _Socios, _Wa, _msg

ROBY = "ROBY ANTIFRIZZ NEW 180 ML FIJ CAB ENV x 1"


def _orden(estado="pendiente", horas=1, **k):
    creado = (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat()
    return {"estado": estado, "created_at": creado, "sku_id": "133099", "cantidad": 1, **k}


def test_reserva_lo_pendiente_de_las_ultimas_48_horas():
    por_sku = reservas.calcular([
        _orden(), _orden("preparado"), _orden("retirado"), _orden(horas=60),
        _orden(transferencia="rechazada"),
        _orden(sku_id="MULTI", items_stock=[{"sku_id": "133099", "cantidad": 2},
                                            {"sku_id": "7", "cantidad": 1}])])
    assert por_sku == {"133099": 4, "7": 1}


def test_la_busqueda_descuenta_lo_reservado():
    reservas.sumar([{"sku_id": "133099", "cantidad": 1}])
    r = reservas.descontar([{"sku_id": "133099", "cantidad_visible": 1, "vendible": True,
                             "sin_stock": False}])[0]
    assert r["sin_stock"] and not r["vendible"]
    r = reservas.descontar([{"sku_id": "133099", "cantidad_visible": 3, "vendible": True,
                             "sin_stock": False}])[0]
    assert r["cantidad_visible"] == 2 and r["vendible"]


def test_cantidades_en_texto():
    assert ch.cantidad_pedida("Quiero 2") == 2
    assert ch.cantidad_pedida("mejor dos") == 2
    assert ch.cantidad_pedida("necesito 3 unidades") == 3
    assert ch.cantidad_pedida("2") is None                     # puede ser una opción
    assert ch.cantidad_pedida("retiro") is None
    assert ch.pide_otro_igual("Y quiero otro igual")
    assert ch.pide_otro_igual("dame uno más")
    assert not ch.pide_otro_igual("quiero otro producto")


@pytest.fixture
def entorno(monkeypatch):
    from app.services.session_service import SessionService

    def armar(stock=1, pedidos=()):
        import app.services.order_service as osmod

        class _Orders:
            async def list_all(self, limit=300):
                return list(pedidos)
        monkeypatch.setattr(osmod, "get_order_service", lambda *a, **k: _Orders())
        deps = {"wa": _Wa(), "sku": SKUService.from_rows([
                    {"external_id": "133099", "name": ROBY, "price": 9107.95,
                     "brand": "Laboratorios Cuenca", "stock": stock}]),
                "session": SessionService("redis://127.0.0.1:1"), "intent": _Intent({}),
                "payment": _Rec(), "audio": _Rec(), "image": _Img(), "perf": _Rec(),
                "config": _Cfg({}), "socios": _Socios(), "msgs": _Rec(), "metrics": _Rec(),
                "rag": _Rag()}
        monkeypatch.setattr(wh, "_deps", lambda s=None: deps)
        return deps

    return armar


async def test_segundo_pedido_frenado_por_lo_reservado():
    from app.services.session_service import SessionService
    ss = SessionService("redis://127.0.0.1:1")
    reservas.sumar([{"sku_id": "133099", "cantidad": 1}])        # el primer pedido
    pedido = {"sku_id": "133099", "nombre": ROBY, "precio": 7286.36, "cantidad": 1}
    msg = await ch._frenar_por_stock(ss, PHONE, {}, pedido, [pedido], 1, "vivo")
    assert msg and "no nos queda stock" in msg


async def test_queda_menos_de_lo_pedido_ofrece_lo_que_hay():
    from app.services.session_service import SessionService
    ss = SessionService("redis://127.0.0.1:1")
    reservas.sumar([{"sku_id": "133099", "cantidad": 1}])
    pedido = {"sku_id": "133099", "nombre": ROBY, "precio": 7286.36, "cantidad": 3}
    msg = await ch._frenar_por_stock(ss, PHONE, {}, pedido, [pedido], 3, "vivo")
    assert "quedan *2*" in msg
    s = await ss.get(PHONE)
    assert s["pending_cantidad"] == 2 and s["estado"] == "esperando_entrega"


async def test_quiero_2_eligiendo_la_entrega_cambia_la_cantidad(entorno):
    deps = entorno(stock=5)
    ss = deps["session"]
    await ss.set_pending(PHONE, "133099", ROBY, 7286.36, 1, opciones=[])
    await ss.set_estado(PHONE, "esperando_entrega")
    await wh.procesar_mensajes([_msg("Quiero 2")])
    s = await ss.get(PHONE)
    assert s["pending_cantidad"] == 2 and s["estado"] == "esperando_entrega"
    assert "*2*" in deps["wa"].enviados[-1]


async def test_quiero_2_con_una_sola_unidad(entorno):
    deps = entorno(stock=1)
    ss = deps["session"]
    await ss.set_pending(PHONE, "133099", ROBY, 7286.36, 1, opciones=[])
    await ss.set_estado(PHONE, "esperando_entrega")
    await wh.procesar_mensajes([_msg("Quiero 2")])
    assert (await ss.get(PHONE))["pending_cantidad"] == 1
    assert "queda *1*" in deps["wa"].enviados[-1]


async def _pedido_cerrado(ss):
    await ss.set_pending(PHONE, "133099", ROBY, 7286.36, 1, opciones=[])
    await ss.set_estado(PHONE, "pedido_confirmado")


async def test_otro_igual_sin_stock_no_lo_vende(entorno):
    deps = entorno(stock=1, pedidos=[_orden()])          # el primer pedido
    await _pedido_cerrado(deps["session"])
    await wh.procesar_mensajes([_msg("Y quiero otro igual")])
    assert "no me quedan" in deps["wa"].enviados[-1]
    assert (await deps["session"].get(PHONE))["estado"] == "pedido_confirmado"


async def test_otro_igual_con_stock_arma_otro_pedido(entorno):
    deps = entorno(stock=3, pedidos=[_orden()])
    await _pedido_cerrado(deps["session"])
    await wh.procesar_mensajes([_msg("Y quiero otro igual")])
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "esperando_entrega" and s["pending_cantidad"] == 1
    assert "Otro" in deps["wa"].enviados[-1]
