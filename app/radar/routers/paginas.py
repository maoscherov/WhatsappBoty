"""
Pantallas de Radar (decisión 12 del plan del tramo 2).

HTML estático que no interpola nada; todo dato llega por JSON y el JS lo
escribe con textContent. Por eso la CSP no necesita 'unsafe-inline' ni hashes:
solo el JS y el CSS propios, imágenes propias (el QR) y fetch al mismo origen.

Sin sesión válida una pantalla manda al login (303) en lugar de contestar el
401 JSON de la API; con la sesión de otro rol sigue el 403. GET /radar/inicio
es el aterrizaje por rol: a él redirigen el canje del link, GET / y GET /radar.
"""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.radar import auditoria
from app.radar.auth import Sesion, ip_de, requiere_rol, sesion_actual
from app.radar.contexto import contexto
from app.radar.lineas import listar_lineas

router = APIRouter(tags=["radar-paginas"])

ESTATICOS = Path(__file__).resolve().parent.parent / "static"
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
       "form-action 'none'; base-uri 'none'; frame-ancestors 'none'")
CABECERAS_HTML = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                  "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}
_TIPOS = {"radar.js": "text/javascript; charset=utf-8", "radar.css": "text/css; charset=utf-8"}


def _html(nombre: str) -> HTMLResponse:
    return HTMLResponse((ESTATICOS / nombre).read_text(encoding="utf-8"), headers=CABECERAS_HTML)


def _redirigir(destino: str) -> RedirectResponse:
    """Siempre un destino armado por el servidor: nada del pedido llega acá (sería una redirección abierta)."""
    return RedirectResponse(destino, status_code=303)


async def _sesion_o_none(request: Request) -> Optional[Sesion]:
    """sesion_actual para pantallas: sin sesión válida devuelve None y la ruta redirige al login, porque un 401
    JSON no le sirve a quien está en el navegador. Cualquier otro error (503 arrancando) sigue su camino."""
    try:
        return await sesion_actual(request)
    except HTTPException as e:
        if e.status_code == 401:
            return None
        raise


def _pantalla_para(*roles: str):
    """requiere_rol para pantallas: sin sesión devuelve None (la ruta redirige al login).
    Con la sesión de otro rol sigue el 403 de siempre."""
    exigir_rol = requiere_rol(*roles)

    async def _dep(sesion: Optional[Sesion] = Depends(_sesion_o_none)) -> Optional[Sesion]:
        return None if sesion is None else await exigir_rol(sesion)
    return _dep


@router.get("/radar/login", response_class=HTMLResponse)
async def pagina_login():
    return _html("login.html")


@router.get("/radar/inicio")
async def inicio(request: Request, sesion: Optional[Sesion] = Depends(_sesion_o_none)):
    """Aterrizaje por rol (decisión 2). No audita: la consola ya registra su apertura cuando se abre."""
    if sesion is None:
        return _redirigir("/radar/login")
    if sesion.rol == "admin":
        return _redirigir("/radar/consola")
    if sesion.rol == "dueno":
        async with contexto(request).db.tenant_tx(sesion.tenant_id) as con:
            lineas = await listar_lineas(con, sesion.lineas_permitidas)       # sin las de baja, por created_at
        return _redirigir(f"/radar/conectar?linea={lineas[0]['id']}" if lineas else "/radar/conectar")
    # gestor, lector y soporte: su pantalla llega con el tablero (tramo 4)
    return _redirigir("/radar/api/yo")


@router.get("/")
@router.get("/radar")
async def raiz():
    return _redirigir("/radar/inicio")


@router.get("/radar/consola", response_class=HTMLResponse)
async def consola(request: Request, admin: Optional[Sesion] = Depends(_pantalla_para("admin"))):
    if admin is None:
        return _redirigir("/radar/login")
    ctx = contexto(request)
    async with ctx.db.tenant_tx(admin.tenant_id) as con:
        await auditoria.registrar(con, tenant_id=admin.tenant_id, actor_user_id=admin.user_id, actor_rol=admin.rol,
                                  accion="consola_abierta", tipo_objeto="user", objeto_id=admin.user_id,
                                  ip=ip_de(request))
    return _html("consola.html")


@router.get("/radar/conectar", response_class=HTMLResponse)
async def conectar(dueno: Optional[Sesion] = Depends(_pantalla_para("dueno"))):
    if dueno is None:
        return _redirigir("/radar/login")
    return _html("conectar.html")


@router.get("/radar/estaticos/{nombre}")
async def estatico(nombre: str):
    tipo = _TIPOS.get(nombre)          # lista cerrada: ningún otro archivo se sirve
    if tipo is None:
        raise HTTPException(status_code=404)
    return Response((ESTATICOS / nombre).read_bytes(), media_type=tipo,
                    headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"})
