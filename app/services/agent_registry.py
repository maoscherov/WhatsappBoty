"""
Registro de conexiones websocket de los agentes de sucursal.

Estado EN MEMORIA DEL PROCESO: hoy uvicorn corre con un solo worker (Procfile/
Dockerfile). Si algún día se pasa a --workers N, un lookup puede caer en un
worker sin la conexión: habrá que mover esto a Redis pub/sub.

`lookup` es request/response sobre el WS: se manda {"op":"lookup", req_id} y
se espera el {"op":"lookup_result"} con ese req_id resolviendo un Future.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class LookupResult:
    items: list = field(default_factory=list)      # CatalogItem dicts del ERP
    missing: list = field(default_factory=list)    # barcodes/ids que no conoce


class AgentRegistry:
    def __init__(self):
        self._conns: dict[str, object] = {}                 # branch_id -> WebSocket
        self._futures: dict[str, asyncio.Future] = {}       # req_id -> Future

    def register(self, branch_id: str, ws) -> Optional[object]:
        """Registra la conexión. Devuelve la anterior si había (para cerrarla)."""
        anterior = self._conns.get(branch_id)
        self._conns[branch_id] = ws
        return anterior if anterior is not ws else None

    def unregister(self, branch_id: str, ws):
        """Desregistra SOLO si sigue siendo la conexión actual (una nueva pudo
        haberla reemplazado). Resuelve con None los lookups pendientes."""
        if self._conns.get(branch_id) is ws:
            del self._conns[branch_id]
            for req_id, fut in list(self._futures.items()):
                if not fut.done():
                    fut.set_result(None)
                self._futures.pop(req_id, None)

    def connected(self, branch_id: str) -> bool:
        return branch_id in self._conns

    def resolver_lookup(self, req_id: str, items: list, missing: list):
        """Llamado por el handler del WS cuando llega un lookup_result."""
        fut = self._futures.pop(req_id, None)
        if fut and not fut.done():
            fut.set_result(LookupResult(items=items or [], missing=missing or []))
        else:
            logger.info(f"lookup_result tardío/desconocido descartado: {req_id}")

    async def lookup(self, branch_id: str, barcodes: Optional[list[str]] = None,
                     ids: Optional[list[str]] = None,
                     timeout: float = 5.0) -> Optional[LookupResult]:
        """Consulta en vivo al ERP vía el agente. None = sin agente o timeout."""
        ws = self._conns.get(branch_id)
        if ws is None:
            return None
        # Contrato del agente (ws.rs): `ids: Vec<i64>`. Un string hace fallar
        # la deserialización y el agente descarta el mensaje sin responder
        # (caso real 11/9: lookup por "182288" → timeout). Se mandan enteros;
        # los no numéricos se descartan con log.
        ids_int: list[int] = []
        for i in ids or []:
            try:
                ids_int.append(int(str(i).strip()))
            except (TypeError, ValueError):
                logger.warning(f"lookup: id no numérico descartado: {i!r}")
        req_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._futures[req_id] = fut
        try:
            await ws.send_json({"op": "lookup", "req_id": req_id,
                                "barcodes": [str(b) for b in (barcodes or [])],
                                "ids": ids_int})
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"lookup a {branch_id} sin respuesta en {timeout}s")
            return None
        except Exception as e:
            logger.warning(f"lookup a {branch_id} falló: {e}")
            return None
        finally:
            self._futures.pop(req_id, None)

    async def sync_now(self, branch_id: str) -> bool:
        """Pide al agente una corrida de sync inmediata. False si no está."""
        ws = self._conns.get(branch_id)
        if ws is None:
            return False
        try:
            await ws.send_json({"op": "sync_now"})
            return True
        except Exception as e:
            logger.warning(f"sync_now a {branch_id} falló: {e}")
            return False


_instance: Optional[AgentRegistry] = None


def get_agent_registry() -> AgentRegistry:
    global _instance
    if _instance is None:
        _instance = AgentRegistry()
    return _instance
