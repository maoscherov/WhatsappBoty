"""
Cotización de receta con VARIOS productos (5/10). Antes la pantalla del
operador admitía uno solo; ahora /bo/paylink acepta `items`, cotiza cada uno
(precio − % obra social − % socio/empleado) y arma el pedido con todos.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import checkout_helper as ch
from app.services.session_service import get_session_service

PHONE = "5490000000555"


@pytest.fixture
def empleado(monkeypatch):
    monkeypatch.setattr(ch, "descuento_para", lambda phone, cfg, socio_svc=None: (20.0, "empleado"))


def _post(**body):
    return TestClient(app).post("/bo/paylink", json={"phone": PHONE, **body})


def test_cotiza_varios_con_descuento_de_empleado(empleado):
    r = _post(items=[{"detalle": "Sinalgico 20 mg x10", "monto": 1000, "pct_os": 40},
                     {"detalle": "Aceite de argán", "monto": 196.51, "cantidad": 2}],
              plantilla="receta", modo="cotizar", enviar=False)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["descuento"] == {"pct": 20.0, "tipo": "empleado"}
    # Sinalgico: 1000 − 40% OS − 20% = 480; argán: 196.51 − 20% = 157.21 (x2)
    assert [l["precio_final"] for l in b["items"]] == [480.0, 157.21]
    assert b["total"] == round(480 + 157.21 * 2, 2)
    assert "Sinalgico 20 mg x10" in b["mensaje"] and "Aceite de argán x2" in b["mensaje"]
    assert "Total: *$794.42*" in b["mensaje"]
    assert "http" not in b["mensaje"]


def test_enviar_arma_el_pedido_con_todos(empleado):
    ss = get_session_service("redis://127.0.0.1:1")
    asyncio.run(ss.set_estado(PHONE, "operador", motivo="receta"))
    r = _post(items=[{"detalle": "A", "monto": 1000}, {"detalle": "B", "monto": 500, "cantidad": 3}],
              plantilla="receta", modo="cotizar", delegar=True, enviar=True)
    assert r.status_code == 200, r.text
    s = asyncio.run(ss.get(PHONE))
    assert s["receta_validada"] is True and s["estado"] == "esperando_confirmacion"
    assert [(i["nombre"], i["precio"], i["cantidad"]) for i in s["pending_items"]] == \
        [("A", 800.0, 1), ("B", 400.0, 3)]
    asyncio.run(ss.clear_pending(PHONE))


def test_item_libre_sin_monto_es_error():
    r = _post(items=[{"detalle": "X"}], modo="cotizar", enviar=False)
    assert r.status_code == 422


def test_un_solo_producto_sigue_igual_y_trae_el_descuento(empleado):
    r = _post(detalle="Yasminelle", monto=1000, plantilla="receta", modo="cotizar", enviar=False)
    b = r.json()
    assert b["ok"] and "800" in b["mensaje"]
    assert b["descuento"]["tipo"] == "empleado" and "tipo_cliente" in b


def test_cotizar_un_sku_x_n_muestra_el_total_que_se_cobra():
    """Re-revisión de la ronda 2: la sesión (y el link, el snapshot y el ERP)
    usan el unitario redondeado x cantidad; el mensaje de la cotización tiene
    que mostrar ese mismo total, no el del unitario sin redondear (los precios
    de Mercurio traen más de 2 decimales). CAMBIO QUE TAMBIÉN AFECTA A LA
    FARMACIA: a lo sumo unos centavos en el texto de la cotización."""
    r = _post(detalle="Bolsa", monto=1000.005, cantidad=3, modo="cotizar", enviar=False)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["total"] == round(round(1000.005, 2) * 3, 2)
    assert f"${b['total']:,.2f}" in b["mensaje"]
