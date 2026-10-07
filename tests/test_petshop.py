"""
Petshop por capacidad (spec §6.2, `tests/test_petshop.py`): cada test de
petshop tiene su par de farmacia "igual que hoy". Los perfiles se eligen con la
fixture `usar_perfil` (tests/conftest.py).
"""

# ── Recetas: _formatear_productos (intent_service) ──────────────────────────────
_PIPETA = {"nombre": "PIPETA FRONTLINE PLUS PERRO 10-20KG", "precio": 15000.0, "estado": "disponible",
           "cantidad_visible": 4, "sku_id": "20", "requiere_receta": "si"}
_PIPETA_URGENTE = {**_PIPETA, "sku_id": "21", "requiere_receta": "ambiguo", "urgente": True}


def test_formatear_productos_petshop_no_marca_receta(usar_perfil):
    from app.services.intent_service import IntentService
    usar_perfil("petshop")
    txt = IntentService("")._formatear_productos([_PIPETA, _PIPETA_URGENTE])
    assert txt == (
        "1. PIPETA FRONTLINE PLUS PERRO 10-20KG | $15000.00 | Disponible (cantidad aprox: 4) | ID: 20\n"
        "2. PIPETA FRONTLINE PLUS PERRO 10-20KG | $15000.00 | Disponible (cantidad aprox: 4)"
        " | STOCK BAJO - ofrecer con urgencia | ID: 21")


def test_formatear_productos_farmacia_marca_receta_como_hoy(usar_perfil):
    from app.services.intent_service import IntentService
    usar_perfil("farmacia")
    txt = IntentService("")._formatear_productos([_PIPETA, _PIPETA_URGENTE])
    assert txt == (
        "1. PIPETA FRONTLINE PLUS PERRO 10-20KG | $15000.00 | Disponible (cantidad aprox: 4)"
        " | REQUIERE RECETA | ID: 20\n"
        "2. PIPETA FRONTLINE PLUS PERRO 10-20KG | $15000.00 | Disponible (cantidad aprox: 4)"
        " | STOCK BAJO - ofrecer con urgencia | REQUIERE RECETA | ID: 21")


# ══════════════════════════════════════════════════════════════════════════════
# Recetas (recetas = False)
# ══════════════════════════════════════════════════════════════════════════════
import pytest

from app.services.sku_service import SKUService

_PIPETA_ERP = ("MEDICAMENTOS", "PERROS", "ANTIPARASITARIOS", "Pipeta Frontline 10-20kg")


def test_explicar_receta_petshop_no_vende_con_receta(usar_perfil):
    from app.services.catalog_rules import ORIGENES, explicar_receta
    usar_perfil("petshop")
    # La referencia diría "si": el perfil sin recetas gana antes de consultarla.
    assert explicar_receta(*_PIPETA_ERP, referencia=lambda b: "si") == ("no", "sin_recetas")
    assert ORIGENES["sin_recetas"] == "Este comercio no vende con receta"


def test_explicar_receta_farmacia_igual_que_hoy(usar_perfil):
    from app.services.catalog_rules import explicar_receta
    usar_perfil("farmacia")
    assert explicar_receta(*_PIPETA_ERP, referencia=lambda b: None) == ("ambiguo", "sin_referencia")
    assert explicar_receta("Medicamentos Bajo Receta", "", "", "Pipeta Frontline 10-20kg",
                           referencia=lambda b: None) == ("si", "categoria_bajo_receta")


def _item_medicamento():
    from app.models.sync import CatalogItemIn
    return CatalogItemIn(external_id="9001", hash="a" * 64,
                         name="PIPETA FRONTLINE PLUS PERRO 10-20KG", category="MEDICAMENTOS",
                         rubro="PERROS", subrubro="ANTIPARASITARIOS",
                         barcodes=["7790000000001"])


def test_fila_del_catalogo_petshop_no_requiere_receta(usar_perfil, monkeypatch):
    from app.services import receta_referencia
    from app.services.catalog_store import _fila
    usar_perfil("petshop")
    monkeypatch.setattr(receta_referencia, "_MAPA", {"7790000000001": "si"})
    fila = _fila("mascotas-oeste", _item_medicamento(), "mercurio")
    assert fila[17] == "no"          # requiere_receta ($18 del upsert)


def test_fila_del_catalogo_farmacia_igual_que_hoy(usar_perfil, monkeypatch):
    from app.services import receta_referencia
    from app.services.catalog_store import _fila
    usar_perfil("farmacia")
    monkeypatch.setattr(receta_referencia, "_MAPA", {})
    fila = _fila("farmacia-centro", _item_medicamento(), "observer-gestion")
    assert fila[17] == "ambiguo"


def _catalogo_con_receta():
    base = {"hash": "a" * 64, "troquel": None, "brand": "", "drug": None, "form": None,
            "rubro": "PERROS", "subrubro": "", "therapeutic_actions": [], "stock": 5,
            "visible": True, "active": True, "source": "mercurio"}
    return SKUService.from_rows([
        {**base, "external_id": "20", "name": "PIPETA FRONTLINE PLUS PERRO 10-20KG",
         "price": 25000, "barcodes": ["7790000000020"],
         "category": "Medicamentos Bajo Receta", "requiere_receta": "si"},
        {**base, "external_id": "21", "name": "DRONTAL PLUS PERRO X 2", "price": 9000,
         "barcodes": [], "category": "MEDICAMENTOS", "requiere_receta": "ambiguo"},
    ])


@pytest.mark.parametrize("modo", ["conservador", "estricto"])
def test_necesita_receta_petshop_nunca(usar_perfil, modo):
    from app.services.checkout_helper import necesita_receta
    usar_perfil("petshop")
    sku = _catalogo_con_receta()
    assert necesita_receta(sku, "20", modo) is False
    assert necesita_receta(sku, "21", modo) is False


def test_necesita_receta_farmacia_igual_que_hoy(usar_perfil):
    from app.services.checkout_helper import necesita_receta
    usar_perfil("farmacia")
    sku = _catalogo_con_receta()
    assert necesita_receta(sku, "20", "conservador") is True
    assert necesita_receta(sku, "20", "estricto") is True
    assert necesita_receta(sku, "21", "conservador") is True
    assert necesita_receta(sku, "21", "estricto") is False


_ALTS_RECETA = [
    {"sku_id": "20", "nombre": "Pipeta Frontline Plus Perro 10-20kg", "precio": 25000.0,
     "requiere_receta": "si"},
    {"sku_id": "30", "nombre": "Dog Chow Adulto 3kg", "precio": 9800.0, "requiere_receta": "no"},
]


def test_texto_alternativas_petshop_sin_requiere_receta(usar_perfil):
    from app.services.checkout_helper import texto_alternativas
    usar_perfil("petshop")
    t = texto_alternativas(_ALTS_RECETA)
    assert "receta" not in t.lower()
    assert "• Pipeta Frontline Plus Perro 10-20kg — $25,000.00\n" in t


def test_texto_alternativas_farmacia_igual_que_hoy(usar_perfil):
    from app.services.checkout_helper import texto_alternativas
    usar_perfil("farmacia")
    t = texto_alternativas(_ALTS_RECETA)
    assert "• Pipeta Frontline Plus Perro 10-20kg — $25,000.00 (requiere receta)\n" in t
    assert "• Dog Chow Adulto 3kg — $9,800.00\n" in t


# ══════════════════════════════════════════════════════════════════════════════
# Links (links_como_receta = False)
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("texto,esperado", [
    ("te mando la receta https://drive.google.com/file/d/abc/view", True),
    ("ahí va receta_ana.jpg", True),
    ("www.fotos.com/receta", True),
    ("el link https://pagos.ejemplo.com/pay/abc123 no me abre", False),
    ("hola, tenés pipetas?", False),
])
def test_contiene_link_regresion_farmacia(texto, esperado):
    """Regresión escrita ANTES del cambio de firma: pasa con el código de hoy
    y sigue pasando después (sin dominio propio)."""
    from app.services.checkout_helper import contiene_link
    assert contiene_link(texto) is esperado


@pytest.mark.parametrize("base_url,esperado", [
    ("https://cerca.remedia.ar", "remedia.ar"),
    ("https://farmacia.remedia.ar/", "remedia.ar"),
    ("HTTPS://Cerca.Remedia.AR:8443/bo", "remedia.ar"),
    ("https://www.cerca.remedia.ar", "remedia.ar"),
    ("https://bot.mascotasdeloeste.com.ar", "bot.mascotasdeloeste.com.ar"),
    ("https://mascotasdeloeste.com.ar", "mascotasdeloeste.com.ar"),
    ("http://localhost:8000", "localhost"),
    ("http://127.0.0.1:8000", "127.0.0.1"),
    ("https://[mal", ""),
    ("", ""),
])
def test_dominio_propio(base_url, esperado):
    from app.services.checkout_helper import dominio_propio
    assert dominio_propio(base_url) == esperado


@pytest.mark.parametrize("texto,dominio,esperado", [
    ("mirá https://cerca.remedia.ar/promos", "remedia.ar", False),
    ("pagá acá https://cerca.remedia.ar/pay/abc", "remedia.ar", False),
    ("te mando la receta https://drive.google.com/file/d/abc", "remedia.ar", True),
    ("receta.jpg", "remedia.ar", True),
    ("https://bot.mascotasdeloeste.com.ar/pay/xyz", "bot.mascotasdeloeste.com.ar", False),
    ("https://bot.mascotasdeloeste.com.ar/status", "bot.mascotasdeloeste.com.ar", False),
    ("https://otra.com.ar/x", "bot.mascotasdeloeste.com.ar", True),
    # Sin dominio propio: /pay/ se excluye igual y remedia.ar deja de ser especial.
    ("https://pagos.ejemplo.com/pay/abc", "", False),
    ("https://remedia.ar/x", "", True),
])
def test_contiene_link_con_dominio_propio(texto, dominio, esperado):
    from app.services.checkout_helper import contiene_link
    assert contiene_link(texto, dominio) is esperado


# ══════════════════════════════════════════════════════════════════════════════
# Beneficios (§4.4): socios, empleados, cuenta corriente y pago manual
# ══════════════════════════════════════════════════════════════════════════════
class _SociosBen:
    def __init__(self, socios):
        self.s = socios

    def find_by_phone(self, phone):
        return self.s.get(phone)

    def contexto_para_prompt(self, phone):
        return "Nombre de pila (para saludar): Ana" if phone in self.s else None


_CFG_BEN = {"socio_discount_pct": "15", "empleado_discount_pct": "20",
            "socio_discount_en_catalogo": "true", "receta_mode": "conservador",
            "cc_enabled": "true"}


@pytest.fixture
def empleados_ben(monkeypatch):
    """Listado de empleados falso (mismo patrón que test_descuento_entrega.py)."""
    import sys
    import types
    padron = {}
    mod = types.ModuleType("app.services.empleado_service")

    class _Svc:
        def find_by_phone(self, phone):
            return padron.get(phone)
    svc = _Svc()
    mod.get_empleado_service = lambda *a, **k: svc
    monkeypatch.setitem(sys.modules, "app.services.empleado_service", mod)
    return padron


def test_beneficios_descuento_para_petshop_apaga_socio_y_empleado(usar_perfil, empleados_ben):
    from app.services import checkout_helper as ch
    usar_perfil("petshop")
    empleados_ben["549E"] = {"nombre_pila": "Ema"}
    socios = _SociosBen({"549E": {"nombre": "Ema"}, "549S": {"nombre": "Sol"}})
    assert ch.descuento_para("549E", _CFG_BEN, socios) == (0.0, "")
    assert ch.descuento_para("549S", _CFG_BEN, socios) == (0.0, "")


def test_beneficios_descuento_para_farmacia_igual_que_hoy(usar_perfil, empleados_ben):
    from app.services import checkout_helper as ch
    usar_perfil("farmacia")
    empleados_ben["549E"] = {"nombre_pila": "Ema"}
    socios = _SociosBen({"549E": {"nombre": "Ema"}, "549S": {"nombre": "Sol"}})
    assert ch.descuento_para("549E", _CFG_BEN, socios) == (20.0, "empleado")
    assert ch.descuento_para("549S", _CFG_BEN, socios) == (15.0, "socio")


def _res_ben():
    return [{"sku_id": "P1", "nombre": "DOG CHOW ADULTO 15KG", "precio": 10000.0,
             "requiere_receta": "no", "vendible": True}]


def test_beneficios_aplicar_descuento_petshop_no_toca_precios(usar_perfil, empleados_ben):
    from app.services import checkout_helper as ch
    usar_perfil("petshop")
    empleados_ben["549E"] = {"nombre_pila": "Ema"}
    out, pct = ch.aplicar_descuento_socio(_res_ben(), "549E", _CFG_BEN,
                                          _SociosBen({"549E": {"nombre": "Ema"}}))
    assert pct == 0.0
    assert out[0]["precio"] == 10000.0 and "precio_lista" not in out[0]


def test_beneficios_aplicar_descuento_farmacia_igual_que_hoy(usar_perfil, empleados_ben):
    from app.services import checkout_helper as ch
    usar_perfil("farmacia")
    empleados_ben["549E"] = {"nombre_pila": "Ema"}
    out, pct = ch.aplicar_descuento_socio(_res_ben(), "549E", _CFG_BEN,
                                          _SociosBen({"549E": {"nombre": "Ema"}}))
    assert pct == 20.0
    assert out[0]["precio"] == pytest.approx(8000.0) and out[0]["precio_lista"] == 10000.0


async def _link_de_empleado(monkeypatch, empleados_ben):
    """Link de un empleado con el precio pendiente ya bonificado ($8.000 de $10.000)."""
    from app.services import checkout_helper as ch
    from app.services import config_service as cs
    from app.services.session_service import SessionService
    empleados_ben["549E"] = {"nombre_pila": "Ema"}

    class _CfgBen:
        async def get_all(self):
            return dict(_CFG_BEN)

        async def get_hours(self):
            return {}

        def is_open_now(self, hours):
            return True
    monkeypatch.setattr(cs, "get_config_service", lambda *a, **k: _CfgBen())

    async def _sin_freno(*a, **k):
        return None, None
    monkeypatch.setattr(ch, "_chequear_stock_vivo", _sin_freno)
    # Precio de lista mayor al cobrado: con descuento vigente sale la línea "🎉".
    monkeypatch.setattr(ch, "precio_sin_descuento", lambda items, pct, sku_svc=None: 10000.0)

    class _Pago:
        async def crear_link(self, **k):
            return "https://pago/ben", None

    ss = SessionService("redis://127.0.0.1:1")
    await ss.set_pending("549E", sku_id="P1", sku_nombre="DOG CHOW ADULTO 15KG",
                         precio=8000.0, cantidad=1, opciones=[])
    return await ch.crear_link_y_responder(_Pago(), ss, "549E", await ss.get("549E"),
                                           "retiro", None)


async def test_beneficios_link_petshop_sin_linea_de_empleado(usar_perfil, empleados_ben,
                                                             monkeypatch):
    usar_perfil("petshop")
    resp, link = await _link_de_empleado(monkeypatch, empleados_ben)
    assert link == "https://pago/ben"
    assert "Como empleado" not in resp and "🎉" not in resp
    assert "$8,000.00" in resp


async def test_beneficios_link_farmacia_con_linea_de_empleado_igual_que_hoy(
        usar_perfil, empleados_ben, monkeypatch):
    usar_perfil("farmacia")
    resp, link = await _link_de_empleado(monkeypatch, empleados_ben)
    assert link == "https://pago/ben"
    assert "🎉 Como empleado te aplicamos un 20% de descuento (precio de lista: $10,000.00)." in resp


class _SinExcepcionesCC:
    async def es_excepcion(self, socio):
        return False


async def test_beneficios_habilitado_cc_petshop_none(usar_perfil, empleados_ben, monkeypatch):
    from app.services import checkout_helper as ch
    import app.services.cc_service as ccmod
    monkeypatch.setattr(ccmod, "_instance", _SinExcepcionesCC())
    usar_perfil("petshop")
    empleados_ben["549E"] = {"nombre_pila": "Ema"}
    socios = _SociosBen({"549S": {"nombre": "Sol", "dni": "1"}})
    assert await ch.habilitado_cc("549S", _CFG_BEN, socios, 1000) is None
    assert await ch.habilitado_cc("549E", _CFG_BEN, socios, 1000) is None


async def test_beneficios_habilitado_cc_farmacia_igual_que_hoy(usar_perfil, empleados_ben,
                                                               monkeypatch):
    from app.services import checkout_helper as ch
    import app.services.cc_service as ccmod
    monkeypatch.setattr(ccmod, "_instance", _SinExcepcionesCC())
    usar_perfil("farmacia")
    empleados_ben["549E"] = {"nombre_pila": "Ema"}
    socios = _SociosBen({"549S": {"nombre": "Sol", "dni": "1"}})
    assert (await ch.habilitado_cc("549S", _CFG_BEN, socios, 1000))["nombre"] == "Sol"
    assert (await ch.habilitado_cc("549E", _CFG_BEN, socios, 1000))["empleado"] is True


def test_beneficios_pide_pago_manual_sin_cc_ni_mp():
    from app.services.checkout_helper import pide_pago_manual
    assert pide_pago_manual("me lo anotás en cuenta corriente?", incluir_cuenta_corriente=False) is False
    assert pide_pago_manual("lo pago con mercado pago", incluir_mercado_pago=False) is False
    # Los demás medios manuales siguen contando con los dos apagados.
    assert pide_pago_manual("te pago por transferencia", incluir_cuenta_corriente=False,
                            incluir_mercado_pago=False) is True
    assert pide_pago_manual("lo pago en la sucursal cuando retiro", incluir_cuenta_corriente=False,
                            incluir_mercado_pago=False) is True


def test_beneficios_pide_pago_manual_farmacia_igual_que_hoy():
    from app.services.checkout_helper import pide_pago_manual
    assert pide_pago_manual("me lo anotás en cuenta corriente?") is True
    assert pide_pago_manual("lo pago con mercado pago") is True
    assert pide_pago_manual("tenés ibuprofeno?") is False


def test_beneficios_pago_mp_manual_default_y_editable():
    from app.routers.backoffice import ConfigUpdate
    from app.services.config_service import DEFAULTS
    assert DEFAULTS["pago_mp_manual"] == "true"          # default: todo igual que hoy
    assert ConfigUpdate(pago_mp_manual="false").model_dump()["pago_mp_manual"] == "false"


class _IntentSimBen:
    def __init__(self):
        self.kwargs = []

    async def procesar_rapido(self, mensaje, **k):
        self.kwargs.append(k)
        return {"intencion": "saludo", "respuesta": "¡Hola!"}

    async def procesar(self, mensaje, **k):
        self.kwargs.append(k)
        return {"intencion": "desconocido", "respuesta": "¿En qué te ayudo?"}


async def _simular_hola(monkeypatch):
    """POST /simulate con "hola" desde un socio del padrón; devuelve los kwargs al modelo."""
    from app.routers import simulate as sim
    from app.services.session_service import SessionService
    intent = _IntentSimBen()
    ss = SessionService("redis://127.0.0.1:1")

    class _Perf:
        async def record(self, *a, **k):
            return None

    class _Cfg:
        async def get_all(self):
            return {}
    monkeypatch.setattr(sim, "get_sku_service", lambda *a, **k: None)
    monkeypatch.setattr(sim, "get_session_service", lambda *a, **k: ss)
    monkeypatch.setattr(sim, "get_intent_service", lambda *a, **k: intent)
    monkeypatch.setattr(sim, "get_perf_service", lambda *a, **k: _Perf())
    monkeypatch.setattr(sim, "get_socio_service",
                        lambda *a, **k: _SociosBen({"549SIM": {"nombre": "Ana"}}))
    monkeypatch.setattr(sim, "get_config_service", lambda *a, **k: _Cfg())
    monkeypatch.setattr(sim, "payment_svc_para", lambda *a, **k: None)
    r = await sim.simulate(sim.SimulateRequest(phone="549SIM", message="hola"))
    assert r.respuesta == "¡Hola!"
    return intent.kwargs


async def test_beneficios_simulate_petshop_sin_contexto_de_socio(usar_perfil, monkeypatch):
    usar_perfil("petshop")
    kwargs = await _simular_hola(monkeypatch)
    assert kwargs and all(k.get("contexto_cliente") is None for k in kwargs)


async def test_beneficios_simulate_farmacia_igual_que_hoy(usar_perfil, monkeypatch):
    usar_perfil("farmacia")
    kwargs = await _simular_hola(monkeypatch)
    assert kwargs[0]["contexto_cliente"] == "Nombre de pila (para saludar): Ana"


# ══════════════════════════════════════════════════════════════════════════════
# Salud de la mascota (§4.5): con sintomas="derivar" un síntoma va a una persona
# ══════════════════════════════════════════════════════════════════════════════
from app.services.session_service import SessionService

_TEL_SALUD = "5493410000077"


class _MetricasSalud:
    def __init__(self):
        self.eventos = []

    async def evento(self, *a, **k):
        self.eventos.append((a, k))


class _WaSalud:
    def __init__(self):
        self.enviados = []

    async def send_text(self, phone, texto, **k):
        self.enviados.append(texto)
        return True


class _CfgSalud:
    def __init__(self, valores):
        self.v = dict(valores)

    async def get_all(self):
        return dict(self.v)


def _deps_salud(cfg=None):
    return {"session": SessionService("redis://127.0.0.1:1"), "metrics": _MetricasSalud(),
            "wa": _WaSalud(), "config": _CfgSalud(cfg or {})}


def test_motivo_consulta_salud():
    from app.services.checkout_helper import MOTIVO_CONSULTA_SALUD
    assert MOTIVO_CONSULTA_SALUD == "consulta_salud"


def test_texto_consulta_salud_config_gana_y_vacio_cae_al_perfil(usar_perfil):
    from app.services.checkout_helper import texto_consulta_salud
    p = usar_perfil("petshop")
    assert texto_consulta_salud({}) == p.textos["consulta_salud_message"]
    assert texto_consulta_salud({"consulta_salud_message": ""}) == p.textos["consulta_salud_message"]
    assert texto_consulta_salud({"consulta_salud_message": "Te paso con el equipo"}) == "Te paso con el equipo"


async def test_sin_precios_inventados_con_sintoma_deriva_en_petshop(usar_perfil):
    from app.routers.webhook import _sin_precios_inventados
    from app.services.config_service import valores_base
    p = usar_perfil("petshop")
    deps = _deps_salud()
    session = await deps["session"].get(_TEL_SALUD)
    r = await _sin_precios_inventados(deps, _TEL_SALUD, session, "Dale Vomitol $5.000", [],
                                      valores_base(), None, sintoma=True)
    assert r == p.textos["consulta_salud_message"]
    s = await deps["session"].get(_TEL_SALUD)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "consulta_salud"
    assert not s.get("farmaceutico_ofrecido")


async def test_sin_precios_inventados_con_sintoma_farmacia_igual_que_hoy(usar_perfil):
    from app.routers.webhook import _sin_precios_inventados
    from app.services.config_service import DEFAULTS
    usar_perfil("farmacia")
    deps = _deps_salud()
    session = await deps["session"].get(_TEL_SALUD)
    r = await _sin_precios_inventados(deps, _TEL_SALUD, session, "Tomá Ibupirac $5.000", [],
                                      dict(DEFAULTS), None, sintoma=True)
    assert r == ("Para eso lo mejor es que te asesore el farmacéutico 🙌 "
                 "¿Querés que te pase con él?")
    s = await deps["session"].get(_TEL_SALUD)
    assert s.get("farmaceutico_ofrecido") is True
    assert s.get("estado") != "operador"


def test_agregar_oferta_farmaceutico_vacio_no_agrega_en_petshop(usar_perfil):
    from app.services.checkout_helper import agregar_oferta_farmaceutico
    usar_perfil("petshop")
    r = agregar_oferta_farmaceutico("Te ofrezco Pipeta X", {"sintoma_farmaceutico_message": ""})
    assert r == "Te ofrezco Pipeta X"


def test_agregar_oferta_farmaceutico_farmacia_igual_que_hoy(usar_perfil):
    from app.services.checkout_helper import agregar_oferta_farmaceutico
    usar_perfil("farmacia")
    r = agregar_oferta_farmaceutico("Te ofrezco Ibupirac", {"sintoma_farmaceutico_message": ""})
    assert r == ("Te ofrezco Ibupirac\n\nSi preferís, decime \"farmacéutico\" y te paso "
                 "con el nuestro para que te oriente.")


async def test_derivar_consulta_salud_envia_guarda_y_deriva(usar_perfil):
    from app.routers.webhook import _derivar_consulta_salud
    from app.services.config_service import valores_base
    p = usar_perfil("petshop")
    deps = _deps_salud(valores_base())
    r = await _derivar_consulta_salud(deps, _TEL_SALUD, "mi perro vomita")
    assert r == p.textos["consulta_salud_message"]
    assert deps["wa"].enviados == [r]
    s = await deps["session"].get(_TEL_SALUD)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "consulta_salud"
    assert [(m["role"], m["content"]) for m in s["history"][-2:]] == [
        ("user", "mi perro vomita"), ("assistant", r)]


def test_metricas_cuentan_las_derivaciones_nuevas():
    from app.services.metrics_store import _DERIVACIONES
    assert "derivado_consulta_salud" in _DERIVACIONES
    assert "imagen_indicacion_veterinaria" in _DERIVACIONES


def test_dashboard_cuenta_las_derivaciones_nuevas():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "app" / "static" / "dashboard.html"
            ).read_text(encoding="utf-8")
    ini = html.index("const DERIV_INTENTS")
    conjunto = html[ini:html.index("]);", ini)]
    assert "'derivado_consulta_salud'" in conjunto
    assert "'imagen_indicacion_veterinaria'" in conjunto


# ══════════════════════════════════════════════════════════════════════════════
# Visión (§4.6): prompt y categorías del clasificador salen del perfil
# ══════════════════════════════════════════════════════════════════════════════
from types import SimpleNamespace


def _vision_con_clientes_falsos(provider, raw):
    """ImageService con clientes Anthropic y OpenAI falsos: guardan lo que
    reciben y devuelven `raw` como respuesta del modelo."""
    from app.services.image_service import ImageService
    llamadas = {"anthropic": [], "openai": []}

    async def _create_anthropic(**k):
        llamadas["anthropic"].append(k)
        return SimpleNamespace(content=[SimpleNamespace(text=raw)])

    async def _create_openai(**k):
        llamadas["openai"].append(k)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=raw))])

    svc = ImageService.__new__(ImageService)
    svc._provider = provider
    svc._anthropic = SimpleNamespace(messages=SimpleNamespace(create=_create_anthropic))
    svc._openai = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create_openai)))
    return svc, llamadas


def _texto_anthropic(k):
    return k["messages"][0]["content"][1]["text"]


def _texto_openai(k):
    return k["messages"][0]["content"][0]["text"]


def _json_vision(tipo, items=""):
    import json
    return json.dumps({"tipo": tipo, "items": items})


def test_parse_petshop_apaga_receta_bono_y_credencial(usar_perfil):
    from app.services.image_service import ImageService
    usar_perfil("petshop")
    for tipo in ("receta", "bono", "credencial"):
        assert ImageService._parse(_json_vision(tipo))["tipo"] == "otro", tipo
    assert ImageService._parse(_json_vision("indicacion_veterinaria"))["tipo"] == "indicacion_veterinaria"
    r = ImageService._parse(_json_vision("producto", "Royal Canin Medium Adult 15kg"))
    assert r == {"tipo": "producto", "items": "Royal Canin Medium Adult 15kg"}


def test_parse_farmacia_igual_que_hoy(usar_perfil):
    from app.services.image_service import ImageService
    usar_perfil("farmacia")
    for tipo in ("receta", "bono", "credencial", "comprobante", "producto", "otro"):
        assert ImageService._parse(_json_vision(tipo))["tipo"] == tipo
    assert ImageService._parse(_json_vision("indicacion_veterinaria"))["tipo"] == "otro"


def test_parse_con_categorias_explicitas(usar_perfil):
    from app.services.image_service import ImageService
    usar_perfil("petshop")
    assert ImageService._parse(_json_vision("receta"), categorias=("receta", "otro"))["tipo"] == "receta"
    assert ImageService._parse("sin json") is None


async def test_vision_petshop_manda_el_prompt_del_perfil(usar_perfil):
    from app.services.perfil import VISION_PETSHOP
    usar_perfil("petshop")
    svc, llamadas = _vision_con_clientes_falsos("anthropic", _json_vision("receta"))
    r = await svc.analizar(b"foto", "image/jpeg")
    assert _texto_anthropic(llamadas["anthropic"][0]) == VISION_PETSHOP.prompt
    assert r["tipo"] == "otro"                          # receta no existe en petshop
    svc, llamadas = _vision_con_clientes_falsos("openai", _json_vision("producto", "Pipeta"))
    r = await svc.analizar(b"foto", "image/jpeg")
    assert _texto_openai(llamadas["openai"][0]) == VISION_PETSHOP.prompt
    assert r == {"tipo": "producto", "items": "Pipeta"}


async def test_vision_farmacia_manda_el_prompt_de_hoy(usar_perfil):
    from app.services import image_service
    from app.services.prompts import VISION_PROMPT_FARMACIA
    usar_perfil("farmacia")
    assert image_service._PROMPT is VISION_PROMPT_FARMACIA          # alias, mismo objeto
    svc, llamadas = _vision_con_clientes_falsos("anthropic", _json_vision("receta"))
    r = await svc.analizar(b"foto", "image/jpeg")
    assert _texto_anthropic(llamadas["anthropic"][0]) == image_service._PROMPT
    assert r["tipo"] == "receta"
    svc, llamadas = _vision_con_clientes_falsos("openai", _json_vision("bono", "Cassará"))
    await svc.analizar(b"foto", "image/jpeg")
    assert _texto_openai(llamadas["openai"][0]) == image_service._PROMPT


# ══════════════════════════════════════════════════════════════════════════════
# Pagos y avisos (§4.7): confirmación de pago, descriptor de la tarjeta, Payway
# y las páginas /pay con la marca del perfil.
# ══════════════════════════════════════════════════════════════════════════════
import hashlib
import json
from types import SimpleNamespace

import pytest

_CONF_RETIRO_FARMACIA = (
    "✅ *¡Pago confirmado!*\n\n"
    "Recibimos tu pago de *Ibuprofeno 600*. 🙌\n"
    "🔑 *Tu código de retiro es: 123456*\nRetiralo desde las 10:00\n\n"
    "Guardalo para presentarlo al retirar. ¡Muchas gracias! 💊")
_CONF_ENVIO_FARMACIA = (
    "✅ *¡Pago confirmado!*\n\n"
    "Recibimos tu pago de *Ibuprofeno 600*. 🙌\n"
    "🚚 Te lo enviamos a domicilio a *San Martín 123*. Nos comunicamos para coordinar la entrega.\n"
    "📋 Código de pedido: *123456*\n\n"
    "¡Muchas gracias! 💊")
_CONF_MP_FARMACIA = (
    "✅ *¡Pago confirmado!*\n\n"
    "Recibimos tu pago de *Royal Canin 15KG*. 🙌\n"
    "🔑 *Tu código de retiro es: 654321*\n\n"
    "Guardalo para presentarlo al retirar. ¡Muchas gracias! 💊")


def test_mensaje_pago_confirmado_farmacia_igual_que_hoy(usar_perfil):
    from app.services.checkout_helper import mensaje_pago_confirmado
    p = usar_perfil("farmacia")
    assert mensaje_pago_confirmado("Ibuprofeno 600", "retiro", None, "123456",
                                   "Retiralo desde las 10:00", p.emoji) == _CONF_RETIRO_FARMACIA
    assert mensaje_pago_confirmado("Ibuprofeno 600", "envio", "San Martín 123", "123456",
                                   "Retiralo desde las 10:00", p.emoji) == _CONF_ENVIO_FARMACIA
    # Sin texto de horario no queda una línea vacía de más
    assert "123456*\n\nGuardalo" in mensaje_pago_confirmado("X", "retiro", None, "123456", "", p.emoji)


def test_mensaje_pago_confirmado_petshop(usar_perfil):
    from app.services.checkout_helper import mensaje_pago_confirmado
    p = usar_perfil("petshop")
    for tipo, direccion in (("retiro", None), ("envio", "San Martín 123")):
        m = mensaje_pago_confirmado("Royal Canin 15KG", tipo, direccion, "654321", "", p.emoji)
        assert m.endswith("¡Muchas gracias! 🐾"), tipo
        assert "💊" not in m, tipo


def test_mensaje_pago_confirmado_con_sucursal(usar_perfil):
    from app.services.checkout_helper import mensaje_pago_confirmado
    p = usar_perfil("petshop")
    m = mensaje_pago_confirmado("Royal Canin 15KG", "retiro", None, "654321", "", p.emoji,
                                sucursal="Sucursal Piloto")
    assert m.endswith("Guardalo para presentarlo al retirar en *Sucursal Piloto*. ¡Muchas gracias! 🐾")
    # Vacía o con espacios: el texto de hoy
    assert "al retirar. ¡Muchas gracias! 🐾" in mensaje_pago_confirmado(
        "X", "retiro", None, "1", "", p.emoji, sucursal="  ")
    # El envío no nombra la sucursal
    assert "Sucursal Piloto" not in mensaje_pago_confirmado(
        "X", "envio", "Mitre 100", "1", "", p.emoji, sucursal="Sucursal Piloto")


class _CfgPago:
    """Config falsa para la confirmación de pago (MP y Payway)."""
    def __init__(self, extra=None):
        self.v = {"pickup_minutes": "30", **(extra or {})}

    async def get_all(self):
        return dict(self.v)

    async def get_hours(self):
        return {}

    def get_pickup_text(self, hours, minutes):
        return ""


def _mp_aprobado(monkeypatch, ref, cfg_extra=None):
    import app.routers.mp_webhook as mpw
    enviados = []

    class _Pay:
        async def get_payment_info(self, pid):
            return {"status": "approved", "external_reference": ref,
                    "transaction_amount": 9800.0, "payment_method_id": "visa",
                    "additional_info": {"items": [{"title": "Royal Canin 15KG", "quantity": 1}]}}

    class _Orders:
        async def find_by_payment(self, pid):
            return None

        async def create(self, **kw):
            return {"order_id": "ORD-MP", "pickup_code": "654321"}

    class _Wa:
        async def send_text(self, phone, msg):
            enviados.append(msg)
            return True

    monkeypatch.setattr(mpw, "get_payment_service", lambda *a, **k: _Pay())
    monkeypatch.setattr(mpw, "get_order_service", lambda *a: _Orders())
    monkeypatch.setattr(mpw, "get_whatsapp_service", lambda *a: _Wa())
    monkeypatch.setattr(mpw, "get_config_service", lambda *a: _CfgPago(cfg_extra))
    return mpw, enviados


async def test_mp_confirmacion_petshop_con_sucursal(usar_perfil, monkeypatch):
    usar_perfil("petshop")
    mpw, enviados = _mp_aprobado(monkeypatch, "5491100000101_S1", {"retiro_sucursal": "Sucursal Piloto"})
    r = await mpw.procesar_pago("PAGO-PET-MP-1")
    assert r["status"] == "ok"
    assert enviados[0].endswith("al retirar en *Sucursal Piloto*. ¡Muchas gracias! 🐾")
    assert "💊" not in enviados[0]


async def test_mp_confirmacion_farmacia_igual_que_hoy(usar_perfil, monkeypatch):
    usar_perfil("farmacia")
    mpw, enviados = _mp_aprobado(monkeypatch, "5491100000102_S1")
    r = await mpw.procesar_pago("PAGO-FARM-MP-1")
    assert r["status"] == "ok"
    assert enviados[0] == _CONF_MP_FARMACIA


def _payway_aprobado(monkeypatch, pid, payway_id, phone, cfg_extra=None):
    import app.routers.payway as pw
    import app.services.config_service as cs
    kv = {f"payway:pending:{pid}": json.dumps({
        "id": pid, "phone": phone, "sku_id": "S1", "sku_nombre": "Royal Canin 15KG",
        "cantidad": 1, "total": 9800.0})}
    enviados = []

    class _Redis:
        async def get(self, k):
            return kv.get(k)

        async def setex(self, k, ttl, v):
            kv[k] = v

    class _Pw:
        async def crear_pago(self, **k):
            return {"id": payway_id, "status": "approved", "card_brand": "Visa"}, None

    class _Orders:
        async def find_by_payment(self, pid):
            return None

        async def create(self, **kw):
            return {"order_id": "ORD-PW", "pickup_code": "654321"}

    class _Wa:
        async def send_text(self, phone, msg):
            enviados.append(msg)
            return True

    # payway.py:216 hace _redis().setex fuera de un try: sin este fake, revienta sin Redis
    monkeypatch.setattr(pw, "_redis", lambda: _Redis())
    monkeypatch.setattr(pw, "get_payway_service", lambda *a, **k: _Pw())
    monkeypatch.setattr(pw, "get_order_service", lambda *a: _Orders())
    monkeypatch.setattr(pw, "get_whatsapp_service", lambda *a: _Wa())
    monkeypatch.setattr(cs, "get_config_service", lambda *a, **k: _CfgPago(cfg_extra))
    return pw, enviados


async def test_payway_confirmacion_petshop_con_sucursal(usar_perfil, monkeypatch):
    usar_perfil("petshop")
    pw, enviados = _payway_aprobado(monkeypatch, "PID-PET-1", "PW-PET-1", "5491100000201",
                                    {"retiro_sucursal": "Sucursal Piloto"})
    r = await pw.payway_charge(pw.ChargeIn(pid="PID-PET-1", token="tok", bin="450799"))
    assert r == {"status": "approved"}
    assert enviados[0].endswith("al retirar en *Sucursal Piloto*. ¡Muchas gracias! 🐾")
    assert "💊" not in enviados[0]


async def test_payway_confirmacion_farmacia_igual_que_hoy(usar_perfil, monkeypatch):
    usar_perfil("farmacia")
    pw, enviados = _payway_aprobado(monkeypatch, "PID-FARM-1", "PW-FARM-1", "5491100000202")
    r = await pw.payway_charge(pw.ChargeIn(pid="PID-FARM-1", token="tok", bin="450799"))
    assert r == {"status": "approved"}
    assert enviados[0] == _CONF_MP_FARMACIA


class _RespHttp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data


def _http_falso(monkeypatch, modulo, status, data):
    """Reemplaza httpx.AsyncClient y devuelve la lista de payloads posteados."""
    capturados = []

    class _Cliente:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None, timeout=None):
            capturados.append(json)
            return _RespHttp(status, data)

    monkeypatch.setattr(modulo.httpx, "AsyncClient", _Cliente)
    return capturados


async def test_statement_descriptor_por_perfil(usar_perfil, monkeypatch):
    from app.services import payment_service as ps
    cap = _http_falso(monkeypatch, ps, 201, {"init_point": "https://mp.test/x"})
    svc = ps.PaymentService("TEST-TOKEN", "https://bot.test/mp/notification")
    usar_perfil("farmacia")
    link, err = await svc.crear_link(sku_id="S1", nombre="Royal Canin 15KG", precio=9800.0, phone="549")
    assert (link, err) == ("https://mp.test/x", None)
    assert cap[-1]["statement_descriptor"] == "FARMACIA AMI"
    # Misma instancia: el descriptor se lee en cada link, no en __init__
    usar_perfil("petshop")
    await svc.crear_link(sku_id="S1", nombre="Royal Canin 15KG", precio=9800.0, phone="549")
    assert cap[-1]["statement_descriptor"] == "MASCOTAS DEL OESTE"


async def test_statement_descriptor_ascii_y_hasta_22(usar_perfil, monkeypatch):
    from app.services import payment_service as ps
    cap = _http_falso(monkeypatch, ps, 201, {"init_point": "https://mp.test/x"})
    usar_perfil("petshop", comercio="Piñata Mascotas Ñuñoa Sucursal Centro")
    await ps.PaymentService("TEST-TOKEN", "https://bot.test/mp/notification").crear_link(
        sku_id="S1", nombre="Royal Canin 15KG", precio=9800.0, phone="549")
    d = cap[-1]["statement_descriptor"]
    assert d == "PINATA MASCOTAS NUNOA"
    assert d.isascii() and len(d) <= 22


async def test_payway_crear_pago_con_el_comercio_del_perfil(usar_perfil, monkeypatch):
    from app.services import payway_service as pws
    cap = _http_falso(monkeypatch, pws, 201, {"id": 1, "status": "approved"})
    svc = pws.PaywayService("pub", "priv", sandbox=True, cybersource=True)
    usar_perfil("petshop")
    data, err = await svc.crear_pago(token="tok", amount=9800.0, site_transaction_id="t1",
                                     payment_method_id=1, bin="450799", producto="Royal Canin 15KG")
    assert err is None
    pl = cap[-1]
    assert pl["description"] == "Compra Mascotas del Oeste"
    assert pl["fraud_detection"]["bill_to"]["last_name"] == "Mascotas del Oeste"
    assert pl["fraud_detection"]["retail_transaction_data"]["ship_to"]["last_name"] == "Mascotas del Oeste"
    assert pl["device_unique_identifier"] == "mascotas-del-oeste-web"
    assert pl["fraud_detection"]["device_unique_identifier"] == "mascotas-del-oeste-web"
    # Con el fingerprint del navegador, se usa ese
    await svc.crear_pago(token="tok", amount=9800.0, site_transaction_id="t2",
                         payment_method_id=1, bin="450799", device_id="fp-123")
    assert cap[-1]["device_unique_identifier"] == "fp-123"


async def test_payway_crear_pago_farmacia_igual_que_hoy(usar_perfil, monkeypatch):
    from app.services import payway_service as pws
    cap = _http_falso(monkeypatch, pws, 201, {"id": 1, "status": "approved"})
    usar_perfil("farmacia")
    await pws.PaywayService("pub", "priv", sandbox=True, cybersource=True).crear_pago(
        token="tok", amount=4770.0, site_transaction_id="t1", payment_method_id=1, bin="450799")
    pl = cap[-1]
    assert pl["description"] == "Compra Remedia"
    assert pl["fraud_detection"]["bill_to"]["last_name"] == "Remedia"
    assert pl["device_unique_identifier"] == "remedia-web"


# ── Review Focus: VERTICAL y COMERCIO_NOMBRE como los cargan en Railway ─────────
async def test_payway_con_vertical_en_mayusculas_y_comercio_con_enie_y_tildes(usar_perfil,
                                                                              monkeypatch):
    from app.services import payway_service as pws
    cap = _http_falso(monkeypatch, pws, 201, {"id": 1, "status": "approved"})
    p = usar_perfil(" PETSHOP ", comercio="  Ñandú Mascotas Güemes  ")
    assert (p.clave, p.comercio) == ("petshop", "Ñandú Mascotas Güemes")
    await pws.PaywayService("pub", "priv", sandbox=True, cybersource=True).crear_pago(
        token="tok", amount=9800.0, site_transaction_id="t1", payment_method_id=1, bin="450799")
    pl = cap[-1]
    assert pl["description"] == "Compra Ñandú Mascotas Güemes"
    assert pl["fraud_detection"]["bill_to"]["last_name"] == "Ñandú Mascotas Güemes"
    assert pl["device_unique_identifier"] == "nandu-mascotas-guemes-web"
    assert pl["fraud_detection"]["device_unique_identifier"] == "nandu-mascotas-guemes-web"


# ── /pay y páginas de estado ────────────────────────────────────────────────────
# Mismo pendiente y mismos fakes que el snapshot de farmacia de la Task 1.
_PENDIENTE_PAY = {"total": "4770.0", "sku_nombre": "IBUPROFENO 600 MG X 10", "estado": "pendiente"}
_SNAPSHOT_FARMACIA = {
    "pay": "85c4271036760ed8f4185d0d2c860bf6c7bfb926d2d36e4a882283a22f84ad52",
    "vencido": "a23c99347472476c32a97742e46978de209ec2c2a8d4165ed33386df16f47283",
    "ok": "0b8cb84ff34ec20836cd88e807afa23f41e8e0b2e9b3b5fdba629ac1d0e44eea",
    "cancelado": "a975ad681e7c07c62cf5ef0c9260c8cd109c578870b0d820376e98fa31225994",
}


def _pay_falso(monkeypatch):
    import app.routers.payway as pw

    async def _pend(pid):
        return dict(_PENDIENTE_PAY) if pid == "abc123" else None
    monkeypatch.setattr(pw, "_get_pending", _pend)
    monkeypatch.setattr(pw, "get_payway_service", lambda *a, **k: SimpleNamespace(
        base_url="https://pw.test/api/v2", public_key="PUBKEY"))
    monkeypatch.setattr(pw, "get_settings", lambda: SimpleNamespace(
        payway_public_key="", payway_private_key="", payway_sandbox=True,
        payway_site_id="", payway_template_id="", payway_cybersource=False,
        payway_cs_org_id="ORG", payway_cs_merchant_id="MERCH"))
    return pw


async def _paginas(pw) -> dict:
    return {"pay": (await pw.pay_page("abc123")).body,
            "vencido": (await pw.pay_page("vencido")).body,
            "ok": (await pw.payway_return("ok")).body,
            "cancelado": (await pw.payway_return("")).body}


@pytest.mark.parametrize("clave", ["farmacia", "mutual"])
async def test_paginas_de_pago_farmacia_identicas_al_snapshot(usar_perfil, monkeypatch, clave):
    usar_perfil(clave)
    pw = _pay_falso(monkeypatch)
    paginas = await _paginas(pw)
    assert {k: hashlib.sha256(v).hexdigest() for k, v in paginas.items()} == _SNAPSHOT_FARMACIA


async def test_paginas_de_pago_petshop(usar_perfil, monkeypatch):
    usar_perfil("petshop")
    pw = _pay_falso(monkeypatch)
    paginas = {k: v.decode("utf-8") for k, v in (await _paginas(pw)).items()}
    assert "<title>Pagar · Mascotas del Oeste</title>" in paginas["pay"]
    assert "<title>Mascotas del Oeste</title>" in paginas["ok"]
    for nombre, pagina in paginas.items():
        assert '<div class="logo">M</div>' in pagina, nombre
        assert '<div class="wordmark">Mascotas del Oeste</div>' in pagina, nombre
        assert "Pago seguro procesado por Payway · Mascotas del Oeste" in pagina, nombre
        assert "Remed" not in pagina and "Farmacia" not in pagina, nombre
    # Lo demás de la página no cambia: producto, total y claves del formulario
    assert "IBUPROFENO 600 MG X 10" in paginas["pay"] and "4,770.00" in paginas["pay"]
    assert 'PUBLIC_KEY="PUBKEY"' in paginas["pay"]


async def test_paginas_de_pago_escapan_el_nombre_del_comercio(usar_perfil, monkeypatch):
    usar_perfil("petshop", comercio="Ñandú & Cía <MO>")
    pw = _pay_falso(monkeypatch)
    pagina = (await pw.pay_page("abc123")).body.decode("utf-8")
    assert '<div class="logo">Ñ</div>' in pagina
    assert '<div class="wordmark">Ñandú &amp; Cía &lt;MO&gt;</div>' in pagina
    assert "<title>Pagar · Ñandú &amp; Cía &lt;MO&gt;</title>" in pagina
    assert "<MO>" not in pagina
