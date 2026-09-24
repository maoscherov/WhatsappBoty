"""
Pantallas de Radar (decisión 12 del plan del tramo 2).

HTML estático que no interpola nada; todo dato llega por JSON y el JS lo
escribe con textContent. Por eso la CSP no necesita 'unsafe-inline' ni hashes:
solo el JS y el CSS propios, imágenes propias (el QR) y fetch al mismo origen.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app.radar import auditoria
from app.radar.auth import Sesion, ip_de, requiere_rol
from app.radar.contexto import contexto

router = APIRouter(tags=["radar-paginas"])

ESTATICOS = Path(__file__).resolve().parent.parent / "static"
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
       "form-action 'none'; base-uri 'none'; frame-ancestors 'none'")
CABECERAS_HTML = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                  "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}
_TIPOS = {"radar.js": "text/javascript; charset=utf-8", "radar.css": "text/css; charset=utf-8"}


def _html(nombre: str) -> HTMLResponse:
    return HTMLResponse((ESTATICOS / nombre).read_text(encoding="utf-8"), headers=CABECERAS_HTML)


@router.get("/radar/consola", response_class=HTMLResponse)
async def consola(request: Request, admin: Sesion = Depends(requiere_rol("admin"))):
    ctx = contexto(request)
    async with ctx.db.tenant_tx(admin.tenant_id) as con:
        await auditoria.registrar(con, tenant_id=admin.tenant_id, actor_user_id=admin.user_id, actor_rol=admin.rol,
                                  accion="consola_abierta", tipo_objeto="user", objeto_id=admin.user_id,
                                  ip=ip_de(request))
    return _html("consola.html")


@router.get("/radar/conectar", response_class=HTMLResponse)
async def conectar(dueno: Sesion = Depends(requiere_rol("dueno"))):
    return _html("conectar.html")


@router.get("/radar/estaticos/{nombre}")
async def estatico(nombre: str):
    tipo = _TIPOS.get(nombre)          # lista cerrada: ningún otro archivo se sirve
    if tipo is None:
        raise HTTPException(status_code=404)
    return Response((ESTATICOS / nombre).read_bytes(), media_type=tipo,
                    headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"})
