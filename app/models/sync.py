"""
Modelos Pydantic del contrato agente ↔ servidor (/v1/sync/*, /v1/agent/ws).

El contrato lo fija el agente (remedia-agent, ya implementado): ver
docs/superpowers/specs/2026-09-08-remedia-agent-design.md §2. El precio viaja
como STRING decimal ("19641.26") o null — nunca float.
"""

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class CatalogItemIn(BaseModel):
    external_id: str = Field(min_length=1)          # idProducto del ERP
    hash: str = Field(pattern=r"^[0-9a-f]{64}$")    # blake3 hex
    barcodes: list[str] = []
    troquel: Optional[int] = None
    name: str = Field(min_length=1)
    brand: Optional[str] = None
    drug: Optional[str] = None
    form: Optional[str] = None
    category: str = ""
    rubro: str = ""
    subrubro: str = ""
    therapeutic_actions: list[str] = []
    price: Optional[Decimal] = None                 # None = sin precio (no gratis)
    stock: int = 0
    visible: bool = True
    active: bool = True


class CatalogBatchIn(BaseModel):
    schema_version: int
    branch_id: str
    source: str = "observer-gestion"
    mode: str = "delta"                             # delta | full (informativo)
    batch: int = 1
    total_batches: int = 1
    generated_at: str = ""
    items: list[CatalogItemIn] = Field(min_length=1, max_length=1000)


class ManifestEntryIn(BaseModel):
    external_id: str = Field(min_length=1)
    hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ManifestIn(BaseModel):
    branch_id: str
    generated_at: str = ""
    items: list[ManifestEntryIn]


class HeartbeatIn(BaseModel):
    branch_id: str
    agent_version: str = ""
    erp_version: str = ""
    erp_status: str = "ok"                          # ok | no_autorizado | inalcanzable | error
    last_sync_ok_at: Optional[str] = None
    catalog_count: Optional[int] = None
    pending_batches: Optional[int] = None
