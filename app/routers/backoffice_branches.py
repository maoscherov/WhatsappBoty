"""
Panel: gestión de sucursales del sync de catálogo (agente ERP).

Auth: la misma BO_KEY que el resto de /bo/* (reusa _auth de backoffice).
El token de la sucursal se devuelve UNA sola vez (al crear o rotar); después
solo existe hasheado.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.config import get_settings
from app.routers.backoffice import _auth
from app.services.agent_registry import get_agent_registry
from app.services.branch_auth import generar_token
from app.services.branch_store import branch_id_valido, estado_derivado, get_branch_store
from app.services.catalog_refresher import get_catalog_refresher
from app.services.catalog_store import get_catalog_store
from app.services.db import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bo")


def _db():
    db = get_db(get_settings().database_url)
    if not db.available():
        raise HTTPException(status_code=503, detail="Postgres no disponible")
    return db


class BranchIn(BaseModel):
    branch_id: str
    nombre: str


class BranchPatch(BaseModel):
    nombre: Optional[str] = None
    activa: Optional[bool] = None


class ExtrasIn(BaseModel):
    requiere_receta_override: Optional[str] = None   # si | ambiguo | no | null
    pausado_manual: Optional[bool] = None
    imagen_url: Optional[str] = None


@router.post("/branches")
async def bo_branch_create(body: BranchIn, _=Depends(_auth)):
    if not branch_id_valido(body.branch_id):
        raise HTTPException(status_code=422,
                            detail="branch_id inválido ([a-z0-9-]{3,40})")
    token, token_hash = generar_token()
    creada = await get_branch_store(_db()).crear(body.branch_id, body.nombre, token_hash)
    if not creada:
        raise HTTPException(status_code=409, detail="la sucursal ya existe")
    logger.info(f"Sucursal creada: {body.branch_id}")
    # El token viaja UNA vez; no se puede volver a pedir (solo rotar).
    return {"branch_id": body.branch_id, "token": token}


@router.get("/branches")
async def bo_branch_list(_=Depends(_auth)):
    db = _db()
    store = get_branch_store(db)
    cat = get_catalog_store(db)
    registry = get_agent_registry()
    out = []
    for row in await store.listar():
        out.append({
            "branch_id": row["branch_id"],
            "nombre": row["nombre"],
            "activa": row["activa"],
            "estado": estado_derivado(row),
            "conectada_ws": registry.connected(row["branch_id"]),
            "items_en_db": await cat.count_items(row["branch_id"]),
            "last_heartbeat_at": row.get("last_heartbeat_at"),
            "agent_version": row.get("agent_version"),
            "erp_version": row.get("erp_version"),
            "erp_status": row.get("erp_status"),
            "last_sync_ok_at": row.get("last_sync_ok_at"),
            "catalog_count": row.get("catalog_count"),
            "pending_batches": row.get("pending_batches"),
            "last_catalog_push_at": row.get("last_catalog_push_at"),
            "last_manifest_at": row.get("last_manifest_at"),
        })
    return out


@router.post("/branches/{branch_id}/rotate-token")
async def bo_branch_rotate(branch_id: str, _=Depends(_auth)):
    token, token_hash = generar_token()
    ok = await get_branch_store(_db()).rotar_token(branch_id, token_hash)
    if not ok:
        raise HTTPException(status_code=404, detail="sucursal no encontrada")
    logger.info(f"Token rotado: {branch_id}")
    return {"branch_id": branch_id, "token": token}


@router.patch("/branches/{branch_id}")
async def bo_branch_patch(branch_id: str, body: BranchPatch, _=Depends(_auth)):
    ok = await get_branch_store(_db()).actualizar(
        branch_id, nombre=body.nombre, activa=body.activa)
    if not ok:
        raise HTTPException(status_code=404, detail="sucursal no encontrada")
    return {"ok": True}


@router.post("/branches/{branch_id}/sync-now")
async def bo_branch_sync_now(branch_id: str, _=Depends(_auth)):
    if not await get_agent_registry().sync_now(branch_id):
        raise HTTPException(status_code=409, detail="la sucursal no está conectada")
    return {"ok": True}


@router.get("/branches/{branch_id}/lookup")
async def bo_branch_lookup(branch_id: str, _=Depends(_auth),
                           barcode: str = Query(""), id: str = Query("")):
    """Lookup en vivo al ERP (debug/operador). 504 si el agente no responde."""
    res = await get_agent_registry().lookup(
        branch_id,
        barcodes=[barcode] if barcode else [],
        ids=[id] if id else [],
        timeout=get_settings().live_lookup_timeout_s,
    )
    if res is None:
        raise HTTPException(status_code=504, detail="el agente no respondió")
    return {"items": res.items, "missing": res.missing}


@router.get("/catalogo/estado")
async def bo_catalogo_estado(_=Depends(_auth)):
    """Fuente actual del catálogo (erp/csv), sucursal, total y última recarga/sync."""
    from app.services.catalog_source import estado
    return await estado()


@router.post("/catalogo/recargar")
async def bo_catalogo_recargar(_=Depends(_auth)):
    """Fuerza la recarga del catálogo en memoria según la fuente vigente."""
    from app.services.catalog_source import aplicar_fuente
    return await aplicar_fuente()


@router.put("/catalog/{external_id}/extras")
async def bo_catalog_extras(external_id: str, body: ExtrasIn, _=Depends(_auth)):
    """
    Datos manuales de un producto ERP (sucursal activa): override de receta,
    pausa manual, imagen. Sobreviven a los syncs (catalog_extras).
    """
    from app.services.catalog_source import resolver_branch_default
    branch = await resolver_branch_default()
    if not branch:
        raise HTTPException(status_code=409, detail="sin sucursal ERP activa")
    if body.requiere_receta_override is not None and \
            body.requiere_receta_override not in ("si", "ambiguo", "no", ""):
        raise HTTPException(status_code=422,
                            detail="requiere_receta_override: si | ambiguo | no | vacío")
    campos = {k: v for k, v in body.model_dump().items() if v is not None}
    if "requiere_receta_override" in campos and campos["requiere_receta_override"] == "":
        campos["requiere_receta_override"] = None   # limpiar el override
    await get_catalog_store(_db()).set_extras(
        branch, external_id, **campos)
    get_catalog_refresher().schedule(branch, {external_id})
    return {"ok": True}
