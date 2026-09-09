"""
Endpoints del agente de sucursal (remedia-agent): recepción del catálogo.

POST /v1/sync/catalog       → upsert de un lote de productos (delta o full)
POST /v1/sync/full-manifest → reconciliación total (resend + desactivar ausentes)
POST /v1/sync/heartbeat     → estado del agente/ERP para el panel

Auth: Bearer por sucursal (branch_auth), fail-closed. Los errores de DB se
propagan como 500: el agente encola y reintenta; un 200 sobre un write fallido
desincroniza la sucursal hasta el próximo manifiesto.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.config import get_settings
from app.models.sync import CatalogBatchIn, HeartbeatIn, ManifestIn
from app.services.branch_auth import Branch, require_branch
from app.services.branch_store import get_branch_store
from app.services.catalog_refresher import get_catalog_refresher
from app.services.catalog_store import get_catalog_store
from app.services.db import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/sync")


def _verificar_branch(body_branch_id: str, branch: Branch):
    if body_branch_id != branch.branch_id:
        raise HTTPException(status_code=403,
                            detail="branch_id no coincide con el token")


def _store():
    return get_catalog_store(get_db(get_settings().database_url))


def _es_default(branch_id: str) -> bool:
    return branch_id == get_settings().default_branch_id


@router.post("/catalog")
async def sync_catalog(body: CatalogBatchIn, branch: Branch = Depends(require_branch)):
    if body.schema_version != 1:
        raise HTTPException(status_code=422, detail="schema_version no soportado")
    _verificar_branch(body.branch_id, branch)
    logger.info(f"sync/catalog {branch.branch_id}: lote {body.batch}/{body.total_batches} "
                f"({len(body.items)} ítems, mode={body.mode})")
    try:
        received, upserted = await _store().upsert_items(
            branch.branch_id, body.items, source=body.source)
    except Exception as e:
        logger.error(f"sync/catalog {branch.branch_id} falló: {e}")
        raise HTTPException(status_code=500, detail=f"error de catálogo: {str(e)[:150]}")

    if _es_default(branch.branch_id):
        get_catalog_refresher().schedule(
            branch.branch_id,
            {i.external_id for i in body.items},
            inmediata=(body.batch >= body.total_batches),
        )
    return {"received": received, "upserted": upserted,
            "unchanged": received - upserted}


@router.post("/full-manifest")
async def sync_full_manifest(body: ManifestIn, branch: Branch = Depends(require_branch)):
    _verificar_branch(body.branch_id, branch)
    if not body.items:
        # Un ERP que respondió mal podría mandar un manifiesto vacío y
        # desactivar TODO el catálogo. No.
        raise HTTPException(status_code=422, detail="manifiesto vacío")
    try:
        resend, deactivated = await _store().full_manifest(branch.branch_id, body.items)
    except Exception as e:
        logger.error(f"sync/full-manifest {branch.branch_id} falló: {e}")
        raise HTTPException(status_code=500, detail=f"error de manifiesto: {str(e)[:150]}")

    logger.info(f"sync/full-manifest {branch.branch_id}: {len(body.items)} entradas, "
                f"resend={len(resend)}, deactivated={deactivated}")
    if _es_default(branch.branch_id) and deactivated:
        get_catalog_refresher().schedule(branch.branch_id, set(), inmediata=True)
    return {"resend": resend, "deactivated": deactivated}


@router.post("/heartbeat", status_code=204)
async def sync_heartbeat(body: HeartbeatIn, branch: Branch = Depends(require_branch)):
    _verificar_branch(body.branch_id, branch)
    try:
        await get_branch_store(get_db(get_settings().database_url)).heartbeat(
            branch.branch_id,
            agent_version=body.agent_version,
            erp_version=body.erp_version,
            erp_status=body.erp_status,
            last_sync_ok_at=body.last_sync_ok_at or "",
            catalog_count=body.catalog_count,
            pending_batches=body.pending_batches,
        )
    except Exception as e:
        logger.error(f"sync/heartbeat {branch.branch_id} falló: {e}")
        raise HTTPException(status_code=500, detail="error registrando heartbeat")
