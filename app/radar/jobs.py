"""
Cola de jobs de Radar en Postgres (§6.2): tabla `jobs` sin contenido.

- encolar() corre dentro de tenant_tx (el job hereda el tenant por DEFAULT).
- reclamar() usa radar_jobs_reclamar (SECURITY DEFINER, FOR UPDATE SKIP
  LOCKED): reclama entre tenants desde una tabla sin contenido; recién después
  el worker entra a tenant_tx(job.tenant_id).
- Un job reclamado queda 'corriendo' con un lease; si el proceso muere, al
  vencer el lease otro worker lo retoma -- salvo que ya no le queden
  intentos disponibles, en cuyo caso radar_jobs_reclamar lo pasa a 'fallido'
  en vez de reclamarlo de nuevo (ver nota de revisión de Task 2 en r0003).
- fallar() guarda solo el TIPO de error (nunca el mensaje) y reintenta con
  backoff exponencial hasta max_intentos.
"""

import re
import uuid
from dataclasses import dataclass
from typing import Optional

import asyncpg

from app.radar.db import RadarDB

LEASE_S = 300


@dataclass(frozen=True)
class Job:
    id: uuid.UUID
    tenant_id: uuid.UUID
    tipo: str
    link_id: uuid.UUID
    causa: Optional[str]
    intentos: int


async def encolar(con: asyncpg.Connection, *, tipo: str, link_id: uuid.UUID, causa: Optional[str] = None,
                  en_segundos: float = 0) -> Optional[uuid.UUID]:
    return await con.fetchval(
        "INSERT INTO jobs (tipo, link_id, causa, ejecutar_desde) "
        "VALUES ($1, $2, $3, now() + make_interval(secs => $4)) ON CONFLICT DO NOTHING RETURNING id",
        tipo, link_id, causa, float(en_segundos))


async def reclamar(db: RadarDB, *, lote: int = 10, lease_s: int = LEASE_S) -> list[Job]:
    async with db.sin_tenant() as con:
        filas = await con.fetch("SELECT * FROM radar_jobs_reclamar($1, $2)", lote, lease_s)
    return [Job(id=f["id"], tenant_id=f["tenant_id"], tipo=f["tipo"], link_id=f["link_id"], causa=f["causa"],
                intentos=f["intentos"]) for f in filas]


async def completar(db: RadarDB, job: Job) -> None:
    async with db.tenant_tx(job.tenant_id) as con:
        await con.execute("UPDATE jobs SET estado = 'hecho', bloqueado_hasta = NULL, updated_at = now() "
                          "WHERE id = $1", job.id)


async def reprogramar(db: RadarDB, job: Job, en_segundos: float) -> None:
    async with db.tenant_tx(job.tenant_id) as con:
        await con.execute(
            "UPDATE jobs SET estado = 'pendiente', intentos = 0, bloqueado_hasta = NULL, ultimo_error = NULL, "
            "ejecutar_desde = now() + make_interval(secs => $2), updated_at = now() WHERE id = $1",
            job.id, float(en_segundos))


async def fallar(db: RadarDB, job: Job, error: BaseException) -> str:
    nombre = re.sub(r"[^A-Za-z_]", "", type(error).__name__)[:60] or "Error"
    async with db.tenant_tx(job.tenant_id) as con:
        return await con.fetchval(
            """
            UPDATE jobs SET ultimo_error = $2, bloqueado_hasta = NULL, updated_at = now(),
                   estado = CASE WHEN intentos >= max_intentos THEN 'fallido' ELSE 'pendiente' END,
                   ejecutar_desde = now() + make_interval(
                       secs => least(30 * power(2, greatest(intentos - 1, 0)), 3600))
             WHERE id = $1 RETURNING estado
            """, job.id, nombre)


async def programar_salud(db: RadarDB) -> int:
    async with db.sin_tenant() as con:
        return await con.fetchval("SELECT radar_jobs_programar_salud()")
