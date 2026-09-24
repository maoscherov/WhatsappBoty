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
