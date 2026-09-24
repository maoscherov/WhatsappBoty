"""
Worker de la cola de Radar (§6.2).

- Como proceso aparte: `python -m app.radar.worker` (misma imagen, mismas
  variables RADAR_). No corre migraciones: las corre el servicio web.
- No se corren a la vez el embebido y el proceso aparte (ver docs de despliegue).
- Embebido (por defecto, RADAR_WORKER_EMBEBIDO=true): el lifespan del servicio
  web corre `bucle` como tarea. Motivo: las claves de WAHA viven en el
  FileSecretStore de un volumen de Railway, y un volumen se monta en un solo
  servicio. Con un gestor de secretos externo se pasa a proceso aparte.

Cada job se reclama sin tenant (función SECURITY DEFINER sobre una tabla sin
contenido) y el handler entra a tenant_tx(job.tenant_id). Un error en un job no
corta el ciclo: se registra el tipo de error y se reintenta con backoff.
"""

import asyncio
import logging
import signal
import time
from typing import Awaitable, Callable, Optional

from app.radar import fin_vinculo, salud
from app.radar import jobs as cola
from app.radar.contexto import RadarContexto
from app.radar.jobs import Job

logger = logging.getLogger("app.radar.worker")

HANDLERS: dict[str, Callable[[RadarContexto, Job], Awaitable[str]]] = {
    "fin_vinculo": fin_vinculo.ejecutar,
    "chequeo_salud": salud.ejecutar,
    "aviso_caida": fin_vinculo.avisar_caida,
}


async def correr_una_vez(ctx: RadarContexto, *, lote: int = 1) -> int:
    # De a un job por vuelta: el lease corre desde el reclamo, y en un lote los
    # últimos llegaban a su turno con el lease vencido (otro worker los retomaba).
    trabajos = await cola.reclamar(ctx.db, lote=lote)
    for job in trabajos:
        try:
            handler = HANDLERS.get(job.tipo)
            if handler is None:
                raise LookupError("tipo de job sin handler")
            resultado = await handler(ctx, job)
            if resultado == "reprogramar":
                await cola.reprogramar(ctx.db, job, salud.INTERVALO_S)
            else:
                await cola.completar(ctx.db, job)
        except Exception as e:  # un job roto no corta el ciclo
            logger.warning("job %s (%s) falló: %s", job.id, job.tipo, type(e).__name__)
            await cola.fallar(ctx.db, job, e)
    return len(trabajos)


async def bucle(ctx: RadarContexto, *, parar: asyncio.Event, pausa_s: float = 2.0,
                salud_cada_s: float = float(salud.INTERVALO_S)) -> None:
    ultima: Optional[float] = None
    while not parar.is_set():
        if ultima is None or time.monotonic() - ultima >= salud_cada_s:
            try:
                await cola.programar_salud(ctx.db)
            except Exception as e:
                logger.warning("no se pudieron programar los chequeos de salud: %s", type(e).__name__)
            ultima = time.monotonic()
        try:
            hechos = await correr_una_vez(ctx)
        except Exception as e:
            logger.warning("ciclo del worker falló: %s", type(e).__name__)
            hechos = 0
        if hechos == 0:
            try:
                await asyncio.wait_for(parar.wait(), timeout=pausa_s)
            except asyncio.TimeoutError:
                pass


async def _main() -> None:
    from app.radar.app import construir_contexto, validar_settings
    from app.radar.settings import get_radar_settings

    logging.basicConfig(level="INFO")
    rs = get_radar_settings()
    validar_settings(rs)
    ctx = await construir_contexto(rs)
    parar = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, parar.set)
        except (NotImplementedError, RuntimeError):   # Windows
            pass
    try:
        await bucle(ctx, parar=parar)
    finally:
        await ctx.cerrar()


if __name__ == "__main__":
    asyncio.run(_main())
