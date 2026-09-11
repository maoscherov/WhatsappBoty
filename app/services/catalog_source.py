"""
Fuente del catálogo del bot: ERP (Postgres, sincronizado por el agente) o CSV.

Decisión (11/9): el ERP es la fuente única cuando hay una sucursal sincronizada.
Ya no hace falta la variable DEFAULT_BRANCH_ID: si hay exactamente UNA
sucursal activa con catálogo en `catalog_items`, el bot la usa solo. La
variable queda como override para cuando haya más de una. La config
`catalogo_fuente` ("erp" | "csv") es el botón de pánico para volver al CSV.
"""

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Última resolución (evita una query por mensaje; los syncs la refrescan).
_cache: dict = {"branch_id": None, "at": 0.0}
_CACHE_SECS = 60.0

# Estado observable (para /bo/catalogo/estado).
estado_recarga: dict = {"fuente": "csv", "branch_id": None, "at": None, "total": 0}


def invalidar_cache():
    _cache["at"] = 0.0


async def fuente_configurada() -> str:
    """'erp' (default) | 'csv'. Best-effort: sin config, 'erp'."""
    try:
        from app.config import get_settings
        from app.services.config_service import get_config_service
        cfg = await get_config_service(get_settings().redis_url).get_all()
        return (cfg.get("catalogo_fuente") or "erp").strip().lower()
    except Exception:
        return "erp"


async def resolver_branch_default(forzar: bool = False) -> Optional[str]:
    """
    Sucursal cuyo catálogo ERP usa el bot, o None (→ CSV).
      1. catalogo_fuente == "csv"      → None
      2. DEFAULT_BRANCH_ID seteada     → esa (override)
      3. exactamente una sucursal activa con filas en catalog_items → esa
      4. varias → None + warning (hace falta el override); ninguna → None
    """
    if not forzar and time.time() - _cache["at"] < _CACHE_SECS:
        return _cache["branch_id"]

    from app.config import get_settings
    from app.services.db import get_db
    settings = get_settings()
    resultado: Optional[str] = None

    if await fuente_configurada() == "csv":
        resultado = None
    elif settings.default_branch_id:
        resultado = settings.default_branch_id
    else:
        db = get_db(settings.database_url)
        if db.available():
            rows = await db.fetch(
                """
                SELECT b.branch_id, COUNT(c.external_id) AS n
                FROM branches b
                JOIN catalog_items c ON c.branch_id = b.branch_id
                WHERE b.activa
                GROUP BY b.branch_id
                HAVING COUNT(c.external_id) > 0
                """)
            if len(rows) == 1:
                resultado = rows[0]["branch_id"]
            elif len(rows) > 1:
                logger.warning(
                    f"Hay {len(rows)} sucursales con catálogo ERP y no está "
                    "DEFAULT_BRANCH_ID: el bot sigue con el CSV hasta que se defina.")

    _cache["branch_id"] = resultado
    _cache["at"] = time.time()
    return resultado


def marcar_recarga(fuente: str, branch_id: Optional[str], total: int):
    estado_recarga.update({"fuente": fuente, "branch_id": branch_id,
                           "at": time.time(), "total": total})


async def aplicar_fuente() -> dict:
    """
    Recarga el catálogo en memoria según la fuente vigente: ERP si hay
    sucursal resuelta, si no el CSV. Se llama al cambiar `catalogo_fuente`
    desde el backoffice y desde POST /bo/catalogo/recargar.
    """
    from app.config import get_settings
    invalidar_cache()
    branch = await resolver_branch_default(forzar=True)
    if branch:
        from app.services.catalog_refresher import get_catalog_refresher
        refresher = get_catalog_refresher()
        refresher._branch_id = branch
        await refresher.recargar()
    else:
        from app.services.sku_service import reload_sku_service
        svc = reload_sku_service(get_settings().sku_csv_path)
        marcar_recarga("csv", None, svc.total)
    return await estado()


async def estado() -> dict:
    """Qué está leyendo el bot ahora mismo (para el badge del backoffice)."""
    from app.config import get_settings
    from app.services.db import get_db
    from app.services.sku_service import get_sku_service
    settings = get_settings()
    total_mem = 0
    try:
        total_mem = get_sku_service(settings.sku_csv_path).total
    except Exception:
        pass
    ultimo_sync = None
    branch = estado_recarga.get("branch_id")
    if branch:
        try:
            row = await get_db(settings.database_url).fetchrow(
                "SELECT last_catalog_push_at, last_heartbeat_at FROM branches "
                "WHERE branch_id = $1", branch)
            if row:
                ultimo_sync = {
                    "last_catalog_push_at": row["last_catalog_push_at"],
                    "last_heartbeat_at": row["last_heartbeat_at"],
                }
        except Exception:
            pass
    return {
        "fuente": estado_recarga.get("fuente") or "csv",
        "fuente_configurada": await fuente_configurada(),
        "branch_id": branch,
        "total_productos": total_mem,
        "ultima_recarga_at": estado_recarga.get("at"),
        "ultimo_sync": ultimo_sync,
    }
