"""Health check del despliegue de Radar (Railway lo consulta en /health)."""

import os

from fastapi import APIRouter, Request, Response

router = APIRouter(tags=["radar-health"])


def _commit() -> str | None:
    return (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "")[:9] or None


@router.get("/health")
async def health(request: Request, response: Response):
    ctx = request.app.state.radar
    if ctx is None:
        response.status_code = 503
        return {"status": "arrancando", "modo": "radar", "commit": _commit()}
    resultados = await ctx.db.salud()
    fuente = await ctx.fuente.salud()
    ok = resultados["ok"] and fuente["ok"]
    response.status_code = 200 if ok else 503
    return {
        "status": "ok" if ok else "degradado",
        "modo": "radar",
        "resultados": resultados,
        "fuente": fuente,
        "commit": _commit(),
    }
