"""
access_audit_log (§4.4). Cada fila: actor, rol, acción, tipo de objeto, UUID
interno del objeto, fecha e IP. NUNCA teléfonos, JID, nombres ni texto:
`detalle` solo admite claves de la lista blanca, con valores numéricos,
booleanos, enums conocidos o un HMAC hex de 64 caracteres. La tabla tiene
además un CHECK que rechaza strings con 6 dígitos seguidos o '@'.

Retención: 24 meses (el job de purga es del tramo 6). radar_app solo inserta.
"""

import json
import re
import uuid
from decimal import Decimal
from typing import Any, Optional

import asyncpg

from app.radar.parametros import PARAMETROS

ACCIONES = frozenset({
    "tenant_creado", "tenants_listados", "linea_creada", "usuario_invitado", "invitacion_reenviada",
    "rol_cambiado", "parametro_cambiado", "parametro_propuesto", "consentimiento_registrado", "login_canjeado",
    "sesion_cerrada", "sesion_revocada", "soporte_otorgado", "soporte_revocado", "acceso_soporte",
})
TIPOS_OBJETO = frozenset({
    "tenant", "line", "user", "membership", "consent", "session", "support_grant", "login_token",
})
ROLES_ACTOR = frozenset({"admin", "dueno", "gestor", "lector", "soporte", "sistema"})

# clave -> tipo admitido
CLAVES_DETALLE = {
    "longitud_termino": "entero", "resultados": "entero", "cantidad": "entero", "horas": "entero",
    "contact_hmac": "hmac",
    "parametro": "parametro", "valor_anterior": "valor_parametro", "valor_nuevo": "valor_parametro",
    "rol_anterior": "rol", "rol_nuevo": "rol",
    "ambito": "ambito", "proposito": "proposito",
}
_HMAC = re.compile(r"^[0-9a-f]{64}$")
_ROLES = frozenset({"admin", "dueno", "gestor", "lector", "soporte"})
_ENUMS_PARAMETROS = frozenset(v for p in PARAMETROS.values() for v in p.orden)


class DetalleProhibido(ValueError):
    pass


def _es_entero(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _validar_valor(clave: str, tipo: str, valor: Any) -> Any:
    ok = {
        "entero": lambda v: _es_entero(v),
        "hmac": lambda v: isinstance(v, str) and bool(_HMAC.match(v)),
        "parametro": lambda v: v in PARAMETROS,
        "valor_parametro": lambda v: v is None or isinstance(v, (bool, int, float, Decimal))
                                     or v in _ENUMS_PARAMETROS,
        "rol": lambda v: v in _ROLES,
        "ambito": lambda v: v in ("tenant", "linea"),
        "proposito": lambda v: v in ("login", "invitacion"),
    }[tipo](valor)
    if not ok:
        raise DetalleProhibido(f"detalle.{clave}: valor no admitido")
    return float(valor) if isinstance(valor, Decimal) else valor


def validar_detalle(detalle: Optional[dict]) -> dict:
    limpio: dict[str, Any] = {}
    for clave, valor in (detalle or {}).items():
        tipo = CLAVES_DETALLE.get(clave)
        if tipo is None:
            raise DetalleProhibido(f"detalle.{clave}: clave fuera de la lista blanca")
        limpio[clave] = _validar_valor(clave, tipo, valor)
    return limpio


async def registrar(con: asyncpg.Connection, *, tenant_id: uuid.UUID, actor_user_id: Optional[uuid.UUID],
                    actor_rol: str, accion: str, tipo_objeto: str, objeto_id: Optional[uuid.UUID] = None,
                    ip: Optional[str] = None, detalle: Optional[dict] = None) -> int:
    if accion not in ACCIONES:
        raise ValueError(f"acción de auditoría desconocida: {accion}")
    if tipo_objeto not in TIPOS_OBJETO:
        raise ValueError(f"tipo de objeto desconocido: {tipo_objeto}")
    if actor_rol not in ROLES_ACTOR:
        raise ValueError(f"rol de actor desconocido: {actor_rol}")
    return await con.fetchval(
        "INSERT INTO access_audit_log (tenant_id, actor_user_id, actor_rol, accion, tipo_objeto, objeto_id, ip, detalle) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb) RETURNING id",
        tenant_id, actor_user_id, actor_rol, accion, tipo_objeto, objeto_id, ip,
        json.dumps(validar_detalle(detalle)),
    )
