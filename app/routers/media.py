"""
Servidor de imágenes integrado en el bot principal.
Montado en /media/ — usar un volume de Railway en IMAGES_DIR.

GET  /media/{filename}     → sirve la imagen (público)
POST /media/upload         → sube imagen, opcionalmente named por sku_id
GET  /media/list           → lista archivos
DELETE /media/{filename}   → borra imagen
"""

import re
import logging
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, Header, HTTPException, Query
from fastapi.responses import FileResponse, Response

from app.config import get_settings
from app.services.blob_store import get_blob_store

_CT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
       ".webp": "image/webp", ".gif": "image/gif",
       # Audios (para escuchar el original de una transcripción) y PDF (recetas).
       ".ogg": "audio/ogg", ".opus": "audio/ogg", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
       ".pdf": "application/pdf"}

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/media")

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_MB = 5


def _images_dir() -> Path:
    p = Path(get_settings().images_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _check_auth(key: str):
    secret = get_settings().image_server_api_key
    if secret and key != secret:
        raise HTTPException(status_code=401, detail="API key inválida")


def _safe(name: str) -> str:
    name = Path(name).name
    return re.sub(r"[^\w.\-]", "_", name)


@router.get("/health")
def media_health():
    from app.services import chat_media
    d = _images_dir()
    return {"status": "ok", "images_count": len(list(d.glob("*"))), "dir": str(d),
            "chat": chat_media.estado()}


@router.get("/list")
def media_list(x_api_key: str = Header(default="")):
    _check_auth(x_api_key)
    files = sorted(_images_dir().glob("*"))
    settings = get_settings()
    base = settings.images_base_url.rstrip("/") or ""
    return [
        {
            "filename": f.name,
            "size_kb": round(f.stat().st_size / 1024, 1),
            "url": f"{base}/media/{f.name}",
        }
        for f in files if f.is_file()
    ]


@router.post("/upload")
async def media_upload(
    file: UploadFile = File(...),
    x_api_key: str = Header(default=""),
    sku_id: str = Query(default=""),
):
    """
    Sube una imagen. Si se pasa sku_id, el archivo se llama {sku_id}{ext}.
    Ejemplo: POST /media/upload?sku_id=IBU-001
    """
    _check_auth(x_api_key)

    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"Extensión no permitida: {ext}")

    filename = _safe(f"{sku_id}{ext}" if sku_id else (file.filename or f"img{ext}"))
    content = await file.read()

    if len(content) / (1024 * 1024) > MAX_MB:
        raise HTTPException(status_code=413, detail=f"Máximo {MAX_MB}MB")

    dest = _images_dir() / filename
    dest.write_bytes(content)

    settings = get_settings()
    base = settings.images_base_url.rstrip("/") or ""
    logger.info(f"Imagen subida: {filename} ({len(content)//1024}KB)")
    return {"status": "ok", "filename": filename, "url": f"{base}/media/{filename}"}


@router.delete("/{filename}")
def media_delete(filename: str, x_api_key: str = Header(default="")):
    _check_auth(x_api_key)
    path = _images_dir() / _safe(filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="No encontrado")
    path.unlink()
    return {"status": "ok", "deleted": filename}


@router.get("/chat/{archivo}")
async def media_chat(archivo: str, key: str | None = Query(None), exp: str | None = Query(None),
                     sig: str | None = Query(None),
                     x_bo_key: str | None = Header(None, alias="x-bo-key")):
    """
    Sirve un adjunto de una conversación (foto, PDF, audio, documento). Pide
    la clave del backoffice (?key= o x-bo-key) o una firma vigente (?exp=&sig=,
    la que devuelven el historial y la vista en vivo).
    """
    from urllib.parse import quote
    from app.services import chat_media

    id_, _ = chat_media.separar(archivo)
    if not chat_media.acceso_valido(id_, key or x_bo_key, exp, sig):
        raise HTTPException(status_code=403, detail="Acceso denegado")
    res = await chat_media.cargar(id_)
    if not res:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    data, ext, nombre = res
    ext = ext.lower()
    tipo = chat_media.CONTENT_TYPE.get(ext) or _CT.get(ext) or "application/octet-stream"
    nombre = nombre or f"{id_}{ext}"
    # Se ven en el navegador: fotos, PDF, audio y video. El resto se descarga.
    modo = "inline" if chat_media.tipo_de(ext) != "archivo" else "attachment"
    return Response(content=data, media_type=tipo, headers={
        "Content-Disposition": f"{modo}; filename*=UTF-8''{quote(nombre)}",
        "Cache-Control": "private, max-age=3600"})


@router.get("/{filename}")
def media_serve(filename: str):
    """Sirve la imagen. Público — WhatsApp necesita acceso sin auth."""
    path = _images_dir() / _safe(filename)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Imagen no encontrada")
    return FileResponse(path)
