"""
Tests de la lógica de negocio que no depende de servicios externos:
búsqueda de catálogo, receta desde categoría, matchers de entrega/humano.
"""

import csv
import os
import tempfile

import pytest

from app.services.sku_service import SKUService, requiere_derivacion
from app.services import checkout_helper as ch


# ── Catálogo de prueba (formato base, con categorías) ──────────────────────────
_ROWS = [
    # SKU, Nombre, Precio, Marca, Laboratorio, barras..., Categoria, Es_Medicamento
    ("1", "Platsul A Cre X800", "193316", "Soubeiran", "Soubeiran", "111", "Medicamentos Bajo Receta", "true"),
    ("2", "Ibuprofeno Fecofar Sus X90", "606.45", "Fecofar", "Fecofar", "222", "Medicamentos Bajo Receta", "true"),
    ("3", "Buscapina N Cto Gts X20", "5000", "Boehringer", "Boehringer", "333", "Venta Libre", "true"),
    ("4", "Shampoo Sedal Ceramidas 190 ml", "3000", "Sedal", "Unilever", "444", "Shampoo", "false"),
    ("5", "Bagovit A Plus Cre X100", "54734", "Bago", "Bago", "555", "Dermocosmética", "false"),
]


@pytest.fixture
def sku_svc():
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["SKU", "Nombre", "Precio", "Marca", "Laboratorio",
                    "Codigo_Barras_1", "Categoria", "Es_Medicamento"])
        w.writerows(_ROWS)
    svc = SKUService(path)
    yield svc
    os.remove(path)


# ── Receta derivada de la categoría ────────────────────────────────────────────
def test_receta_desde_categoria(sku_svc):
    platsul = sku_svc.get_by_barcode("111")
    busca = sku_svc.get_by_barcode("333")
    shampoo = sku_svc.get_by_barcode("444")
    assert platsul.requiere_receta == "si"   # Medicamentos Bajo Receta, no es OTC
    assert busca.requiere_receta == "no"     # Venta Libre
    assert shampoo.requiere_receta == "no"


def test_override_venta_libre(sku_svc):
    # Ibuprofeno está categorizado "Medicamentos Bajo Receta" en el fixture,
    # pero es OTC → la lista blanca lo fuerza a "no".
    ibu = sku_svc.get_by_barcode("222")
    assert ibu.requiere_receta == "no"


def test_es_venta_libre():
    from app.services.sku_service import es_venta_libre
    assert es_venta_libre("Elea AZIATOP ADVANCE 20 mg CAP x 28") is True
    assert es_venta_libre("Bayer Consumer ACTRON 600") is True
    assert es_venta_libre("Soubeiran Chobet PLATSUL A CRE x 200") is False


def test_sin_stock_y_vendible():
    from app.models.sku import SKU
    def _sku(**kw):
        base = dict(sku_id="1", barcode="1", sku_nombre="X", sku_nombre_original="X")
        base.update(kw)
        return SKU(**base)

    # Con dato de stock 0/neg y sin precio → sin stock, no vendible
    sin = _sku(stock_actual=-1, cantidad_visible=0, precio_venta=0)
    assert sin.sin_stock is True and sin.vendible is False and sin.estado == "sin_stock"

    # Con stock y precio → vendible
    ok = _sku(stock_actual=5, cantidad_visible=5, precio_venta=100)
    assert ok.sin_stock is False and ok.vendible is True and ok.estado == "disponible"

    # Formato base (sin dato de stock) con precio → vendible, "consultar"
    base = _sku(stock_actual=None, cantidad_visible=0, precio_venta=100)
    assert base.sin_stock is False and base.vendible is True and base.estado == "consultar"


def test_requiere_derivacion():
    assert requiere_derivacion("si", "conservador") is True
    assert requiere_derivacion("si", "estricto") is True
    assert requiere_derivacion("ambiguo", "conservador") is True
    assert requiere_derivacion("ambiguo", "estricto") is False
    assert requiere_derivacion("no", "conservador") is False


# ── Búsqueda ────────────────────────────────────────────────────────────────────
def test_busqueda_encuentra(sku_svc):
    r = sku_svc.buscar("platsul")
    assert r and "platsul" in r[0]["nombre"].lower()


def test_busqueda_sin_resultados(sku_svc):
    assert sku_svc.buscar("producto inexistente xyzzy") == []


def test_busqueda_sinonimo(sku_svc):
    # "ibuprofeno" no está literal pero el producto sí; el catálogo lo tiene por nombre
    r = sku_svc.buscar("ibuprofeno")
    assert any("ibuprofeno" in x["nombre"].lower() for x in r)


def test_busqueda_nombre_completo_no_arrastra_por_palabra_generica(sku_svc):
    """
    Regresión: un pedido con palabras de marketing ("plus", "power") no debe
    ranquear por esa palabra suelta. Caso real: "Framintrol Power NAD+"
    devolvía primero un GILLETTE DEO GEL *POWER* RUSH (perfumería) y el bot
    terminó enviando un link de pago de ese producto.
    """
    r = sku_svc.buscar("Bagovit A Plus crema hidratante")
    assert r, "debería encontrar el Bagovit"
    assert "bagovit" in r[0]["nombre"].lower(), f"primero quedó: {r[0]['nombre']}"


def test_busqueda_prioriza_token_distintivo(sku_svc):
    """El nombre propio pesa más que un formato compartido ('cre', 'x100')."""
    r = sku_svc.buscar("platsul crema x800")
    assert r and "platsul" in r[0]["nombre"].lower(), f"primero quedó: {r[0]['nombre']}"


# ── Matchers de entrega / humano ────────────────────────────────────────────────
@pytest.mark.parametrize("txt", ["retiro", "lo paso a buscar", "voy a la sucursal"])
def test_match_retiro(txt):
    assert ch.match_retiro(txt) is True
    assert ch.match_envio(txt) is False


@pytest.mark.parametrize("txt", ["envio", "a domicilio", "mandámelo a casa"])
def test_match_envio(txt):
    assert ch.match_envio(txt) is True
    assert ch.match_retiro(txt) is False


@pytest.mark.parametrize("txt", ["si ahí", "dale ahí", "sí, a mi domicilio"])
def test_afirma_envio(txt):
    assert ch.afirma_envio(txt) is True


@pytest.mark.parametrize("txt", ["lo quiero en otra dirección", "cambiar la dirección", "a otro lado"])
def test_quiere_cambiar_direccion(txt):
    assert ch.quiere_cambiar_direccion(txt) is True


@pytest.mark.parametrize("txt,esperado", [
    ("otra dirección donado 608 bis", "donado 608 bis"),
    ("donado 608 bis", "donado 608 bis"),
    ("16 de enero 9279", "16 de enero 9279"),
    ("lo quiero en otra dirección", None),   # sin dirección concreta → pedirla
    ("cuánto sale", None),                   # no es una dirección
    ("gracias", None),
])
def test_extraer_direccion(txt, esperado):
    assert ch.extraer_direccion_de(txt) == esperado


@pytest.mark.parametrize("txt", ["quiero hablar con una persona", "me pasás con un asesor?",
                                 "necesito atención humana"])
def test_pide_humano(txt):
    assert ch.pide_humano(txt) is True


@pytest.mark.parametrize("txt", ["quiero buscapina", "algo para una persona mayor", "hola"])
def test_no_pide_humano(txt):
    assert ch.pide_humano(txt) is False


# ── Derivación por receta: hand-off limpio ──────────────────────────────────────
async def test_derivar_receta_limpia_pending():
    """Al derivar por receta, se limpia el pending y queda en operador —
    así no queda un producto que re-dispare la derivación en cada mensaje."""
    from app.services.session_service import SessionService

    ss = SessionService("redis://127.0.0.1:1")  # sin Redis → in-memory
    await ss.set_pending("549", sku_id="X1", sku_nombre="Lotrial", precio=100.0,
                         cantidad=1, opciones=[])

    class _FakeSku:
        def get_by_id(self, sku_id):
            return type("S", (), {"requiere_receta": "si"})()

    msg = await ch.derivar_si_receta(_FakeSku(), ss, {"receta_mode": "conservador"}, "549", "X1")
    assert msg is not None
    sess = await ss.get("549")
    assert sess["estado"] == "operador"
    assert sess["pending_sku_id"] is None   # producto limpiado


# ── Coherencia respuesta ↔ producto pendiente ──────────────────────────────────
def test_precios_mencionados_formatos():
    """Reconoce el precio en los formatos que escribe el LLM."""
    assert 18057.61 in ch.precios_mencionados("link de pago ($18,057.61):")
    assert 18057.61 in ch.precios_mencionados("sale $18.057,61 con envío")
    assert 3148.88 in ch.precios_mencionados("el DOVE sale $3148.88")
    assert ch.precios_mencionados("no me figura disponible en este momento") == set()


def test_producto_respaldado_por_respuesta():
    """
    Regresión del caso real: el bot respondió "no me figura disponible" y el
    sistema igual dejó pendiente un GILLETTE de $18.057 (primer resultado de
    una búsqueda mala), terminando en un link de pago de ese producto.
    Sin precio en la respuesta no hay producto respaldado.
    """
    resultados = [
        {"sku_id": "A", "nombre": "GILLETTE DEO GEL POWER RUSH", "precio": 18057.61},
        {"sku_id": "B", "nombre": "FRAMINTROL COM x 30", "precio": 49866.75},
    ]
    sin_stock = "No me figura disponible el Framintrol Power NAD+ en este momento."
    assert ch.producto_respaldado(sin_stock, resultados) is None

    ofrece = "Sí, tenemos FRAMINTROL COM x 30 a $49.866,75. ¿Te lo reservo?"
    elegido = ch.producto_respaldado(ofrece, resultados)
    assert elegido and elegido["sku_id"] == "B"


# ── Consultas en medio del flujo (no deben tomarse como respuesta del paso) ─────
@pytest.mark.parametrize("txt", [
    "tendrás el precio", "cuánto sale el envío?", "hacen envío a Funes?",
    "cuánto tardan en prepararlo",
])
def test_consulta_no_es_direccion(txt):
    """
    Regresión: estando en 'esperando_direccion' cualquier texto se tomaba como
    domicilio y el link salía con la pregunta del cliente como dirección.
    """
    assert ch.extraer_direccion_de(txt) is None
    assert ch.parece_direccion(txt) is False


@pytest.mark.parametrize("txt", [
    "San Javier 837", "Av. Pellegrini 1234 piso 3", "donado 608 bis", "9 de julio 1200",
])
def test_direccion_sigue_reconociendose(txt):
    """El guard no debe rechazar direcciones reales."""
    assert ch.extraer_direccion_de(txt) or ch.parece_direccion(txt)


def test_consulta_no_matchea_entrega():
    """'tendrás el precio' no es ni retiro ni envío → debe responderse, no repetir."""
    t = "tendrás el precio"
    assert ch.match_retiro(t) is False
    assert ch.match_envio(t) is False
    assert ch.afirma_envio(t) is False


# ── Aceptación de la oferta de consultar un producto sin stock ─────────────────
@pytest.mark.parametrize("txt", ["si por favor", "dale", "sí, consultalo", "bueno", "claro"])
def test_acepta_consulta_sin_stock(txt):
    """
    El "si por favor" del caso real (respuesta a "¿lo consulto con el equipo?")
    debe reconocerse como aceptación para derivar a una persona.
    """
    from app.routers.webhook import _match_si, _match_no
    assert _match_si(txt) is True
    assert _match_no(txt) is False


@pytest.mark.parametrize("txt", ["no", "no gracias", "mejor no"])
def test_rechaza_consulta_sin_stock(txt):
    from app.routers.webhook import _match_no
    assert _match_no(txt) is True


# ── La aceptación de "consultar sin stock" no debe secuestrar una compra ───────
@pytest.mark.parametrize("txt", ["si por favor", "dale", "sí, consultalo", "ok dale", "claro que si"])
def test_afirmacion_pura(txt):
    from app.routers.webhook import _es_afirmacion_pura
    assert _es_afirmacion_pura(txt) is True


@pytest.mark.parametrize("txt", [
    "dale dame un dove",              # afirma pero además pide otro producto
    "ok mandame el link de pago",     # afirma pero pide otra cosa
    "si, pero quiero el shampoo",
    "no gracias",
])
def test_no_es_afirmacion_pura(txt):
    """Regresión: un 'dale' dentro de otro pedido no debe derivar la conversación."""
    from app.routers.webhook import _es_afirmacion_pura
    assert _es_afirmacion_pura(txt) is False
