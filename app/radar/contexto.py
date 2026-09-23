"""
Contexto de Radar: lo que los routers necesitan y que en tests se reemplaza
por fakes. Vive en app.state.radar; lo arma el lifespan (producción) o la
fixture (tests).
"""

from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.radar.db import RadarDB
from app.radar.fuente import FuenteStore
from app.radar.settings import RadarSettings


@dataclass
class RadarContexto:
    settings: RadarSettings
    db: RadarDB
    fuente: FuenteStore

    async def cerrar(self) -> None:
        await self.db.close()
        await self.fuente.close()


def contexto(request: Request) -> RadarContexto:
    ctx = request.app.state.radar
    if ctx is None:
        raise HTTPException(status_code=503, detail="Radar arrancando")
    return ctx
