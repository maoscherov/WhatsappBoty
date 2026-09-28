"""
Archivos de las conversaciones (lo que manda el cliente y lo que adjunta el
operador): fotos, PDF, audios, Word, Excel, videos.

Viven en un bucket S3 (Railway Buckets, variables S3_*) y se borran solos a
los 6 meses. Sin bucket configurado caen al disco (CHAT_MEDIA_DIR). Antes
vivían en Redis con 7 días de vida: lo que quedó ahí se sigue sirviendo hasta
que vence (ver `cargar`). El bucket es privado: los archivos se sirven por
/media/chat con clave o firma, nunca con una URL directa al bucket.

La referencia que va al historial es "/media/chat/{id}{ext}" (la extensión
dice el tipo sin abrir el archivo). Para abrirla hace falta la clave del
backoffice o una firma temporal: `firmar()` agrega ?exp=&sig= a la URL, así
el backoffice y WhatsApp (que descarga lo que adjunta el operador) la abren
sin conocer la clave.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

from app.config import get_settings

logger = logging.getLogger(__name__)

MAX_BYTES = 15 * 1024 * 1024
PREFIJO = "/media/chat/"

_EXT_POR_MIME = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif", "application/pdf": ".pdf",
    "audio/ogg": ".ogg", "audio/opus": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
    "audio/aac": ".aac", "audio/amr": ".amr",
    "video/mp4": ".mp4", "video/3gpp": ".3gp", "video/quicktime": ".mov",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/plain": ".txt", "text/csv": ".csv",
}
CONTENT_TYPE = {v: k for k, v in _EXT_POR_MIME.items()}
CONTENT_TYPE.update({".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".ogg": "audio/ogg",
                     ".opus": "audio/ogg"})

_TIPO_POR_EXT = {
    ".jpg": "imagen", ".jpeg": "imagen", ".png": "imagen", ".webp": "imagen", ".gif": "imagen",
    ".pdf": "pdf",
    ".ogg": "audio", ".opus": "audio", ".mp3": "audio", ".m4a": "audio", ".aac": "audio",
    ".amr": "audio",
    ".mp4": "video", ".3gp": "video", ".mov": "video",
}

# Lo que el operador puede adjuntar (WhatsApp manda imagen o documento).
EXT_ADJUNTABLES = {".jpg", ".jpeg", ".png", ".webp", ".pdf", ".doc", ".docx", ".xls",
                   ".xlsx", ".ppt", ".pptx", ".txt", ".csv"}


def _dir() -> Path:
    return Path(get_settings().chat_media_dir)


def _limpio(s: str) -> str:
    return re.sub(r"[^\w\-]", "", s or "")


def ext_para(mime: str = "", nombre: str = "") -> str:
    """Extensión a partir del nombre del archivo o, si no trae, del mime."""
    ext = Path(nombre or "").suffix.lower()
    if ext and re.fullmatch(r"\.[a-z0-9]{1,5}", ext):
        return ".jpg" if ext == ".jpeg" else ext
    return _EXT_POR_MIME.get((mime or "").split(";")[0].strip().lower(), ".bin")


def tipo_de(ref_o_ext: str) -> str:
    """imagen | pdf | audio | video | archivo. Las referencias viejas (sin
    extensión) eran fotos, salvo los audios, que empiezan con "aud"."""
    ref_o_ext = (ref_o_ext or "").split("?")[0]
    # ".pdf" suelto: para Path es un nombre oculto sin extensión.
    ext = (ref_o_ext if ref_o_ext.startswith(".") else Path(ref_o_ext).suffix).lower()
    if ext:
        return _TIPO_POR_EXT.get(ext, "archivo")
    return "audio" if Path(ref_o_ext or "").name.startswith("aud") else "imagen"


def separar(ref: str) -> tuple[str, str]:
    """'/media/chat/abc.pdf?sig=…' → ('abc', '.pdf')."""
    nombre = Path((ref or "").split("?")[0]).name
    ext = Path(nombre).suffix.lower()
    return _limpio(nombre[: -len(ext)] if ext else nombre), ext


# ── Bucket S3 ───────────────────────────────────────────────────────────────────
_cliente_s3 = None


def _s3():
    """Cliente S3 si el bucket está configurado (S3_*), si no None."""
    global _cliente_s3
    st = get_settings()
    if not (st.s3_bucket and st.s3_endpoint and st.s3_access_key_id and st.s3_secret_access_key):
        return None
    if _cliente_s3 is None:
        import boto3
        from botocore.config import Config
        _cliente_s3 = boto3.client(
            "s3", endpoint_url=st.s3_endpoint, region_name=st.s3_region or "auto",
            aws_access_key_id=st.s3_access_key_id,
            aws_secret_access_key=st.s3_secret_access_key,
            config=Config(signature_version="s3v4", retries={"max_attempts": 3}))
    return _cliente_s3


def _clave(id_: str) -> str:
    return f"chat/{id_}"


def _s3_guardar(id_: str, data: bytes, ext: str, nombre: str) -> None:
    from urllib.parse import quote
    _s3().put_object(
        Bucket=get_settings().s3_bucket, Key=_clave(id_), Body=data,
        ContentType=CONTENT_TYPE.get(ext, "application/octet-stream"),
        # Los metadatos de S3 solo aceptan ASCII: el nombre va codificado.
        Metadata={"ext": ext, "nombre": quote(nombre or "")})


def _s3_cargar(id_: str) -> Optional[tuple[bytes, str, str]]:
    from urllib.parse import unquote
    try:
        obj = _s3().get_object(Bucket=get_settings().s3_bucket, Key=_clave(id_))
    except Exception as e:
        if "NoSuchKey" in type(e).__name__ or "NoSuchKey" in str(e) or "404" in str(e):
            return None
        raise
    meta = obj.get("Metadata") or {}
    return obj["Body"].read(), meta.get("ext", ""), unquote(meta.get("nombre", ""))


def _s3_limpiar(limite: float) -> int:
    cli, bucket, borrados = _s3(), get_settings().s3_bucket, 0
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": "chat/"}
        if token:
            kw["ContinuationToken"] = token
        res = cli.list_objects_v2(**kw)
        viejos = [o["Key"] for o in res.get("Contents", [])
                  if o["LastModified"].timestamp() < limite]
        for k in viejos:
            cli.delete_object(Bucket=bucket, Key=k)
            borrados += 1
        if not res.get("IsTruncated"):
            return borrados
        token = res.get("NextContinuationToken")


# ── Guardar / cargar / limpiar ──────────────────────────────────────────────────
async def guardar(id_: str, data: bytes, ext: str, nombre: str = "") -> Optional[str]:
    """Guarda el archivo (bucket o, sin bucket, disco) y devuelve la
    referencia para el historial, o None si no se pudo."""
    import asyncio
    id_ = _limpio(id_)[-40:]
    if not id_ or not data:
        return None
    if len(data) > MAX_BYTES:
        logger.warning(f"Adjunto {id_} de {len(data) // 1024} KB supera el máximo: no se guarda")
        return None
    try:
        if _s3():
            await asyncio.to_thread(_s3_guardar, id_, data, ext, nombre)
        else:
            d = _dir()
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{id_}{ext}").write_bytes(data)
            (d / f"{id_}.json").write_text(
                json.dumps({"ext": ext, "nombre": nombre or "", "bytes": len(data)}),
                encoding="utf-8")
        return f"{PREFIJO}{id_}{ext}"
    except Exception as e:
        logger.warning(f"No se pudo guardar el adjunto {id_}: {e}")
        return None


async def cargar(id_: str) -> Optional[tuple[bytes, str, str]]:
    """(bytes, ext, nombre original). Busca en el bucket, después en el disco
    (lo guardado antes de configurar el bucket) y por último en Redis
    (archivos viejos, que vencen a los 7 días)."""
    import asyncio
    id_ = _limpio(id_)
    if not id_:
        return None
    if _s3():
        try:
            res = await asyncio.to_thread(_s3_cargar, id_)
            if res:
                return res
        except Exception as e:
            logger.warning(f"No se pudo leer el adjunto {id_} del bucket: {e}")
    d = _dir()
    meta_path = d / f"{id_}.json"
    try:
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            ext = meta.get("ext", "")
            return (d / f"{id_}{ext}").read_bytes(), ext, meta.get("nombre", "")
    except Exception as e:
        logger.warning(f"No se pudo leer el adjunto {id_}: {e}")
    try:
        from app.services.blob_store import get_blob_store
        res = await get_blob_store(get_settings().redis_url).load(f"chat:{id_}")
        if res:
            return res[0], res[1], ""
    except Exception:
        pass
    return None


def limpiar_viejos(dias: Optional[int] = None) -> int:
    """Borra los archivos con más de `dias` (por defecto, la retención
    configurada: 180), en el bucket y en el disco. Devuelve cuántos borró."""
    dias = dias if dias is not None else get_settings().chat_media_retencion_dias
    limite = time.time() - dias * 86400
    borrados = 0
    if _s3():
        try:
            borrados += _s3_limpiar(limite)
        except Exception as e:
            logger.warning(f"Limpieza del bucket falló: {e}")
    d = _dir()
    if d.exists():
        for p in d.iterdir():
            try:
                if p.is_file() and p.stat().st_mtime < limite:
                    p.unlink()
                    if p.suffix != ".json":
                        borrados += 1
            except Exception:
                pass
    if borrados:
        logger.info(f"Adjuntos: {borrados} archivos con más de {dias} días borrados")
    return borrados


def estado() -> dict:
    """Para /media/health: dónde se guardan los adjuntos. Sin bucket, el
    disco de Railway se borra en cada deploy."""
    st = get_settings()
    if _s3():
        try:
            _s3().head_bucket(Bucket=st.s3_bucket)
            ok = True
        except Exception as e:
            logger.warning(f"Bucket {st.s3_bucket} no responde: {e}")
            ok = False
        return {"almacenamiento": "bucket", "bucket": st.s3_bucket, "bucket_ok": ok,
                "retencion_dias": st.chat_media_retencion_dias}
    d = _dir()
    archivos = [p for p in d.glob("*") if p.suffix != ".json"] if d.exists() else []
    raiz = d
    while raiz.parent != raiz and not os.path.ismount(raiz):
        raiz = raiz.parent
    return {"almacenamiento": "disco", "dir": str(d), "archivos": len(archivos),
            "volumen": raiz != Path(raiz.anchor) and os.path.ismount(raiz),
            "retencion_dias": st.chat_media_retencion_dias}


# ── Firma de URLs ───────────────────────────────────────────────────────────────
def _firma(id_: str, exp: int) -> str:
    clave = (get_settings().bo_key or "").encode()
    return hmac.new(clave, f"{id_}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def firmar(ref: Optional[str], horas: int = 24) -> Optional[str]:
    """'/media/chat/abc.pdf' → '/media/chat/abc.pdf?exp=…&sig=…'."""
    if not ref or PREFIJO not in ref:
        return ref
    base = ref.split("?")[0]
    id_, _ = separar(base)
    exp = int(time.time()) + horas * 3600
    return f"{base}?exp={exp}&sig={_firma(id_, exp)}"


def firmar_en_texto(texto: str, horas: int = 24) -> str:
    """Firma las referencias que aparecen dentro de un mensaje (la vista en
    vivo guarda las fotos como texto "📷 /media/chat/{id}")."""
    if not texto or PREFIJO not in texto:
        return texto
    return re.sub(r"/media/chat/[\w.\-]+(\?[\w=&]*)?",
                  lambda m: firmar(m.group(0).split("?")[0], horas), texto)


def acceso_valido(id_: str, key: Optional[str], exp: Optional[str], sig: Optional[str]) -> bool:
    clave = get_settings().bo_key
    if not clave:
        return True                     # sin clave configurada (desarrollo)
    if key and hmac.compare_digest(key, clave):
        return True
    try:
        exp_i = int(exp or 0)
    except ValueError:
        return False
    if not sig or exp_i < time.time():
        return False
    return hmac.compare_digest(sig, _firma(_limpio(id_), exp_i))
