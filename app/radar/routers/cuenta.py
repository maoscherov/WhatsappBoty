"""API autenticada de la cuenta: quién soy, líneas, usuarios y roles."""

from fastapi import APIRouter, Depends

from app.radar.auth import Sesion, sesion_actual

router = APIRouter(prefix="/radar/api", tags=["radar-cuenta"])


def _sesion_json(sesion: Sesion) -> dict:
    return {
        "user_id": str(sesion.user_id),
        "tenant_id": str(sesion.tenant_id),
        "rol": sesion.rol,
        "email": sesion.email,
        "es_kis": sesion.es_kis,
        "lineas_permitidas": [str(x) for x in sesion.lineas_permitidas]
        if sesion.lineas_permitidas is not None else None,
    }


@router.get("/yo")
async def yo(sesion: Sesion = Depends(sesion_actual)):
    return _sesion_json(sesion)
