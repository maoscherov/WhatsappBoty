"""
Pago por transferencia (minuta 14/10): alias configurable, pedido pendiente de
comprobante, OCR del comprobante y aprobación automática por debajo de un monto.
"""
from datetime import date, datetime, timezone

import pytest

import app.services.order_service as osmod
from app.routers import webhook as wh
from app.services import transferencia as trf
from app.services.sku_service import SKUService
from tests.test_webhook_secuencias import PHONE, _Cfg, _Intent, _Rag, _Rec, _Socios, _Wa, _msg

CFG = {"transferencia_alias": "tafirol.farma", "transferencia_titular": "Mutual Independencia",
       "transferencia_auto_max": "50000", "efectivo_enabled": "true"}
HOY = (datetime.utcnow()).date()


def _info(**k):
    base = {"importe": "$ 9.892,47", "fecha": HOY.strftime("%d/%m/%Y"),
            "destinatario": "MUTUAL INDEPENDENCIA", "cuenta_destino": "tafirol.farma",
            "operacion": "123456789", "origen": "Micaela"}
    base.update(k)
    return base


# ── Reglas ───────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("valor,esperado", [
    ("$ 26.863,96", 26863.96), ("26863.96", 26863.96), ("$26,863.96", 26863.96),
    ("$ 9.892", 9892.0), (12000, 12000.0), ("", None)])
def test_importe(valor, esperado):
    assert trf.importe_de(valor) == esperado


def test_fecha():
    assert trf.fecha_de("15/10/2026") == date(2026, 10, 15)
    assert trf.fecha_de("15 oct 2026 - 10:32") == date(2026, 10, 15)
    assert trf.fecha_de("2026-10-15T10:32") == date(2026, 10, 15)


def test_validar():
    hoy = HOY
    assert trf.validar(_info(), 9892.47, CFG, hoy) == (True, "ok")
    assert not trf.validar(_info(importe="$ 5.000"), 9892.47, CFG, hoy)[0]             # importe
    assert "supera" in trf.validar(_info(importe="$ 60.000"), 60000, CFG, hoy)[1]       # máximo
    assert "desactivada" in trf.validar(_info(), 9892.47, {**CFG, "transferencia_auto_max": "0"}, hoy)[1]
    assert "fecha" in trf.validar(_info(fecha="01/09/2026"), 9892.47, CFG, hoy)[1]
    assert "destinatario" in trf.validar(_info(destinatario="Juan Perez", cuenta_destino="juan.mp"),
                                         9892.47, CFG, hoy)[1]
    assert not trf.validar(None, 100, CFG, hoy)[0]


# ── Flujo completo ───────────────────────────────────────────────────────────────
class _Orders:
    def __init__(self):
        self.o = {}

    async def create(self, **k):
        oid = f"P{len(self.o) + 1}"
        o = {"order_id": oid, "pickup_code": "4321", "estado": "pendiente",
             "created_at": datetime.now(timezone.utc).isoformat(), **k, **(k.pop("extra", None) or {})}
        o.update(k.get("extra") or {})
        self.o[oid] = o
        return o

    async def get(self, oid):
        return self.o.get(oid)

    async def actualizar(self, oid, **c):
        self.o[oid].update(c)
        return self.o[oid]

    async def list_all(self, limit=300):
        return list(self.o.values())


class _Img:
    def __init__(self, info):
        self.info = info

    async def analizar(self, data, mime):
        return {"tipo": "comprobante", "items": ""}

    async def leer_comprobante(self, data, mime):
        return self.info


@pytest.fixture
def entorno(monkeypatch):
    from app.services.session_service import SessionService
    orders = _Orders()
    monkeypatch.setattr(osmod, "get_order_service", lambda *a, **k: orders)

    def armar(info=None, cfg=None):
        deps = {
            "wa": _Wa(), "sku": SKUService.from_rows([{"external_id": "4", "name": "EVAGINA JLI x 200",
                                                       "price": 9892.47, "brand": "Evagina", "stock": 3}]),
            "session": SessionService("redis://127.0.0.1:1"), "intent": _Intent({}),
            "payment": _Rec(), "audio": _Rec(), "image": _Img(info or _info()), "perf": _Rec(),
            "config": _Cfg({**CFG, **(cfg or {})}), "socios": _Socios(), "msgs": _Rec(),
            "metrics": _Rec(), "rag": _Rag()}
        monkeypatch.setattr(wh, "_deps", lambda s=None: deps)
        import app.services.config_service as cs
        monkeypatch.setattr(cs, "get_config_service", lambda *a, **k: deps["config"])

        async def _bajar(url):
            return b"\x89PNG comprobante"
        monkeypatch.setattr(wh, "_descargar_url", _bajar)
        deps["orders"] = orders
        return deps

    return armar


async def _pedido_y_transferencia(deps):
    ss = deps["session"]
    await ss.set_pending(PHONE, "4", "EVAGINA JLI x 200", 9892.47, 1, opciones=[])
    s = await ss.get(PHONE)
    s["history"] = [{"role": "assistant", "content": "¿Cómo preferís recibirlo?"}]
    await ss.save(PHONE, s)
    await wh.procesar_mensajes([_msg("Lo retiro en sucursal y pago con transferencia")])


async def test_elegir_transferencia_crea_el_pedido_y_pasa_el_alias(entorno):
    deps = entorno()
    await _pedido_y_transferencia(deps)
    o = list(deps["orders"].o.values())[-1]
    assert o["pago"] == "transferencia" and o["transferencia"] == trf.PENDIENTE
    assert o["tipo_entrega"] == "retiro"
    assert "tafirol.farma" in deps["wa"].enviados[-1] and "9,892.47" in deps["wa"].enviados[-1]
    assert (await deps["session"].get(PHONE))["estado"] == "esperando_comprobante"


async def test_comprobante_valido_aprueba_el_pedido(entorno):
    deps = entorno()
    await _pedido_y_transferencia(deps)
    await wh.procesar_mensajes([_msg("", tipo="image", media_url="https://x/c.png", mime="image/png")])
    o = list(deps["orders"].o.values())[-1]
    assert o["transferencia"] == trf.APROBADA and o["comprobante"]["operacion"] == "123456789"
    assert "Recibimos tu transferencia" in deps["wa"].enviados[-1]
    assert "4321" in deps["wa"].enviados[-1]                      # código de retiro
    assert (await deps["session"].get(PHONE))["estado"] == "pedido_confirmado"


async def test_comprobante_que_no_cierra_pasa_a_una_persona(entorno):
    deps = entorno(info=_info(importe="$ 5.000"))
    await _pedido_y_transferencia(deps)
    await wh.procesar_mensajes([_msg("", tipo="image", media_url="https://x/c.png", mime="image/png")])
    o = list(deps["orders"].o.values())[-1]
    assert o["transferencia"] == trf.A_VERIFICAR and "importe" in o["transferencia_motivo"]
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador" and s["comprobante_info"]["motivo"]
    assert "verificamos" in deps["wa"].enviados[-1]


async def test_ya_transferi_pide_el_comprobante(entorno):
    deps = entorno()
    await _pedido_y_transferencia(deps)
    await wh.procesar_mensajes([_msg("ya te transferí")])
    assert "comprobante" in deps["wa"].enviados[-1]


async def test_pasame_el_alias_sin_pedido(entorno):
    deps = entorno()
    await wh.procesar_mensajes([_msg("me pasas alias para transferir")])
    assert "tafirol.farma" in deps["wa"].enviados[-1]


async def test_sin_alias_sigue_derivando(entorno):
    deps = entorno(cfg={"transferencia_alias": ""})
    await _pedido_y_transferencia(deps)
    assert (await deps["session"].get(PHONE))["estado"] == "operador"
    assert not deps["orders"].o


async def test_operador_aprueba_la_transferencia(entorno, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.config import get_settings
    from app.routers import orders_api
    deps = entorno(info=_info(importe="$ 5.000"))
    await _pedido_y_transferencia(deps)
    await wh.procesar_mensajes([_msg("", tipo="image", media_url="https://x/c.png", mime="image/png")])
    oid = list(deps["orders"].o)[-1]
    monkeypatch.setattr(orders_api, "get_order_service", lambda *a, **k: deps["orders"])
    monkeypatch.setattr(orders_api, "get_whatsapp_service", lambda *a, **k: deps["wa"])
    monkeypatch.setattr(get_settings(), "bo_key", "CLAVE")
    app = FastAPI()
    app.include_router(orders_api.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as cli:
        r = await cli.patch(f"/orders/api/{oid}/transferencia?key=CLAVE",
                            json={"accion": "aprobar", "agente": "claudia"})
    assert r.status_code == 200, r.text
    assert deps["orders"].o[oid]["transferencia"] == trf.APROBADA
    assert "4321" in deps["wa"].enviados[-1]
