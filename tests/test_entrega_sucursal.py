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
