"""
El operador arma un pedido desde el backoffice (1/10): varios productos,
retiro o envío, link / efectivo (mostrador o repartidor) / cuenta corriente,
y alta de socio en el mismo paso.
"""
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app.services.db as dbmod
import app.services.order_service as osmod
from app.config import get_settings
from app.routers import backoffice_pedidos as bp
from app.services.config_service import DEFAULTS
from app.services.db import Database
from app.services.session_service import SessionService
from app.services.sku_service import SKUService

PHONE = "5493415807742"


class _Wa:
    def __init__(self):
        self.enviados = []

    async def send_text(self, phone, texto, **k):
        self.enviados.append(texto)
        return True


class _Pay:
    async def crear_link(self, **k):
        self.ultimo = k
        return "https://pago.test/abc", None


class _Orders:
    def __init__(self):
        self.creados = []

    async def create(self, **k):
        o = {"order_id": f"P{len(self.creados) + 1}", "pickup_code": "1234", **k}
        self.creados.append(o)
        return o


class _Socios:
    def __init__(self):
        self.socios = {}

    def find_by_phone(self, phone):
        return self.socios.get(phone)

    def contexto_para_prompt(self, phone):
        return None


class _Cfg:
    def __init__(self, extra=None):
        self.v = {**DEFAULTS, "envio_costo": "2000", "socio_discount_pct": "10", **(extra or {})}

    async def get_all(self):
        return dict(self.v)


class _Metrics:
    async def evento(self, *a, **k):
        pass


def _filas():
    base = {"hash": "a" * 64, "barcodes": [], "troquel": None, "brand": "", "drug": None,
            "form": None, "category": "Perfumeria", "rubro": "", "subrubro": "",
            "therapeutic_actions": [], "stock": 5, "visible": True, "active": True,
            "requiere_receta": "no", "source": "t"}
    return [{**base, "external_id": "1", "name": "COLGATE ULTRA BLANCO x 90", "price": 1000.0},
            {**base, "external_id": "2", "name": "JABON DOVE x 90", "price": 500.0},
            {**base, "external_id": "3", "name": "FEMIDEN COM x 28", "price": 35619.17,
             "requiere_receta": "si", "category": "Medicamentos Bajo Receta"}]


@pytest.fixture
def entorno(monkeypatch):
    from app.routers import webhook as wh
    from app.services import message_store as ms
    deps = {"wa": _Wa(), "payment": _Pay(), "session": SessionService("redis://127.0.0.1:1"),
            "sku": SKUService.from_rows(_filas()), "socios": _Socios(), "config": _Cfg(),
            "metrics": _Metrics()}
    monkeypatch.setattr(wh, "_deps", lambda s=None: deps)
    orders = _Orders()
    monkeypatch.setattr(osmod, "get_order_service", lambda *a, **k: orders)
    hist = []

    async def _hist(phone, role, content, autor=None, **k):
        hist.append((role, content, autor))
    monkeypatch.setattr(ms, "guardar_historico", _hist)
    monkeypatch.setattr(get_settings(), "bo_key", "CLAVE")
    deps["orders"], deps["hist"] = orders, hist
    return deps


@pytest.fixture
async def cli(entorno):
    app = FastAPI()
    app.include_router(bp.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t",
                           headers={"x-bo-key": "CLAVE"}) as ac:
        yield ac


async def test_efectivo_con_repartidor_varios_productos(cli, entorno):
    r = await cli.post("/bo/pedido", json={
        "phone": PHONE, "items": [{"sku_id": "1", "cantidad": 2}, {"detalle": "Pañales x30", "monto": 3000}],
        "entrega": "envio", "direccion": "San Javier 837", "pago": "efectivo",
        "repartidor": "Juan", "agente": "Lore"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["total"] == 2000 + 3000 + 2000                # productos + envío
    o = entorno["orders"].creados[-1]
    assert o["pago"] == "efectivo" and o["tipo_entrega"] == "envio"
    assert o["extra"] == {"origen": "operador", "armado_por": "Lore", "cobra": "repartidor",
                          "repartidor": "Juan"}
    assert j["pedido"] == o["order_id"]
    assert entorno["wa"].enviados[-1] == j["mensaje"]
    assert entorno["hist"][-1] == ("operator", j["mensaje"], "Lore")
    s = await entorno["session"].get(PHONE)
    assert s.get("estado") == "pedido_confirmado" and not s.get("agente")


async def test_cuenta_corriente_a_no_socio_y_queda_con_el_operador(cli, entorno):
    r = await cli.post("/bo/pedido", json={
        "phone": PHONE, "items": [{"sku_id": "2"}], "pago": "cuenta_corriente",
        "devolver_al_bot": False, "agente": "Lore"})
    assert r.status_code == 200, r.text
    o = entorno["orders"].creados[-1]
    assert o["pago"] == "cuenta_corriente" and o["total"] == 500
    s = await entorno["session"].get(PHONE)
    assert s["estado"] == "operador" and s["agente"] == "Lore"


async def test_link_con_descuento_de_socio(cli, entorno):
    entorno["socios"].socios[PHONE] = {"nombre": "Ana", "domicilio": "Córdoba 1000"}
    r = await cli.post("/bo/pedido", json={
        "phone": PHONE, "items": [{"sku_id": "1"}, {"sku_id": "2", "monto": 800}],
        "entrega": "envio", "pago": "link"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["link"] == "https://pago.test/abc" and j["descuento_pct"] == 10
    # Colgate con 10% (900) + Dove a precio fijado por el operador (800) + envío
    assert j["total"] == 900 + 800 + 2000
    assert "https://pago.test/abc" in j["mensaje"]
    assert "Córdoba 1000" in j["mensaje"]                  # domicilio del socio


async def test_validaciones(cli):
    assert (await cli.post("/bo/pedido", json={"phone": PHONE, "items": []})).status_code == 422
    assert (await cli.post("/bo/pedido", json={"phone": PHONE, "items": [{"sku_id": "999"}]})).status_code == 404
    assert (await cli.post("/bo/pedido", json={"phone": PHONE, "items": [{"detalle": "x"}]})).status_code == 422
    r = await cli.post("/bo/pedido", json={"phone": PHONE, "items": [{"sku_id": "1"}], "entrega": "envio"})
    assert r.status_code == 422 and "dirección" in r.json()["detail"]


# ── Alta de socio (Postgres) ────────────────────────────────────────────────────
@pytest.fixture
async def db(pg_dsn):
    d = Database(pg_dsn)
    assert await d.connect()
    await d.execute("TRUNCATE socios RESTART IDENTITY")
    prev = dbmod._instance
    dbmod._instance = d
    yield d
    dbmod._instance = prev
    await d.close()


async def test_alta_de_socio(cli, db, monkeypatch, tmp_path):
    from app.services.socio_service import SocioService
    svc = SocioService(str(tmp_path / "no-existe.xlsx"))
    monkeypatch.setattr(bp, "get_socio_service", lambda *a, **k: svc)
    r = await cli.post("/bo/socios/alta", json={"phone": PHONE, "nombre": "Claudia",
                                                "apellido": "Muff", "dni": "20.123.456",
                                                "domicilio": "San Javier 837"})
    assert r.status_code == 200, r.text
    assert svc.find_by_phone(PHONE)["nombre_pila"] == "Claudia"     # el bot lo reconoce ya
    filas = await db.fetch("SELECT celular, dni FROM socios")
    assert [(f["celular"], f["dni"]) for f in filas] == [("3415807742", "20123456")]
    r2 = await cli.post("/bo/socios/alta", json={"phone": PHONE, "nombre": "Otra"})
    assert r2.status_code == 409


async def test_receta_validada_por_el_operador_lleva_el_descuento(cli, entorno):
    """Femiden 2/10: la cotización de receta aplicaba el 20% y el pedido
    armado no — el cliente vio un precio y el link cobró otro."""
    entorno["socios"].socios[PHONE] = {"nombre": "Laura"}
    r = await cli.post("/bo/pedido", json={
        "phone": PHONE, "items": [{"sku_id": "3"}], "pago": "link"})
    assert r.status_code == 200, r.text
    assert r.json()["total"] == round(35619.17 * 0.9, 2)
