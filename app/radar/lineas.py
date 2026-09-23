"""
Líneas (§2.1, §6.4): alta con sus parámetros de ámbito Línea, lectura y
listado. El almacén de fuente se deriva de retencion_fuente_dias
(0 = permanente); el purgable llega en el tramo 6, así que pedirlo es 422.
"""

import json
import uuid
from typing import Optional

import asyncpg

from app.radar.fuente import ALMACENES_DISPONIBLES
from app.radar.parametros import DE_LINEA, iniciales, validar

COLUMNAS = ("id", "nombre", "estado", "almacen_fuente", *DE_LINEA, "parametros_propuestos",
            "fuente_purgada_hasta", "created_at")
_SELECT = "SELECT " + ", ".join(COLUMNAS) + " FROM lines"


class AlmacenNoDisponible(ValueError):
    pass


def almacen_para(retencion_fuente_dias: int) -> str:
    return "purgable" if retencion_fuente_dias > 0 else "permanente"


def verificar_almacen(valores: dict) -> str:
    almacen = almacen_para(valores["retencion_fuente_dias"])
    if almacen not in ALMACENES_DISPONIBLES:
        raise AlmacenNoDisponible(
            "retencion_fuente_dias > 0 requiere el almacén purgable, que no existe en este despliegue (tramo 6)")
    return almacen


def resolver_valores_linea(parciales: dict) -> dict:
    """Iniciales de §2.1 pisados por los valores explícitos (None = no enviado)."""
    valores = iniciales("linea")
    for nombre, valor in parciales.items():
        if nombre not in DE_LINEA:
            raise ValueError(f"no es un parámetro de línea: {nombre}")
        if valor is not None:
            valores[nombre] = validar(nombre, valor)
    return valores


async def crear_linea(con: asyncpg.Connection, *, tenant_id: uuid.UUID, nombre: str, valores: dict) -> uuid.UUID:
    almacen = verificar_almacen(valores)
    return await con.fetchval(
        "INSERT INTO lines (tenant_id, nombre, almacen_fuente, duracion_vinculo_dias, retencion_fuente_dias, "
        "retencion_tras_desvinculo_dias, tope_ia_mensual_usd) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
        tenant_id, nombre, almacen, valores["duracion_vinculo_dias"], valores["retencion_fuente_dias"],
        valores["retencion_tras_desvinculo_dias"], valores["tope_ia_mensual_usd"],
    )


async def leer_linea(con: asyncpg.Connection, line_id: uuid.UUID) -> Optional[dict]:
    fila = await con.fetchrow(_SELECT + " WHERE id = $1", line_id)
    return dict(fila) if fila else None


async def listar_lineas(con: asyncpg.Connection, permitidas: Optional[list[uuid.UUID]]) -> list[dict]:
    filas = await con.fetch(
        _SELECT + " WHERE estado <> 'de_baja' AND ($1::uuid[] IS NULL OR id = ANY($1::uuid[])) ORDER BY created_at",
        permitidas)
    return [dict(f) for f in filas]


def propuesta_de_linea(fila: dict) -> dict:
    """Propuesta pendiente de un admin de KIS (lines.parametros_propuestos, JSONB
    que asyncpg entrega como str). {} si no hay."""
    crudo = fila.get("parametros_propuestos")
    return json.loads(crudo) if crudo else {}


def linea_json(fila: dict) -> dict:
    return {
        "id": str(fila["id"]),
        "nombre": fila["nombre"],
        "estado": fila["estado"],
        "almacen_fuente": fila["almacen_fuente"],
        "parametros": {
            "duracion_vinculo_dias": fila["duracion_vinculo_dias"],
            "retencion_fuente_dias": fila["retencion_fuente_dias"],
            "retencion_tras_desvinculo_dias": fila["retencion_tras_desvinculo_dias"],
            "tope_ia_mensual_usd": str(fila["tope_ia_mensual_usd"]) if fila["tope_ia_mensual_usd"] is not None else None,
        },
        "parametros_propuestos": propuesta_de_linea(fila),
        "fuente_purgada_hasta": fila["fuente_purgada_hasta"].isoformat() if fila["fuente_purgada_hasta"] else None,
        "created_at": fila["created_at"].isoformat(),
    }
