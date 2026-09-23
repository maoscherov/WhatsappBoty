"""
Links mágicos (§3 P0): un solo uso, con vencimiento, canjeados por POST.
Un único módulo para login e invitación; cambian el vencimiento y el texto.
Límite: MAX_PEDIDOS_LOGIN por usuario cada VENTANA_PEDIDOS, contado sobre
login_tokens. El email nunca lleva datos de conversación.
"""

import logging
import re
import uuid
from typing import Literal, Optional

from app.radar.auth import INVITACION, LINK_MAGICO, MAX_PEDIDOS_LOGIN, VENTANA_PEDIDOS, generar_token
from app.radar.contexto import RadarContexto
from app.radar.mailer import Email, huella_token

logger = logging.getLogger("app.radar.links")

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

TEXTOS = {
    "login": (
        "Tu acceso a Radar de {tenant}",
        "Entrá a Radar de {tenant} con este link. Vence en 15 minutos y sirve una sola vez.\n\n"
        "{url}\n\nSi no lo pediste, ignorá este mail.\n",
    ),
    "invitacion": (
        "Invitación a Radar de {tenant}",
        "Te invitaron a Radar, el tablero de conversaciones de WhatsApp de {tenant}. "
        "Entrá con este link; vence en 7 días y sirve una sola vez.\n\n{url}\n",
    ),
}


class EmailInvalido(ValueError):
    pass


def normalizar_email(email: str) -> str:
    e = (email or "").strip().lower()
    if len(e) > 254 or not _EMAIL.match(e):
        raise EmailInvalido("email inválido")
    return e


def url_canje(base: str, tenant_id: uuid.UUID, token: str) -> str:
    """El token va en el FRAGMENTO: el navegador nunca lo manda al servidor, así
    no queda en el access log de uvicorn (path?query) ni en los logs HTTP de
    Railway. La página de canje lo copia al formulario POST."""
    return f"{base.rstrip('/')}/radar/login/canjear?t={tenant_id}#k={token}"


async def enviar_link(ctx: RadarContexto, *, tenant_id: uuid.UUID, user_id: uuid.UUID, email: str,
                      proposito: Literal["login", "invitacion"], ip: Optional[str]) -> bool:
    """Crea un login_token y manda el mail. False si el usuario superó el límite."""
    duracion = INVITACION if proposito == "invitacion" else LINK_MAGICO
    async with ctx.db.tenant_tx(tenant_id) as con:
        recientes = await con.fetchval(
            "SELECT count(*) FROM login_tokens WHERE user_id = $1 AND created_at > now() - $2::interval",
            user_id, VENTANA_PEDIDOS)
        if recientes >= MAX_PEDIDOS_LOGIN:
            logger.info("link no enviado: límite por usuario alcanzado (user %s)", user_id)
            return False
        token, h = generar_token()
        await con.execute(
            "INSERT INTO login_tokens (tenant_id, user_id, token_hash, proposito, expires_at, ip_solicitud) "
            "VALUES ($1, $2, $3, $4, now() + $5::interval, $6)",
            tenant_id, user_id, h, proposito, duracion, ip)
        nombre_tenant = await con.fetchval("SELECT nombre FROM tenants WHERE id = $1", tenant_id)
    asunto, cuerpo = TEXTOS[proposito]
    url = url_canje(ctx.settings.public_base_url, tenant_id, token)
    await ctx.mailer.enviar(Email(
        para=email, asunto=asunto.format(tenant=nombre_tenant),
        texto=cuerpo.format(tenant=nombre_tenant, url=url), huella=huella_token(token)))
    return True


async def enviar_link_seguro(ctx: RadarContexto, *, tenant_id: uuid.UUID, user_id: uuid.UUID, email: str,
                             proposito: Literal["login", "invitacion"], ip: Optional[str]) -> bool:
    """enviar_link que nunca lanza: si falla (base o mailer) loguea el error SIN
    email ni token y devuelve False. Para que un fallo no corte a otros tenants
    ni convierta un alta ya hecha en un 500."""
    try:
        return await enviar_link(ctx, tenant_id=tenant_id, user_id=user_id, email=email, proposito=proposito, ip=ip)
    except Exception as e:
        logger.warning("link no enviado (%s): tenant %s user %s: %s", proposito, tenant_id, user_id, type(e).__name__)
        return False
