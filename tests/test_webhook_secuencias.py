"""
Secuencias reales por el webhook completo, con dependencias falsas.

Casos del 23/9 (Farmacia Mutual):
- 5:50  "¿Me pasás con las chicas?" con un producto pendiente → el modelo
        prometió pasar con alguien, nadie derivó, y "Bueno" preguntó la entrega.
- 5:55  receta electrónica en PDF → se descartó en silencio; "Necesito eso"
        confirmó el Colpuril que había quedado pendiente.
"""
import csv
import os
import tempfile

import pytest

from app.routers import webhook as wh
from app.services.config_service import DEFAULTS
from app.services.session_service import SessionService
from app.services.sku_service import SKUService

PHONE = "5493410000001"

_ROWS = [
    ("10", "Colpuril Retard 300/0.5 mg Cap X50", "36951.52", "Bago", "Bago", "1010",
     "Medicamentos Bajo Receta", "true"),
    ("11", "Taural F 20 mg Com X30", "12000", "Roemmers", "Roemmers", "1111",
     "Medicamentos Bajo Receta", "true"),
]


class _Rec:
    """Cualquier método no definido es un async no-op que queda registrado."""
    def __init__(self):
        self.llamadas = []

    def __getattr__(self, nombre):
        async def _f(*a, **k):
            self.llamadas.append((nombre, a, k))
            return None
        return _f


class _Wa(_Rec):
    def __init__(self):
        super().__init__()
        self.enviados = []

    async def send_text(self, phone, texto, **k):
        self.enviados.append(texto)
        return True

    async def mark_read(self, *a, **k):
        return None


class _Cfg:
    def __init__(self, extra=None):
        self.v = dict(DEFAULTS)
        self.v.update(extra or {})

    async def get_all(self):
        return dict(self.v)

    async def get(self, k):
        return self.v.get(k)

    async def get_hours(self):
        return {}

    def is_open_now(self, hours):
        return True


class _Intent:
    """Devuelve respuestas guionadas según el texto del cliente."""
    def __init__(self, guion):
        self.guion = guion
        self.vistos = []

    async def procesar_rapido(self, mensaje, **k):
        self.vistos.append(("rapido", mensaje))
        return self.guion.get(mensaje, {"intencion": "saludo", "respuesta": "¡Hola!"})

    async def procesar(self, mensaje, **k):
        self.vistos.append(("procesar", mensaje))
        return self.guion.get(mensaje, {"intencion": "desconocido", "respuesta": "¿En qué te ayudo?"})


class _Img:
    def __init__(self, tipo="receta"):
        self.tipo = tipo
        self.mimes = []

    async def analizar(self, data, mime):
        self.mimes.append(mime)
        return {"tipo": self.tipo, "items": ""}

    async def leer_receta(self, *a, **k):
        return None


class _Rag:
    def enabled(self):
        return False


class _Socios:
    total = 0

    def find_by_phone(self, phone):
        return None

    def contexto_para_prompt(self, phone):
        return None


@pytest.fixture
def entorno(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["SKU", "Nombre", "Precio", "Marca", "Laboratorio",
                    "Codigo_Barras_1", "Categoria", "Es_Medicamento"])
        w.writerows(_ROWS)

    def armar(guion=None, img_tipo="receta", cfg=None):
        deps = {
            "wa": _Wa(), "sku": SKUService(path),
            "session": SessionService("redis://127.0.0.1:1"),
            "intent": _Intent(guion or {}), "payment": _Rec(), "audio": _Rec(),
            "image": _Img(img_tipo), "perf": _Rec(), "config": _Cfg(cfg),
            "socios": _Socios(), "msgs": _Rec(), "metrics": _Rec(), "rag": _Rag(),
        }
        monkeypatch.setattr(wh, "_deps", lambda s=None: deps)

        async def _pdf(url):
            return b"%PDF-1.4 receta"
        monkeypatch.setattr(wh, "_descargar_url", _pdf)
        return deps

    yield armar
    os.remove(path)


_n = [0]


def _msg(texto="", tipo="text", **extra):
    _n[0] += 1
    return {"from": PHONE, "id": f"wamid.{_n[0]}", "type": tipo, "text": texto,
            "audio_id": None, "image_id": None, "image_mime_type": extra.pop("mime", "image/jpeg"),
            "phone_number_id": "x", **extra}


async def _pendiente_colpuril(ss):
    await ss.set_pending(PHONE, sku_id="10", sku_nombre="Colpuril Retard 300/0.5 mg Cap X50",
                         precio=31408.79, cantidad=1, opciones=[])


# ── 5:50 — "me pasás con las chicas" ────────────────────────────────────────────
async def test_me_pasas_con_las_chicas_deriva_y_bueno_no_pregunta_entrega(entorno):
    deps = entorno()
    await _pendiente_colpuril(deps["session"])

    await wh.procesar_mensajes([_msg("Me pasas con las chicas?")])
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador"
    assert "equipo" in deps["wa"].enviados[-1]

    antes = len(deps["wa"].enviados)
    await wh.procesar_mensajes([_msg("Bueno")])
    assert len(deps["wa"].enviados) == antes          # modo operador: el bot calla
    assert not any("retiro" in t.lower() for t in deps["wa"].enviados)


async def test_derivacion_prometida_por_el_modelo_se_cumple(entorno):
    """Frase que el detector no cubre: responde el modelo prometiendo a una
    persona. El sistema tiene que derivar de verdad y soltar el pendiente."""
    guion = {"¿me ayudan ustedes con esto?": {
        "intencion": "desconocido", "confirmacion": None,
        "respuesta": "Claro, te paso con alguien del equipo. Por favor, aguardá un momento."}}
    deps = entorno(guion)
    await _pendiente_colpuril(deps["session"])

    await wh.procesar_mensajes([_msg("¿me ayudan ustedes con esto?")])
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador"
    assert s.get("derivada_motivo") == "derivacion_prometida"
    assert not s.get("pending_sku_id")

    antes = len(deps["wa"].enviados)
    await wh.procesar_mensajes([_msg("Bueno")])
    assert len(deps["wa"].enviados) == antes


# ── 5:55 — receta en PDF ────────────────────────────────────────────────────────
async def test_receta_en_pdf_se_lee_y_deriva(entorno):
    deps = entorno(img_tipo="receta")
    await _pendiente_colpuril(deps["session"])

    await wh.procesar_mensajes([_msg("", tipo="document", media_url="https://kapso/rpe.pdf",
                                     mime="application/pdf")])
    assert deps["image"].mimes == ["application/pdf"]
    assert deps["wa"].enviados, "el PDF no puede quedar sin respuesta"
    assert "receta" in deps["wa"].enviados[-1].lower()
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador"

    # "Hola" y "Necesito eso" ya no confirman nada: la conversación es del operador.
    antes = len(deps["wa"].enviados)
    await wh.procesar_mensajes([_msg("Hola")])
    await wh.procesar_mensajes([_msg("Necesito eso")])
    assert len(deps["wa"].enviados) == antes


async def test_pdf_con_texto_eso_en_el_mismo_lote(entorno):
    deps = entorno(img_tipo="receta")
    await wh.procesar_mensajes([
        _msg("Necesito eso"),
        _msg("", tipo="document", media_url="https://kapso/rpe.pdf", mime="application/pdf"),
    ])
    # Una sola respuesta: la de la receta (el texto deíctico se descarta).
    assert len(deps["wa"].enviados) == 1 and "receta" in deps["wa"].enviados[0].lower()


async def test_documento_ilegible_se_deriva(entorno):
    deps = entorno()
    await wh.procesar_mensajes([_msg("", tipo="document", media_url="https://kapso/orden.docx",
                                     mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")])
    assert deps["wa"].enviados and "archivo" in deps["wa"].enviados[-1]
    assert (await deps["session"].get(PHONE))["estado"] == "operador"
    assert deps["image"].mimes == []                    # no se intentó leer


async def test_video_no_se_ignora(entorno):
    deps = entorno()
    await wh.procesar_mensajes([_msg("", tipo="video", media_url="https://kapso/v.mp4")])
    assert deps["wa"].enviados
    assert (await deps["session"].get(PHONE))["estado"] == "operador"


async def test_sticker_si_se_ignora(entorno):
    deps = entorno()
    await wh.procesar_mensajes([_msg("", tipo="sticker")])
    assert deps["wa"].enviados == []


# ── "necesito eso" con un pendiente que lleva receta ────────────────────────────
async def test_eso_no_confirma_un_pendiente_con_receta(entorno):
    deps = entorno()
    await _pendiente_colpuril(deps["session"])
    await wh.procesar_mensajes([_msg("Necesito eso")])
    ultimo = deps["wa"].enviados[-1]
    assert "nombre del producto" in ultimo and "Colpuril" in ultimo
    assert "retiro" not in ultimo.lower()
    assert not (await deps["session"].get(PHONE)).get("pending_sku_id")


def test_referencia_ambigua_reglas():
    from app.services.checkout_helper import referencia_ambigua_bloquea as r
    assert r("necesito eso", {"pending_at": 10}, True) is True
    # Cotizado por el operador (receta vista): "quiero eso" sí confirma
    assert r("quiero eso", {"pending_at": 10, "receta_validada": True}, True) is False
    # Sin receta: bloquea solo si hubo un adjunto después del pendiente
    assert r("necesito eso", {"pending_at": 10, "_adjunto_at": 20}, False) is True
    assert r("necesito eso", {"pending_at": 30, "_adjunto_at": 20}, False) is False
    assert r("necesito eso", {"pending_at": 10}, False) is False
    # No es deíctico: nunca bloquea
    assert r("sí, el colpuril", {"pending_at": 10}, True) is False


@pytest.mark.parametrize("txt,esperado", [
    ("Claro, te paso con alguien del equipo. Aguardá un momento.", True),
    ("Te derivo con una de las chicas.", True),
    ("¿Querés que te pase con alguien del equipo?", False),
    ("Si querés te paso con alguien del equipo.", False),
    ("Puedo pasarte con el farmacéutico si preferís.", False),
    ("Tengo el Actron a $4.770. ¿Te sirve?", False),
])
def test_derivacion_prometida(txt, esperado):
    from app.services.checkout_helper import derivacion_prometida
    assert derivacion_prometida(txt) is esperado


@pytest.mark.parametrize("txt", ["Me pasas con las chicas?", "pasame con alguna de las chicas",
                                 "quiero hablar con alguien", "me pasás con la farmacéutica?"])
def test_pide_humano_chicas(txt):
    from app.services.checkout_helper import pide_humano
    assert pide_humano(txt) is True


@pytest.mark.parametrize("txt", ["algo para las chicas de 5 años", "un regalo para las chicas"])
def test_pide_humano_chicas_no_dispara(txt):
    from app.services.checkout_helper import pide_humano
    assert pide_humano(txt) is False


def test_kapso_documento_trae_caption_y_mime():
    evento = {"message": {"from": "+5493410000001", "id": "w1", "type": "document",
                          "document": {"caption": "esta es la receta", "mime_type": "application/pdf"},
                          "kapso": {"media_url": "https://kapso/rpe.pdf"}}}
    m = wh._kapso_a_mensajes(evento)[0]
    assert m["type"] == "document" and m["text"] == "esta es la receta"
    assert m["image_mime_type"] == "application/pdf" and m["media_url"].endswith(".pdf")


def test_bloque_adjunto_pdf():
    from app.services.image_service import bloque_adjunto
    assert bloque_adjunto("QQ==", "application/pdf")["type"] == "document"
    assert bloque_adjunto("QQ==", "image/jpeg")["type"] == "image"


# ══════════════════════════════════════════════════════════════════════════════
# 23/9 10:57 — audio de María: "jabón, aveno y la crema Topics"
# ══════════════════════════════════════════════════════════════════════════════
class _Audio:
    def __init__(self, texto):
        self.texto = texto
        self.prompts = []

    async def transcribir(self, data, filename="audio.ogg", prompt=None):
        self.prompts.append(prompt)
        return self.texto


class _Msgs:
    def __init__(self):
        self.guardados = []

    async def save(self, phone, role, content, autor=None, origen=None, media=None, media_nombre=None):
        self.guardados.append({"role": role, "content": content, "origen": origen, "media": media})


_MARIA = "Hola chicas, buen día, ¿cómo va? Me dicen si tienen jabón, aveno y la crema Topics."


@pytest.fixture
def entorno_maria(monkeypatch, entorno):
    import app.services.catalog_live as cl

    async def _sin_erp(*a, **k):
        return None
    monkeypatch.setattr(cl, "lookup_y_aplicar", _sin_erp)
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["SKU", "Nombre", "Precio", "Marca", "Laboratorio",
                    "Codigo_Barras_1", "Categoria", "Es_Medicamento"])
        w.writerows([
            ("20", "Jabon Vertiente Frutos Rojos X90", "3500", "Vertiente", "Vertiente", "2020", "Jabones", "false"),
            ("21", "AVENO JAB x 120", "17609.31", "Aveno", "Andromaco", "2121", "Jabones", "false"),
            ("22", "DERMAGLOS F GLICOLICO MANDELICO CRE x 50", "40458.27", "Dermaglos", "Andromaco",
             "2222", "Dermocosmética", "false"),
        ])

    class _Blob:
        async def save(self, *a, **k):
            return True
    import app.services.blob_store as bs
    monkeypatch.setattr(bs, "get_blob_store", lambda *a, **k: _Blob())

    def armar(transcripcion=_MARIA):
        guion = {_MARIA: {
            "intencion": "consulta_stock", "entidad_producto": "jabón",
            "entidades_adicionales": ["avena", "crema Topics"],
            "respuesta": ("¡Hola María! Justo no tengo stock del jabón Vertiente frutos rojos en este "
                          "momento. En cuanto a la avena y la crema Topics, ¿Te gustaría que te "
                          "ofrezca otras opciones de jabón?")}}
        deps = entorno(guion)
        deps["sku"] = SKUService(path)
        deps["audio"] = _Audio(transcripcion)
        deps["msgs"] = _Msgs()
        return deps

    yield armar
    os.remove(path)


async def test_audio_kapso_se_transcribe_con_vocabulario_y_se_guarda(entorno_maria):
    deps = entorno_maria()
    await wh.procesar_mensajes([_msg("", tipo="audio", media_url="https://kapso/a.ogg",
                                     texto_transcripto="jabón, avena y la crema a tópicos")])
    # Se usó NUESTRA transcripción, con las marcas como guía
    assert deps["audio"].prompts and "Atopix" in deps["audio"].prompts[0]
    assert "Aveno" in deps["audio"].prompts[0]
    # Historial: el mensaje del cliente queda marcado como audio, con el original
    u = [g for g in deps["msgs"].guardados if g["role"] == "user"]
    assert u and u[-1]["origen"] == "audio" and u[-1]["content"] == _MARIA
    assert (u[-1]["media"] or "").startswith("/media/chat/aud")


async def test_audio_si_falla_la_transcripcion_propia_usa_la_de_kapso(entorno_maria):
    deps = entorno_maria(transcripcion=None)
    await wh.procesar_mensajes([_msg("", tipo="audio", media_url="https://kapso/a.ogg",
                                     texto_transcripto="hola, tenés ibuprofeno?")])
    u = [g for g in deps["msgs"].guardados if g["role"] == "user"]
    assert u and u[-1]["content"] == "hola, tenés ibuprofeno?" and u[-1]["origen"] == "audio"


async def test_maria_respuesta_limpia(entorno_maria):
    deps = entorno_maria()
    await wh.procesar_mensajes([_msg(_MARIA)])
    r = deps["wa"].enviados[-1]
    # "avena" del modelo vuelve a "aveno" del cliente → encuentra el jabón Aveno tal cual
    assert "• aveno: AVENO JAB x 120" in r
    # "crema Topics" no tiene nada en común con la Dermaglos: no se ofrece
    assert "DERMAGLOS" not in r and "crema Topics: no lo encontré" in r
    # Una sola pregunta: fuera la vaga del modelo y sin la de consultar con el equipo
    assert "ofrezca otras opciones" not in r
    assert "consulte con el equipo" not in r


def test_restaurar_palabras_cliente():
    from app.services.sku_service import restaurar_palabras_cliente as r
    assert r("avena", _MARIA) == "aveno"
    assert r("jabón avena", _MARIA) == "jabón aveno"
    assert r("crema Topics", _MARIA) == "crema Topics"          # no inventa
    assert r("ibuprofeno", "tenés ibuprofeno?") == "ibuprofeno"
    assert r("avena", "quiero un jabón de avena") == "avena"     # el cliente dijo avena
    assert r(None, _MARIA) is None


def test_alguna_palabra_coincide():
    from app.services.sku_service import alguna_palabra_coincide as a
    assert a("crema Topics", "DERMAGLOS F GLICOLICO MANDELICO CRE x 50") is False
    assert a("avena", "AVENO JAB x 120") is True
    assert a("crema atopix", "Atopix (Pieles Atopicas) Crema X150Gr") is True
    assert a("crema", "Cualquier Crema X50") is True                # solo tipo: no objeta
    assert a("crema Topics", "Otra Crema X50") is False


def test_vocabulario_audio_trae_marcas_del_catalogo(entorno_maria):
    from app.services.sku_service import vocabulario_audio
    deps = entorno_maria()
    v = vocabulario_audio(deps["sku"])
    assert v.startswith("Consulta a una farmacia") and len(v) <= 650
    assert "Atopix" in v and "Vertiente" in v


@pytest.mark.parametrize("txt,esperado", [
    ("¡Hola María! Justo no tengo stock del jabón. En cuanto a la avena y la crema Topics, "
     "¿Te gustaría que te ofrezca otras opciones de jabón?", "¡Hola María! Justo no tengo stock del jabón."),
    ("Tengo el Actron a $4.770, ¿te gustaría considerarlo?", "Tengo el Actron a $4.770."),
    ("¿Te gustaría que te lo encargue?", "¿Te gustaría que te lo encargue?"),
    ("¿Querés que te pase con alguien del equipo?", "¿Querés que te pase con alguien del equipo?"),
])
def test_cierre_vago_oracion_completa(txt, esperado):
    from app.services.checkout_helper import quitar_cierres_vagos
    assert quitar_cierres_vagos(txt) == esperado


# ── 27/9: los adjuntos que el bot no lee se guardan para el operador ────────────
async def test_documento_ilegible_queda_guardado_con_su_nombre(entorno):
    from app.services import chat_media
    deps = entorno()
    await wh.procesar_mensajes([_msg("", tipo="document", media_url="https://kapso/orden.docx",
                                     filename="Orden médica.docx",
                                     mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")])
    guardados = [k for n, a, k in deps["msgs"].llamadas if n == "save" and a[1] == "user"]
    assert guardados and guardados[-1]["media"].endswith(".docx")
    assert guardados[-1]["media_nombre"] == "Orden médica.docx"
    assert guardados[-1]["origen"] == "documento"
    id_, _ = chat_media.separar(guardados[-1]["media"])
    data, ext, nombre = await chat_media.cargar(id_)
    assert ext == ".docx" and nombre == "Orden médica.docx"


async def test_receta_pdf_queda_guardada_como_pdf(entorno):
    deps = entorno(img_tipo="receta")
    await wh.procesar_mensajes([_msg("", tipo="document", media_url="https://kapso/rpe.pdf",
                                     filename="rpe.pdf", mime="application/pdf")])
    guardados = [k for n, a, k in deps["msgs"].llamadas if n == "save" and a[1] == "user"]
    assert guardados and guardados[0]["media"].endswith(".pdf")
    assert guardados[0]["media_nombre"] == "rpe.pdf"


# ── 27/9: fuera de horario vende lo que no lleva receta ─────────────────────────
class _CfgCerrado(_Cfg):
    def is_open_now(self, hours):
        return False

    def proxima_apertura(self, hours):
        return "mañana a las 8:00"


@pytest.fixture
def cerrado(entorno):
    def armar(guion=None, img_tipo="receta", cfg=None):
        deps = entorno(guion, img_tipo, cfg)
        c = _CfgCerrado(cfg)
        deps["config"] = c
        return deps
    return armar


async def test_fuera_de_horario_el_bot_atiende(cerrado):
    deps = cerrado()
    await wh.procesar_mensajes([_msg("Hola")])
    assert deps["wa"].enviados == ["¡Hola!"]


async def test_fuera_de_horario_apagado_responde_cerrado(cerrado):
    deps = cerrado(cfg={"vender_fuera_horario": "false"})
    await wh.procesar_mensajes([_msg("Hola")])
    assert len(deps["wa"].enviados) == 1 and "¡Hola!" not in deps["wa"].enviados


async def test_fuera_de_horario_pedir_una_persona_avisa_cuando_abrimos(cerrado):
    deps = cerrado()
    await wh.procesar_mensajes([_msg("Me pasás con una persona?")])
    r = deps["wa"].enviados[-1]
    assert "alguien del equipo" in r
    assert "fuera de horario" in r and "mañana a las 8:00" in r
    assert "En un momento" not in r
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador" and s.get("_derivada_fuera_horario") is True
    # Lo que quedó en la sesión es lo que se mandó de verdad
    assert s["history"][-1]["content"] == r
    # No vuelve al bot ni recibe el "ya te atienden" durante la noche
    assert PHONE not in await deps["session"].derivadas_sin_atender(0)
    assert PHONE not in await deps["session"].derivadas_para_aviso(0)


async def test_fuera_de_horario_receta_en_pdf_avisa_cuando_abrimos(cerrado):
    deps = cerrado(img_tipo="receta")
    await wh.procesar_mensajes([_msg("", tipo="document", media_url="https://kapso/rpe.pdf",
                                     mime="application/pdf")])
    r = deps["wa"].enviados[-1]
    assert "receta" in r.lower() and "mañana a las 8:00" in r
    assert (await deps["session"].get(PHONE))["estado"] == "operador"


async def test_liberar_limpia_la_marca_de_fuera_de_horario(cerrado):
    deps = cerrado()
    await wh.procesar_mensajes([_msg("Me pasás con una persona?")])
    await deps["session"].liberar(PHONE)
    assert not (await deps["session"].get(PHONE)).get("_derivada_fuera_horario")


# ── "soy socio" y no está en el padrón ──────────────────────────────────────────
async def test_dice_ser_socio_sin_padron_deriva(entorno):
    deps = entorno()
    await wh.procesar_mensajes([_msg("Hola, soy socia de la mutual, tenés ibuprofeno?")])
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "socio_no_reconocido"
    assert "DNI" in deps["wa"].enviados[-1]


async def test_socio_del_padron_no_se_deriva(entorno):
    class _EnPadron(_Socios):
        def find_by_phone(self, phone):
            return {"nombre": "Ana", "nombre_pila": "Ana"}

        def contexto_para_prompt(self, phone):
            return "Nombre de pila (para saludar): Ana"
    deps = entorno()
    deps["socios"] = _EnPadron()
    await wh.procesar_mensajes([_msg("Hola, soy socia")])
    assert (await deps["session"].get(PHONE)).get("estado") != "operador"


def test_dice_ser_socio():
    from app.services.checkout_helper import dice_ser_socio as d
    for t in ("soy socio", "Soy socia de la mutual", "estoy afiliada",
              "mi número de socio es 1234", "somos socios"):
        assert d(t), t
    assert not d("quiero ser socio")
    assert not d("No soy socio, ¿igual me lo venden?")
    assert not d("¿los socios tienen descuento?")


def test_aviso_fuera_horario():
    from app.services.checkout_helper import aviso_fuera_horario as a
    r = a("Dale Ana, te paso con alguien del equipo. En un momento te contactamos 🙌",
          "mañana a las 8:00")
    assert r.startswith("Dale Ana, te paso con alguien del equipo.")
    assert "En un momento" not in r and r.endswith("(mañana a las 8:00) 🙏")
    assert a("Aguardá un momento por favor.", "").startswith("Eso lo ve alguien del equipo.")


def test_proxima_apertura(monkeypatch):
    from datetime import datetime as _dt
    from app.services import config_service as cs

    class _Reloj(_dt):
        ahora = None

        @classmethod
        def now(cls, tz=None):
            return cls.ahora.replace(tzinfo=tz)
    monkeypatch.setattr(cs, "datetime", _Reloj)
    semana = {d: {"active": True, "open": "08:00", "close": "20:00"}
              for d in ("mon", "tue", "wed", "thu", "fri")}
    semana["sat"] = {"active": True, "open": "09:00", "close": "13:00"}
    h = {"enabled": True, "schedule": semana}
    svc = cs.ConfigService.__new__(cs.ConfigService)
    _Reloj.ahora = _dt(2026, 9, 28, 6, 30)          # lunes, antes de abrir
    assert svc.proxima_apertura(h) == "hoy a las 8:00"
    _Reloj.ahora = _dt(2026, 9, 28, 22, 0)          # lunes a la noche
    assert svc.proxima_apertura(h) == "mañana a las 8:00"
    _Reloj.ahora = _dt(2026, 10, 2, 21, 0)          # viernes a la noche
    assert svc.proxima_apertura(h) == "mañana a las 9:00"
    _Reloj.ahora = _dt(2026, 10, 3, 14, 0)          # sábado a la tarde
    assert svc.proxima_apertura(h) == "el lunes a las 8:00"


async def test_nota_envio_fuera_horario(monkeypatch):
    from app.services import checkout_helper as ch
    from app.services import config_service as cs

    class _C:
        abierto = False

        async def get_hours(self):
            return {}

        def is_open_now(self, h):
            return self.abierto

        def proxima_apertura(self, h):
            return "mañana a las 8:00"
    c = _C()
    monkeypatch.setattr(cs, "get_config_service", lambda *a, **k: c)
    r = await ch.nota_envio_fuera_horario("Link: x", "envio")
    assert r.endswith("el envío sale apenas abramos (mañana a las 8:00).")
    assert await ch.nota_envio_fuera_horario("Link: x", "retiro") == "Link: x"
    c.abierto = True
    assert await ch.nota_envio_fuera_horario("Link: x", "envio") == "Link: x"


# ── 1/10: "sí" a la consulta ofrecida → deriva (antes se perdía el mensaje) ─────
@pytest.mark.parametrize("respuesta", [
    "Si. Confirmame cuales tenes",
    "si, necesito un presupuesto si es que lo llegan a conseguir",
    "Me gustaría saber el precio",
    "dale",
])
async def test_acepta_la_consulta_ofrecida_y_deriva(entorno, respuesta):
    deps = entorno()
    s = await deps["session"].get(PHONE)
    s["derivacion_ofrecida"] = "perfumes para mujer"
    await deps["session"].save(PHONE, s)
    await wh.procesar_mensajes([_msg(respuesta)])
    assert deps["wa"].enviados, "no puede quedar sin respuesta"
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "sin_stock"


async def test_otra_cosa_tras_la_oferta_responde_igual(entorno):
    """Caso real 1/10: la variable de la sesión pisaba la de configuración y
    el mensaje siguiente a la oferta explotaba sin respuesta."""
    deps = entorno()
    s = await deps["session"].get(PHONE)
    s["derivacion_ofrecida"] = "perfumes para mujer"
    await deps["session"].save(PHONE, s)
    await wh.procesar_mensajes([_msg("dale, mandame un Dove")])
    assert deps["wa"].enviados
    assert (await deps["session"].get(PHONE)).get("estado") != "operador"


async def test_error_inesperado_deriva_y_avisa(entorno, monkeypatch):
    """Red de seguridad 1/10: un error no deja al cliente sin respuesta."""
    deps = entorno()

    async def _explota(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(deps["intent"], "procesar_rapido", _explota)
    monkeypatch.setattr(deps["intent"], "procesar", _explota)
    await wh.procesar_mensajes([_msg("tenés algo para el dolor de cabeza?")])
    assert deps["wa"].enviados and "alguien del equipo" in deps["wa"].enviados[-1]
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "error_bot"


# ── Auditoría 2/10: consulta de saldo de cuenta corriente → persona ─────────────
@pytest.mark.parametrize("txt", [
    "Hola chicas, me dirian lo que debo asi les pago",
    "Saldo de cuenta corriente",
    "me pasás el resumen de mi cuenta?",
    "cuánto te debo?",
    "les debo recetas?",
    "cuanto es la cuota de socio",
])
def test_consulta_saldo_detecta(txt):
    from app.services.checkout_helper import consulta_saldo
    assert consulta_saldo(txt)


@pytest.mark.parametrize("txt", ["anotámelo en la cuenta", "quiero pagar con cuenta corriente",
                                 "tenés ibuprofeno?", "debo tomarlo con comida?"])
def test_consulta_saldo_no_confunde(txt):
    from app.services.checkout_helper import consulta_saldo
    assert not consulta_saldo(txt)


def test_cuanto_debo_con_pedido_es_el_total():
    from app.services.checkout_helper import consulta_saldo
    assert not consulta_saldo("cuánto te debo?", hay_pedido=True)
    assert consulta_saldo("saldo de mi cuenta", hay_pedido=True)


async def test_saldo_de_cuenta_corriente_deriva(entorno):
    deps = entorno()
    await wh.procesar_mensajes([_msg("Saldo de cuenta corriente")])
    assert "revisar tu cuenta" in deps["wa"].enviados[-1]
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "consulta_cuenta_corriente"


# ── Auditoría 2/10: precios inventados no llegan al cliente ─────────────────────
def _bloqueos(deps):
    return [c for c in deps["metrics"].llamadas
            if c[0] == "evento" and c[1] and c[1][0] == "respuesta_bloqueada"]


async def test_perfumes_inventados_no_se_envian(entorno):
    txt = "tenés perfumes importados?"
    guion = {txt: {"intencion": "consulta_stock", "entidad_producto": "perfumes importados",
                   "respuesta": "Tenemos Chanel No. 5 - $7.200 y Dior Sauvage $6.400. ¿Cuál querés?"}}
    deps = entorno(guion)
    await wh.procesar_mensajes([_msg(txt)])
    enviado = deps["wa"].enviados[-1]
    assert "7.200" not in enviado and "Chanel" not in enviado
    assert "consulte con el equipo" in enviado
    assert _bloqueos(deps)
    # El "sí" siguiente deriva
    await wh.procesar_mensajes([_msg("dale")])
    assert (await deps["session"].get(PHONE))["estado"] == "operador"


async def test_precio_real_del_catalogo_pasa(entorno):
    txt = "precio del taural"
    guion = {txt: {"intencion": "consulta_precio", "entidad_producto": "taural",
                   "sku_seleccionado_index": 1,
                   "respuesta": "El Taural F 20 mg Com X30 está $12.000."}}
    deps = entorno(guion)
    await wh.procesar_mensajes([_msg(txt)])
    assert not _bloqueos(deps)


async def test_precio_inventado_con_resultados_lista_los_reales(entorno):
    txt = "precio del taural"
    guion = {txt: {"intencion": "consulta_precio", "entidad_producto": "taural",
                   "respuesta": "El Taural sale $9.999 y el genérico $3.500."}}
    deps = entorno(guion)
    await wh.procesar_mensajes([_msg(txt)])
    assert _bloqueos(deps)
    assert not any("9.999" in t or "3.500" in t for t in deps["wa"].enviados)


def test_precio_de_lista_como_si_tuviera_descuento():
    """Nivea 1/10: "$36.221 ya con tu 20%" cuando bonificado sale $28.977."""
    from app.services.checkout_helper import precios_inventados, referencias_de_precio
    res = [{"precio": 28977.32, "precio_lista": 36221.65}]
    malo = "La Nivea Q10 está a $36.221,65, ya con tu 20% de descuento."
    unit, tot = referencias_de_precio(res, {}, {}, malo)
    assert precios_inventados(malo, unit, tot) == [36221.65]
    bien = "Precio de lista $36.221,65; con tu 20% te queda $28.977,32."
    unit, tot = referencias_de_precio(res, {}, {}, bien)
    assert precios_inventados(bien, unit, tot) == []


def test_numeros_que_no_son_precios_no_cuentan():
    from app.services.checkout_helper import precios_inventados
    assert precios_inventados("Tengo Ibuprofeno 600 x 100 y Aspirina 500 mg", [], []) == []
    assert precios_inventados("2 unidades a $1.000 son $2.000", [1000], []) == []


# ── Auditoría 2/10: "encargalo" tras "no lo tengo" no cobra el sustituto ────────
async def test_encargalo_deriva_y_no_cobra_el_sustituto(entorno):
    deps = entorno()
    await _pendiente_colpuril(deps["session"])
    await deps["session"].add_message(
        PHONE, "assistant",
        "No me figura disponible el Colpuril x30, te lo puedo encargar. "
        "Tengo el Colpuril Retard x50 a $31.408,79.")
    await wh.procesar_mensajes([_msg("sí, encargalo")])
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "encargo"
    assert not s.get("pending_sku_id")
    assert "encargarlo" in deps["wa"].enviados[-1]
    assert not any("retiro" in t.lower() for t in deps["wa"].enviados)


# ── 5/10: "lo anoto en la cuenta" con el link ya enviado ────────────────────────
async def _link_enviado(ss):
    await _pendiente_colpuril(ss)
    await ss.set_entrega(PHONE, "retiro", None)
    await ss.set_estado(PHONE, "esperando_pago")


class _Empleados:
    def find_by_phone(self, phone):
        return {"nombre": "María", "apellido": "Belén", "activo": True} if phone == PHONE else None


async def test_empleada_anota_en_la_cuenta_con_link_enviado(entorno, monkeypatch):
    from app.services import empleado_service as es
    from app.services import checkout_helper as chh
    monkeypatch.setattr(es, "get_empleado_service", lambda *a, **k: _Empleados())

    async def _sin_freno(*a, **k):
        return None, None
    monkeypatch.setattr(chh, "_chequear_stock_vivo", _sin_freno)
    deps = entorno()
    await _link_enviado(deps["session"])
    await wh.procesar_mensajes([_msg("lo anoto en la cuenta")])
    assert "cuenta corriente" in deps["wa"].enviados[-1].lower()
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "pedido_confirmado"


async def test_cuenta_corriente_no_habilitada_deriva(entorno, monkeypatch):
    from app.services import empleado_service as es

    class _Nadie:
        def find_by_phone(self, phone):
            return None
    monkeypatch.setattr(es, "get_empleado_service", lambda *a, **k: _Nadie())
    deps = entorno()
    await _link_enviado(deps["session"])
    await wh.procesar_mensajes([_msg("lo anoto en la cuenta")])
    assert "cargarlo a tu cuenta" in deps["wa"].enviados[-1]
    s = await deps["session"].get(PHONE)
    assert s["estado"] == "operador" and s["derivada_motivo"] == "cuenta_corriente_no_habilitada"
    assert not any("Lo que tengo disponible" in t for t in deps["wa"].enviados)


# ── 5/10: "bueno dale, y el talco quiero el grande el de 200g" ──────────────────
def _catalogo_belen():
    base = {"hash": "a" * 64, "barcodes": [], "troquel": None, "brand": "", "drug": None,
            "form": None, "category": "Perfumeria", "rubro": "", "subrubro": "",
            "therapeutic_actions": [], "stock": 5, "visible": True, "active": True,
            "requiere_receta": "no", "source": "t"}
    return SKUService.from_rows([
        {**base, "external_id": "83744", "name": "SIEMPRE L ADAPT P NOCHE DIA TOA HIG TOA x 32", "price": 17183.43},
        {**base, "external_id": "85746", "name": "REXONA EFFIC.TAL.ORIG TAL x 100", "price": 4284.49},
        {**base, "external_id": "86260", "name": "REXONA EFFICIENT ORIGINAL 200GR POL TAL x 200", "price": 6605.68},
    ])


async def test_confirma_y_pide_otro_producto_lo_busca_y_lo_suma(entorno):
    txt = "Bueno dale, y el talco quiero el grande el de 200g"
    guion = {txt: {"intencion": "social", "confirmacion": True, "entidad_producto": None,
                   "entidades_adicionales": ["talco rexona 200g"],
                   "respuesta": "Perfecto. Sobre el talco de 200g no me figura disponible."}}
    deps = entorno(guion)
    deps["sku"] = _catalogo_belen()
    await deps["session"].set_pending(PHONE, sku_id="83744",
                                      sku_nombre="SIEMPRE L ADAPT P NOCHE DIA TOA HIG TOA x 32",
                                      precio=17183.43, cantidad=1, opciones=[])
    await wh.procesar_mensajes([_msg(txt)])
    enviado = deps["wa"].enviados[-1]
    assert "no me figura" not in enviado
    assert "200GR" in enviado and "Tu pedido queda así" in enviado
    s = await deps["session"].get(PHONE)
    assert [i["sku_id"] for i in s["pending_items"]] == ["83744", "86260"]
    assert s["estado"] == "esperando_entrega"          # confirmó: pasa a retiro/envío
