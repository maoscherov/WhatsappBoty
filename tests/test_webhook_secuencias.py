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

    async def save(self, phone, role, content, autor=None, origen=None, media=None):
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
