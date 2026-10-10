"""
product_events (§6.4, §9): instrumentación de producto por línea. Solo tenant,
línea, usuario, nombre de evento, UUID internos y valores numéricos. Nada de
texto libre: lo rechazan esta función y el CHECK de la tabla.
"""

import json
import re
import uuid
from typing import Any, Optional

import asyncpg

EVENTOS = frozenset({
    "invitacion_enviada", "login_canjeado", "consentimiento_registrado", "linea_creada",
    # tramo 2 (§9: QR mostrado → WORKING, mediana P0 → WORKING)
    "vinculo_iniciado", "vinculo_working", "vinculo_cerrado",
})
_CLAVE = re.compile(r"^[a-z_]{1,40}$")


class ValoresProhibidos(ValueError):
    pass


def validar_valores(valores: Optional[dict]) -> dict:
    limpio: dict[str, Any] = {}
    for clave, valor in (valores or {}).items():
        if not isinstance(clave, str) or not _CLAVE.match(clave):
            raise ValoresProhibidos(f"clave inválida: {clave!r}")
        if isinstance(valor, bool) or not isinstance(valor, (int, float)):
            raise ValoresProhibidos(f"valores.{clave}: solo números")
        limpio[clave] = valor
    return limpio


async def registrar_evento(con: asyncpg.Connection, *, tenant_id: uuid.UUID, evento: str,
                           line_id: Optional[uuid.UUID] = None, user_id: Optional[uuid.UUID] = None,
                           objeto_id: Optional[uuid.UUID] = None, valores: Optional[dict] = None) -> int:
    if evento not in EVENTOS:
        raise ValueError(f"evento de producto desconocido: {evento}")
    return await con.fetchval(
        "INSERT INTO product_events (tenant_id, line_id, user_id, evento, objeto_id, valores) "
        "VALUES ($1, $2, $3, $4, $5, $6::jsonb) RETURNING id",
        tenant_id, line_id, user_id, evento, objeto_id, json.dumps(validar_valores(valores)),
    )
