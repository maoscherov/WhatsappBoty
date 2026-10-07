"""
Pesos y presentaciones (spec petshop §5, "Correcciones de pesos y
presentaciones"). CAMBIO QUE TAMBIÉN AFECTA A LA FARMACIA: corre con los dos
perfiles.

- Bug 1: "la bolsa de 15 kg" se tomaba como domicilio ("bolsa de 15") y en
  esperando_entrega salía un link con envío a esa "dirección".
- Bug 2: "sí, pero el de 3 kg" confirmaba la bolsa de 15 pendiente, porque
  numeros_de ignora los números de una cifra y los decimales.
"""
import pytest

from app.routers import webhook as wh
from app.services import checkout_helper as ch
from app.services.sku_service import SKUService, nombre_coincide
from test_webhook_secuencias import PHONE, _msg, entorno  # noqa: F401  (entorno es fixture)


@pytest.fixture(params=["farmacia", "petshop"])
def perfil(request, usar_perfil):
    return usar_perfil(request.param)


# ── Bug 1: un peso o un envase no es un domicilio ────────────────────────────────
@pytest.mark.parametrize("txt", [
    "la bolsa de 15 kg",
    "mandame la de 15 kilos",
    "dos latas de 85",
    "el de 2 litros",          # ya daba None (una cifra no es altura): guarda
    "una de 3 kgs",            # ídem
])
def test_peso_o_envase_no_es_domicilio(perfil, txt):
    assert ch.extraer_direccion_de(txt) is None
    assert ch.parece_direccion(txt) is False


@pytest.mark.parametrize("txt,esperado", [
    ("san javier 837", "san javier 837"),
    ("Ruta 8 kilómetro 52", "Ruta 8 kilómetro 52"),     # por eso no va "kilo\w*"
    ("16 de enero 9279", "16 de enero 9279"),
    ("donado 608 piso 2", "donado 608 piso 2"),
])
def test_direccion_con_numeros_sigue_siendo_domicilio(perfil, txt, esperado):
    assert ch.extraer_direccion_de(txt) == esperado


# ── Por el webhook ───────────────────────────────────────────────────────────────
_ROYAL_15 = "ROYAL CANIN MEDIUM ADULT 15KG"
_ROYAL_3 = "ROYAL CANIN MEDIUM ADULT 3KG"


def _catalogo_royal():
    base = {"hash": "b" * 64, "barcodes": [], "troquel": None, "brand": "Royal Canin",
            "drug": None, "form": None, "category": "Alimento Perros", "rubro": "",
            "subrubro": "", "therapeutic_actions": [], "stock": 5, "visible": True,
            "active": True, "requiere_receta": "no", "source": "t"}
    return SKUService.from_rows([
        {**base, "external_id": "RC15", "name": _ROYAL_15, "price": 98000.0},
        {**base, "external_id": "RC3", "name": _ROYAL_3, "price": 24500.0},
    ])


class _Pago:
    """Proveedor de pago falso: registra cada link pedido."""
    def __init__(self):
        self.links = []

    async def crear_link(self, sku_id, nombre, precio, phone, cantidad=1):
        self.links.append(nombre)
        return f"https://pago.test/{len(self.links)}", None


def _armar(entorno, monkeypatch, guion):
    """Entorno del webhook con el catálogo Royal Canin y un cobro falso. El
    webhook elige el proveedor en cada mensaje (payment_svc_para, webhook.py:1278),
    así que se fija ahí y no solo en deps["payment"]."""
    deps = entorno(guion)
    deps["sku"] = _catalogo_royal()
    pago = _Pago()
    deps["payment"] = pago
    monkeypatch.setattr(wh, "payment_svc_para", lambda cfg, s=None: pago)
    return deps, pago


async def _pendiente_royal_15(deps, estado):
    await deps["session"].set_pending(PHONE, sku_id="RC15", sku_nombre=_ROYAL_15,
                                      precio=98000.0, cantidad=1, opciones=[])
    if estado != "esperando_confirmacion":
        await deps["session"].set_estado(PHONE, estado)


async def test_bolsa_de_15_kg_en_entrega_no_genera_envio(perfil, entorno, monkeypatch):
    txt = "la bolsa de 15 kg"
    deps, pago = _armar(entorno, monkeypatch, {txt: {
        "intencion": "pedido",
        "respuesta": "Sí, es la de 15 kg. ¿La retirás o te la enviamos?"}})
    await _pendiente_royal_15(deps, "esperando_entrega")

    await wh.procesar_mensajes([_msg(txt)])

    s = await deps["session"].get(PHONE)
    assert pago.links == []                                   # ningún link
    assert s["estado"] == "esperando_entrega"                 # sigue eligiendo la entrega
    assert s.get("tipo_entrega") != "envio" and not s.get("direccion_envio")
    assert deps["wa"].enviados and "bolsa de 15*" not in deps["wa"].enviados[-1]


# ── Bug 2: presentaciones con unidad ─────────────────────────────────────────────
@pytest.mark.parametrize("txt,esperado", [
    ("royal canin 7,5 kg", {("g", 7500.0)}),
    ("400GRS", {("g", 400.0)}),
    ("1L", {("ml", 1000.0)}),
    ("pretal kipper n 4", {("n", 4.0)}),
    ("Nº 4", {("n", 4.0)}),
    ("ibuprofeno 600", set()),
    # Dosis combinada de farmacia: la unidad vale para los dos números
    ("Janumet 50/1000 Mg Comp.X 28", {("g", 0.05), ("g", 1.0)}),
])
def test_presentaciones_de(perfil, txt, esperado):
    assert ch.presentaciones_de(txt) == esperado


@pytest.mark.parametrize("entidad,pendiente,contradice", [
    ("royal canin 3 kg", "ROYAL CANIN MEDIUM ADULT 15KG", True),        # hoy confirma (bug)
    ("royal canin 15 kg", "ROYAL CANIN MEDIUM ADULT 15KG", False),
    ("royal canin 7.5 kg", "ROYAL CANIN MINI ADULT 7,5 KG", False),     # 7500 g = 7500 g
    ("royal urinary 400 gr", "ROYAL URINARY CAT LATA 400GRS", False),
    ("royal urinary 1.5 kg", "ROYAL URINARY CAT LATA 400GRS", True),    # hoy confirma (bug)
    ("shampoo 2 litros", "SHAMPOO OSSPRET PERRO 1L", True),            # hoy confirma (bug)
    ("pretal kipper nº 3", "PRETAL KIPPER Nº 4", True),                # hoy confirma (bug)
    ("pretal kipper n 3", "PRETAL KIPPER Nº 4", True),                 # hoy confirma (bug)
    ("curflex x 30", "CURFLEX PLUS X 60", True),                        # regla de hoy
    ("ibuprofeno 600", "IBUPROFENO 600 MG X 10", False),                # sin unidad: regla de hoy
    ("royal canin", "ROYAL CANIN MEDIUM ADULT 15KG", False),            # sin presentación
    ("janumet 50 mg", "Janumet 50/1000 Mg Comp.X 28", False),           # guarda farmacia (dosis combinada)
])
def test_entidad_contradice_pendiente_por_presentacion(perfil, entidad, pendiente, contradice):
    assert nombre_coincide(entidad, pendiente)      # mismo producto: decide la presentación
    assert ch.entidad_contradice_pendiente(entidad, pendiente) is contradice


async def test_si_pero_el_de_3_kg_no_confirma_la_de_15(perfil, entorno, monkeypatch):
    txt = "sí, pero el de 3 kg"
    deps, pago = _armar(entorno, monkeypatch, {txt: {
        "intencion": "pedido", "confirmacion": True,
        "entidad_producto": "royal canin 3 kg",
        "respuesta": "Tengo la Royal Canin Medium Adult de 3 kg. ¿Te sirve?"}})
    await _pendiente_royal_15(deps, "esperando_confirmacion")

    await wh.procesar_mensajes([_msg(txt)])

    s = await deps["session"].get(PHONE)
    assert s["estado"] == "esperando_confirmacion"            # no pasó a la entrega
    assert "preferís" not in deps["wa"].enviados[-1].lower()  # no preguntó retiro/envío
    assert pago.links == []
    # Va como otro pedido: se buscó el de 3 kg y quedó entre las opciones
    assert "RC3" in [o["sku_id"] for o in s.get("pending_opciones") or []]


async def test_si_la_de_15_kg_confirma(perfil, entorno, monkeypatch):
    txt = "sí, la de 15 kg"
    deps, pago = _armar(entorno, monkeypatch, {txt: {
        "intencion": "pedido", "confirmacion": True,
        "entidad_producto": "royal canin 15 kg",
        "respuesta": "¡Perfecto!"}})
    await _pendiente_royal_15(deps, "esperando_confirmacion")

    await wh.procesar_mensajes([_msg(txt)])

    s = await deps["session"].get(PHONE)
    assert s["estado"] == "esperando_entrega"                 # confirmó: elige la entrega
    assert s["pending_sku_id"] == "RC15"
