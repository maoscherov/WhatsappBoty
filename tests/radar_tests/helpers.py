"""Atajos de test que escriben directo en la base como radar_app."""

import uuid

from app.radar.db import RadarDB


async def crear_tenant_directo(db: RadarDB, nombre: str = "Farmacia Test", rubro: str = "farmacia",
                               perfil: str = "estandar") -> uuid.UUID:
    tid = uuid.uuid4()
    async with db.tenant_tx(tid) as con:
        await con.fetchval(
            "SELECT radar_admin_crear_tenant($1, $2, $3, $4, 12, FALSE, TRUE, 'lotes')",
            tid, nombre, rubro, perfil,
        )
    return tid


async def crear_usuario(db: RadarDB, tenant_id: uuid.UUID, email: str, rol: str,
                        lineas_permitidas: list[uuid.UUID] | None = None) -> uuid.UUID:
    async with db.tenant_tx(tenant_id) as con:
        uid = await con.fetchval(
            "INSERT INTO users (email, nombre) VALUES ($1, $2) RETURNING id", email, email.split("@")[0])
        await con.execute(
            "INSERT INTO memberships (user_id, rol, lineas_permitidas) VALUES ($1, $2, $3)",
            uid, rol, lineas_permitidas)
    return uid


async def crear_linea_directa(db: RadarDB, tenant_id: uuid.UUID, nombre: str = "Línea 1",
                              estado: str = "sin_vinculo") -> uuid.UUID:
    async with db.tenant_tx(tenant_id) as con:
        lid = await con.fetchval("INSERT INTO lines (nombre) VALUES ($1) RETURNING id", nombre)
        if estado != "sin_vinculo":
            await con.execute("UPDATE lines SET estado = $2 WHERE id = $1", lid, estado)
    return lid
