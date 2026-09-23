"""
Tokens, sesiones y cookie de Radar (§3 P0, §7).

- Tokens (link mágico, invitación, sesión): secrets.token_urlsafe(32), guardados
  hasheados con sha256, como en app/services/branch_auth.py. El claro viaja una
  sola vez: en el link mágico (se canjea por POST y se invalida) o en la cookie.
- Cookie `radar_sesion` = "<tenant_id>.<token>.<firma>", firma HMAC-SHA256 con
  RADAR_COOKIE_SECRET sobre "<tenant_id>.<token>". HttpOnly, Secure,
  SameSite=Lax, Path=/radar. La sesión queda atada al tenant dentro del token
  (§7): el lookup corre dentro de tenant_tx(tenant_id) y es por hash.
- Comparaciones con hmac.compare_digest; el lookup por hash usa un índice único.
"""

import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

import asyncpg
from fastapi import Depends, HTTPException, Request, Response

from app.radar.contexto import RadarContexto, contexto

COOKIE = "radar_sesion"
LINK_MAGICO = timedelta(minutes=15)
INVITACION = timedelta(days=7)
SESION = timedelta(days=30)
MAX_PEDIDOS_LOGIN = 3                 # por usuario, cada VENTANA_PEDIDOS
VENTANA_PEDIDOS = timedelta(minutes=15)
ROLES_MEMBRESIA = ("admin", "dueno", "gestor", "lector")
ROLES_SESION = ROLES_MEMBRESIA + ("soporte",)
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
_FIRMA = re.compile(r"^[0-9a-f]{64}$")


def generar_token() -> tuple[str, str]:
    """(token en claro, sha256 hex). El claro se entrega una sola vez."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def token_valido(token: str) -> bool:
    return bool(_TOKEN.match(token or ""))


def firmar(secret: str, contenido: str) -> str:
    return hmac.new(secret.encode(), contenido.encode(), hashlib.sha256).hexdigest()


def armar_cookie(secret: str, tenant_id: uuid.UUID, token: str) -> str:
    contenido = f"{tenant_id}.{token}"
    return f"{contenido}.{firmar(secret, contenido)}"


def abrir_cookie(secret: str, valor: str) -> Optional[tuple[uuid.UUID, str]]:
    partes = (valor or "").split(".")
    if len(partes) != 3:
        return None
    tid, token, firma = partes
    # Forma antes de comparar: compare_digest sobre str exige ASCII y Starlette
    # decodifica la cookie como latin-1; un byte raro daría TypeError (500).
    if not _FIRMA.match(firma) or not token_valido(token):
        return None
    if not hmac.compare_digest(firma.encode(), firmar(secret, f"{tid}.{token}").encode()):
        return None
    try:
        return uuid.UUID(tid), token
    except ValueError:
        return None


@dataclass
class Sesion:
    id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    rol: str
    email: Optional[str]                       # None en sesiones de soporte (el usuario vive en el tenant KIS)
    lineas_permitidas: Optional[list[uuid.UUID]]   # None = todas
    es_kis: bool

    def puede_ver_linea(self, line_id: uuid.UUID) -> bool:
        return self.lineas_permitidas is None or line_id in self.lineas_permitidas


async def emitir_sesion(con: asyncpg.Connection, *, tenant_id: uuid.UUID, user_id: uuid.UUID, rol: str,
                        ip: Optional[str], duracion: timedelta = SESION) -> str:
    if rol not in ROLES_SESION:
        raise ValueError(f"rol de sesión desconocido: {rol}")
    token, h = generar_token()
    await con.execute(
        "INSERT INTO sessions (tenant_id, user_id, rol, token_hash, expires_at, ip) "
        "VALUES ($1, $2, $3, $4, now() + $5::interval, $6)",
        tenant_id, user_id, rol, h, duracion, ip,
    )
    return token


async def resolver_sesion(con: asyncpg.Connection, tenant_id: uuid.UUID, token: str) -> Optional[Sesion]:
    fila = await con.fetchrow(
        """
        SELECT s.id, s.user_id, s.rol, u.email, m.lineas_permitidas, t.es_kis
        FROM sessions s
        JOIN tenants t ON t.id = s.tenant_id
        LEFT JOIN users u ON u.id = s.user_id
        LEFT JOIN memberships m ON m.user_id = s.user_id AND m.tenant_id = s.tenant_id
        WHERE s.token_hash = $1 AND s.revoked_at IS NULL AND s.expires_at > now()
        """,
        hash_token(token),
    )
    if fila is None:
        return None
    await con.execute(
        "UPDATE sessions SET last_seen_at = now() WHERE id = $1 "
        "AND (last_seen_at IS NULL OR last_seen_at < now() - interval '5 minutes')",
        fila["id"],
    )
    permitidas = fila["lineas_permitidas"]
    return Sesion(
        id=fila["id"], tenant_id=tenant_id, user_id=fila["user_id"], rol=fila["rol"], email=fila["email"],
        lineas_permitidas=list(permitidas) if permitidas is not None else None, es_kis=fila["es_kis"],
    )


async def revocar_sesion(con: asyncpg.Connection, session_id: uuid.UUID) -> None:
    await con.execute("UPDATE sessions SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL", session_id)


async def revocar_sesiones_de(con: asyncpg.Connection, user_id: uuid.UUID) -> int:
    estado = await con.execute(
        "UPDATE sessions SET revoked_at = now() WHERE user_id = $1 AND revoked_at IS NULL", user_id)
    return int(estado.split()[-1])


def ip_de(request: Request) -> Optional[str]:
    """Radar corre detrás de un único proxy confiable (Railway), que agrega el
    salto real al FINAL de X-Forwarded-For; el primero lo puede inventar el
    cliente. La IP es evidencia (consents, access_audit_log): se toma el último."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[-1].strip()[:64]
    return request.client.host if request.client else None


async def sesion_actual(request: Request) -> Sesion:
    ctx = contexto(request)
    abierta = abrir_cookie(ctx.settings.cookie_secret, request.cookies.get(COOKIE, ""))
    if abierta is None:
        raise HTTPException(status_code=401, detail="sin sesión")
    tenant_id, token = abierta
    async with ctx.db.tenant_tx(tenant_id) as con:
        sesion = await resolver_sesion(con, tenant_id, token)
    if sesion is None:
        raise HTTPException(status_code=401, detail="sesión inválida o vencida")
    return sesion


def requiere_rol(*roles: str):
    async def _dep(sesion: Sesion = Depends(sesion_actual)) -> Sesion:
        if sesion.rol not in roles:
            raise HTTPException(status_code=403, detail="rol insuficiente")
        return sesion
    return _dep


def set_cookie_sesion(response: Response, ctx: RadarContexto, tenant_id: uuid.UUID, token: str,
                      max_age: int = int(SESION.total_seconds())) -> None:
    response.set_cookie(
        COOKIE, armar_cookie(ctx.settings.cookie_secret, tenant_id, token),
        httponly=True, secure=ctx.settings.cookie_secure, samesite="lax", path="/radar", max_age=max_age,
    )


def borrar_cookie_sesion(response: Response, ctx: RadarContexto) -> None:
    response.delete_cookie(COOKIE, path="/radar", httponly=True, secure=ctx.settings.cookie_secure,
                           samesite="lax")
