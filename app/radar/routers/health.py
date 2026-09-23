"""Health check del despliegue de Radar (Railway lo consulta en /health)."""

import os

from fastapi import APIRouter, Request

router = APIRouter(tags=["radar-health"])


def _commit() -> str | None:
    return (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "")[:9] or None


@router.get("/health")
async def health(request: Request):
    return {"status": "ok", "modo": "radar", "commit": _commit()}
