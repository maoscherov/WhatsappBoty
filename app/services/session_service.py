"""
Persistencia de sesión de conversación en Redis.

Cierre por inactividad (minuta 2026-07-31): a los 15 minutos sin actividad un
job (main.py) envía el mensaje de cierre y borra la sesión. El TTL de Redis
queda como red de seguridad por si el job no corre.

Si Redis no está disponible, opera en modo in-memory (sin persistencia entre
reinicios del servidor). El bot funciona igual pero pierde el historial si
la instancia se reinicia.
"""

import json
import logging
import time
import redis.asyncio as aioredis
from typing import Optional

logger = logging.getLogger(__name__)

SESSION_TTL = 60 * 60 * 25     # red de seguridad — debe superar las 24hs del link de pago
INACTIVITY_CLOSE = 60 * 15     # inactividad que dispara el cierre con aviso
MAX_HISTORY = 10

_EMPTY_SESSION = lambda: {
    "history": [],
    "pending_sku_id": None,
    "pending_sku_nombre": None,
    "pending_precio": None,
    "pending_cantidad": 1,
    "pending_opciones": [],
    "estado": "idle",
    "tipo_entrega": None,      # "retiro" | "envio"
    "direccion_envio": None,   # dirección de envío elegida
}


class SessionService:
    def __init__(self, redis_url: str):
        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._memory: dict[str, dict] = {}       # fallback in-memory sesiones
        self._redis_ok: Optional[bool] = None    # None = no testeado aún
        self._processed_ids: set[str] = set()    # fallback in-memory dedup

    def _key(self, phone: str) -> str:
        return f"session:{phone}"

    async def _use_redis(self) -> bool:
        if self._redis_ok is None:
            self._redis_ok = await self.ping()
            if not self._redis_ok:
                logger.warning("Redis no disponible — usando sesiones en memoria")
        return self._redis_ok

    async def get(self, phone: str) -> dict:
        if await self._use_redis():
            try:
                raw = await self._redis.get(self._key(phone))
                if raw:
                    return json.loads(raw)
            except Exception:
                self._redis_ok = False
        return self._memory.get(phone, _EMPTY_SESSION())

    async def save(self, phone: str, session: dict):
        session["history"] = session["history"][-MAX_HISTORY:]
        session["_last_activity"] = time.time()
        if await self._use_redis():
            try:
                await self._redis.setex(self._key(phone), SESSION_TTL, json.dumps(session))
                return
            except Exception:
                self._redis_ok = False
        self._memory[phone] = session

    async def add_message(self, phone: str, role: str, content: str):
        session = await self.get(phone)
        session["history"].append({"role": role, "content": content, "ts": time.time()})
        await self.save(phone, session)

    async def set_pending(self, phone: str, sku_id: str, sku_nombre: str, precio: float,
                          cantidad: int = 1, opciones: list | None = None):
        session = await self.get(phone)
        cant = max(1, int(cantidad))

        # Un pending nuevo arranca el carrito de cero (reemplaza, no suma) —
        # SALVO que sea el MISMO producto que ya era el principal: el modelo a
        # veces re-selecciona el índice del producto vigente al confirmar, y
        # resetear ahí pisó un carrito de 3 ítems y el link cobró uno solo
        # (caso real 31/8: $51.214 en vez de $104.855).
        items_previos = session.get("pending_items") or []
        if sku_id == session.get("pending_sku_id") and len(items_previos) > 1:
            items = [dict(i, cantidad=cant, precio=precio) if i.get("sku_id") == sku_id else i
                     for i in items_previos]
        else:
            items = [{"sku_id": sku_id, "nombre": sku_nombre,
                      "precio": precio, "cantidad": cant}]

        session.update({
            "pending_sku_id": sku_id,
            "pending_sku_nombre": sku_nombre,
            "pending_precio": precio,
            "pending_cantidad": cant,
            "pending_opciones": opciones if opciones is not None else session.get("pending_opciones", []),
            "pending_items": items,
            "estado": "esperando_confirmacion",
        })
        # Elegir un producto concreto levanta la espera de elección de opciones
        session.pop("_espera_eleccion", None)
        await self.save(phone, session)

    async def agregar_item(self, phone: str, sku_id: str, sku_nombre: str, precio: float,
                           cantidad: int = 1) -> list[dict]:
        """
        Suma un producto al pedido en curso (carrito). El primero del carrito
        sigue siendo el pending "principal" por compatibilidad; el link de pago
        se arma con la lista completa.
        """
        session = await self.get(phone)
        items = session.get("pending_items") or []
        if not items and session.get("pending_sku_id"):
            items = [{"sku_id": session["pending_sku_id"],
                      "nombre": session["pending_sku_nombre"],
                      "precio": session["pending_precio"],
                      "cantidad": session.get("pending_cantidad", 1)}]
        items.append({"sku_id": sku_id, "nombre": sku_nombre,
                      "precio": precio, "cantidad": max(1, int(cantidad))})
        session["pending_items"] = items
        session["estado"] = "esperando_confirmacion"
        await self.save(phone, session)
        return items

    async def guardar_extras(self, phone: str, extras: list[dict]):
        """
        Guarda los productos ADICIONALES que se le mostraron al cliente
        ("Sobre lo demás que me pediste"), para que pueda sumarlos después.

        Antes se mostraban con nombre y precio pero no se guardaban en ningún
        lado: eran texto, no productos. Un "mandame todos" terminaba cobrando
        sólo el producto principal (caso real 27/8).
        """
        session = await self.get(phone)
        session["extras_ofrecidos"] = extras
        await self.save(phone, session)

    async def sumar_extras(self, phone: str) -> list[dict]:
        """
        Suma al pedido todos los adicionales ofrecidos y los limpia (si no, un
        segundo "todos" los cobraría dos veces). Devuelve el carrito completo.
        """
        session = await self.get(phone)
        extras = session.get("extras_ofrecidos") or []
        if not extras:
            return []
        items = session.get("pending_items") or []
        if not items and session.get("pending_sku_id"):
            items = [{"sku_id": session["pending_sku_id"],
                      "nombre": session["pending_sku_nombre"],
                      "precio": session["pending_precio"],
                      "cantidad": session.get("pending_cantidad", 1)}]
        for e in extras:
            items.append({"sku_id": e["sku_id"], "nombre": e["nombre"],
                          "precio": e["precio"], "cantidad": e.get("cantidad", 1)})
        session["pending_items"] = items
        session["extras_ofrecidos"] = []
        session["estado"] = "esperando_confirmacion"
        await self.save(phone, session)
        return items

    async def clear_pending(self, phone: str):
        session = await self.get(phone)
        session.update({
            "pending_sku_id": None,
            "pending_sku_nombre": None,
            "pending_precio": None,
            "pending_cantidad": 1,
            "pending_opciones": [],
            "pending_items": [],
            "extras_ofrecidos": [],
            "estado": "idle",
            "tipo_entrega": None,
            "direccion_envio": None,
        })
        await self.save(phone, session)

    async def set_entrega(self, phone: str, tipo: str, direccion: str | None = None):
        """Guarda el modo de entrega elegido (retiro/envío) y la dirección si aplica."""
        session = await self.get(phone)
        session["tipo_entrega"] = tipo
        session["direccion_envio"] = direccion
        await self.save(phone, session)

    async def set_estado(self, phone: str, estado: str, motivo: str | None = None):
        session = await self.get(phone)
        if estado == "operador" and session.get("estado") != "operador":
            # Derivación nueva: timestamp para la alerta del backoffice y el
            # aviso automático si el humano demora en responder.
            session["derivada_at"] = time.time()
            session["derivada_motivo"] = motivo
            session.pop("_handoff_avisado", None)
        session["estado"] = estado
        await self.save(phone, session)

    async def delete(self, phone: str):
        """Cierra la conversación: elimina la sesión (sale de la lista de activas)."""
        if await self._use_redis():
            try:
                await self._redis.delete(self._key(phone))
                return
            except Exception:
                self._redis_ok = False
        self._memory.pop(phone, None)

    async def delete_all(self) -> int:
        """Elimina todas las sesiones activas (útil para resetear antes de una demo)."""
        n = 0
        if await self._use_redis():
            try:
                keys = await self._redis.keys("session:*")
                for key in keys:
                    await self._redis.delete(key)
                    n += 1
                return n
            except Exception:
                self._redis_ok = False
        n = len(self._memory)
        self._memory.clear()
        return n

    async def is_processed(self, msg_id: str) -> bool:
        """
        Retorna True si el mensaje ya fue procesado (deduplicación de webhooks).
        Usa Redis como primera línea; si falla cae a un set in-memory para que
        los retries de WhatsApp no se cuelen aunque Redis tenga un micro-corte.
        """
        key = f"processed:{msg_id}"
        if await self._use_redis():
            try:
                result = await self._redis.set(key, "1", ex=300, nx=True)
                if result is None:
                    return True   # ya existía en Redis → duplicado
                # Guardado en Redis OK → también marcamos en memoria por si acaso
                self._processed_ids.add(msg_id)
                return False
            except Exception:
                pass  # Redis falló → fallback a memoria

        # Fallback in-memory (instancia única: Railway, Render, etc.)
        if msg_id in self._processed_ids:
            return True
        self._processed_ids.add(msg_id)
        # Evitar leak: si crece demasiado, descartar la mitad más antigua
        if len(self._processed_ids) > 2000:
            self._processed_ids = set(list(self._processed_ids)[1000:])
        return False

    async def list_all(self) -> list[tuple[str, dict]]:
        """Devuelve todas las sesiones activas como lista de (phone, session)."""
        if await self._use_redis():
            try:
                keys = await self._redis.keys("session:*")
                result = []
                for key in keys:
                    raw = await self._redis.get(key)
                    if raw:
                        phone = key.removeprefix("session:")
                        result.append((phone, json.loads(raw)))
                return sorted(result, key=lambda x: x[0])
            except Exception:
                pass
        return list(self._memory.items())

    async def inactivas(self, threshold_secs: int = INACTIVITY_CLOSE,
                        threshold_pago_secs: int | None = None) -> list[tuple[str, dict]]:
        """
        Sesiones sin actividad hace más de `threshold_secs`, candidatas al
        cierre con aviso. Excluye las derivadas a operador (las maneja el
        humano; a esas las limpia el TTL de Redis).

        `threshold_pago_secs`: umbral distinto (mayor) para sesiones en
        estado esperando_pago — el link de pago vale 24hs y no corresponde
        "cerrar" a los 15 minutos a alguien que todavía puede pagar.

        Las sesiones anteriores al deploy (sin _last_activity) se estampan
        ahora en vez de cerrarse, para no mandar avisos masivos al deployar.
        """
        out = []
        now = time.time()
        for phone, session in await self.list_all():
            if session.get("estado") == "operador":
                continue
            last = session.get("_last_activity")
            if last is None:
                await self.save(phone, session)   # estampa _last_activity
                continue
            umbral = threshold_secs
            if session.get("estado") == "esperando_pago" and threshold_pago_secs:
                umbral = threshold_pago_secs
            if now - float(last) >= umbral:
                out.append((phone, session))
        return out

    async def derivadas_para_aviso(self, threshold_secs: int) -> list[str]:
        """
        Teléfonos derivados a operador hace más de `threshold_secs` que todavía
        no recibieron el aviso de demora. No detectamos la respuesta del humano
        (contesta desde la app de WhatsApp), así que es un único aviso por
        derivación — configurable/apagable desde el backoffice.
        """
        out = []
        now = time.time()
        for phone, session in await self.list_all():
            if session.get("estado") != "operador" or session.get("_handoff_avisado"):
                continue
            derivada = session.get("derivada_at")
            if derivada and now - float(derivada) >= threshold_secs:
                out.append(phone)
        return out

    async def cierre_ya_avisado(self, phone: str, ttl: int = 3 * 3600) -> bool:
        """
        True si ya se le mandó el aviso de cierre hace poco (y lo marca si no).

        Protege contra el aviso repetido: si la sesión reaparece —dos instancias
        del job durante un deploy, o un Redis compartido entre entornos— el
        cliente recibiría el mismo mensaje varias veces.
        """
        key = f"cierre_avisado:{phone}"
        if await self._use_redis():
            try:
                creado = await self._redis.set(key, "1", ex=ttl, nx=True)
                return creado is None      # ya existía → alguien lo avisó antes
            except Exception:
                pass
        ya = key in self._processed_ids
        self._processed_ids.add(key)
        return ya

    async def derivadas_sin_atender(self, threshold_secs: int) -> list[str]:
        """
        Conversaciones derivadas hace más de `threshold_secs` que **nadie tomó**
        (sin agente asignado). Se usan para devolverlas al bot: si no hay quien
        atienda, es mejor que el bot siga ayudando a que el cliente quede mudo.
        """
        out = []
        now = time.time()
        for phone, session in await self.list_all():
            if session.get("estado") != "operador" or session.get("agente"):
                continue
            derivada = session.get("derivada_at")
            if derivada and now - float(derivada) >= threshold_secs:
                out.append(phone)
        return out

    async def liberar(self, phone: str):
        """
        Devuelve la conversación al bot (sale del modo operador).

        También reinicia los contadores de la charla: si no, el reloj de
        "conversación larga" sigue corriendo desde el inicio original y el bot
        vuelve a derivar en el primer mensaje, entrando en un bucle.
        """
        session = await self.get(phone)
        session["estado"] = "idle"
        for k in ("derivada_at", "derivada_motivo", "_handoff_avisado", "agente",
                  "_conv_inicio", "_negativos", "derivacion_ofrecida",
                  "extras_ofrecidos"):
            session.pop(k, None)
        await self.save(phone, session)

    async def marcar_handoff_avisado(self, phone: str):
        session = await self.get(phone)
        session["_handoff_avisado"] = True
        await self.save(phone, session)

    async def ping(self) -> bool:
        try:
            return await self._redis.ping()
        except Exception:
            return False


_instance: Optional[SessionService] = None


def get_session_service(redis_url: str = "redis://localhost:6379") -> SessionService:
    global _instance
    if _instance is None:
        _instance = SessionService(redis_url)
    return _instance
