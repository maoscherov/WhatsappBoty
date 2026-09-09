"""
Websocket saliente del agente de sucursal: WS /v1/agent/ws.

El agente se conecta con su Bearer en el handshake, manda un "hello" y queda
a la espera de lookups. Pinga cada 30 s con {"op":"ping"} y espera un
{"op":"pong"} DE TEXTO (no frame de protocolo): tras 2 pings sin pong corta y
reconecta con backoff.
"""

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import get_settings
from app.services.agent_registry import get_agent_registry
from app.services.branch_auth import resolver_branch
from app.services.branch_store import get_branch_store
from app.services.db import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

HELLO_TIMEOUT_SECS = 10
IDLE_TIMEOUT_SECS = 90   # el agente pinga cada 30 s; 90 sin nada = muerto


@router.websocket("/v1/agent/ws")
async def agent_ws(ws: WebSocket):
    # Auth ANTES de accept(): si falla, el cliente ve el cierre 1008.
    auth = ws.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        await ws.close(code=1008)
        return
    try:
        branch = await resolver_branch(auth[7:].strip())
    except Exception:
        await ws.close(code=1008)
        return

    await ws.accept()

    # Primer mensaje: hello con el branch_id del token.
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=HELLO_TIMEOUT_SECS)
        hello = json.loads(raw)
        if hello.get("op") != "hello" or hello.get("branch_id") != branch.branch_id:
            await ws.close(code=1008)
            return
    except Exception:
        try:
            await ws.close(code=1008)
        except Exception:
            pass
        return

    registry = get_agent_registry()
    anterior = registry.register(branch.branch_id, ws)
    if anterior is not None:
        # Reconexión desde otra IP con la vieja "viva": la nueva la reemplaza.
        try:
            await anterior.close(code=1000)
        except Exception:
            pass
    logger.info(f"Agente conectado: {branch.branch_id} "
                f"v{hello.get('agent_version', '?')}")
    try:
        await get_branch_store(get_db(get_settings().database_url)).set_agent_version(
            branch.branch_id, str(hello.get("agent_version") or ""))
    except Exception:
        pass

    try:
        while True:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=IDLE_TIMEOUT_SECS)
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(f"WS {branch.branch_id}: mensaje no-JSON ignorado")
                continue
            op = msg.get("op")
            if op == "ping":
                await ws.send_json({"op": "pong"})
            elif op == "pong":
                pass
            elif op == "lookup_result":
                registry.resolver_lookup(str(msg.get("req_id") or ""),
                                         msg.get("items") or [],
                                         msg.get("missing") or [])
            else:
                logger.info(f"WS {branch.branch_id}: op desconocida {op!r} ignorada")
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except Exception as e:
        logger.warning(f"WS {branch.branch_id}: {e}")
    finally:
        registry.unregister(branch.branch_id, ws)
        logger.info(f"Agente desconectado: {branch.branch_id}")
        try:
            await ws.close()
        except Exception:
            pass
