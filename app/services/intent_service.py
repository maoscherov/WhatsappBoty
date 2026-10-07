"""
Clasificador de intenciones y generador de respuestas usando Claude API.

Dos modelos para optimizar latencia:
  - procesar_rapido() → claude-haiku-3-5  (~400-600ms)
      Clasifica intención, extrae entidad, genera respuesta para casos simples.
      Se usa siempre como Claude 1 (antes de buscar SKU).
  - procesar()        → claude-sonnet-4-5 (~1500-2500ms)
      Genera respuesta final cuando hay resultados del catálogo (Claude 2).
      También se usa en el flujo de confirmación donde la precisión es crítica.

Prompt caching activado en ambos: el system prompt se cachea 5 minutos en
los servidores de Anthropic → ahorra ~200-400ms por llamado repetido.
"""

import json
import logging
import re
from typing import Optional

import anthropic
import openai

from app.services.perfil import get_perfil

# El prompt de farmacia vive en prompts.py, armado por bloques. Se re-exporta
# acá: tests/test_logic.py y otros lo importan de este módulo.
from app.services.prompts import SYSTEM_PROMPT  # noqa: F401

logger = logging.getLogger(__name__)

# Modelos por proveedor y "tier" (fast = clasificación/simple, full = catálogo/confirmación)
_MODELS = {
    "anthropic": {"fast": "claude-haiku-4-5-20251001", "full": "claude-sonnet-4-5"},
    "openai":    {"fast": "gpt-4o-mini",               "full": "gpt-4o"},
}

# Compatibilidad con imports existentes (ej. /bo/diag/claude)
MODEL_FAST = _MODELS["anthropic"]["fast"]
MODEL_FULL = _MODELS["anthropic"]["full"]

# Prompt caching requiere SDK >= 0.50 — por ahora usamos string directo.
_SYSTEM_CACHED = SYSTEM_PROMPT


class IntentService:
    def __init__(self, anthropic_key: str, openai_key: str = "", provider: str = "anthropic"):
        self._provider = provider if provider in ("anthropic", "openai") else "anthropic"
        self._anthropic = anthropic.AsyncAnthropic(api_key=anthropic_key) if anthropic_key else None
        self._openai = openai.AsyncOpenAI(api_key=openai_key) if openai_key else None
        logger.info(f"IntentService: perfil '{get_perfil().clave}', proveedor primario '{self._provider}' "
                    f"(anthropic={'ok' if self._anthropic else 'no'}, openai={'ok' if self._openai else 'no'})")

    def _system_prompt(self) -> str:
        """Prompt del perfil de rubro activo. Se lee en cada llamada (nunca se
        guarda en la instancia): el singleton no queda con un perfil viejo."""
        return get_perfil().system_prompt

    # ── Helpers internos ──────────────────────────────────────────────────────

    def _build_messages(self, history: list[dict]) -> list[dict]:
        """
        Construye la lista de mensajes para Claude respetando las reglas de la API:
        - roles válidos (operator → assistant),
        - se saltean las referencias de imagen "📷 ..." (solo para el backoffice),
        - se MERGEAN mensajes consecutivos del mismo rol (la API exige alternancia),
        - se descartan mensajes assistant iniciales (debe empezar con user).
        """
        msgs: list[dict] = []
        for m in history[-8:]:
            if m.get("role") not in ("user", "assistant", "operator"):
                continue
            content = (m.get("content") or "").strip()
            if not content or content.startswith("📷"):
                continue
            role = "assistant" if m["role"] == "operator" else m["role"]
            if msgs and msgs[-1]["role"] == role:
                msgs[-1]["content"] += "\n" + content   # merge consecutivo
            else:
                msgs.append({"role": role, "content": content})
        while msgs and msgs[0]["role"] == "assistant":
            msgs.pop(0)   # la conversación con la API debe arrancar en user
        return msgs

    def _armar(self, history: list[dict], user_content: str) -> list[dict]:
        """Historial + mensaje actual, garantizando alternancia y que termina en user."""
        msgs = self._build_messages(history)
        if msgs and msgs[-1]["role"] == "user":
            msgs[-1]["content"] += "\n" + user_content   # merge para no repetir rol
        else:
            msgs.append({"role": "user", "content": user_content})
        return msgs

    async def _llamar(self, tier: str, messages: list[dict]) -> dict:
        """
        Llama al LLM y devuelve el JSON parseado. Usa el proveedor primario y,
        si falla (ej. Anthropic sin crédito), cae automáticamente al otro.
        """
        orden = [self._provider, "anthropic" if self._provider == "openai" else "openai"]
        for prov in orden:
            if prov == "anthropic" and not self._anthropic:
                continue
            if prov == "openai" and not self._openai:
                continue
            try:
                if prov == "openai":
                    return await self._call_openai(tier, messages)
                return await self._call_anthropic(tier, messages)
            except Exception as e:
                logger.warning(f"💥 LLM {prov} [{tier}] falló [{type(e).__name__}]: {str(e)[:200]} — probando fallback")
                continue
        return self._error("Disculpá, tuve un problema procesando tu mensaje. ¿Me lo repetís?")

    async def _call_anthropic(self, tier: str, messages: list[dict]) -> dict:
        model = _MODELS["anthropic"][tier]
        # Prefill (fuerza JSON) y, si falla, sin prefill como red de seguridad.
        for con_prefill in (True, False):
            try:
                msgs = messages + ([{"role": "assistant", "content": "{"}] if con_prefill else [])
                response = await self._anthropic.messages.create(
                    model=model, max_tokens=512, system=self._system_prompt(), messages=msgs,
                )
                text = response.content[0].text if getattr(response, "content", None) else ""
                return self._parse_response(("{" + text) if con_prefill else text)
            except Exception:
                if con_prefill:
                    continue
                raise   # que _llamar decida el fallback

    async def _call_openai(self, tier: str, messages: list[dict]) -> dict:
        model = _MODELS["openai"][tier]
        # JSON mode: OpenAI garantiza JSON válido (el prompt ya menciona JSON).
        resp = await self._openai.chat.completions.create(
            model=model, max_tokens=512, temperature=0.3,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": self._system_prompt()}] + messages,
        )
        return self._parse_response(resp.choices[0].message.content or "")

    @staticmethod
    def _error(msg: str) -> dict:
        return {"intencion": "desconocido", "entidad_producto": None, "respuesta": msg}

    # ── API pública ───────────────────────────────────────────────────────────

    async def procesar_rapido(
        self,
        mensaje: str,
        history: list[dict],
        contexto_cliente: Optional[str] = None,
    ) -> dict:
        """
        Primera pasada rápida — usa MODEL_FAST (Haiku, ~400-600ms).

        Clasifica intención + extrae entidad + genera respuesta.
        Para intenciones simples (saludo, social, agradecimiento, desconocido)
        esta respuesta se usa directamente sin un segundo llamado.
        Para intenciones con SKU el webhook descarta la respuesta y llama
        a procesar() con los resultados del catálogo.
        """
        messages = self._armar(history, self._con_contexto(mensaje, contexto_cliente))
        result = await self._llamar("fast", messages)
        logger.debug(f"Haiku → intención={result.get('intencion')} entidad={result.get('entidad_producto')}")
        return result

    async def procesar(
        self,
        mensaje: str,
        history: list[dict],
        resultados_sku: Optional[list[dict]] = None,
        label_sku: str = "RESULTADOS DEL CATÁLOGO",
        contexto_cliente: Optional[str] = None,
        contexto_kb: Optional[str] = None,
        situacion: Optional[str] = None,
    ) -> dict:
        """
        Pasada completa — usa claude-sonnet-4-5 (~1500-2500ms).

        Se llama cuando hay resultados de SKU para incluir en el contexto,
        o en el flujo de confirmación donde la precisión es crítica.

        `situacion`: en qué punto del flujo está la conversación, para que la
        respuesta atienda la consulta sin sacar al cliente de ese paso.
        """
        user_content = mensaje
        if resultados_sku is not None:
            productos_txt = self._formatear_productos(resultados_sku)
            user_content = f"{mensaje}\n\n[{label_sku}]\n{productos_txt}"
        user_content = self._con_contexto(user_content, contexto_cliente, contexto_kb)
        if situacion:
            user_content += f"\n\n[SITUACIÓN DEL FLUJO]\n{situacion}"
        messages = self._armar(history, user_content)
        result = await self._llamar("full", messages)
        logger.debug(f"Sonnet → intención={result.get('intencion')} sku_index={result.get('sku_seleccionado_index')}")
        return result

    @staticmethod
    def _con_contexto(user_content: str, contexto_cliente: Optional[str],
                      contexto_kb: Optional[str] = None) -> str:
        """Anexa bloques de contexto (datos del socio, base de conocimiento).
        El de socio solo con la capacidad `socios`; la KB va con el rótulo del perfil."""
        p = get_perfil()
        if contexto_cliente and p.socios:
            user_content += f"\n\n[DATOS DEL SOCIO]\n{contexto_cliente}"
        if contexto_kb:
            user_content += (
                f"\n\n[{p.rotulo_kb}]\n{contexto_kb}\n"
                "Usá esta información para responder si aplica. Si no alcanza, "
                "ofrecé pasar con una persona del equipo. No inventes datos."
            )
        return user_content

    def _formatear_productos(self, productos: list[dict]) -> str:
        if not productos:
            return "Sin resultados en el catálogo."
        recetas = get_perfil().recetas
        lines = []
        for i, p in enumerate(productos, start=1):
            if p.get("sin_stock"):
                estado_txt = "SIN STOCK - no ofrecer para comprar, sugerir alternativas disponibles"
            elif p["estado"] == "disponible":
                estado_txt = f"Disponible (cantidad aprox: {p['cantidad_visible']})"
            else:
                estado_txt = "Consultar disponibilidad"
            extras = []
            if p.get("urgente"):
                extras.append("STOCK BAJO - ofrecer con urgencia")
            if recetas and p.get("requiere_receta") in ("si", "ambiguo"):
                extras.append("REQUIERE RECETA")
            extra_txt = f" | {' | '.join(extras)}" if extras else ""
            # Precio viejo del ERP: no se informa (lo confirma el equipo).
            precio_txt = ("PRECIO A CONFIRMAR - no informar precio, ofrecer consultarlo con el equipo"
                          if p.get("precio_dudoso") else f"${p['precio']:.2f}")
            # Número explícito para que sku_seleccionado_index coincida sin ambigüedad
            lines.append(f"{i}. {p['nombre']} | {precio_txt} | {estado_txt}{extra_txt} | ID: {p['sku_id']}")
        return "\n".join(lines)

    @staticmethod
    def _parse_response(raw: str) -> dict:
        try:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                return json.loads(match.group())
        except (json.JSONDecodeError, AttributeError):
            pass
        return {
            "intencion": "desconocido",
            "entidad_producto": None,
            "respuesta": "Disculpá, no entendí bien. ¿Me podés repetir en qué te puedo ayudar?",
        }


_instance: Optional[IntentService] = None


def get_intent_service(anthropic_key: str, openai_key: str = "",
                       provider: str = "anthropic") -> IntentService:
    global _instance
    if _instance is None:
        _instance = IntentService(anthropic_key, openai_key, provider)
    return _instance
