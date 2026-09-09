"""
Recarga del catálogo en memoria tras un sync del ERP.

El sync escribe en Postgres; el bot busca en memoria (rapidfuzz). Este módulo
junta las dos cosas: acumula los external_id cambiados, espera un debounce de
3 segundos (los lotes llegan de a 500, uno atrás del otro) y recarga
SKUService desde Postgres con un swap atómico del singleton. El handler puede
forzar la recarga inmediata al fin de corrida (último lote o manifiesto).

Si hay openai_api_key, también reindexa los embeddings de los ids cambiados
(incremental). Con más de 5.000 ids (primera carga) se difiere a una tarea de
fondo para no demorar la recarga.
"""

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

DEBOUNCE_SECS = 3.0
REINDEX_DIFERIDO_UMBRAL = 5000


class CatalogRefresher:
    def __init__(self):
        self._pending_ids: set[str] = set()
        self._branch_id: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()          # nunca dos recargas concurrentes

    def schedule(self, branch_id: str, changed_ids: set[str], inmediata: bool = False):
        """Acumula ids y programa la recarga (debounce), o la fuerza ya."""
        self._branch_id = branch_id
        self._pending_ids |= {str(i) for i in changed_ids}
        if self._task and not self._task.done():
            self._task.cancel()
        delay = 0.0 if inmediata else DEBOUNCE_SECS
        self._task = asyncio.create_task(self._run(delay))

    async def _run(self, delay: float):
        try:
            if delay:
                await asyncio.sleep(delay)
            await self.recargar()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"CatalogRefresher: recarga falló: {e}")

    async def recargar(self):
        """SELECT del catálogo de la sucursal → SKUService.from_rows → swap."""
        from app.config import get_settings
        from app.services.db import get_db
        from app.services.catalog_store import get_catalog_store
        from app.services.sku_service import SKUService, set_sku_service

        async with self._lock:
            branch_id = self._branch_id or get_settings().default_branch_id
            if not branch_id:
                return
            ids = self._pending_ids
            self._pending_ids = set()

            t0 = time.perf_counter()
            store = get_catalog_store(get_db(get_settings().database_url))
            rows, extras = await store.load_rows(branch_id)
            if not rows:
                logger.warning(f"CatalogRefresher: sin filas para {branch_id}, no se recarga")
                return
            svc = SKUService.from_rows(rows, extras)
            set_sku_service(svc)
            logger.info(f"Catálogo ERP recargado: {svc.total} productos de {branch_id} "
                        f"en {time.perf_counter() - t0:.1f}s ({len(ids)} cambiados)")

        # Embeddings fuera del lock: la búsqueda ya usa el catálogo nuevo.
        await self._reindexar(svc, ids)

    async def _reindexar(self, sku_svc, ids: set[str]):
        from app.config import get_settings
        settings = get_settings()
        if not settings.openai_api_key or not ids:
            return
        try:
            from app.services.db import get_db
            from app.services.embeddings import get_embedding_service
            from app.services.rag_service import get_rag_service
            rag = get_rag_service(get_db(settings.database_url),
                                  get_embedding_service(settings.openai_api_key))
            if len(ids) > REINDEX_DIFERIDO_UMBRAL:
                # Primera carga / manifiesto grande: a una tarea de fondo.
                asyncio.create_task(rag.reindex_ids(sku_svc, ids))
                logger.info(f"RAG: reindex de {len(ids)} ids diferido a tarea de fondo")
            else:
                await rag.reindex_ids(sku_svc, ids)
        except Exception as e:
            logger.warning(f"RAG incremental falló (la búsqueda fuzzy sigue): {e}")


_instance: Optional[CatalogRefresher] = None


def get_catalog_refresher() -> CatalogRefresher:
    global _instance
    if _instance is None:
        _instance = CatalogRefresher()
    return _instance
