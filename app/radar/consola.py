"""
C1 de la Consola KIS (§3.1): todas las líneas de todos los clientes, con el
último vínculo de cada una, por la función SECURITY DEFINER
radar_admin_consola_lineas(). Sin conversaciones ni teléfonos: del número solo
el sufijo. Los campos de tramos futuros van en null con su tramo en `pendiente`.
"""

import uuid
from datetime import datetime
from typing import Optional

from app.radar.contexto import RadarContexto
from app.radar.vinculo_estados import restriccion_activa, semaforo

PENDIENTES = {"ultimo_mensaje": "tramo 3", "sincronizacion": "tramo 3", "huecos": "tramo 3",
              "gasto_ia_mes": "tramo 5"}


def _iso(v: Optional[datetime]) -> Optional[str]:
    return v.isoformat() if v else None


def fila_consola(f: dict, ahora: datetime) -> dict:
    restringida = restriccion_activa(f["restriccion_hasta"], f["restriccion_sin_fecha"], ahora)
    return {
        "tenant_id": str(f["tenant_id"]),
        "tenant_nombre": f["tenant_nombre"],
        "line_id": str(f["line_id"]),
        "line_nombre": f["line_nombre"],
        "line_estado": f["line_estado"],
        "link_id": str(f["link_id"]) if f["link_id"] else None,
        "link_estado": f["link_estado"],
        "waha_status": f["waha_status"],
        "numero": ("…" + f["numero_sufijo"]) if f["numero_sufijo"] else None,
        "observado_hasta": _iso(f["observado_hasta"]),
        "caido_desde": _iso(f["caido_desde"]),
        "ultimo_status_at": _iso(f["ultimo_status_at"]),
        "engine": f["engine"],
        "salud": ("restricción activa" if restringida else
                  ("sin restricción" if f["link_id"] else None)),
        "worker": (f"{f['worker_nombre']} ({f['worker_sesiones']}/{f['worker_max_sesiones']})"
                   if f["worker_nombre"] else None),
        "semaforo": semaforo(f, ahora),
        **{campo: None for campo in PENDIENTES},
        "pendiente": dict(PENDIENTES),
    }


async def listar_lineas_consola(ctx: RadarContexto, *, estado: Optional[str] = None,
                                tenant_id: Optional[uuid.UUID] = None) -> list[dict]:
    async with ctx.db.sin_tenant() as con:
        filas = await con.fetch("SELECT * FROM radar_admin_consola_lineas()")
        ahora = await con.fetchval("SELECT now()")
    return [fila_consola(dict(f), ahora) for f in filas
            if (estado is None or f["line_estado"] == estado) and (tenant_id is None or f["tenant_id"] == tenant_id)]
