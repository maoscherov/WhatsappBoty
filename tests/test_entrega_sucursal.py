"""
Spec 2026-10-06 §5: una pregunta en medio de la elección de entrega no es una
elección. Caso real (Mascotas del Oeste, 6/10): en esperando_entrega el cliente
preguntó "¿en qué sucursal puede ser?"; "sucursal" matcheaba como retiro y
salió el link sin contestarle. Se responde (con la sucursal que cargó el
comercio, si la cargó) y se vuelve a ofrecer la elección. Un pedido con forma
de pregunta ("¿me lo podés enviar?") sigue siendo elección.

Es una corrección para todos los rubros: cada test corre con el perfil
farmacia y con el petshop.
"""
import pytest

from app.routers import webhook as wh
from app.services import checkout_helper as chh
from app.services.config_service import valores_base
from app.services.sku_service import SKUService
from test_webhook_secuencias import PHONE, _msg, entorno  # noqa: F401  (entorno es fixture)

_INFO = "Lo retirás en *Sucursal Piloto*, Calle Falsa 123, de 9 a 20 hs 🐾"
_SUC = {"retiro_sucursal": "Sucursal Piloto",
        "retiro_info_message": "Lo retirás en *{sucursal}*, Calle Falsa 123, de 9 a 20 hs 🐾"}
_REPREGUNTA = "¿Preferís *retiro en Sucursal Piloto* o *envío a domicilio*? 🙂"
_HORARIO = "de lunes a sábado de 9 a 20"


@pytest.fixture(params=["farmacia", "petshop"], autouse=True)
def perfil(request, usar_perfil):
    return usar_perfil(request.param)


class _CfgEnt:
    """Config falsa con los textos del perfil activo y un horario cargado."""
    def __init__(self, extra=None):
        self.v = {**valores_base(), **(extra or {})}

    async def get_all(self):
        return dict(self.v)

    async def get(self, k):
        return self.v.get(k)

    async def get_hours(self):
        return {"enabled": False}

    def is_open_now(self, hours):
        return True

    def proxima_apertura(self, hours):
        return ""

    def texto_horario(self, hours):
        return _HORARIO


class _IntentEnt:
    """Modelo guionado que guarda los kwargs de cada llamada."""
    def __init__(self, guion=None):
        self.guion = guion or {}
        self.llamadas = []

    async def procesar_rapido(self, mensaje, **k):
        self.llamadas.append(("rapido", mensaje, k))
        return self.guion.get(mensaje, {"intencion": "saludo", "respuesta": "¡Hola!"})

    async def procesar(self, mensaje, **k):
        self.llamadas.append(("procesar", mensaje, k))
        return self.guion.get(mensaje, {"intencion": "desconocido", "respuesta": "Te cuento 🙂"})

    def vio(self, tipo):
        return [c for c in self.llamadas if c[0] == tipo]


def _catalogo():
    base = {"hash": "e" * 64, "barcodes": [], "troquel": None, "brand": "Dog Chow", "drug": None,
            "form": None, "category": "ALIMENTOS", "rubro": "PERROS", "subrubro": "",
            "therapeutic_actions": [], "stock": 5, "visible": True, "active": True,
            "requiere_receta": "no", "source": "t"}
    return SKUService.from_rows([
        {**base, "external_id": "30", "name": "DOG CHOW ADULTO RAZAS MEDIANAS 15KG", "price": 52000.0},
    ])


async def _armar(entorno, monkeypatch, estado, cfg=None, guion=None):
    """Webhook con un Dog Chow pendiente (venta libre en los dos rubros) en
    `estado`. Devuelve (deps, links): cada link generado queda como (tipo, dirección)."""
    deps = entorno()
    deps["config"] = _CfgEnt(cfg)
    deps["intent"] = _IntentEnt(guion)
    deps["sku"] = _catalogo()
    links = []

    async def _link(payment_svc, session_svc, phone, session, tipo_entrega="retiro", direccion=None):
        links.append((tipo_entrega, direccion))
        await session_svc.set_estado(phone, "esperando_pago")
        return f"LINK {tipo_entrega}", "https://pago/x"
    monkeypatch.setattr(chh, "crear_link_y_responder", _link)

    async def _sin_freno(*a, **k):
        return None, None
    monkeypatch.setattr(chh, "_chequear_stock_vivo", _sin_freno)
    monkeypatch.delitem(chh._ULTIMA_DIRECCION, PHONE, raising=False)
    await deps["session"].set_pending(PHONE, sku_id="30", sku_nombre="DOG CHOW ADULTO RAZAS MEDIANAS 15KG",
                                      precio=52000.0, cantidad=1, opciones=[])
    if estado != "esperando_confirmacion":          # set_pending ya la deja en confirmación
        await deps["session"].set_estado(PHONE, estado)
    return deps, links


async def _estado(deps):
    return (await deps["session"].get(PHONE)).get("estado")


# ── Funciones puras ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("txt", [
    "en que sucursal puede ser?", "en qué sucursal lo retiro?", "donde queda la sucursal?",
    "a que hora puedo pasar?", "como hago para retirarlo?", "cual sucursal?",
    "que sucursal me queda mas cerca?", "tienen sucursal en moron?", "¿Dónde retiro?",
    "cuánto sale el envío?", "en q sucursal?", "dónde lo retiro",
])
def test_es_pregunta_entrega_detecta_preguntas(txt):
    assert chh.es_pregunta_entrega(txt) is True


@pytest.mark.parametrize("txt", [
    "retiro", "retiro en sucursal", "lo paso a buscar", "voy a la sucursal", "envio",
    "a domicilio", "mandámelo a casa", "si ahí", "dale ahí", "sí, a mi domicilio",
    "me lo podés enviar", "¿me lo podés enviar?", "me lo envías?", "lo puedo retirar hoy?",
    "hacen envío a Funes?", "cuando salga del trabajo lo paso a buscar", "como siempre, retiro",
    "dale, lo busco", "ok",
])
def test_es_pregunta_entrega_no_confunde_elecciones(txt):
    assert chh.es_pregunta_entrega(txt) is False


def test_pregunta_por_retiro_y_respuesta_sin_sucursal():
    assert chh.pregunta_por_retiro("cuánto sale el envío?") is False
    assert chh.pregunta_por_retiro("donde queda la sucursal?") is True
    assert chh.responder_pregunta_retiro({}) == ""
    assert chh.responder_pregunta_retiro({"retiro_sucursal": "   "}) == ""
    assert chh.responder_pregunta_retiro({"retiro_sucursal": "Sucursal Piloto"}) == \
        "Lo retirás en *Sucursal Piloto* 🏪"
    assert chh.responder_pregunta_retiro(_SUC) == _INFO


def test_textos_de_entrega_sin_sucursal_igual_que_hoy():
    assert chh.pregunta_entrega({}) == \
        "¡Genial! ¿Cómo preferís recibirlo: *retiro en sucursal* o *envío a domicilio*?"
    assert chh.pregunta_entrega({}, saludo=False) == \
        "¿Preferís *retiro en sucursal* o *envío a domicilio*? 🙂"
    assert chh.pregunta_entrega({"retiro_sucursal": ""}, saludo=False) == \
        "¿Preferís *retiro en sucursal* o *envío a domicilio*? 🙂"
    assert chh.texto_entrega("retiro", None) == \
        "🏪 Lo retirás en la sucursal (te enviamos el código al confirmar el pago)."


def test_textos_de_entrega_nombran_la_sucursal():
    cfg = {"retiro_sucursal": "Sucursal Piloto"}
    assert chh.pregunta_entrega(cfg) == \
        "¡Genial! ¿Cómo preferís recibirlo: *retiro en Sucursal Piloto* o *envío a domicilio*?"
    assert chh.pregunta_entrega(cfg, saludo=False) == _REPREGUNTA
    assert chh.texto_entrega("retiro", None, sucursal="Sucursal Piloto") == \
        "🏪 Lo retirás en *Sucursal Piloto* (te enviamos el código al confirmar el pago)."
    assert chh.texto_entrega("retiro", None, sucursal="") == \
        "🏪 Lo retirás en la sucursal (te enviamos el código al confirmar el pago)."
    assert chh.texto_entrega("envio", "Mitre 100", sucursal="Sucursal Piloto") == \
        "🚚 Te lo enviamos a domicilio a *Mitre 100*."


def test_responder_horario_con_cierre():
    cfg = _CfgEnt()
    assert chh.responder_horario(cfg, {"enabled": False}) == \
        f"Atendemos {_HORARIO} 🕐 ¿Te ayudo con algo más?"
    assert chh.responder_horario(cfg, {"enabled": False}, cierre=_REPREGUNTA) == \
        f"Atendemos {_HORARIO} 🕐 {_REPREGUNTA}"


async def test_link_de_retiro_nombra_la_sucursal(monkeypatch):
    from app.services import config_service as cs
    from app.services.session_service import SessionService
    cfg = _CfgEnt(_SUC)
    monkeypatch.setattr(cs, "get_config_service", lambda *a, **k: cfg)

    async def _sin_freno(*a, **k):
        return None, None
    monkeypatch.setattr(chh, "_chequear_stock_vivo", _sin_freno)

    class _Pago:
        async def crear_link(self, **k):
            return "https://pago/abc", None
    ss = SessionService("redis://127.0.0.1:1")
    await ss.set_pending(PHONE, sku_id="30", sku_nombre="DOG CHOW ADULTO RAZAS MEDIANAS 15KG",
                         precio=52000.0, cantidad=1, opciones=[])
    resp, link = await chh.crear_link_y_responder(_Pago(), ss, PHONE, await ss.get(PHONE), "retiro", None)
    assert link == "https://pago/abc"
    assert "🏪 Lo retirás en *Sucursal Piloto* (te enviamos el código al confirmar el pago)." in resp


# ── Webhook ──────────────────────────────────────────────────────────────────────
async def test_a_pregunta_por_la_sucursal_al_elegir_entrega_se_responde(entorno, monkeypatch):
    deps, links = await _armar(entorno, monkeypatch, "esperando_entrega", _SUC)
    await wh.procesar_mensajes([_msg("en que sucursal puede ser?")])
    assert deps["wa"].enviados[-1] == f"{_INFO}\n\n{_REPREGUNTA}"
    assert links == [] and deps["intent"].llamadas == []
    assert await _estado(deps) == "esperando_entrega"
    # La elección que sigue funciona como siempre.
    await wh.procesar_mensajes([_msg("retiro")])
    assert links == [("retiro", None)]


async def test_b_sin_sucursal_cargada_responde_el_modelo_sin_inventar(entorno, monkeypatch):
    deps, links = await _armar(entorno, monkeypatch, "esperando_entrega")
    await wh.procesar_mensajes([_msg("en que sucursal puede ser?")])
    assert links == []
    assert await _estado(deps) == "esperando_entrega"
    proc = deps["intent"].vio("procesar")
    assert [c[1] for c in proc] == ["en que sucursal puede ser?"]
    situacion = proc[0][2]["situacion"]
    assert "Nunca inventes direcciones" in situacion and "*retiro en sucursal*" in situacion
    assert deps["wa"].enviados[-1] == "Te cuento 🙂"


async def test_c_cuanto_sale_el_envio_lo_responde_el_modelo(entorno, monkeypatch):
    deps, links = await _armar(entorno, monkeypatch, "esperando_entrega", _SUC)
    await wh.procesar_mensajes([_msg("cuánto sale el envío?")])
    assert links == []
    assert await _estado(deps) == "esperando_entrega"
    proc = deps["intent"].vio("procesar")
    assert [c[1] for c in proc] == ["cuánto sale el envío?"]
    assert "*retiro en Sucursal Piloto*" in proc[0][2]["situacion"]


@pytest.mark.parametrize("txt,estado,link", [
    ("¿me lo podés enviar?", "esperando_direccion", []),
    ("lo puedo retirar hoy?", "esperando_pago", [("retiro", None)]),
])
async def test_d_pedido_con_forma_de_pregunta_sigue_eligiendo(entorno, monkeypatch, txt, estado, link):
    deps, links = await _armar(entorno, monkeypatch, "esperando_entrega", _SUC)
    await wh.procesar_mensajes([_msg(txt)])
    assert await _estado(deps) == estado
    assert links == link


async def test_f_horario_al_elegir_entrega_vuelve_a_ofrecer_la_eleccion(entorno, monkeypatch):
    deps, links = await _armar(entorno, monkeypatch, "esperando_entrega", _SUC)
    await wh.procesar_mensajes([_msg("hasta qué hora puedo retirar?")])
    r = deps["wa"].enviados[-1]
    assert r == f"Atendemos {_HORARIO} 🕐 {_REPREGUNTA}"
    assert "¿Te ayudo con algo más?" not in r
    assert links == []
    assert await _estado(deps) == "esperando_entrega"


async def test_f_horario_fuera_de_la_entrega_igual_que_hoy(entorno):
    deps = entorno()
    deps["config"] = _CfgEnt(_SUC)
    deps["intent"] = _IntentEnt()
    await wh.procesar_mensajes([_msg("hasta qué hora puedo retirar?")])
    assert deps["wa"].enviados[-1] == f"Atendemos {_HORARIO} 🕐 ¿Te ayudo con algo más?"


async def test_e_pregunta_por_la_sucursal_al_confirmar_no_confirma(entorno, monkeypatch):
    deps, links = await _armar(entorno, monkeypatch, "esperando_confirmacion", _SUC)
    await wh.procesar_mensajes([_msg("en que sucursal puede ser?")])
    assert deps["wa"].enviados[-1] == f"{_INFO}\n\n¿Lo confirmamos?"
    assert links == [] and deps["intent"].llamadas == []
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "esperando_confirmacion" and s["pending_sku_id"] == "30"


async def test_e_sin_sucursal_la_pregunta_al_confirmar_va_al_modelo(entorno, monkeypatch):
    deps, links = await _armar(entorno, monkeypatch, "esperando_confirmacion")
    await wh.procesar_mensajes([_msg("en que sucursal puede ser?")])
    assert [c[1] for c in deps["intent"].vio("procesar")] == ["en que sucursal puede ser?"]
    assert links == []
    assert await _estado(deps) == "esperando_confirmacion"


async def test_g_donde_queda_la_sucursal_sin_pedido_le_llega_al_modelo(entorno):
    txt = "¿dónde queda la sucursal?"
    deps = entorno()
    deps["config"] = _CfgEnt(_SUC)
    deps["intent"] = _IntentEnt({txt: {"intencion": "desconocido", "entidad_producto": None,
                                       "respuesta": "Queda en Calle Falsa 123 🙂"}})
    await wh.procesar_mensajes([_msg(txt)])
    proc = deps["intent"].vio("procesar")
    assert len(proc) == 1 and _INFO in proc[0][2]["contexto_kb"]
    assert (await deps["session"].get(PHONE)).get("estado") != "operador"
    assert deps["wa"].enviados[-1] == "Queda en Calle Falsa 123 🙂"


async def test_g_sin_sucursal_cargada_igual_que_hoy(entorno):
    txt = "¿dónde queda la sucursal?"
    deps = entorno()
    deps["config"] = _CfgEnt()
    deps["intent"] = _IntentEnt({txt: {"intencion": "desconocido", "entidad_producto": None,
                                       "respuesta": "Queda en Calle Falsa 123 🙂"}})
    await wh.procesar_mensajes([_msg(txt)])
    assert deps["intent"].vio("procesar") == []          # sin KB ni sucursal: no hay con qué
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "no_entendido"
