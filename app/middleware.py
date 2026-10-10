"""Middleware compartido por los dos modos de la app (bot y Radar)."""

import logging

_log = logging.getLogger("app.errors")


async def log_errores(request, call_next):
    """Loguea cualquier 5xx con método + ruta para rastrearlo en Railway.
    Nunca loguea cuerpos ni query strings."""
    try:
        response = await call_next(request)
    except Exception as e:
        _log.exception(f"💥 500 en {request.method} {request.url.path} — {type(e).__name__}: {e}")
        raise
    if response.status_code >= 500:
        _log.error(f"💥 {response.status_code} en {request.method} {request.url.path}")
    return response
