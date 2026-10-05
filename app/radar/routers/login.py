"""
POST /radar/login          pide un link mágico (202 siempre: no revela si el email existe)
GET  /radar/login/canjear  página mínima: formulario POST + script que copia #k al campo, SIN auto-envío
                           (un escáner de links, ejecute JS o no, no consume el token), con CSP
POST /radar/login/canjear  canjea el token (UPDATE ... WHERE used_at IS NULL: un solo uso), emite la sesión
                           y redirige a /radar/inicio
POST /radar/logout         revoca la sesión
(La pantalla de login, GET /radar/login, y el aterrizaje, GET /radar/inicio, están en paginas.py.)
"""

import base64
import hashlib
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.radar import auditoria, eventos_producto
from app.radar.auth import (Sesion, borrar_cookie_sesion, emitir_sesion, hash_token, ip_de,
                            revocar_sesion, sesion_actual, set_cookie_sesion, token_valido)
from app.radar.contexto import contexto
from app.radar.contexto import RadarContexto
from app.radar.links import EmailInvalido, enviar_link_seguro, normalizar_email

router = APIRouter(prefix="/radar", tags=["radar-login"])
logger = logging.getLogger("app.radar.login")


class PedidoLogin(BaseModel):
    email: str = Field(max_length=254)


@router.post("/login", status_code=202)
async def pedir_link(pedido: PedidoLogin, request: Request, tareas: BackgroundTasks):
    """Responde 202 enseguida, sin buscar el email: la búsqueda y los mails van
    en segundo plano, así ni el tiempo ni un fallo del mailer revelan si existe."""
    try:
        email = normalizar_email(pedido.email)
    except EmailInvalido:
        return {"ok": True}
    tareas.add_task(_mandar_links, contexto(request), email, ip_de(request))
    return {"ok": True}


async def _mandar_links(ctx: RadarContexto, email: str, ip) -> None:
    """Un link por tenant donde exista el email; el fallo de uno no corta a los demás."""
    try:
        async with ctx.db.sin_tenant() as con:
            filas = await con.fetch("SELECT user_id, tenant_id FROM radar_auth_usuarios_por_email($1)", email)
    except Exception as e:
        logger.warning("pedido de login no procesado: %s", type(e).__name__)
        return
    for f in filas:
        await enviar_link_seguro(ctx, tenant_id=f["tenant_id"], user_id=f["user_id"], email=email,
                                 proposito="login", ip=ip)


# Único script de la página: copia el token del fragmento (#k=...) al campo
# oculto. No envía el formulario: eso lo hace la persona con el botón.
_SCRIPT_CANJE = (
    "var m = /^#k=([A-Za-z0-9_-]{32,64})$/.exec(location.hash);"
    "if (m) { document.forms[0].k.value = m[1]; }"
)
# CSP (§7, "aplicar CSP"): nada se carga ni se ejecuta salvo ese script, por hash.
_CSP = (
    "default-src 'none'; script-src 'sha256-"
    + base64.b64encode(hashlib.sha256(_SCRIPT_CANJE.encode()).digest()).decode()
    + "'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)

_PAGINA_CANJE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>Radar</title></head>
<body>
<form method="post" action="/radar/login/canjear">
  <input type="hidden" name="t" value="{t}">
  <input type="hidden" name="k" value="">
  <button type="submit">Entrar a Radar</button>
</form>
<noscript>Este link necesita JavaScript para completar el ingreso.</noscript>
<script>{script}</script>
</body></html>"""


@router.get("/login/canjear", response_class=HTMLResponse)
async def pagina_canje(t: uuid.UUID):
    """Solo recibe el tenant: el token viaja en el fragmento y nunca llega acá."""
    return HTMLResponse(_PAGINA_CANJE.format(t=t, script=_SCRIPT_CANJE),
                        headers={"Content-Security-Policy": _CSP})


@router.post("/login/canjear")
async def canjear(request: Request, t: uuid.UUID = Form(...), k: str = Form(...)):
    ctx = contexto(request)
    if not token_valido(k):
        raise HTTPException(status_code=400, detail="link inválido")
    ip = ip_de(request)
    async with ctx.db.tenant_tx(t) as con:
        fila = await con.fetchrow(
            "UPDATE login_tokens SET used_at = now() "
            "WHERE token_hash = $1 AND used_at IS NULL AND expires_at > now() "
            "RETURNING id, user_id, proposito",
            hash_token(k))
        if fila is None:
            raise HTTPException(status_code=400, detail="link inválido o vencido")
        rol = await con.fetchval("SELECT rol FROM memberships WHERE user_id = $1", fila["user_id"])
        if rol is None:
            raise HTTPException(status_code=403, detail="sin membresía")
        token = await emitir_sesion(con, tenant_id=t, user_id=fila["user_id"], rol=rol, ip=ip)
        await auditoria.registrar(con, tenant_id=t, actor_user_id=fila["user_id"], actor_rol=rol,
                                  accion="login_canjeado", tipo_objeto="login_token", objeto_id=fila["id"],
                                  ip=ip, detalle={"proposito": fila["proposito"]})
        await eventos_producto.registrar_evento(con, tenant_id=t, evento="login_canjeado", user_id=fila["user_id"])
    resp = RedirectResponse("/radar/inicio", status_code=303)       # el aterrizaje por rol decide a qué pantalla
    set_cookie_sesion(resp, ctx, t, token)
    return resp


@router.post("/logout")
async def salir(request: Request, sesion: Sesion = Depends(sesion_actual)):
    ctx = contexto(request)
    async with ctx.db.tenant_tx(sesion.tenant_id) as con:
        await revocar_sesion(con, sesion.id)
        await auditoria.registrar(con, tenant_id=sesion.tenant_id, actor_user_id=sesion.user_id,
                                  actor_rol=sesion.rol, accion="sesion_cerrada", tipo_objeto="session",
                                  objeto_id=sesion.id, ip=ip_de(request))
    resp = JSONResponse({"ok": True})
    borrar_cookie_sesion(resp, ctx)
    return resp
