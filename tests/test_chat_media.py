"""
Adjuntos de las conversaciones (27/9): se guardan en el volumen 6 meses, el
backoffice los ve con su tipo y nombre, no se abren sin clave o firma, y el
operador puede adjuntar archivos al cliente.
"""
import os
import time
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.services import chat_media
from app.services.message_store import mensaje_a_dict


@pytest.fixture
def clave(monkeypatch):
    monkeypatch.setattr(get_settings(), "bo_key", "CLAVE")
    return "CLAVE"


def test_ext_y_tipo():
    assert chat_media.ext_para("application/pdf") == ".pdf"
    assert chat_media.ext_para("application/octet-stream", "Orden médica.DOCX") == ".docx"
    assert chat_media.ext_para("image/jpeg", "foto.jpeg") == ".jpg"
    assert chat_media.tipo_de("/media/chat/abc.pdf") == "pdf"
    assert chat_media.tipo_de("/media/chat/abc.xlsx") == "archivo"
    assert chat_media.tipo_de("/media/chat/abc.mp4") == "video"
    # Referencias viejas, sin extensión: fotos, salvo los audios
    assert chat_media.tipo_de("/media/chat/wamid123") == "imagen"
    assert chat_media.tipo_de("/media/chat/aud123") == "audio"


async def test_guardar_y_cargar():
    ref = await chat_media.guardar("wamid.ABC==", b"%PDF-1.4", ".pdf", "receta.pdf")
    assert ref == "/media/chat/wamidABC.pdf"
    data, ext, nombre = await chat_media.cargar("wamidABC")
    assert data == b"%PDF-1.4" and ext == ".pdf" and nombre == "receta.pdf"
    assert await chat_media.guardar("x", b"0" * (chat_media.MAX_BYTES + 1), ".pdf") is None


async def test_limpiar_viejos_borra_solo_los_de_mas_de_6_meses():
    await chat_media.guardar("viejo", b"a", ".jpg")
    await chat_media.guardar("nuevo", b"b", ".jpg")
    hace_7_meses = time.time() - 210 * 86400
    for n in ("viejo.jpg", "viejo.json"):
        os.utime(chat_media._dir() / n, (hace_7_meses, hace_7_meses))
    assert chat_media.limpiar_viejos(180) == 1
    assert await chat_media.cargar("viejo") is None
    assert await chat_media.cargar("nuevo") is not None


def test_firma(clave):
    url = chat_media.firmar("/media/chat/abc.pdf")
    _, qs = url.split("?")
    p = dict(x.split("=") for x in qs.split("&"))
    assert chat_media.acceso_valido("abc", None, p["exp"], p["sig"])
    assert not chat_media.acceso_valido("otro", None, p["exp"], p["sig"])      # otra foto
    assert not chat_media.acceso_valido("abc", None, str(int(time.time()) - 1), p["sig"])
    assert not chat_media.acceso_valido("abc", None, None, None)
    assert chat_media.acceso_valido("abc", "CLAVE", None, None)


def test_historial_devuelve_tipo_nombre_y_url_firmada(clave):
    fila = {"id": 1, "role": "user", "content": "📷 /media/chat/abc.pdf", "autor": None,
            "origen": "documento", "media": "/media/chat/abc.pdf", "media_nombre": "receta.pdf",
            "created_at": datetime.now(timezone.utc)}
    d = mensaje_a_dict(fila)
    assert d["media_tipo"] == "pdf" and d["media_nombre"] == "receta.pdf"
    assert d["media"].startswith("/media/chat/abc.pdf?exp=") and "&sig=" in d["media"]
    assert "?exp=" in d["content"]


# ── /media/chat ─────────────────────────────────────────────────────────────────
@pytest.fixture
def cliente_media():
    from app.routers import media
    app = FastAPI()
    app.include_router(media.router)
    return TestClient(app)


async def test_media_chat_pide_clave_o_firma(clave, cliente_media):
    await chat_media.guardar("rec1", b"%PDF-1.4", ".pdf", "receta.pdf")
    await chat_media.guardar("doc1", b"PK..", ".docx", "orden médica.docx")
    assert cliente_media.get("/media/chat/rec1.pdf").status_code == 403

    r = cliente_media.get(chat_media.firmar("/media/chat/rec1.pdf"))
    assert r.status_code == 200 and r.content == b"%PDF-1.4"
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("inline")

    r = cliente_media.get("/media/chat/doc1.docx", headers={"x-bo-key": "CLAVE"})
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment")
    assert "orden%20m%C3%A9dica.docx" in r.headers["content-disposition"]
    assert cliente_media.get("/media/chat/nada.pdf?key=CLAVE").status_code == 404


# ── Operador adjunta un archivo ─────────────────────────────────────────────────
class _Wa:
    def __init__(self):
        self.enviados = []

    async def send_image(self, to, url, caption=""):
        self.enviados.append(("image", url, None, caption))
        return True

    async def send_document(self, to, url, filename="", caption=""):
        self.enviados.append(("document", url, filename, caption))
        return True


@pytest.fixture
def backoffice(monkeypatch, clave):
    from app.routers import backoffice as bo
    from app.services import message_store
    from app.services.session_service import SessionService

    wa = _Wa()
    ss = SessionService("redis://127.0.0.1:1")
    historial = []

    async def _hist(phone, role, content, autor=None, **k):
        historial.append({"role": role, "content": content, **k})
    monkeypatch.setattr(bo, "get_whatsapp_service", lambda *a, **k: wa)
    monkeypatch.setattr(bo, "get_session_service", lambda *a, **k: ss)
    monkeypatch.setattr(message_store, "guardar_historico", _hist)
    monkeypatch.setattr(get_settings(), "public_base_url", "https://cerca.test")
    app = FastAPI()
    app.include_router(bo.router)
    return TestClient(app), wa, ss, historial


def test_operador_adjunta_pdf(backoffice):
    cli, wa, ss, historial = backoffice
    r = cli.post("/bo/session/549341/attachment", headers={"x-bo-key": "CLAVE"},
                 files={"file": ("presupuesto.pdf", b"%PDF-1.4", "application/pdf")},
                 data={"caption": "Te paso el presupuesto", "agente": "Belén"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["media_tipo"] == "pdf" and j["media_nombre"] == "presupuesto.pdf"

    tipo, url, nombre, caption = wa.enviados[0]
    assert tipo == "document" and nombre == "presupuesto.pdf" and caption == "Te paso el presupuesto"
    assert url.startswith("https://cerca.test/media/chat/op") and "&sig=" in url

    h = historial[0]
    assert h["role"] == "operator" and h["origen"] == "documento"
    assert h["media"].endswith(".pdf") and h["media_nombre"] == "presupuesto.pdf"

    # La vista en vivo lo muestra con su tipo y firmado
    en_vivo = cli.get("/bo/session/549341", headers={"x-bo-key": "CLAVE"}).json()["history"][-1]
    assert en_vivo["role"] == "operator" and en_vivo["media_tipo"] == "pdf"
    assert "?exp=" in en_vivo["media"]


def test_operador_adjunta_foto_va_como_imagen(backoffice):
    cli, wa, _, historial = backoffice
    r = cli.post("/bo/session/549341/attachment", headers={"x-bo-key": "CLAVE"},
                 files={"file": ("foto.png", b"\x89PNG", "image/png")})
    assert r.status_code == 200
    assert wa.enviados[0][0] == "image"
    assert historial[0]["origen"] == "imagen"


def test_operador_no_puede_adjuntar_ejecutables(backoffice):
    cli, wa, _, _ = backoffice
    r = cli.post("/bo/session/549341/attachment", headers={"x-bo-key": "CLAVE"},
                 files={"file": ("virus.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 415 and wa.enviados == []


# ── Bucket S3 (Railway Buckets, 28/9) ───────────────────────────────────────────
class _S3Falso:
    """Lo mínimo de la API de S3 que usa chat_media, en memoria."""
    def __init__(self):
        self.objetos = {}

    def put_object(self, Bucket, Key, Body, ContentType, Metadata):
        from datetime import datetime, timezone
        for v in Metadata.values():
            v.encode("ascii")                      # S3 rechaza metadatos no ASCII
        self.objetos[Key] = {"Body": Body, "Metadata": Metadata, "ContentType": ContentType,
                             "LastModified": datetime.now(timezone.utc)}

    def get_object(self, Bucket, Key):
        import io
        if Key not in self.objetos:
            raise Exception("An error occurred (NoSuchKey)")
        o = self.objetos[Key]
        return {"Body": io.BytesIO(o["Body"]), "Metadata": o["Metadata"]}

    def list_objects_v2(self, Bucket, Prefix, **kw):
        return {"Contents": [{"Key": k, "LastModified": o["LastModified"]}
                             for k, o in self.objetos.items() if k.startswith(Prefix)],
                "IsTruncated": False}

    def delete_object(self, Bucket, Key):
        self.objetos.pop(Key, None)

    def head_bucket(self, Bucket):
        return {}


@pytest.fixture
def bucket(monkeypatch):
    s3 = _S3Falso()
    monkeypatch.setattr(chat_media, "_s3", lambda: s3)
    monkeypatch.setattr(get_settings(), "s3_bucket", "remedia-adjuntos")
    return s3


async def test_bucket_guarda_y_carga_con_nombre_con_tildes(bucket):
    ref = await chat_media.guardar("wamid.X1", b"PK..", ".docx", "Orden médica Nº 3.docx")
    assert ref == "/media/chat/wamidX1.docx"
    assert "chat/wamidX1" in bucket.objetos
    assert bucket.objetos["chat/wamidX1"]["ContentType"].endswith("wordprocessingml.document")
    data, ext, nombre = await chat_media.cargar("wamidX1")
    assert data == b"PK.." and ext == ".docx" and nombre == "Orden médica Nº 3.docx"
    assert not chat_media._dir().exists()          # nada al disco


async def test_bucket_sigue_leyendo_lo_que_quedo_en_disco(monkeypatch, bucket):
    monkeypatch.setattr(chat_media, "_s3", lambda: None)
    await chat_media.guardar("viejo1", b"%PDF", ".pdf", "receta.pdf")   # antes del bucket
    monkeypatch.setattr(chat_media, "_s3", lambda: bucket)
    assert (await chat_media.cargar("viejo1"))[0] == b"%PDF"
    assert await chat_media.cargar("no-existe") is None


async def test_bucket_limpia_lo_de_mas_de_6_meses(bucket):
    from datetime import datetime, timedelta, timezone
    await chat_media.guardar("viejo", b"a", ".jpg")
    await chat_media.guardar("nuevo", b"b", ".jpg")
    bucket.objetos["chat/viejo"]["LastModified"] = datetime.now(timezone.utc) - timedelta(days=200)
    assert chat_media.limpiar_viejos(180) == 1
    assert list(bucket.objetos) == ["chat/nuevo"]


def test_estado_dice_bucket_o_disco(bucket, monkeypatch):
    e = chat_media.estado()
    assert e["almacenamiento"] == "bucket" and e["bucket_ok"] is True
    monkeypatch.setattr(chat_media, "_s3", lambda: None)
    assert chat_media.estado()["almacenamiento"] == "disco"


async def test_media_chat_sirve_desde_el_bucket(clave, cliente_media, bucket):
    await chat_media.guardar("rec9", b"%PDF-1.4", ".pdf", "receta.pdf")
    r = cliente_media.get(chat_media.firmar("/media/chat/rec9.pdf"))
    assert r.status_code == 200 and r.content == b"%PDF-1.4"
    assert r.headers["content-type"] == "application/pdf"
