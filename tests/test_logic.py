"""
Tests de la lógica de negocio que no depende de servicios externos:
búsqueda de catálogo, receta desde categoría, matchers de entrega/humano.
"""

import csv
import io
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


async def test_liberar_reinicia_contadores_de_conversacion():
    """
    Regresión: al volver del modo operador quedaban el reloj de la charla y el
    contador de negatividad. Con un `_conv_inicio` viejo, el bot derivaba por
    "conversación larga" en el primer mensaje y entraba en bucle: derivar →
    liberar → derivar.
    """
    import time
    from app.services.session_service import SessionService

    ss = SessionService("redis://127.0.0.1:1")
    await ss.set_estado("X", "operador", motivo="prestamo")
    s = await ss.get("X")
    s["_conv_inicio"] = time.time() - 8 * 3600     # charla de anoche
    s["_negativos"] = 2
    s["derivacion_ofrecida"] = "prestamo"
    await ss.save("X", s)

    await ss.liberar("X")
    s2 = await ss.get("X")
    assert s2["estado"] == "idle"
    for k in ("_conv_inicio", "_negativos", "derivacion_ofrecida",
              "derivada_at", "derivada_motivo", "agente"):
        assert k not in s2, f"quedó {k} y volvería a derivar"


# ── Simulador de plazo fijo (AMT) ──────────────────────────────────────────────
def test_amt_interes_por_dias_exactos():
    """Interés simple prorrateado por días exactos: monto × TNA × días / 365."""
    from app.services.mutual_helper import simular_amt
    s = simular_amt(1_000_000, 30, {})
    assert abs(s["online"]["interes"] - 1_000_000 * 0.26 * 30 / 365) < 0.01
    assert abs(s["presencial"]["interes"] - 1_000_000 * 0.235 * 30 / 365) < 0.01
    assert s["online"]["interes"] > s["presencial"]["interes"]   # online rinde más
    assert s["online"]["total"] == round(s["monto"] + s["online"]["interes"], 2)


def test_amt_valida_monto_y_plazo():
    from app.services.mutual_helper import simular_amt
    assert simular_amt(500, 30, {})["error"] == "monto_minimo"       # mínimo $1.000
    assert simular_amt(50_000, 90, {})["error"] == "plazo_invalido"  # máximo 60 días
    assert simular_amt(50_000, 20, {})["error"] == "plazo_invalido"  # mínimo 29 días
    assert "error" not in simular_amt(1_000, 29, {})


def test_amt_avisa_sellado_reducido_y_lo_no_incluido():
    from app.services.mutual_helper import simular_amt, texto_amt
    txt = texto_amt(simular_amt(100_000, 29, {}), {})
    assert "sellado" in txt.lower()
    assert "mitad" in txt.lower()          # a 29 días se reduce
    assert "no incluye" in txt.lower()     # y no está contemplado en el número


async def test_no_repetir_aviso_de_cierre():
    """
    Regresión: un cliente recibió tres veces el mismo mensaje de cierre. Si la
    sesión reaparece (dos instancias del job durante un deploy, o un Redis
    compartido entre entornos), el aviso no debe repetirse.
    """
    from app.services.session_service import SessionService
    ss = SessionService("redis://127.0.0.1:1")
    assert await ss.cierre_ya_avisado("549A") is False   # primera vez: se avisa
    assert await ss.cierre_ya_avisado("549A") is True    # ya avisado: no repetir
    assert await ss.cierre_ya_avisado("549B") is False   # otro cliente sí recibe


def test_sin_precio_no_hay_producto_ofrecido():
    """
    Regresión (caso real, tintura rubio ceniza): el bot respondió "no tengo
    stock" pero el modelo igual eligió un índice, y ese producto —un L'Oréal de
    $33.437 que nadie pidió— quedó pendiente y terminó en un link de pago.
    Sin precio dicho al cliente, no hay nada ofrecido.
    """
    resultados = [
        {"sku_id": "A", "nombre": "LOREAL MAGIC RETOUCH RUBIO CLARO MEDIO", "precio": 33437.53},
        {"sku_id": "B", "nombre": "KOLESTON SING 60 RUBIO OSCUR", "precio": 9555.28},
    ]
    sin_stock = ("Justo no tengo stock de una tintura rubio ceniza en este momento. "
                 "¿Te gustaría que te encargue la tintura rubio ceniza?")
    assert ch.producto_respaldado(sin_stock, resultados) is None

    ofrece = "Te puedo ofrecer la tintura KOLESTON SING 60 RUBIO OSCUR por $9.555,28."
    elegido = ch.producto_respaldado(ofrece, resultados)
    assert elegido and elegido["sku_id"] == "B"


# ── Carrito: varios productos en un pedido ─────────────────────────────────────
async def test_carrito_acumula_items():
    """'Agregame también...' suma al pedido en curso en vez de pisarlo."""
    from app.services.session_service import SessionService
    ss = SessionService("redis://127.0.0.1:1")
    await ss.set_pending("549", sku_id="T1", sku_nombre="Tintura Koleston",
                         precio=9555.28, cantidad=1, opciones=[])
    items = await ss.agregar_item("549", "G1", "Gomitas de menta", 300.0, 2)
    assert len(items) == 2
    assert items[0]["nombre"] == "Tintura Koleston"
    assert items[1]["cantidad"] == 2
    s = await ss.get("549")
    assert s["estado"] == "esperando_confirmacion"
    # Un pedido NUEVO reinicia el carrito
    await ss.set_pending("549", sku_id="X", sku_nombre="Otro", precio=100, cantidad=1)
    assert len((await ss.get("549"))["pending_items"]) == 1


async def test_link_de_pago_suma_el_carrito():
    """El link sale por el total de todos los productos, con el detalle."""
    from app.services.session_service import SessionService
    ss = SessionService("redis://127.0.0.1:1")
    await ss.set_pending("549", sku_id="T1", sku_nombre="Tintura", precio=1000.0, cantidad=1)
    await ss.agregar_item("549", "G1", "Gomitas", 300.0, 2)

    capturado = {}
    class _FakePago:
        async def crear_link(self, sku_id, nombre, precio, phone, cantidad=1):
            capturado.update(sku_id=sku_id, nombre=nombre, precio=precio, cantidad=cantidad)
            return "https://pago/x", None

    sesion = await ss.get("549")
    respuesta, link = await ch.crear_link_y_responder(_FakePago(), ss, "549", sesion, "retiro", None)
    assert link
    assert capturado["sku_id"] == "MULTI"
    assert capturado["precio"] == 1000.0 + 300.0 * 2      # total del carrito
    assert "Tintura + Gomitas x2" in respuesta
    assert "$1,600.00" in respuesta


class TestExtrasElegibles:
    """
    Regresión (caso real 27/8, María Belén): pidió 3 productos, el bot mostró
    los 3 con precio, ella dijo "Si mándame todos" y el link salió por UNO.
    Los productos del bloque "Sobre lo demás que me pediste" se mostraban pero
    no existían para el sistema: eran texto, no productos elegibles.
    """

    @pytest.mark.parametrize("txt", [
        "si mandame todos", "mandame todos", "los tres", "quiero todos",
        "dale, todo", "ambos", "las dos", "todas",
    ])
    def test_pide_todos(self, txt):
        assert ch.pide_todos(txt) is True

    @pytest.mark.parametrize("txt", [
        "no, todos no", "no quiero todos", "solo el gel", "el serum nada más",
        "cuánto sale?",
    ])
    def test_no_pide_todos(self, txt):
        assert ch.pide_todos(txt) is False

    async def test_los_extras_quedan_guardados_con_su_precio(self):
        from app.services.session_service import SessionService
        ss = SessionService("redis://127.0.0.1:1")
        extras = [
            {"sku_id": "S1", "nombre": "Serum Eximia Hydra Mat x30ml", "precio": 59147.20},
            {"sku_id": "T1", "nombre": "Toallas Antibacteriales Espadol x10u", "precio": 3960.45},
        ]
        await ss.guardar_extras("549", extras)
        s = await ss.get("549")
        assert len(s["extras_ofrecidos"]) == 2
        assert s["extras_ofrecidos"][0]["precio"] == 59147.20

    async def test_mandame_todos_suma_todo_y_el_link_cobra_la_suma(self):
        """El caso exacto que falló: 3 productos pedidos, 3 productos cobrados."""
        from app.services.session_service import SessionService
        ss = SessionService("redis://127.0.0.1:1")
        await ss.set_pending("549", sku_id="G1", sku_nombre="Eximia Aqua Gel",
                             precio=51214.36, cantidad=1)
        await ss.guardar_extras("549", [
            {"sku_id": "S1", "nombre": "Serum Eximia", "precio": 59147.20},
            {"sku_id": "T1", "nombre": "Toallas Espadol", "precio": 3960.45},
        ])
        items = await ss.sumar_extras("549")
        assert len(items) == 3

        capturado = {}

        class _FakePago:
            async def crear_link(self, sku_id, nombre, precio, phone, cantidad=1):
                capturado["precio"] = precio
                return "https://pago/x", None

        sesion = await ss.get("549")
        _, link = await ch.crear_link_y_responder(_FakePago(), ss, "549", sesion,
                                                  "envio", "San Victor 945")
        assert link
        esperado = round(51214.36 + 59147.20 + 3960.45, 2)
        assert round(capturado["precio"], 2) == esperado

    async def test_sumar_extras_los_limpia_para_no_duplicar(self):
        """Si no se limpian, un segundo "todos" los sumaría dos veces."""
        from app.services.session_service import SessionService
        ss = SessionService("redis://127.0.0.1:1")
        await ss.set_pending("549", sku_id="G1", sku_nombre="Gel", precio=100.0)
        await ss.guardar_extras("549", [{"sku_id": "S1", "nombre": "Serum", "precio": 200.0}])
        await ss.sumar_extras("549")
        assert (await ss.get("549")).get("extras_ofrecidos") == []
        assert await ss.sumar_extras("549") == []          # no vuelve a sumar

    async def test_reseleccionar_el_mismo_producto_no_pisa_el_carrito(self):
        """
        Regresión (caso real 31/8, María Belén): dijo "Todos" (carrito de 3),
        al confirmar el modelo re-eligió el MISMO producto principal y
        set_pending arrancó el carrito de cero — el link cobró $51.214 en vez
        de $104.855. Re-seleccionar el mismo SKU conserva el carrito.
        """
        from app.services.session_service import SessionService
        ss = SessionService("redis://127.0.0.1:1")
        await ss.set_pending("549", sku_id="G1", sku_nombre="Eximia Aqua Gel",
                             precio=51214.36, cantidad=1)
        await ss.guardar_extras("549", [
            {"sku_id": "S1", "nombre": "Serum Eximia", "precio": 50275.12, "cantidad": 1},
            {"sku_id": "T1", "nombre": "Toallas Espadol", "precio": 3366.38, "cantidad": 1},
        ])
        await ss.sumar_extras("549")
        # El modelo re-selecciona el índice del MISMO producto principal
        await ss.set_pending("549", sku_id="G1", sku_nombre="Eximia Aqua Gel",
                             precio=51214.36, cantidad=1)
        s = await ss.get("549")
        assert len(s["pending_items"]) == 3            # el carrito sobrevive

    async def test_elegir_otro_producto_si_resetea_el_carrito(self):
        """La regla original sigue: un producto DISTINTO arranca de cero."""
        from app.services.session_service import SessionService
        ss = SessionService("redis://127.0.0.1:1")
        await ss.set_pending("549", sku_id="G1", sku_nombre="Gel", precio=100.0)
        await ss.agregar_item("549", "S1", "Serum", 200.0, 1)
        await ss.set_pending("549", sku_id="X9", sku_nombre="Otro", precio=50.0)
        s = await ss.get("549")
        assert len(s["pending_items"]) == 1
        assert s["pending_items"][0]["sku_id"] == "X9"

    async def test_reseleccion_con_cantidad_nueva_actualiza_el_principal(self):
        """"Sí, pero que sean 2" sobre el mismo producto: cambia la cantidad,
        no pierde el resto del carrito."""
        from app.services.session_service import SessionService
        ss = SessionService("redis://127.0.0.1:1")
        await ss.set_pending("549", sku_id="G1", sku_nombre="Gel", precio=100.0, cantidad=1)
        await ss.agregar_item("549", "S1", "Serum", 200.0, 1)
        await ss.set_pending("549", sku_id="G1", sku_nombre="Gel", precio=100.0, cantidad=2)
        s = await ss.get("549")
        assert len(s["pending_items"]) == 2
        principal = next(i for i in s["pending_items"] if i["sku_id"] == "G1")
        assert principal["cantidad"] == 2
        assert s["pending_cantidad"] == 2

    async def test_los_extras_no_sobreviven_a_la_conversacion(self):
        """Estado que sobrevive entre charlas: ya nos mordió varias veces."""
        from app.services.session_service import SessionService
        ss = SessionService("redis://127.0.0.1:1")
        await ss.guardar_extras("549", [{"sku_id": "S1", "nombre": "Serum", "precio": 200.0}])
        await ss.clear_pending("549")
        assert not (await ss.get("549")).get("extras_ofrecidos")

        await ss.guardar_extras("549", [{"sku_id": "S1", "nombre": "Serum", "precio": 200.0}])
        await ss.liberar("549")
        assert not (await ss.get("549")).get("extras_ofrecidos")


class TestCotizacionSinLink:
    """
    La cotización de receta es una OFERTA: el link recién va cuando el
    cliente confirma. El operador "envía y delega" (el bot maneja el sí) o
    "envía y sigue atendiendo" (queda en modo operador con el pedido armado).
    """

    async def _sesion_derivada(self, ss, phone="549"):
        await ss.set_estado(phone, "operador", motivo="receta_foto")
        return await ss.get(phone)

    async def test_delegar_deja_al_bot_con_el_pedido_armado(self):
        from app.services.session_service import SessionService
        ss = SessionService("redis://127.0.0.1:1")
        await self._sesion_derivada(ss)
        await ss.armar_cotizacion("549", sku_id="Y1", sku_nombre="Yasminelle",
                                  precio=33422.99, delegar=True)
        s = await ss.get("549")
        assert s["estado"] == "esperando_confirmacion"     # el bot retoma
        assert s["pending_precio"] == 33422.99
        assert s["receta_validada"] is True                # el operador ya vio la receta
        assert "derivada_at" not in s                      # sale de la cola de derivadas

    async def test_seguir_atendiendo_queda_en_operador(self):
        from app.services.session_service import SessionService
        ss = SessionService("redis://127.0.0.1:1")
        await self._sesion_derivada(ss)
        await ss.armar_cotizacion("549", sku_id="Y1", sku_nombre="Yasminelle",
                                  precio=33422.99, delegar=False)
        s = await ss.get("549")
        assert s["estado"] == "operador"                   # el bot sigue mudo
        assert s["pending_precio"] == 33422.99
        assert s["receta_validada"] is True

    async def test_confirmar_no_rederiva_por_receta_y_cobra_lo_cotizado(self):
        """El candado: sin receta_validada este producto volvería al operador
        en loop; con la marca, el sí sigue derecho a entrega y link."""
        from app.services.session_service import SessionService

        class _SkuConReceta:
            def get_by_id(self, sku_id):
                from app.models.sku import SKU
                return SKU(sku_id="Y1", barcode="1", sku_nombre="Yasminelle",
                           sku_nombre_original="Yasminelle", precio_venta=46824.03,
                           requiere_receta="si")

        class _FakePago:
            def __init__(self): self.capturado = {}
            async def crear_link(self, sku_id, nombre, precio, phone, cantidad=1):
                self.capturado.update(precio=precio, cantidad=cantidad)
                return "https://pago/x", None

        class _SinPadron:
            def find_by_phone(self, p): return None

        ss = SessionService("redis://127.0.0.1:1")
        await self._sesion_derivada(ss)
        await ss.armar_cotizacion("549", sku_id="Y1", sku_nombre="Yasminelle",
                                  precio=33422.99, delegar=True)
        pago = _FakePago()
        sesion = await ss.get("549")
        respuesta, intencion = await ch.confirmar_pedido(
            _SkuConReceta(), pago, ss, _SinPadron(),
            {"receta_mode": "conservador", "envio_enabled": "true"},
            "549", sesion, entrega="retiro")
        assert intencion != "derivado_receta"              # NO volvió al operador
        assert pago.capturado["precio"] * pago.capturado.get("cantidad", 1) == 33422.99

    async def test_un_pedido_nuevo_distinto_pierde_el_candado(self):
        """La marca vale para ESE producto cotizado: si después elige otro
        producto con receta, deriva normalmente."""
        from app.services.session_service import SessionService
        ss = SessionService("redis://127.0.0.1:1")
        await ss.armar_cotizacion("549", sku_id="Y1", sku_nombre="Yasminelle",
                                  precio=100.0, delegar=True)
        await ss.set_pending("549", sku_id="OTRO", sku_nombre="Otro remedio",
                             precio=50.0)
        assert not (await ss.get("549")).get("receta_validada")

    async def test_reseleccion_del_mismo_producto_conserva_el_candado(self):
        from app.services.session_service import SessionService
        ss = SessionService("redis://127.0.0.1:1")
        await ss.armar_cotizacion("549", sku_id="Y1", sku_nombre="Yasminelle",
                                  precio=100.0, delegar=True)
        await ss.set_pending("549", sku_id="Y1", sku_nombre="Yasminelle", precio=100.0)
        assert (await ss.get("549")).get("receta_validada") is True

    async def test_cancelar_limpia_el_candado(self):
        from app.services.session_service import SessionService
        ss = SessionService("redis://127.0.0.1:1")
        await ss.armar_cotizacion("549", sku_id="Y1", sku_nombre="Y", precio=1.0,
                                  delegar=True)
        await ss.clear_pending("549")
        assert not (await ss.get("549")).get("receta_validada")

    def test_endpoint_modo_cotizar_sin_link(self):
        from fastapi.testclient import TestClient
        from app.main import app
        r = TestClient(app).post("/bo/paylink", json={
            "phone": "5490000000001", "detalle": "Yasminelle", "monto": 1000,
            "pct_os": 10, "plantilla": "receta", "modo": "cotizar",
            "delegar": True, "enviar": False,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert "http" not in body["mensaje"]               # SIN link
        assert "900" in body["mensaje"]                    # el precio cotizado sí
        assert body.get("link") is None

    def test_endpoint_modo_cotizar_rechaza_placeholder_link(self):
        from fastapi.testclient import TestClient
        from app.main import app
        r = TestClient(app).post("/bo/paylink", json={
            "phone": "549", "detalle": "X", "monto": 100, "modo": "cotizar",
            "mensaje": "Pagá acá {link}", "enviar": False,
        })
        assert r.status_code == 400


class TestCostoEnvio:
    """
    Costo de envío a domicilio (config envio_costo, "0" = gratis). Se suma al
    total del link y se avisa ANTES de elegir la entrega: nada de sorpresas
    en el precio.
    """

    class _FakePago:
        def __init__(self): self.capturado = {}
        async def crear_link(self, sku_id, nombre, precio, phone, cantidad=1):
            self.capturado.update(nombre=nombre, precio=precio, cantidad=cantidad)
            return "https://pago/x", None

    async def _con_costo(self, costo, tipo_entrega):
        from app.services.session_service import SessionService
        from app.services.config_service import get_config_service
        cfg_svc = get_config_service("redis://127.0.0.1:1")
        await cfg_svc.set("envio_costo", str(costo))
        try:
            ss = SessionService("redis://127.0.0.1:1")
            await ss.set_pending("549", sku_id="A1", sku_nombre="Actron", precio=1000.0)
            pago = self._FakePago()
            sesion = await ss.get("549")
            respuesta, link = await ch.crear_link_y_responder(
                pago, ss, "549", sesion, tipo_entrega, "San Victor 945")
            return respuesta, pago.capturado
        finally:
            await cfg_svc.set("envio_costo", "0")

    async def test_envio_suma_el_costo_y_lo_desglosa(self):
        respuesta, cap = await self._con_costo(2000, "envio")
        assert cap["precio"] * cap.get("cantidad", 1) == 3000.0
        assert "envío" in cap["nombre"].lower()
        assert "$3,000.00" in respuesta            # el total del mensaje lo incluye
        assert "2,000" in respuesta                # y el desglose lo aclara

    async def test_retiro_no_suma_nada(self):
        respuesta, cap = await self._con_costo(2000, "retiro")
        assert cap["precio"] * cap.get("cantidad", 1) == 1000.0

    async def test_costo_cero_es_el_comportamiento_de_siempre(self):
        respuesta, cap = await self._con_costo(0, "envio")
        assert cap["precio"] * cap.get("cantidad", 1) == 1000.0
        assert "2,000" not in respuesta

    def test_pregunta_de_entrega_muestra_el_costo(self):
        t = ch.pregunta_entrega({"envio_costo": "2000"})
        assert "envío a domicilio" in t and "+$2,000" in t
        t0 = ch.pregunta_entrega({"envio_costo": "0"})
        assert "+$" not in t0


class TestCotizacionReceta:
    """
    Cotización de recetas desde el backoffice: el operador carga precio y %
    de obra social; si el cliente es socio se combina el 15% (primero OS,
    después socio sobre el resultado — decisión 4/9). El desglose lo arma el
    código para que los números nunca estén mal escritos.
    """

    def _c(self, *a, **kw):
        from app.services.receta_ocr import cotizar_receta
        return cotizar_receta(*a, **kw)

    def test_ambos_descuentos_en_orden_os_primero(self):
        c = self._c(25000.0, pct_os=40, es_socio=True, pct_socio=15)
        # 25000 − 40% = 15000; 15000 − 15% = 12750
        assert c["precio_final"] == 12750.0
        assert c["precio_lista"] == 25000.0
        assert "obra social" in c["desglose"] and "40%" in c["desglose"]
        assert "15%" in c["desglose"] and "socio" in c["desglose"]
        assert "$12,750.00" in c["desglose"]

    def test_solo_obra_social(self):
        c = self._c(10000.0, pct_os=50, es_socio=False, pct_socio=15)
        assert c["precio_final"] == 5000.0
        assert c["pct_socio_aplicado"] == 0     # no es socio: no se aplica
        assert "socio" not in c["desglose"].lower()

    def test_solo_socio(self):
        c = self._c(10000.0, pct_os=0, es_socio=True, pct_socio=15)
        assert c["precio_final"] == 8500.0
        assert "obra social" not in c["desglose"].lower()
        assert "socio" in c["desglose"].lower()

    def test_sin_descuentos(self):
        c = self._c(9990.5)
        assert c["precio_final"] == 9990.5
        assert "$9,990.50" in c["desglose"]
        assert "descuento" not in c["desglose"].lower()

    def test_socio_sin_descuento_configurado_no_aplica(self):
        c = self._c(10000.0, pct_os=10, es_socio=True, pct_socio=0)
        assert c["precio_final"] == 9000.0
        assert c["pct_socio_aplicado"] == 0

    def test_redondeo_a_dos_decimales(self):
        c = self._c(9999.99, pct_os=33.33)
        assert c["precio_final"] == round(9999.99 * (1 - 0.3333), 2)

    def test_mensaje_editado_con_placeholder_link(self, monkeypatch):
        """
        El operador puede editar el texto de la cotización; el {link} lo
        inserta el backend con el link FRESCO del momento del envío — así el
        texto editado nunca queda con un link que cobra un importe viejo.
        """
        from fastapi.testclient import TestClient
        import app.routers.webhook as wh
        from app.main import app

        class _FakePago:
            async def crear_link(self, sku_id, nombre, precio, phone, cantidad=1):
                return "https://pago/FRESCO", None

        monkeypatch.setattr(wh, "payment_svc_para", lambda cfg, s: _FakePago())
        r = TestClient(app).post("/bo/paylink", json={
            "phone": "549341", "detalle": "Yasminelle", "monto": 1000,
            "pct_os": 10, "plantilla": "receta", "enviar": False,
            "mensaje": "Hola! Tu remedio sale $900. Pagá acá: {link} — gracias!",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert "https://pago/FRESCO" in body["mensaje"]
        assert "{link}" not in body["mensaje"]
        assert body["mensaje"].startswith("Hola! Tu remedio")   # el texto editado manda

    def test_endpoint_paylink_receta_responde(self):
        """Smoke HTTP: atraviesa el router con pct_os y plantilla receta (los
        imports y el cálculo corren aunque el proveedor de pago no responda)."""
        from fastapi.testclient import TestClient
        from app.main import app
        r = TestClient(app).post("/bo/paylink", json={
            "phone": "549341", "detalle": "Yasminelle x28", "monto": 25000,
            "pct_os": 40, "plantilla": "receta", "enviar": False,
        })
        assert r.status_code == 200
        body = r.json()
        if body.get("ok"):                       # con proveedor de pago activo
            assert body["cotizacion"]["precio_final"] == 15000.0
            assert "obra social" in body["mensaje"]
        else:                                    # sin credenciales de pago
            assert "error" in body


class TestTablero:
    """Agregador del tablero CERCA (/bo/tablero)."""

    def test_clasificar_fuera_horario(self):
        from app.services.metrics_store import clasificar_fuera_horario
        assert clasificar_fuera_horario("2:13") == "mediodia"   # martes 13hs
        assert clasificar_fuera_horario("4:22") == "nocturno"   # viernes 22hs
        assert clasificar_fuera_horario("1:7") == "nocturno"    # madrugada
        assert clasificar_fuera_horario("5:11") == "finde"      # sábado
        assert clasificar_fuera_horario("6:15") == "finde"      # domingo
        assert clasificar_fuera_horario("basura") == "otro"
        assert clasificar_fuera_horario("") == "otro"

    def test_variacion_pct(self):
        from app.services.metrics_store import variacion_pct
        assert variacion_pct(110, 100) == 10.0
        assert variacion_pct(90, 100) == -10.0
        assert variacion_pct(50, 0) is None     # sin base de comparación
        assert variacion_pct(None, 100) is None

    def test_endpoint_tablero_responde_200(self):
        """
        Regresión (4/9): /bo/tablero devolvió 500 en producción por un import
        faltante que ningún test unitario ejercitaba. El smoke por HTTP
        atraviesa el router de verdad.
        """
        from fastapi.testclient import TestClient
        from app.main import app
        r = TestClient(app).get("/bo/tablero?mes=2026-09")
        assert r.status_code == 200
        body = r.json()
        assert body["vertical"] in ("farmacia", "mutual")
        assert "panorama" in body

    async def test_tablero_sin_db_devuelve_estructura_con_sin_dato(self):
        """Sin Postgres el endpoint no explota: devuelve la estructura con
        badges sin_dato, para que la página siempre renderice."""
        from app.services.metrics_store import MetricsStore

        class _SinDB:
            def available(self): return False
            async def fetch(self, *a): return []

        t = await MetricsStore(_SinDB()).tablero("farmacia", "2026-09")
        assert t["vertical"] == "farmacia"
        assert "panorama" in t and "producto" in t and "pagos" in t
        assert t["panorama"]["conversaciones"]["badge"] == "sin_dato"

        tm = await MetricsStore(_SinDB()).tablero("mutual", "2026-09")
        assert "distribucion" in tm and "derivaciones" in tm


class TestContextoFresco:
    """
    Regresión (caso real 1/9, María): volvió al día siguiente (~22hs después,
    sesión viva por el link pendiente de 24hs) con un pedido nuevo, y el
    modelo — con todo el historial de ayer — respondió "Tu pedido queda
    confirmado para retirar en sucursal... ¡Gracias por tu compra!" sin que
    exista tal pedido. Tras una pausa larga, la charla arranca de cero.
    """

    def test_contexto_vencido(self):
        import time
        from app.services.session_service import contexto_vencido
        vieja = {"_last_activity": time.time() - 3 * 3600, "estado": "esperando_pago"}
        assert contexto_vencido(vieja, minutos=120) is True
        reciente = {"_last_activity": time.time() - 600, "estado": "esperando_pago"}
        assert contexto_vencido(reciente, minutos=120) is False
        # Derivadas a una persona NUNCA se reinician solas: las maneja el humano
        operador = {"_last_activity": time.time() - 9 * 3600, "estado": "operador"}
        assert contexto_vencido(operador, minutos=120) is False
        assert contexto_vencido({}, minutos=120) is False       # sesión nueva
        assert contexto_vencido(vieja, minutos=0) is False      # 0 = apagado

    async def test_reiniciar_contexto_limpia_charla_y_pedido_viejo(self):
        import time
        from app.services.session_service import SessionService
        ss = SessionService("redis://127.0.0.1:1")
        await ss.set_pending("549", sku_id="G1", sku_nombre="Gel", precio=100.0)
        s = await ss.get("549")
        s["history"] = [{"role": "user", "content": "todos", "ts": time.time()}]
        s["estado"] = "esperando_pago"
        s["_espera_eleccion"] = True
        await ss.save("549", s)

        await ss.reiniciar_contexto("549")
        s2 = await ss.get("549")
        assert s2["history"] == []
        assert s2["pending_sku_id"] is None
        assert s2["estado"] == "idle"
        assert "_espera_eleccion" not in s2


class TestConfirmacionesFantasma:
    """
    El modelo declaró "pedido confirmado" y "gracias por tu compra" sin que el
    sistema confirmara nada. Esos anuncios los hace SOLO el código (al armar
    el carrito o generar el link); si vienen del modelo, se recortan.
    """

    def test_recorta_el_mensaje_real_conservando_el_precio(self):
        r = ch.quitar_confirmaciones_fantasma(
            "¡Perfecto, María! Tu pedido queda confirmado para retirar en sucursal. "
            "El Eximia Hydra Legere Aqua Gel X50 Gr está a $51,214.36, ya con tu 15% "
            "de descuento de socio. Te esperamos en la sucursal para retirarlo. "
            "¡Gracias por tu compra! 😊")
        assert "confirmado" not in r.lower()
        assert "gracias por tu compra" not in r.lower()
        assert "te esperamos" not in r.lower()
        assert "$51,214.36" in r          # la oferta real sobrevive intacta

    def test_no_toca_la_pregunta_de_confirmacion(self):
        t = "Total: $1,600.00 — ¿lo confirmamos?"
        assert ch.quitar_confirmaciones_fantasma(t) == t

    def test_no_borra_oraciones_con_importe(self):
        t = "Tu compra confirmada es de $500.00 en total."
        assert "$500.00" in ch.quitar_confirmaciones_fantasma(t)

    def test_nunca_devuelve_vacio(self):
        assert ch.quitar_confirmaciones_fantasma("¡Gracias por tu compra!").strip()


class TestRecetaOcr:
    """
    OCR de recetas (activable con receta_ocr_enabled): al derivar una receta,
    el operador ve en el backoffice paciente, medicamento, el candidato del
    catálogo con precio/stock y si es socio — sin pedirle nada al cliente.
    """

    OCR = {
        "paciente": "Silvia Beatriz Zamponi", "dni": "30561218",
        "obra_social": "Prevención Salud", "nro_afiliado": "46381800011",
        "plan": "A2", "droga": "etinilestradiol, drospirenona",
        "producto_sugerido": "Yasminelle", "presentacion": "comp.rec.x 28",
        "diagnostico": "Anticonceptivos orales", "nro_receta": "1369946",
        "vigencia": "27/08/2026", "medico": "Dr. Pietrobon Diego F.",
        "matricula": "MP 13.359",
    }

    def test_parse_receta_completa_y_con_campos_faltantes(self):
        from app.services.image_service import _parse_receta
        import json as _json
        completo = _parse_receta(_json.dumps(self.OCR))
        assert completo["dni"] == "30561218"
        assert completo["producto_sugerido"] == "Yasminelle"
        parcial = _parse_receta('{"droga": "ibuprofeno"}')   # manuscrita ilegible
        assert parcial["droga"] == "ibuprofeno"
        assert parcial["dni"] == ""                           # faltante → vacío
        assert _parse_receta("no soy json") is None

    def test_find_by_dni(self, tmp_path):
        from app.services.socio_service import SocioService
        p = tmp_path / "padron.csv"
        p.write_text("APELLIDO,NOMBRE,DNI,SOCIO,CELULAR,DOMICILIO\n"
                     "Zamponi,Silvia,30561218,4638,3415551234,San Victor 945\n",
                     encoding="utf-8")
        svc = SocioService(str(p))
        assert svc.find_by_dni("30561218")["nombre"] == "Silvia"
        assert svc.find_by_dni("30.561.218")["nombre"] == "Silvia"   # con puntos
        assert svc.find_by_dni("99999999") is None
        assert svc.find_by_dni("") is None

    def test_armar_receta_info_cruza_catalogo_y_padron(self, tmp_path):
        from app.services.receta_ocr import armar_receta_info
        from app.services.socio_service import SocioService

        class _FakeSku:
            def buscar(self, q, top_n=3):
                if "yasminelle" in q.lower():
                    return [{"sku_id": "Y1", "nombre": "YASMINELLE COM x 28",
                             "precio": 25000.0, "estado": "disponible",
                             "requiere_receta": "si"}]
                return []

        p = tmp_path / "padron.csv"
        p.write_text("APELLIDO,NOMBRE,DNI,SOCIO,CELULAR,DOMICILIO\n"
                     "Zamponi,Silvia,30561218,4638,3415551234,San Victor 945\n",
                     encoding="utf-8")
        socios = SocioService(str(p))

        info = armar_receta_info(self.OCR, _FakeSku(), socios, "5493415551234")
        assert info["ocr"]["producto_sugerido"] == "Yasminelle"
        assert info["candidatos_catalogo"][0]["sku_id"] == "Y1"
        assert info["socio_por_dni"]["nombre"] == "Silvia Zamponi"
        assert info["dni_coincide_padron"] is True

    def test_armar_receta_info_busca_por_droga_si_no_hay_sugerido(self):
        from app.services.receta_ocr import armar_receta_info

        class _FakeSku:
            def __init__(self):
                self.queries = []

            def buscar(self, q, top_n=3):
                self.queries.append(q)
                if "drospirenona" in q.lower():
                    return [{"sku_id": "D1", "nombre": "DROSPIRENONA GEN",
                             "precio": 1.0, "estado": "disponible",
                             "requiere_receta": "si"}]
                return []

        class _SinPadron:
            def find_by_phone(self, p): return None
            def find_by_dni(self, d): return None

        ocr = dict(self.OCR, producto_sugerido="")
        info = armar_receta_info(ocr, _FakeSku(), _SinPadron(), "549341")
        assert info["candidatos_catalogo"][0]["sku_id"] == "D1"
        assert info["socio_por_dni"] is None
        assert info["dni_coincide_padron"] is False

    async def test_receta_info_se_limpia_al_liberar(self):
        from app.services.session_service import SessionService
        ss = SessionService("redis://127.0.0.1:1")
        s = await ss.get("549")
        s["receta_info"] = {"ocr": {"dni": "123"}}
        await ss.save("549", s)
        await ss.liberar("549")
        assert "receta_info" not in await ss.get("549")


class TestTextoDeictico:
    """
    Regresión (caso real 31/8): mandó una FOTO de tres productos y el texto
    "Necesito esos productos". El texto llegó primero, el bot preguntó
    "¿podrías especificar?" y un segundo después la imagen respondió todo.
    Si en el mismo lote hay imagen + texto que solo señala ("esos productos"),
    el texto se descarta: la imagen ES el pedido.
    """

    @pytest.mark.parametrize("txt", [
        "Necesito esos productos", "quiero estos", "esos", "eso",
        "me mandás esos productos?", "los de la foto", "dame estas cosas",
        "necesito los productos de la foto",
    ])
    def test_es_deictico(self, txt):
        assert ch.texto_deictico(txt) is True

    @pytest.mark.parametrize("txt", [
        "necesito ibuprofeno", "quiero el serum eximia", "esos precios están bien?",
        "hola", "cuánto sale el gel?", "necesito esos productos y también un tafirol",
    ])
    def test_no_es_deictico(self, txt):
        assert ch.texto_deictico(txt) is False


class TestDescuentoSocioEnCatalogo:
    """
    El socio ve el precio con descuento desde que se le ofrece el producto, no
    recién en el link. Clave: el descuento se aplica UNA sola vez y arriba de
    todo, para que el precio con descuento sea el único que circula — si no, la
    regla "el precio que el bot dice es el que cobra" se rompe.
    """

    class _FakeSocios:
        def __init__(self, es_socio=True):
            self._es_socio = es_socio

        def find_by_phone(self, phone):
            return {"nombre": "Claudia"} if self._es_socio else None

    CFG = {"socio_discount_pct": "15", "socio_discount_en_catalogo": "true",
           "receta_mode": "conservador"}

    def _res(self, precio=10000.0, receta="no"):
        return [{"sku_id": "1", "nombre": "Actron 600", "precio": precio,
                 "requiere_receta": receta, "vendible": True}]

    def test_socio_ve_el_precio_con_descuento(self):
        out, pct = ch.aplicar_descuento_socio(
            self._res(), "549341", self.CFG, socio_svc=self._FakeSocios())
        assert pct == 15
        assert out[0]["precio"] == 8500.0
        assert out[0]["precio_lista"] == 10000.0   # queda el original, para el mensaje

    def test_no_socio_ve_precio_de_lista(self):
        out, pct = ch.aplicar_descuento_socio(
            self._res(), "549341", self.CFG, socio_svc=self._FakeSocios(es_socio=False))
        assert pct == 0
        assert out[0]["precio"] == 10000.0

    def test_producto_con_receta_no_lleva_descuento(self):
        """El descuento es para venta libre: los de receta derivan a una persona."""
        out, _ = ch.aplicar_descuento_socio(
            self._res(receta="si"), "549341", self.CFG, socio_svc=self._FakeSocios())
        assert out[0]["precio"] == 10000.0

    def test_descuento_en_cero_no_hace_nada(self):
        cfg = {**self.CFG, "socio_discount_pct": "0"}
        out, pct = ch.aplicar_descuento_socio(
            self._res(), "549341", cfg, socio_svc=self._FakeSocios())
        assert pct == 0 and out[0]["precio"] == 10000.0

    def test_se_puede_apagar_desde_el_backoffice(self):
        """Con la config en false vuelve al comportamiento viejo (solo en el link)."""
        cfg = {**self.CFG, "socio_discount_en_catalogo": "false"}
        out, pct = ch.aplicar_descuento_socio(
            self._res(), "549341", cfg, socio_svc=self._FakeSocios())
        assert pct == 0 and out[0]["precio"] == 10000.0

    def test_no_muta_los_resultados_originales(self):
        originales = self._res()
        ch.aplicar_descuento_socio(originales, "549341", self.CFG,
                                   socio_svc=self._FakeSocios())
        assert originales[0]["precio"] == 10000.0

    async def test_el_link_cobra_exactamente_lo_ofrecido(self):
        """
        El riesgo grande: si el descuento se aplicara también al armar el link,
        se cobraría 15% menos de lo que el bot dijo. El precio pendiente ya
        viene con descuento; el link lo respeta tal cual.
        """
        from app.services.session_service import SessionService
        ss = SessionService("redis://127.0.0.1:1")
        # precio YA con descuento, como queda tras la búsqueda
        await ss.set_pending("549", sku_id="A1", sku_nombre="Actron",
                             precio=8500.0, cantidad=1)

        capturado = {}

        class _FakePago:
            async def crear_link(self, sku_id, nombre, precio, phone, cantidad=1):
                capturado["precio"] = precio
                return "https://pago/x", None

        sesion = await ss.get("549")
        respuesta, link = await ch.crear_link_y_responder(
            _FakePago(), ss, "549", sesion, "retiro", None)
        assert link
        assert capturado["precio"] == 8500.0      # NO 7225 (doble descuento)
        assert "$8,500.00" in respuesta


# ── Un mensaje que arranca con "no" nunca confirma ─────────────────────────────
@pytest.mark.parametrize("txt", ["no esta bien", "No, está bien", "no sé", "no  mejor otro"])
def test_empieza_con_no(txt):
    """
    Regresión (caso real): "no esta bien" es ambiguo y el modelo lo tomó como
    confirmación — el cliente recibió el flujo de entrega tras decir que algo
    estaba mal. Ante la duda, se pregunta.
    """
    from app.routers.webhook import _empieza_con_no
    assert _empieza_con_no(txt) is True


@pytest.mark.parametrize("txt", ["dale", "si", "bueno", "nova el shampoo", "noviembre"])
def test_no_empieza_con_no(txt):
    from app.routers.webhook import _empieza_con_no
    assert _empieza_con_no(txt) is False


# ── Pedir foto de un producto → derivar a una persona ──────────────────────────
@pytest.mark.parametrize("txt", [
    "me mandás una foto del producto?", "tenés fotos de la crema?",
    "me pasás una imagen", "quiero ver una foto", "cómo es? tenés foto?",
])
def test_pide_foto_deriva(txt):
    """La foto real del producto la saca y la manda una persona del equipo."""
    assert ch.pide_foto(txt) is True


@pytest.mark.parametrize("txt", ["quiero un ibuprofeno", "hola", "cuánto sale", "dale confirmo"])
def test_no_pide_foto(txt):
    assert ch.pide_foto(txt) is False


# ── Frases de espera y sustitutos presentados como lo pedido ───────────────────
def test_quitar_frases_de_espera():
    """
    Regresión (caso real): "Ahora verifico el Sedal Cerámicas Sha para vos"
    promete una verificación que nunca llega. Se elimina la oración, sin tocar
    frases legítimas.
    """
    r = ch.quitar_frases_de_espera(
        "Tengo el Doncella Algodón a $1002.26. ¿Lo agrego? "
        "Ahora verifico el Sedal Cerámicas Sha para vos.")
    assert "verifico" not in r.lower()
    assert "Doncella" in r
    # No toca ofertas ni derivaciones legítimas
    assert "consulte" in ch.quitar_frases_de_espera("¿Querés que lo consulte con el equipo?")
    assert "contactamos" in ch.quitar_frases_de_espera("En un momento te contactamos!")
    # Nunca devuelve vacío
    assert ch.quitar_frases_de_espera("Ahora verifico eso.") != ""


def test_quitar_frases_de_espera_buscar_y_momento():
    """
    Regresión (caso real, 19/8): "Ahora voy a buscar la disponibilidad de
    Corega para vos" y "Un momento, por favor." se colaron — el filtro no
    cubría la conjugación "buscar" ni la cortesía de espera sola.
    """
    r = ch.quitar_frases_de_espera(
        "3. Estrella Premium en envase de 75 unidades por $1762.96 "
        "Ahora voy a buscar la disponibilidad de Corega para vos.")
    assert "buscar" not in r.lower()
    assert "$1762.96" in r
    r2 = ch.quitar_frases_de_espera("Un momento, por favor. El Corega sale $16,827.20.")
    assert "momento" not in r2.lower()
    assert "$16,827.20" in r2


# ── Pago en sucursal / cuenta corriente → pago manual (deriva o avisa) ────────
@pytest.mark.parametrize("txt", [
    "lo pago en la sucursal cuando lo retiro",
    "lo retiro y pago ahí",
    "pago al retirar",
    "si, lo voy a agregar a mi cuenta corriente",
    "me lo anotás en cuenta corriente?",
])
def test_pago_en_sucursal_o_cuenta_corriente_es_pago_manual(txt):
    """
    Regresión (casos 29 y 31): "lo pago en la sucursal" y "cuenta corriente"
    recibían link de pago igual. Son medios que maneja una persona.
    """
    assert ch.pide_pago_manual(txt) is True


@pytest.mark.parametrize("txt", [
    "te pago con tarjeta", "cómo lo pago?", "mandame el link de pago",
    "lo retiro en la sucursal",
])
def test_pago_normal_no_es_manual(txt):
    assert ch.pide_pago_manual(txt) is False


# ── Recetas "en la nube" / electrónicas → derivar a una persona ───────────────
@pytest.mark.parametrize("txt", [
    "me dijo el doctor que ya estan las recetas en la nube, te podes fijar",
    "tengo la receta electrónica",
    "la receta ya está cargada en el sistema",
    "fijate en la nube que están mis recetas",
])
def test_receta_en_nube_deriva(txt):
    """
    Regresión (caso real, 19/8): "recetas en la nube" recibió "Un momento,
    por favor" y silencio hasta el cierre. El bot no accede al sistema de
    recetas: deriva siempre.
    """
    assert ch.pide_receta_nube(txt) is True


@pytest.mark.parametrize("txt", [
    "necesito algo para la tos", "tenés ibuprofeno?", "te mando la receta por acá",
])
def test_no_receta_nube(txt):
    assert ch.pide_receta_nube(txt) is False


# ── Preguntas por descuento → respuesta fija, nunca inventar ──────────────────
@pytest.mark.parametrize("txt", [
    "tengo descuento de socio?", "hay descuentos?", "tengo precio de socio?",
    "me hacés un descuentito?",
])
def test_pregunta_descuento(txt):
    """
    Regresión (caso 29): el modelo inventó "como socia tenés un descuento" con
    un precio que no existe. Toda pregunta de descuento se responde con texto
    fijo según la config.
    """
    assert ch.pregunta_descuento(txt) is True


@pytest.mark.parametrize("txt", ["quiero un ibuprofeno", "cuánto sale el corega?"])
def test_no_pregunta_descuento(txt):
    assert ch.pregunta_descuento(txt) is False


def test_ultraflex_es_venta_libre():
    """Regresión (caso 30): Ultraflex derivado 'por receta' siendo venta libre."""
    from app.services.sku_service import es_venta_libre
    assert es_venta_libre("ULTRAFLEX COLAGENO POLVO x 300")


class TestConfigPersistente:
    """
    Regresión (21/8): la config editable del backoffice vivía SOLO en Redis.
    Si Redis se reinicia, los valores se pierden en silencio y todo vuelve a
    los defaults del código — el descuento de socios quedaba en 0 sin aviso.
    Postgres es la fuente de verdad; Redis, cache.
    """

    class _FakeDB:
        """Postgres de mentira: guarda en un dict."""
        def __init__(self, filas=None, disponible=True):
            self.filas = dict(filas or {})
            self._disponible = disponible
            self.escrituras = 0

        def available(self):
            return self._disponible

        async def execute(self, query, *args):
            if not self._disponible:
                return None
            if "INSERT INTO config" in query:
                self.filas[args[0]] = args[1]
                self.escrituras += 1
            return "OK"

        async def fetch(self, query, *args):
            if not self._disponible:
                return []
            return [{"clave": k, "valor": v} for k, v in self.filas.items()]

    def _svc(self, db, redis_ok=True, redis_data=None):
        from app.services.config_service import ConfigService
        svc = ConfigService("redis://fake")
        svc._ok = redis_ok
        svc._db = db
        # Redis de mentira: un dict que simula el hash
        redis_data = {} if redis_data is None else redis_data

        class _FakeRedis:
            async def hgetall(self, key):
                if not redis_ok:
                    raise RuntimeError("redis caido")
                return dict(redis_data)

            async def hset(self, key, field=None, value=None, mapping=None):
                if not redis_ok:
                    raise RuntimeError("redis caido")
                if mapping:
                    redis_data.update(mapping)
                else:
                    redis_data[field] = value

        svc._redis = _FakeRedis()
        svc._redis_data = redis_data
        return svc

    async def test_set_persiste_en_postgres_ademas_de_redis(self):
        db = self._FakeDB()
        svc = self._svc(db)
        await svc.set("socio_discount_pct", "15")
        assert db.filas["socio_discount_pct"] == "15"      # durable
        assert svc._redis_data["socio_discount_pct"] == "15"  # y en cache

    async def test_redis_vacio_recupera_desde_postgres(self):
        """El caso que motivó todo: Redis se reinició y quedó sin datos."""
        db = self._FakeDB({"socio_discount_pct": "15"})
        svc = self._svc(db, redis_data={})          # Redis vacío
        cfg = await svc.get_all()
        assert cfg["socio_discount_pct"] == "15"     # no se perdió
        # y repobló el cache, para no volver a pegarle a Postgres
        assert svc._redis_data["socio_discount_pct"] == "15"

    async def test_redis_caido_lee_de_postgres(self):
        db = self._FakeDB({"socio_discount_pct": "15"})
        svc = self._svc(db, redis_ok=False)
        cfg = await svc.get_all()
        assert cfg["socio_discount_pct"] == "15"

    async def test_redis_manda_cuando_tiene_datos(self):
        """Con cache poblado no se consulta Postgres: es el camino caliente."""
        db = self._FakeDB({"socio_discount_pct": "99"})
        svc = self._svc(db, redis_data={"socio_discount_pct": "15"})
        cfg = await svc.get_all()
        assert cfg["socio_discount_pct"] == "15"

    async def test_sin_nada_configurado_usa_defaults(self):
        from app.services.config_service import DEFAULTS
        svc = self._svc(self._FakeDB())
        cfg = await svc.get_all()
        assert cfg["socio_discount_pct"] == DEFAULTS["socio_discount_pct"]

    async def test_sin_postgres_sigue_funcionando_con_redis(self):
        """Degradación: sin Postgres el bot no se cae, solo pierde durabilidad."""
        svc = self._svc(self._FakeDB(disponible=False))
        await svc.set("socio_discount_pct", "15")
        cfg = await svc.get_all()
        assert cfg["socio_discount_pct"] == "15"

    async def test_siembra_postgres_con_lo_que_ya_estaba_en_redis(self):
        """
        Al estrenar la persistencia, lo ya configurado vive SOLO en Redis (el
        descuento en 15%). Si no se copia a Postgres, el próximo reinicio lo
        pierde igual: la primera hidratación lo rescata.
        """
        db = self._FakeDB()                                   # Postgres vacío
        svc = self._svc(db, redis_data={"socio_discount_pct": "15"})
        copiadas = await svc.sincronizar_durable()
        assert copiadas == 1
        assert db.filas["socio_discount_pct"] == "15"

    async def test_no_pisa_postgres_si_ya_tiene_datos(self):
        """Postgres es la verdad: una vez que tiene datos, Redis no lo sobreescribe."""
        db = self._FakeDB({"socio_discount_pct": "20"})
        svc = self._svc(db, redis_data={"socio_discount_pct": "15"})
        assert await svc.sincronizar_durable() == 0
        assert db.filas["socio_discount_pct"] == "20"

    async def test_set_many_persiste_todo(self):
        db = self._FakeDB()
        svc = self._svc(db)
        await svc.set_many({"socio_discount_pct": "15", "pickup_minutes": "45"})
        assert db.filas["socio_discount_pct"] == "15"
        assert db.filas["pickup_minutes"] == "45"


# ── Estilo humano: sacar los rasgos que delatan que escribió una IA ───────────
class TestEstiloHumano:
    """
    El bot escribía con la firma tipográfica del texto generado por IA:
    negritas y viñetas en un chat de WhatsApp, un emoji ritual por mensaje,
    apertura entusiasta en cada turno y cierre de call center.
    """

    def _h(self, t, **kw):
        from app.services.estilo_humano import humanizar
        return humanizar(t, **kw)

    def test_saca_negritas_y_vinetas_conservando_saltos(self):
        r = self._h("Te cuento las opciones:\n• *Préstamo personal*\n• *AMT*")
        assert "*" not in r and "•" not in r
        assert "Préstamo personal" in r and "AMT" in r
        assert "\n" in r          # los saltos estructuran el mensaje

    def test_guion_largo_se_vuelve_puntuacion(self):
        r = self._h("El AMT — rinde más que la caja de ahorro — es a plazo fijo.")
        assert "—" not in r
        assert "rinde más que la caja de ahorro" in r

    def test_importe_intacto_con_cierre_ritual(self):
        r = self._h("La cuota estimada es de *$16.827,20*. ¿Te gustaría proceder con la compra?")
        assert "$16.827,20" in r
        assert "*" not in r and "proceder" not in r.lower()

    def test_importe_con_punto_decimal_no_se_parte(self):
        """El punto de $1762.96 no es fin de oración (bug real del filtro anterior)."""
        r = self._h("Por $1762.96 a 30 días. ¡Espero que esto te sea útil! 🙂✨")
        assert "$1762.96" in r
        assert "espero" not in r.lower()

    def test_rango_de_importes_no_se_toca(self):
        r = self._h("Los montos van de $50.000 – $2.000.000.")
        assert "$50.000" in r and "$2.000.000" in r

    def test_nunca_devuelve_vacio(self):
        """Si todo el mensaje era ritual, se devuelve el original: mejor feo que mudo."""
        assert self._h("¡Perfecto! ¿Hay algo más en lo que pueda ayudarte? 😊").strip()

    def test_saca_apertura_ritual_y_cierre_de_call_center(self):
        r = self._h("¡Genial! Te paso los requisitos. ¿Hay algo más en lo que pueda ayudarte?")
        assert r.startswith("Te paso")
        assert "algo más" not in r

    def test_cierre_ritual_despues_de_emoji(self):
        """El modelo cierra con emoji + pregunta ritual; el ancla tiene que verlo."""
        r = self._h("El alias es AMICORREA 🙌 ¿Te gustaría proceder con la operación?")
        assert "proceder" not in r.lower()
        assert "AMICORREA" in r

    def test_apertura_sola_sobrevive(self):
        """Sin acumulación ritual la interjección es humana: sacarla deja al bot seco."""
        assert "Genial" in self._h("¡Genial! Te lo calculo.")

    def test_no_confunde_adjetivo_con_interjeccion(self):
        t = "Perfecto para tu caso: el AMT a 30 días rinde más."
        assert self._h(t) == t

    def test_saca_muletilla_y_recapitaliza(self):
        r = self._h("Es importante destacar que la cuota social se paga por mes.")
        assert "importante destacar" not in r
        assert r.startswith("La cuota social")

    def test_no_toca_frase_que_solo_se_parece_a_muletilla(self):
        t = "Por otro lado del mostrador te atienden."
        assert self._h(t) == t

    def test_no_toca_el_rioplatense_legitimo(self):
        """"Dale" lo usan los propios mensajes del equipo: no es un tic de IA."""
        t = "Dale, te paso con un oficial de créditos 🙌"
        assert self._h(t) == t

    def test_deja_un_solo_emoji(self):
        r = self._h("Te asocio 🙌 y después te aviso 😊✨👍")
        assert "🙌" in r
        assert "😊" not in r and "✨" not in r and "👍" not in r

    def test_no_toca_las_preguntas_que_son_del_negocio(self):
        """"¿Querés que te pase con un asesor?" es el próximo paso, no un ritual."""
        t = "¿Querés que te pase con un asesor?"
        assert self._h(t) == t

    def test_porcentaje_negativo_no_es_vineta(self):
        r = self._h("-15% en la cuota\n- Sin gastos de apertura")
        assert "-15%" in r
        assert "Sin gastos de apertura" in r
        assert "\n- " not in r


@pytest.mark.parametrize("txt", [
    "sos un bot?", "¿sos un robot?", "hablo con una persona?",
    "esto es automático?", "sos una máquina?", "estoy hablando con un humano?",
    "sos real o sos una ia?",
])
def test_pregunta_si_es_bot(txt):
    """Si preguntan derecho, se admite: no se niega ni se esquiva."""
    from app.services.mutual_helper import pregunta_si_es_bot
    assert pregunta_si_es_bot(txt) is True


@pytest.mark.parametrize("txt", [
    "quiero un préstamo", "me pasás el CVU?", "sos muy amable",
    "necesito hablar con alguien del equipo",
])
def test_no_pregunta_si_es_bot(txt):
    from app.services.mutual_helper import pregunta_si_es_bot
    assert pregunta_si_es_bot(txt) is False


def test_respuesta_de_identidad_admite_y_ofrece_humano():
    from app.services.config_service import DEFAULTS
    msg = DEFAULTS["mutual_bot_identidad_message"].lower()
    assert "asistente" in msg          # lo admite
    assert "equipo" in msg or "persona" in msg   # y ofrece humano


class TestTextosSinRasgosDeIA:
    """
    Los textos que escribimos nosotros son los que más se repiten literal, y la
    repetición idéntica es el tell más fuerte. Estos tests impiden que los
    rasgos vuelvan a entrar por config o por un fallback.
    """

    def _sin_rasgos(self, t):
        """Ni negritas, ni viñetas, ni guiones largos, ni más de un emoji."""
        from app.services.estilo_humano import _EMOJI
        import re as _re
        assert "•" not in t, f"viñeta en: {t!r}"
        assert "—" not in t and "→" not in t, f"guion largo en: {t!r}"
        assert not _re.search(r"(?<![\w*])\*\S[^*\n]*\*", t), f"negrita en: {t!r}"
        assert len(_EMOJI.findall(t)) <= 1, f"más de un emoji en: {t!r}"

    def test_defaults_mutual_sin_rasgos(self):
        from app.services.config_service import DEFAULTS
        for clave, valor in DEFAULTS.items():
            if clave.startswith("mutual_") and isinstance(valor, str) and len(valor) > 25:
                self._sin_rasgos(valor)

    def test_mensajes_compartidos_sin_rasgos(self):
        from app.services.config_service import DEFAULTS
        for clave in ("inactivity_close_message", "auto_liberar_message",
                      "handoff_reminder_message"):
            self._sin_rasgos(DEFAULTS[clave])

    def test_simulaciones_sin_rasgos_pero_con_lo_obligatorio(self):
        from app.services.mutual_helper import texto_simulacion, texto_amt
        sim = {"monto": 1500000, "cuotas": 12, "linea": "preferencial", "tna": 55,
               "cuota": 180000, "incluye_iva": False, "incluye_gastos": False}
        t = texto_simulacion(sim)
        self._sin_rasgos(t)
        assert "estimativo" in t.lower() and "no incluye" in t.lower()

        amt = {"monto": 100000, "dias": 30, "sellado_reducido": False,
               "online": {"tna": 26, "interes": 2136, "total": 102136},
               "presencial": {"tna": 23.5, "interes": 1931, "total": 101931}}
        ta = texto_amt(amt)
        self._sin_rasgos(ta)
        assert "no incluye" in ta.lower() and "sellado" in ta.lower()

    def test_derivacion_varia_su_redaccion(self):
        """Seis veces la misma fórmula es lo que delata: se rota entre variantes."""
        from app.services.mutual_helper import mensaje_derivacion
        vistos = {mensaje_derivacion("saldo") for _ in range(60)}
        assert len(vistos) > 1, "la derivación usa siempre el mismo texto"
        for m in vistos:
            self._sin_rasgos(m)
            assert "saldo" in m.lower()

    def test_derivacion_conserva_el_nombre(self):
        from app.services.mutual_helper import mensaje_derivacion
        for _ in range(20):
            assert mensaje_derivacion("saldo", nombre="Claudia").startswith("Claudia,")


# ── Abreviaturas de góndola y tipo de producto ────────────────────────────────
class TestAbreviaturas:
    def test_expande_siglas_de_gondola(self):
        from app.services.catalogo_enriquecido import expandir_abreviaturas
        assert "talco" in expandir_abreviaturas("REXONA EFFIC.TAL.ORIG TAL x 100").lower()
        assert "jabon" in expandir_abreviaturas("DOVE ORIGINAL JAB x 90").lower()
        assert "shampoo" in expandir_abreviaturas("SEDAL CERAMIDAS SHA x 340").lower()
        assert "desodorante" in expandir_abreviaturas("REXONA ODORONO FEM DES CRE x 60").lower()
        # el nombre original se conserva
        assert "REXONA" in expandir_abreviaturas("REXONA EFFIC.TAL.ORIG TAL x 100")

    def test_sin_abreviaturas_devuelve_igual(self):
        from app.services.catalogo_enriquecido import expandir_abreviaturas
        assert expandir_abreviaturas("IBUPIRAC 400") == "IBUPIRAC 400"

    def test_detecta_tipo_pedido(self):
        from app.services.catalogo_enriquecido import tipos_mencionados
        assert tipos_mencionados("me mandás un talco rexona?") == {"talco"}
        assert tipos_mencionados("jabon dove") == {"jabon"}
        assert tipos_mencionados("quiero un ibuprofeno") == set()

    def test_no_ofrece_otro_tipo_de_producto(self):
        """
        Regresión (caso real 21/8): pidió "talco rexona" y el bot ofreció un
        REXONA ODORONO desodorante en crema como si fuera el talco. La marca
        coincide pero el TIPO de producto no: no es lo pedido.
        """
        from app.services.sku_service import nombre_coincide
        assert not nombre_coincide("talco rexona",
                                   "Unilever REXONA ODORONO C/GLICERINA FEM DES CRE x 60")
        assert nombre_coincide("talco rexona",
                               "Unilever REXONA EFFIC.TAL.ORIG TAL x 100")
        assert not nombre_coincide("jabon dove", "Unilever DOVE AP ROLL ON DES ENV x 55")
        assert nombre_coincide("jabon dove", "Unilever DOVE ORIGINAL JAB x 90")

    def test_busqueda_encuentra_el_tipo_pedido(self, tmp_path):
        """El talco y el jabón existen con stock: el bot tiene que encontrarlos."""
        from app.services.sku_service import SKUService
        cat = tmp_path / "cat.csv"
        cat.write_text(
            "SKU,Nombre,Precio,Marca,Laboratorio,Codigo_Barras_1,Codigo_Barras_2,"
            "Codigo_Barras_3,Codigo_Barras_4,Categoria,Es_Medicamento\n"
            "1,Unilever REXONA ODORONO C/GLICERINA FEM DES CRE x 60,3440,,Unilever,111,,,,Perfumeria,false\n"
            "2,Unilever REXONA EFFIC.TAL.ORIG TAL x 100,4061,,Unilever,222,,,,Perfumeria,false\n"
            "3,Unilever DOVE AP ROLL ON DES ENV x 55,4268,,Unilever,333,,,,Perfumeria,false\n"
            "4,Unilever DOVE ORIGINAL JAB x 90,2355,,Unilever,444,,,,Perfumeria,false\n",
            encoding="utf-8",
        )
        svc = SKUService(str(cat))
        assert svc.buscar("talco rexona")[0]["sku_id"] == "2"
        assert svc.buscar("jabon dove")[0]["sku_id"] == "4"


# ── Conversión de PDFs de existencias → catálogo ──────────────────────────────
class TestCatalogoPdf:
    def test_num_formato_argentino(self):
        from app.services.catalogo_pdf import _num
        assert _num("20497,29") == 20497.29
        assert _num("1.234,56") == 1234.56
        assert _num("-2") == -2.0
        assert _num("") == 0.0

    def test_precio_unitario_desde_valorizacion(self):
        # "Valor" es stock × precio unitario (Koleston real: 2 × $9555.28)
        from app.services.catalogo_pdf import a_fila_catalogo
        fila = a_fila_catalogo(
            {"laboratorio": "Glam", "producto": "KOLESTON SING 60", "troquel": "0",
             "barcode": "77911234", "stock": 2.0, "prom_vta": 0.5, "valor": 19110.56},
            "General")
        assert fila["precio_venta"] == "9555.28"
        assert fila["cantidad_visible"] == "2"
        assert fila["requiere_receta"] == "no"
        assert fila["sku_id"] == "77911234"   # sin troquel → barcode

    def test_stock_negativo_no_es_vendible(self):
        from app.services.catalogo_pdf import a_fila_catalogo
        fila = a_fila_catalogo(
            {"laboratorio": "X", "producto": "COREGA TABS", "troquel": "123",
             "barcode": "77900001", "stock": -2.0, "prom_vta": 0, "valor": -21308.22},
            "Medicamentos")
        assert fila["precio_venta"] == "10654.11"   # el unitario queda positivo
        assert fila["cantidad_visible"] == "0"      # pero no hay para vender
        assert fila["requiere_receta"] == "ambiguo"
        assert fila["es_medicamento"] == "si"

    def test_fusion_actualiza_precio_sin_tocar_receta(self):
        """
        Regresión (pedido 20/8): actualizar el catálogo desde los PDF no debe
        reemplazar todo, y NUNCA debe borrar/cambiar el flag de receta de un
        producto que ya existía.
        """
        from app.models.sku import SKU
        from app.services.catalogo_pdf import fusionar_con_catalogo

        actual = [
            SKU(sku_id="9960248", barcode="7790375269913", sku_nombre="Bagó Calma",
                sku_nombre_original="Bagó Calma", laboratorio="Bagó", categoria="Medicamentos",
                es_medicamento=True, precio_venta=100.0, stock_actual=5, cantidad_visible=5,
                requiere_receta="si", clasificacion="critico"),
            SKU(sku_id="999", barcode="0000000000001", sku_nombre="Producto que no está en el PDF",
                sku_nombre_original="X", precio_venta=50.0, stock_actual=3, cantidad_visible=3,
                requiere_receta="no"),
        ]
        # PDF actualiza el primero (nuevo precio/stock) y trae un producto nuevo
        pdf_bytes = None  # se simula parsear_pdf vía monkeypatch abajo
        import app.services.catalogo_pdf as cp
        filas_fake = [
            {"laboratorio": "Bagó", "producto": "BAGO+CALMA COM x 30", "troquel": "9960248",
             "barcode": "7790375269913", "stock": 2.0, "prom_vta": 0.0, "valor": 40997.0},
            {"laboratorio": "X", "producto": "NUEVO PRODUCTO", "troquel": "",
             "barcode": "9999999999999", "stock": 1.0, "prom_vta": 0.0, "valor": 500.0},
        ]
        orig = cp.parsear_pdf
        cp.parsear_pdf = lambda origen, nombre="": ("Medicamentos", filas_fake)
        try:
            contenido, resumen = fusionar_con_catalogo(actual, [(b"fake", "medicamentos.pdf")])
        finally:
            cp.parsear_pdf = orig

        assert resumen["Actualizados (precio/stock)"] == 1
        assert resumen["Nuevos (no estaban en el catálogo)"] == 1
        assert resumen["Sin cambios (no vinieron en los PDF)"] == 1
        assert resumen["TOTAL catálogo resultante"] == 3

        filas = list(csv.DictReader(io.StringIO(contenido)))
        por_barcode = {f["barcode"]: f for f in filas}
        # Precio/stock actualizados, receta INTACTA (seguía siendo "si")
        actualizado = por_barcode["7790375269913"]
        assert actualizado["precio_venta"] == "20498.50"
        assert actualizado["requiere_receta"] == "si"
        assert actualizado["stock_actual"] == "2"
        # Producto que no vino en el PDF: sin cambios
        sin_cambios = por_barcode["0000000000001"]
        assert sin_cambios["precio_venta"] == "50.00"
        assert sin_cambios["requiere_receta"] == "no"
        # Producto nuevo: entra con su propio flag calculado
        nuevo = por_barcode["9999999999999"]
        assert nuevo["requiere_receta"] == "ambiguo"   # medicamento nuevo

    def test_normalizar_valor_receta(self):
        from app.services.receta_excel import _normalizar
        assert _normalizar("si") == "si"
        assert _normalizar("Si") == "si"
        assert _normalizar("no") == "no"
        assert _normalizar("No aplica (no es medicamento)") == "no"
        assert _normalizar("No encontrado en cruce") is None
        assert _normalizar("ni") is None
        assert _normalizar("") is None
        assert _normalizar(None) is None

    def test_parsear_excel_extrae_barcode_y_receta(self, tmp_path):
        from openpyxl import Workbook
        from app.services.receta_excel import parsear_excel

        wb = Workbook()
        ws = wb.active
        ws.title = "Medicamentos"
        headers = ["Descripcion", "Troquel", "Codigo de barra", "Stock actual",
                   "Prom.", "Precio", "Fact.", "Valor", "Sem.", "Clasif.",
                   "Rank1", "Rank2", "Pct", "ABC", "Prob",
                   "Requiere Receta (cruce SKU)", "ValorOriginal", "Estado"]
        ws.append(headers)
        ws.append(["IBUPROFENO 600", None, "7790001", 10, 1, 100, 0, 0, 0, "",
                   0, 0, 0, "", 0, "si", 0, "OK"])
        ws.append(["ALGO SIN CRUCE", None, "7790002", 5, 1, 50, 0, 0, 0, "",
                   0, 0, 0, "", 0, "No encontrado en cruce", 0, "OK"])
        ws.append(["PLACEHOLDER", None, "1", 3, 1, 30, 0, 0, 0, "",
                   0, 0, 0, "", 0, "si", 0, "OK"])   # barcode placeholder: se ignora
        p = tmp_path / "excel.xlsx"
        wb.save(p)

        resultado = parsear_excel(p.read_bytes())
        assert resultado["7790001"] == "si"
        assert "7790002" not in resultado    # sin cruce: no se toca
        assert "1" not in resultado          # placeholder: no es un barcode real

    def test_fusion_no_pisa_la_whitelist_confirmada_por_la_farmacia(self):
        """
        Regresión (decisión 21/8): el Excel marcó "con receta" a OTC ya
        confirmados por la farmacia el 31/7 (Ibupirac, Actron, Tafirol...) —
        el cruce del Excel no documenta su método para ese campo. La
        whitelist confirmada queda blindada: el Excel puede sumar info
        nueva, pero no la pisa. Los conflictos se reportan para revisión.
        """
        from app.models.sku import SKU
        from app.services.receta_excel import fusionar_receta

        actual = [
            SKU(sku_id="1", barcode="7790001", sku_nombre="Ibupirac 600",
                sku_nombre_original="Ibupirac 600", precio_venta=100.0,
                requiere_receta="no"),   # OTC confirmado por la farmacia
            SKU(sku_id="2", barcode="7790002", sku_nombre="Producto sin cruce",
                sku_nombre_original="X", precio_venta=50.0, requiere_receta="ambiguo"),
            SKU(sku_id="3", barcode="7790003", sku_nombre="Producto nuevo con dato",
                sku_nombre_original="Z", precio_venta=20.0, requiere_receta="ambiguo"),
            SKU(sku_id="4", barcode="0000000000009", sku_nombre="No aparece en excel",
                sku_nombre_original="Y", precio_venta=10.0, requiere_receta="no"),
        ]
        por_barcode_excel = {"7790001": "si", "7790003": "no"}

        contenido, resumen = fusionar_receta(actual, por_barcode_excel)
        filas = {f["barcode"]: f for f in csv.DictReader(io.StringIO(contenido))}

        assert filas["7790001"]["requiere_receta"] == "no"     # whitelist blindada, NO se pisa
        assert filas["7790003"]["requiere_receta"] == "no"     # sin conflicto: sí se actualiza
        assert filas["7790002"]["requiere_receta"] == "ambiguo"  # sin cruce, intacto
        assert filas["0000000000009"]["requiere_receta"] == "no"  # no está en el excel
        assert resumen["Actualizados"] == 1
        assert resumen["Conflictos con la whitelist (no aplicados)"] == 1
        assert resumen["conflictos"][0]["barcode"] == "7790001"

    def test_csv_generado_lo_carga_sku_service(self, tmp_path):
        from app.services.catalogo_pdf import a_fila_catalogo, COLUMNAS_B
        import csv as _csv
        from app.services.sku_service import SKUService
        fila = a_fila_catalogo(
            {"laboratorio": "Bagó", "producto": "BAGO+CALMA COM x 30", "troquel": "9960248",
             "barcode": "7790375269913", "stock": 1.0, "prom_vta": 0.0, "valor": 20497.29},
            "Alimentos")
        p = tmp_path / "cat.csv"
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = _csv.DictWriter(f, fieldnames=COLUMNAS_B)
            w.writeheader()
            w.writerow(fila)
        svc = SKUService(str(p))
        r = svc.buscar("bago calma")
        assert r and r[0]["precio"] == 20497.29 and r[0]["vendible"]


def test_nombre_coincide_no_presenta_sustitutos():
    """
    Regresión (caso real): pidió "sedal cerámicas sha" y se le presentó un
    CAPILATIS ORTIGA como si fuera lo pedido. Un resultado de otra marca se
    ofrece como "lo más parecido", nunca como el producto solicitado.

    Otra marca → no coincide; y desde el caso del talco (21/8), otro TIPO de
    producto tampoco: el acondicionador de la misma línea no es el shampoo
    que pidieron, se ofrece como alternativa pero no como lo pedido.
    """
    from app.services.sku_service import nombre_coincide
    assert nombre_coincide("sedal ceramicas sha", "Unilever SEDAL CERAMIDAS SHA x 340")
    assert not nombre_coincide("sedal ceramicas sha", "Capilatis S.A CAPILATIS ORTIGA SHA X 410")
    assert not nombre_coincide("sedal ceramicas sha", "Unilever SEDAL CERAMIDAS ACO x 340")


def test_busqueda_ignora_palabras_de_formato(sku_svc):
    """'sha'/'shampoo' arrastran a otros shampoos de otra marca: se filtran."""
    r = sku_svc.buscar("sedal ceramidas sha")
    assert r and "sedal" in r[0]["nombre"].lower(), f"primero quedó: {r[0]['nombre']}"


def test_lista_de_opciones_no_es_oferta():
    """
    Regresión (caso real, rubio ceniza): la respuesta con 3 opciones y sus 3
    precios dejaba la PRIMERA como pendiente; "perfecto, rubio oscuro" (opción
    2) confirmó la 1 y salió un link de $33.437 equivocado. Varios precios =
    lista para elegir, no una oferta.
    """
    res = [
        {"sku_id": "A", "nombre": "LOREAL RUBIO CLARO MEDIO", "precio": 33437.53},
        {"sku_id": "B", "nombre": "KOLESTON RUBIO OSCURO", "precio": 9555.28},
        {"sku_id": "C", "nombre": "SOFT COLOR RUBIO AVELLANA", "precio": 17240.93},
    ]
    lista = ("1. LOREAL RUBIO CLARO MEDIO por $33.437,53 "
             "2. KOLESTON RUBIO OSCURO por $9.555,28 "
             "3. SOFT COLOR RUBIO AVELLANA por $17.240,93")
    assert len(ch.productos_con_precio(lista, res)) == 3
    assert ch.producto_respaldado(lista, res) is None
    # Con UN solo precio sí es una oferta concreta
    uno = "Te ofrezco el KOLESTON RUBIO OSCURO por $9.555,28. ¿Te lo confirmo?"
    assert ch.producto_respaldado(uno, res)["sku_id"] == "B"


async def test_set_pending_limpia_espera_eleccion():
    """Elegir un producto concreto levanta la marca de 'opciones sin elegir'."""
    from app.services.session_service import SessionService
    ss = SessionService("redis://127.0.0.1:1")
    s = await ss.get("549")
    s["_espera_eleccion"] = True
    await ss.save("549", s)
    await ss.set_pending("549", sku_id="B", sku_nombre="Koleston", precio=9555.28)
    assert "_espera_eleccion" not in await ss.get("549")
