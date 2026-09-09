"""
Auth por sucursal para los endpoints del agente (/v1/sync/*, /v1/agent/ws).

Cada sucursal tiene un token Bearer propio (aleatorio, guardado hasheado con
sha256, mostrado UNA sola vez al crear o rotar). Fail-closed: sin token, token
desconocido o sucursal inactiva → 401/403; sin Postgres → 503 (el agente
reintenta con backoff). No copiar el fail-open de BO_KEY.
"""

import hashlib
import logging
import secrets
from dataclasses import dataclass
from typing import Optional

from fastapi import Header, HTTPException

from app.config import get_settings
from app.services.db import get_db

logger = logging.getLogger(__name__)


@dataclass
class Branch:
    branch_id: str
    nombre: str
    activa: bool


def generar_token() -> tuple[str, str]:
    """(token en claro, sha256 hex). El claro se muestra una sola vez."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


async def resolver_branch(token: str) -> Branch:
    """
    Resuelve la sucursal de un token Bearer. Lanza HTTPException:
    401 token inválido, 403 sucursal inactiva, 503 sin Postgres.
    El lookup es por hash (índice único): no se compara el token en claro,
    así que no hay comparación de tiempo variable sobre el secreto.
    """
    db = get_db(get_settings().database_url)
    if not db.available():
        raise HTTPException(status_code=503, detail="catálogo ERP no disponible")
    row = await db.fetchrow(
        "SELECT branch_id, nombre, activa FROM branches WHERE token_hash = $1",
        hash_token(token),
    )
    if not row:
        raise HTTPException(status_code=401, detail="token inválido")
    if not row["activa"]:
        raise HTTPException(status_code=403, detail="sucursal inactiva")
    return Branch(branch_id=row["branch_id"], nombre=row["nombre"], activa=True)


async def require_branch(authorization: Optional[str] = Header(None)) -> Branch:
    """Dependencia FastAPI: Authorization: Bearer <token-sucursal>."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="falta Authorization Bearer")
    return await resolver_branch(authorization[7:].strip())
