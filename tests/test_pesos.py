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
    # Ronda de arreglo 1: "gramos" y una "l" sola también son unidades de peso/volumen
    "la de 400 gramos",
    "el de 500 gramos",
    "la de 15 l",
    "dame 2 de 1 l",           # ya daba None (cifras sueltas): guarda
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


async def test_la_de_400_gramos_en_entrega_no_genera_envio(perfil, entorno, monkeypatch):
    txt = "la de 400 gramos"
    deps, pago = _armar(entorno, monkeypatch, {txt: {
        "intencion": "pedido",
        "respuesta": "Sí, es la de 400 gramos. ¿La retirás o te la enviamos?"}})
    await _pendiente_royal_15(deps, "esperando_entrega")

    await wh.procesar_mensajes([_msg(txt)])

    s = await deps["session"].get(PHONE)
    assert pago.links == []                                   # ningún link
    assert s["estado"] == "esperando_entrega"                 # sigue eligiendo la entrega
    assert s.get("tipo_entrega") != "envio" and not s.get("direccion_envio")
    assert deps["wa"].enviados and "de 400*" not in deps["wa"].enviados[-1]


# ── Bug 2: presentaciones con unidad ─────────────────────────────────────────────
# El peso se normaliza a MILIGRAMOS (kg ×1.000.000, g ×1.000) y el volumen a
# mililitros (ruling del 7/10): así las dosis bajo 1 mg se distinguen entre sí.
@pytest.mark.parametrize("txt,esperado", [
    ("royal canin 7,5 kg", {("mg", 7_500_000.0)}),
    ("400GRS", {("mg", 400_000.0)}),
    ("1L", {("ml", 1000.0)}),
    ("pretal kipper n 4", {("n", 4.0)}),
    ("Nº 4", {("n", 4.0)}),
    ("ibuprofeno 600", set()),
    # Dosis combinada de farmacia: la unidad vale para los dos números
    ("Janumet 50/1000 Mg Comp.X 28", {("mg", 50.0), ("mg", 1000.0)}),
    # Todas las unidades del vocabulario
    ("royal canin 2 kilos", {("mg", 2_000_000.0)}),
    ("royal canin 1 kilo", {("mg", 1_000_000.0)}),
    ("royal canin 3 kilogramos", {("mg", 3_000_000.0)}),
    ("royal canin 3 kgs", {("mg", 3_000_000.0)}),
    ("la de 400 gramos", {("mg", 400_000.0)}),
    ("1 gramo", {("mg", 1000.0)}),
    ("2 g", {("mg", 2000.0)}),
    ("5 litros", {("ml", 5000.0)}),
    ("2 lts", {("ml", 2000.0)}),
    ("1,5 l", {("ml", 1500.0)}),
    ("500 cc", {("ml", 500.0)}),
    ("250 ml", {("ml", 250.0)}),
    ("pretal kipper numero 3", {("n", 3.0)}),
    ("pretal kipper número 3", {("n", 3.0)}),
    ("talle 5", {("n", 5.0)}),
    ("pretal kipper n° 2", {("n", 2.0)}),
    # Dosis bajo 1 mg: con gramos y 3 decimales todas caían en 0.0 / 0.001 / 0.002
    ("clonazepam 0,5 mg", {("mg", 0.5)}),
    ("CLONAZEPAM 1 MG", {("mg", 1.0)}),
    ("1,5 mg", {("mg", 1.5)}),
    ("2 mg", {("mg", 2.0)}),
    ("0,25 mg", {("mg", 0.25)}),
    ("0,1 mg", {("mg", 0.1)}),
    ("Gutron 2,5Mg", {("mg", 2.5)}),
    ("Janumet 0,5/1000 Mg", {("mg", 0.5), ("mg", 1000.0)}),
])
def test_presentaciones_de(perfil, txt, esperado):
    assert ch.presentaciones_de(txt) == esperado


@pytest.mark.parametrize("a,b", [
    ("clonazepam 0,5 mg", "CLONAZEPAM 1 MG"),
    ("1,5 mg", "2 mg"),
    ("0,25 mg", "0,1 mg"),
    ("0,5 mg", "0,05 mg"),
    ("400 gramos", "4 kilos"),
])
def test_presentaciones_de_distingue_valores_cercanos(perfil, a, b):
    assert ch.presentaciones_de(a) != ch.presentaciones_de(b)


@pytest.mark.parametrize("a,b", [
    ("0,25 mg", "0.25 MG"),
    ("1 g", "1000 mg"),
    ("0,4 kg", "400 gramos"),
    ("1,5 l", "1500 ml"),
    ("2 kilos", "2 kg"),
])
def test_presentaciones_de_unidades_equivalentes_dan_lo_mismo(perfil, a, b):
    assert ch.presentaciones_de(a) == ch.presentaciones_de(b)


@pytest.mark.parametrize("entidad,pendiente,contradice", [
    ("royal canin 3 kg", "ROYAL CANIN MEDIUM ADULT 15KG", True),        # hoy confirma (bug)
    ("royal canin 15 kg", "ROYAL CANIN MEDIUM ADULT 15KG", False),
    ("royal canin 7.5 kg", "ROYAL CANIN MINI ADULT 7,5 KG", False),     # 7.500.000 mg = 7.500.000 mg
    ("royal urinary 400 gr", "ROYAL URINARY CAT LATA 400GRS", False),
    ("royal urinary 1.5 kg", "ROYAL URINARY CAT LATA 400GRS", True),    # hoy confirma (bug)
    ("shampoo 2 litros", "SHAMPOO OSSPRET PERRO 1L", True),            # hoy confirma (bug)
    ("pretal kipper nº 3", "PRETAL KIPPER Nº 4", True),                # hoy confirma (bug)
    ("pretal kipper n 3", "PRETAL KIPPER Nº 4", True),                 # hoy confirma (bug)
    ("curflex x 30", "CURFLEX PLUS X 60", True),                        # regla de hoy
    ("ibuprofeno 600", "IBUPROFENO 600 MG X 10", False),                # sin unidad: regla de hoy
    ("royal canin", "ROYAL CANIN MEDIUM ADULT 15KG", False),            # sin presentación
    ("janumet 50 mg", "Janumet 50/1000 Mg Comp.X 28", False),           # guarda farmacia (dosis combinada)
    # Ronda de arreglo 1: unidades del vocabulario (kilos, litros, cc, numero, talle) y gramos
    ("royal canin 2 kilos", "ROYAL CANIN MEDIUM ADULT 15KG", True),
    ("royal urinary 400 gramos", "ROYAL URINARY CAT LATA 400GRS", False),
    ("royal urinary 0,4 kg", "ROYAL URINARY CAT LATA 400GRS", False),   # 400.000 mg = 400.000 mg
    ("shampoo 5 litros", "SHAMPOO OSSPRET PERRO 1L", True),
    ("shampoo 1 lt", "SHAMPOO OSSPRET PERRO 1L", False),
    ("shampoo 500 cc", "SHAMPOO OSSPRET PERRO 250 CC", True),
    ("shampoo 250 cc", "SHAMPOO OSSPRET PERRO 250 CC", False),
    ("pretal kipper numero 3", "PRETAL KIPPER Nº 4", True),
    ("pretal kipper talle 3", "PRETAL KIPPER Nº 4", True),
    ("pretal kipper talle 4", "PRETAL KIPPER Nº 4", False),
    # Dosis bajo 1 mg (farmacia): con gramos y 3 decimales se confundían y se cobraba la otra dosis
    ("clonazepam 0,5 mg", "CLONAZEPAM 1 MG", True),
    ("gutron 1,5 mg", "GUTRON 2 MG", True),
    ("nulipar 0,25 mg", "NULIPAR 0,1 MG", True),
    ("nulipar 0,25 mg", "NULIPAR 0.25 MG X 30", False),
    ("clonazepam 0,5 mg", "CLONAZEPAM 0,5 MG X 30", False),
    ("janumet 0,5 mg", "Janumet 1/1000 Mg Comp.X 28", True),
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
