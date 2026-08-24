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
