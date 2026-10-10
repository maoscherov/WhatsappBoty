"""
Consentimiento asistido (§3.1 C2) y texto de P2 para una línea.

El admin de KIS muestra el texto de P2 con los parámetros de la línea, el
titular lo acepta en la misma sesión (presencial o videollamada) y queda en
`consents` con modo='asistido', cargado_por = el admin y user_id = el dueño de
la cuenta. Se manda copia al dueño por email. Sin esto no se crea la sesión.
"""

import json
import uuid
from typing import Optional

import asyncpg

from app.radar.consentimiento import VERSIONES, hash_texto
from app.radar.lineas import leer_linea
from app.radar.mailer import Email
from app.radar.parametros import DE_LINEA
from app.radar.parametros_service import a_json, leer_parametros_tenant

VERSION_VIGENTE = max(VERSIONES, key=lambda v: int(v[1:]))


async def texto_para_linea(con: asyncpg.Connection, tenant_id: uuid.UUID, line_id: uuid.UUID) -> Optional[dict]:
    fila = await leer_linea(con, line_id)
    if fila is None:
        return None
    return {
        "version": VERSION_VIGENTE,
        "texto": VERSIONES[VERSION_VIGENTE],
        "hash": hash_texto(VERSION_VIGENTE),
        "linea_nombre": fila["nombre"],
        "linea_estado": fila["estado"],
        "parametros_linea": a_json({n: fila[n] for n in DE_LINEA}),
        "parametros_tenant": a_json(await leer_parametros_tenant(con, tenant_id)),
    }


async def registrar_consentimiento_asistido(con: asyncpg.Connection, *, tenant_id: uuid.UUID, line_id: uuid.UUID,
                                            dueno_user_id: uuid.UUID, admin_user_id: uuid.UUID, version: str,
                                            opciones: dict, ip: Optional[str], modo_asistencia: str,
                                            aceptado_por_nombre: str) -> uuid.UUID:
    return await con.fetchval(
        "INSERT INTO consents (tenant_id, line_id, user_id, version_texto, hash_texto, opciones, ip, modo, "
        "cargado_por, modo_asistencia, aceptado_por_nombre) "
        "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, 'asistido', $8, $9, $10) RETURNING id",
        tenant_id, line_id, dueno_user_id, version, hash_texto(version), json.dumps(opciones, default=str), ip,
        admin_user_id, modo_asistencia, aceptado_por_nombre)


def email_copia_consentimiento(para: str, *, version: str) -> Email:
    """Copia al dueño: el texto aceptado, su versión y la huella del hash. Sin datos de conversación."""
    h = hash_texto(version)
    return Email(
        para=para,
        asunto="Copia del consentimiento de tu línea en Radar",
        texto=(f"Un admin de Keep IT Simple registró que aceptaste este texto (versión {version}, huella {h[:16]}). "
               "Si no lo aceptaste, respondé este correo.\n\n" + VERSIONES[version]),
        huella=h[:8],
    )
