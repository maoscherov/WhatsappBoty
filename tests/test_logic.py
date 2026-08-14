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


# ── Webhooks de pago con comercio en la ruta (preparación multi-cliente) ────────
def test_rutas_webhook_con_comercio():
    """
    El webhook de MP sólo trae el id del pago: el comercio debe poder venir en
    la URL. Se registran ambas formas — con y sin comercio — para no invalidar
    las preferencias ya emitidas.
    """
    import app.main as m
    rutas = {r.path for r in m.app.routes if hasattr(r, "path")}
    assert {"/mp/notification", "/mp/notification/{comercio}"} <= rutas
    assert {"/payway/notification", "/payway/notification/{comercio}"} <= rutas


# ── Vertical mutual: derivaciones financieras obligatorias ─────────────────────
@pytest.mark.parametrize("txt,motivo", [
    ("hola, te paso el comprobante de la transferencia", "comprobante"),
    ("necesito hacer una transferencia", "transferencia"),
    ("quiero renovar mi plazo fijo", "plazo_fijo_renovacion"),
    ("cuánto es la cuota de mi préstamo?", "cuota_prestamo"),
    ("me decís el saldo de mi caja de ahorro?", "saldo"),
    ("cuándo vence mi plazo fijo", "plazo_fijo_vencimiento"),
])
def test_derivacion_financiera_obligatoria(txt, motivo):
    """
    Datos de cuentas: el bot no debe intentar responderlos nunca (spec 2.9).
    Se resuelve en código, antes de llegar al modelo.
    """
    from app.services.mutual_helper import requiere_derivacion_financiera
    assert requiere_derivacion_financiera(txt) == motivo


@pytest.mark.parametrize("txt", [
    "qué horarios tienen?",
    "cuáles son los requisitos para un préstamo?",
    "qué beneficios tiene ser socio",
    "cuánto está la cuota social",
    "hola buen día",
])
def test_consultas_informativas_no_derivan(txt):
    """Lo institucional lo responde el bot con la base de conocimiento."""
    from app.services.mutual_helper import requiere_derivacion_financiera
    assert requiere_derivacion_financiera(txt) is None


def test_mensaje_derivacion_personalizado():
    from app.services.mutual_helper import mensaje_derivacion
    msg = mensaje_derivacion("saldo", "Claudia")
    assert msg.startswith("Claudia,")
    assert "saldo" in msg.lower()


# ── Webhook de Kapso: traducción a nuestro formato ─────────────────────────────
def test_kapso_traduce_texto():
    from app.routers.webhook import _kapso_a_mensajes
    ev = {"phone_number_id": "123",
          "message": {"id": "wamid.1", "from": "+5493416470114", "type": "text",
                      "text": {"body": "¿qué horarios tienen?"},
                      "kapso": {"direction": "inbound"}}}
    m = _kapso_a_mensajes(ev)[0]
    assert m["from"] == "5493416470114"      # sin el "+"
    assert m["text"] == "¿qué horarios tienen?"
    assert m["phone_number_id"] == "123"


def test_kapso_ignora_salientes():
    """Los mensajes que enviamos vuelven como evento: si se procesaran, el bot
    se respondería a sí mismo."""
    from app.routers.webhook import _kapso_a_mensajes
    ev = {"message": {"id": "x", "from": "549", "type": "text",
                      "text": {"body": "respuesta del bot"},
                      "kapso": {"direction": "outbound"}}}
    assert _kapso_a_mensajes(ev) == []


def test_kapso_audio_ya_transcripto():
    """Kapso transcribe el audio: no hace falta descargarlo ni pasarlo por Whisper."""
    from app.routers.webhook import _kapso_a_mensajes
    ev = {"message": {"id": "a1", "from": "549", "type": "audio",
                      "kapso": {"direction": "inbound",
                                "transcript": {"text": "necesito un préstamo"}}}}
    assert _kapso_a_mensajes(ev)[0]["texto_transcripto"] == "necesito un préstamo"


def test_kapso_imagen_con_url():
    from app.routers.webhook import _kapso_a_mensajes
    ev = {"message": {"id": "i1", "from": "549", "type": "image",
                      "image": {"caption": "mirá esto"},
                      "kapso": {"direction": "inbound", "media_url": "https://x/y.jpg",
                                "media_data": {"content_type": "image/png"}}}}
    m = _kapso_a_mensajes(ev)[0]
    assert m["media_url"] == "https://x/y.jpg"
    assert m["image_mime_type"] == "image/png"
    assert m["text"] == "mirá esto"


# ── Firma del webhook de Kapso ─────────────────────────────────────────────────
def test_firma_kapso():
    """
    Sin verificación, cualquiera que conozca la URL podría inyectar mensajes
    y hacer que el bot le escriba a números arbitrarios.
    """
    import hashlib, hmac, json
    from app.routers.webhook import _firma_kapso_valida

    secret = "sk_test_abc123"
    payload = {"message": {"id": "x", "from": "549", "type": "text", "text": {"body": "hola"}}}
    raw = json.dumps(payload).encode()
    firma = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()

    assert _firma_kapso_valida(raw, firma, secret) is True
    assert _firma_kapso_valida(raw, "sha256=" + firma.upper(), secret) is True   # tolerante
    assert _firma_kapso_valida(raw, "a" * 64, secret) is False                   # firma falsa
    assert _firma_kapso_valida(raw, "", secret) is False                         # sin firma

    # Kapso firma el JSON re-serializado (estilo JavaScript), que no siempre
    # coincide byte a byte con el cuerpo recibido.
    compacto = json.dumps(payload, separators=(",", ":")).encode()
    firma_js = hmac.new(secret.encode(), compacto, hashlib.sha256).hexdigest()
    assert _firma_kapso_valida(raw, firma_js, secret) is True


# ── Simulador de préstamos (Mutual AMI) ────────────────────────────────────────
def test_simulador_amortiza_correctamente():
    """
    La cuota tiene que amortizar el capital exactamente en el plazo: se verifica
    descontando mes a mes hasta que el saldo quede en cero.
    """
    from app.services.mutual_helper import simular_prestamo
    monto, cuotas, tna = 1_500_000, 12, 55
    s = simular_prestamo(monto, cuotas, {})
    i = (tna / 100) / 12
    saldo = monto
    for _ in range(cuotas):
        saldo = saldo + saldo * i - s["cuota"]
    assert abs(saldo) < 1, f"quedó saldo {saldo:.2f}: la cuota no amortiza"


def test_simulador_elige_linea():
    """Preferencial sólo si el monto y el plazo entran en su rango."""
    from app.services.mutual_helper import simular_prestamo
    assert simular_prestamo(1_500_000, 12, {})["linea"] == "preferencial"
    assert simular_prestamo(500_000, 24, {})["linea"] == "general"      # monto bajo
    assert simular_prestamo(2_000_000, 24, {})["linea"] == "general"    # plazo largo
    assert simular_prestamo(1_000_000, 48, {}).get("error") == "plazo_excedido"


def test_simulador_tasas_configurables():
    """Las tasas cambian seguido: deben poder editarse sin tocar código."""
    from app.services.mutual_helper import simular_prestamo
    # 500.000 queda fuera del rango preferencial → usa la tasa general
    base = simular_prestamo(500_000, 24, {})
    otra = simular_prestamo(500_000, 24, {"mutual_tna_general": "90"})
    assert base["tna"] == 75 and otra["tna"] == 90
    assert otra["cuota"] > base["cuota"]

    # Y la preferencial se ajusta por su propia clave
    pref = simular_prestamo(2_000_000, 12, {"mutual_tna_preferencial": "40"})
    assert pref["linea"] == "preferencial" and pref["tna"] == 40


def test_simulador_aclara_lo_que_no_incluye():
    """Sin IVA ni gastos cargados, el mensaje tiene que decirlo explícitamente."""
    from app.services.mutual_helper import simular_prestamo, texto_simulacion
    txt = texto_simulacion(simular_prestamo(1_500_000, 12, {}), {})
    assert "estimativo" in txt.lower()
    assert "no incluye" in txt.lower()


def test_simulacion_ofrece_oficial():
    """La simulación es capital + interés: para avanzar se pasa con un oficial."""
    from app.services.mutual_helper import simular_prestamo, texto_simulacion
    txt = texto_simulacion(simular_prestamo(1_500_000, 12, {}), {})
    assert "oficial" in txt.lower()


# ── Auto-liberación: si nadie atiende, la conversación vuelve al bot ───────────
async def test_auto_liberar_derivadas_sin_atender():
    """
    Sin gente atendiendo, una conversación derivada queda muda. Se devuelve al
    bot, salvo que un agente ya la haya tomado.
    """
    import time
    from app.services.session_service import SessionService

    ss = SessionService("redis://127.0.0.1:1")
    await ss.set_estado("A", "operador", motivo="saldo")      # nadie la tomó
    ss._memory["A"]["derivada_at"] = time.time() - 1200
    await ss.set_estado("B", "operador", motivo="saldo")      # la tomó un agente
    ss._memory["B"]["derivada_at"] = time.time() - 1200
    ss._memory["B"]["agente"] = "belen"
    await ss.set_estado("C", "operador", motivo="saldo")      # recién derivada

    assert await ss.derivadas_sin_atender(600) == ["A"]

    await ss.liberar("A")
    s = await ss.get("A")
    assert s["estado"] == "idle"
    assert "derivada_at" not in s and "derivada_motivo" not in s


@pytest.mark.parametrize("txt", [
    "cuál es el alias?", "me pasás el cbu", "necesito el cvu para transferir",
    "¿dónde deposito?", "a qué cuenta transfiero", "datos para transferencia",
])
def test_pedir_alias_no_deriva(txt):
    """
    Pedir el alias/CBU es información pública que el bot debe responder.
    Distinto de "necesito que hagan una transferencia", que sí deriva.
    """
    from app.services.mutual_helper import requiere_derivacion_financiera
    assert requiere_derivacion_financiera(txt) is None


def test_alias_esta_en_la_base_de_conocimiento():
    from scripts.cargar_kb_mutual import DOCUMENTOS
    texto = " ".join(c for _, c in DOCUMENTOS).upper()
    assert "AMICORREA" in texto
    assert "CBU" in texto and "CVU" in texto   # se busca de todas esas formas


@pytest.mark.parametrize("txt", [
    "quiero un préstamo de un millón y medio en 12 cuotas",
    "y en 24 cuotas?", "cuánto sería en 36 meses",
    "cuánto pagaría por 2 millones", "simulame 500000 en un año",
])
def test_menciona_simulacion(txt):
    from app.services.mutual_helper import menciona_simulacion
    assert menciona_simulacion(txt) is True


@pytest.mark.parametrize("txt", [
    "me pasás el cvu?", "cuál es el alias", "qué horarios tienen",
    "gracias!", "dale", "cómo me asocio",
])
def test_no_simula_si_el_mensaje_no_lo_pide(txt):
    """
    Regresión: tras simular, el modelo arrastraba monto y plazo del turno
    anterior y el bot repetía la misma cuota ante cualquier otra pregunta
    (caso real: se preguntó el CVU y respondió la simulación).
    """
    from app.services.mutual_helper import menciona_simulacion
    assert menciona_simulacion(txt) is False
