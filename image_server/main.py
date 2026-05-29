"""
Servidor de imágenes para el catálogo de productos.
Desplegá como servicio separado en Railway con un volume en /data/images.

Endpoints:
  GET  /images/{filename}     → sirve la imagen (público)
  POST /upload                → sube una imagen (requiere X-Api-Key header)
  GET  /list                  → lista imágenes existentes (requiere X-Api-Key)
  DELETE /images/{filename}   → borra una imagen (requiere X-Api-Key)
  GET  /health                → healthcheck
"""

import os
import shutil
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

IMAGES_DIR = Path(os.getenv("IMAGES_DIR", "/data/images"))
API_KEY = os.getenv("IMAGE_SERVER_API_KEY", "")
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_FILE_SIZE_MB = 5

IMAGES_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Farma Image Server", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _check_auth(x_api_key: str = ""):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="API key inválida")


def _safe_filename(name: str) -> str:
    """Sanitiza el nombre de archivo."""
    import re
    name = Path(name).name  # strip path traversal
    name = re.sub(r"[^\w.\-]", "_", name)
    return name


@app.get("/health")
def health():
    files = list(IMAGES_DIR.glob("*"))
    return {"status": "ok", "images_count": len(files), "images_dir": str(IMAGES_DIR)}


@app.get("/images/{filename}")
def serve_image(filename: str):
    """Sirve una imagen. Público — cualquiera con la URL puede acceder."""
    safe = _safe_filename(filename)
    path = IMAGES_DIR / safe
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Imagen no encontrada")
    return FileResponse(path)


@app.get("/list")
def list_images(x_api_key: str = Header(default="")):
    _check_auth(x_api_key)
    files = sorted(IMAGES_DIR.glob("*"))
    return [
        {
            "filename": f.name,
            "size_kb": round(f.stat().st_size / 1024, 1),
            "url": f"/images/{f.name}",
        }
        for f in files if f.is_file()
    ]


@app.post("/upload")
async def upload_image(
    file: UploadFile = File(...),
    x_api_key: str = Header(default=""),
    sku_id: str = "",
):
    """
    Sube una imagen. El nombre del archivo se puede forzar via sku_id
    (ej: sku_id=IBU-001 → guarda como IBU-001.jpg).
    """
    _check_auth(x_api_key)

    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Extensión no permitida: {ext}. Usá: {ALLOWED_EXTENSIONS}")

    # Si viene sku_id, el archivo se llama {sku_id}{ext}
    if sku_id:
        filename = _safe_filename(f"{sku_id}{ext}")
    else:
        filename = _safe_filename(file.filename or "image" + ext)

    dest = IMAGES_DIR / filename

    # Leer y verificar tamaño
    content = await file.read()
    size_mb = len(content) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(status_code=413, detail=f"Archivo demasiado grande ({size_mb:.1f}MB). Máximo {MAX_FILE_SIZE_MB}MB.")

    dest.write_bytes(content)

    return {
        "status": "ok",
        "filename": filename,
        "size_kb": round(len(content) / 1024, 1),
        "url": f"/images/{filename}",
    }


@app.delete("/images/{filename}")
def delete_image(filename: str, x_api_key: str = Header(default="")):
    _check_auth(x_api_key)
    safe = _safe_filename(filename)
    path = IMAGES_DIR / safe
    if not path.exists():
        raise HTTPException(status_code=404, detail="Imagen no encontrada")
    path.unlink()
    return {"status": "ok", "deleted": safe}
