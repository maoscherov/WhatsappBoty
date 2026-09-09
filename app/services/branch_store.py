"""
CRUD de sucursales (tabla `branches`) + heartbeat + estado derivado.

El estado que ve el panel se deriva de lo que reporta el agente y de lo que
observa el servidor:
  sin_agente   → sin heartbeat hace 15+ min (o nunca)
  erp_<status> → el agente llega pero el ERP no está ok
  atrasada     → hay lotes encolados sin subir
  ok           → resto
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_BRANCH_ID_RE = re.compile(r"^[a-z0-9-]{3,40}$")
HEARTBEAT_STALE_SECS = 15 * 60


def branch_id_valido(branch_id: str) -> bool:
    return bool(_BRANCH_ID_RE.match(branch_id or ""))


def estado_derivado(row: dict) -> str:
    """Badge para el panel a partir de una fila de `branches`."""
    hb = row.get("last_heartbeat_at")
    if not hb:
        return "sin_agente"
    if isinstance(hb, datetime):
        edad = (datetime.now(timezone.utc) - hb).total_seconds()
        if edad > HEARTBEAT_STALE_SECS:
            return "sin_agente"
    erp = (row.get("erp_status") or "ok").strip().lower()
    if erp != "ok":
        return f"erp_{erp}"
    if (row.get("pending_batches") or 0) > 0:
        return "atrasada"
    return "ok"


class BranchStore:
    def __init__(self, db):
        self._db = db

    async def crear(self, branch_id: str, nombre: str, token_hash: str) -> bool:
        """False si ya existe."""
        existente = await self._db.fetchrow(
            "SELECT 1 FROM branches WHERE branch_id = $1", branch_id)
        if existente:
            return False
        await self._db.execute(
            "INSERT INTO branches (branch_id, nombre, token_hash) VALUES ($1, $2, $3)",
            branch_id, nombre, token_hash,
        )
        return True

    async def get(self, branch_id: str) -> Optional[dict]:
        row = await self._db.fetchrow(
            "SELECT * FROM branches WHERE branch_id = $1", branch_id)
        return dict(row) if row else None

    async def listar(self) -> list[dict]:
        rows = await self._db.fetch("SELECT * FROM branches ORDER BY branch_id")
        return [dict(r) for r in rows]

    async def rotar_token(self, branch_id: str, token_hash: str) -> bool:
        res = await self._db.execute(
            "UPDATE branches SET token_hash = $2 WHERE branch_id = $1",
            branch_id, token_hash,
        )
        return bool(res and res.endswith("1"))

    async def actualizar(self, branch_id: str, nombre: Optional[str] = None,
                         activa: Optional[bool] = None) -> bool:
        row = await self.get(branch_id)
        if not row:
            return False
        await self._db.execute(
            "UPDATE branches SET nombre = $2, activa = $3 WHERE branch_id = $1",
            branch_id,
            nombre if nombre is not None else row["nombre"],
            activa if activa is not None else row["activa"],
        )
        return True

    async def heartbeat(self, branch_id: str, agent_version: str = "",
                        erp_version: str = "", erp_status: str = "",
                        last_sync_ok_at: str = "", catalog_count: Optional[int] = None,
                        pending_batches: Optional[int] = None) -> None:
        await self._db.execute(
            """
            UPDATE branches SET
                last_heartbeat_at = now(),
                agent_version   = $2,
                erp_version     = $3,
                erp_status      = $4,
                last_sync_ok_at = NULLIF($5, '')::timestamptz,
                catalog_count   = $6,
                pending_batches = $7
            WHERE branch_id = $1
            """,
            branch_id, agent_version or None, erp_version or None,
            erp_status or None, last_sync_ok_at or "", catalog_count, pending_batches,
        )

    async def marcar_push(self, branch_id: str) -> None:
        await self._db.execute(
            "UPDATE branches SET last_catalog_push_at = now() WHERE branch_id = $1",
            branch_id)

    async def marcar_manifest(self, branch_id: str) -> None:
        await self._db.execute(
            "UPDATE branches SET last_manifest_at = now() WHERE branch_id = $1",
            branch_id)

    async def set_agent_version(self, branch_id: str, agent_version: str) -> None:
        await self._db.execute(
            "UPDATE branches SET agent_version = $2 WHERE branch_id = $1",
            branch_id, agent_version or None)


_instance: Optional[BranchStore] = None


def get_branch_store(db) -> BranchStore:
    global _instance
    if _instance is None:
        _instance = BranchStore(db)
    return _instance
