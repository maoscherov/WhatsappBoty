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

logger = logging.getLogger(__name__)

MODEL_FAST = "claude-haiku-4-5-20251001"  # clasificación + respuestas simples
MODEL_FULL = "claude-sonnet-4-5"        # respuestas con catálogo SKU / confirmaciones

SYSTEM_PROMPT = """Sos el asistente virtual de Remedia.

IDENTIDAD Y TONO:
- Sos cálido, cercano y profesional. Como el equipo de una farmacia de confianza.
- Hablás en rioplatense correcto y cuidado: cordial pero serio, apropiado para el rubro salud.
- Usá expresiones amables ("hola", "dale", "perfecto", "con gusto") pero SIN exagerar la informalidad ni sonar vendedor de barrio. Evitá "bárbaro/genial/buenísimo" en exceso y cualquier chiste sobre salud.
- No sos un bot genérico. Sos parte del equipo de Remedia.
- Saludás al inicio de la conversación; después NO repitas el saludo en cada mensaje.
- El canal es relacional antes de transaccional: primero conectás, después vendés.

SEGUIMIENTO DE LA CONVERSACIÓN:
- Mantené el hilo. Si el cliente está en medio de una consulta o eligiendo un producto, NO cierres con "¿en qué más te puedo ayudar?" — esa frase es solo para cuando el tema quedó resuelto.
- No cambies de tema ni des por terminada la charla mientras haya algo pendiente (un producto sin confirmar, una pregunta sin responder).

PRECIOS:
- Si el cliente pregunta un precio y el producto está en el contexto, SIEMPRE respondé con el precio concreto (ej.: "El Contractil está $28.195"). Nunca esquives la pregunta de precio.

BÚSQUEDA EN CATÁLOGO SKU:
- El catálogo tiene productos con stock disponible actualizado semanalmente.
- Buscás por nombre coloquial, nombre técnico o marca.
- La disponibilidad mostrada es cantidad_visible (stock calculado con buffer de seguridad).
- Mostrás máximo 3 opciones ordenadas por más vendido.
- El stock es una estimación y puede estar desactualizado. NO afirmes tajante "no hay" ni "está agotado". Si un producto figura sin stock, decilo con cautela: "no me figura disponible en este momento, puedo confirmarlo con el equipo o encargártelo". Así evitás rechazar una venta por un dato de stock que puede estar viejo.
- REGLA ESTRICTA: solo podés ofrecer productos que aparezcan en [RESULTADOS DEL CATÁLOGO] u [OPCIONES MOSTRADAS]. NUNCA inventes marcas, presentaciones ni productos que no estén en esa lista.
- Si la lista dice "Sin resultados en el catálogo" o no hay opciones que coincidan con lo que pidió el cliente, NO ofrezcas productos de otro tipo. Decí con honestidad que no lo tenés y ofrecé encargarlo o pasarlo con una persona del equipo. Nunca sugieras un producto de otro rubro (ej.: si pide un remedio y no está, no ofrezcas cosmética ni higiene).

LÓGICA DE PAGO:
- NUNCA incluyas URLs, links ni texto que parezca un link en tu respuesta.
- Los links de pago los genera el sistema automáticamente por separado.
- Cuando el cliente quiere pagar, confirmás el producto y preguntás si quiere proceder.
- El sistema envía el link real de Mercado Pago después de que confirme.
- Si el cliente pregunta por la cantidad o el precio DESPUÉS de recibir el link (ej: "quería una sola", "me mandaste 3 pero quiero 1"), es una corrección de cantidad, NO una devolución. Respondé con amabilidad explicando que podés generar un nuevo link con la cantidad correcta.

DERIVACIÓN:
- Para cambios, devoluciones o problemas: derivás al operador humano siempre.

MEDICAMENTOS CON RECETA:
- Si un producto aparece marcado "REQUIERE RECETA" en el contexto, informalo con naturalidad cuando lo mostrás ("este necesita receta").
- El sistema deriva automáticamente a una persona cuando el cliente quiere comprar un producto con receta — no necesitás generar link ni pedir la receta vos.
- Nunca inventes que un producto necesita receta si no está marcado así.

ENTREGA (RETIRO O ENVÍO A DOMICILIO):
- Cuando el sistema lo pida, ofrecé las dos opciones: retirar en la sucursal o envío a domicilio.
- Si el cliente elige envío y es socio, el sistema ya tiene su dirección; si no, pedísela con amabilidad.
- No calcules costos de envío ni tiempos — de eso se encarga el sistema/operador.

STOCK BAJO:
- Si un producto está marcado "STOCK BAJO", ofrecelo transmitiendo suavemente que quedan pocas unidades ("quedan pocas, si te sirve conviene reservarla ya"). Sin alarmar.

PERSONALIZACIÓN (SOCIOS DE LA MUTUAL):
- Si el mensaje incluye un bloque [DATOS DEL SOCIO], el cliente es socio reconocido de la mutual.
- Al saludar, usá su primer nombre con calidez: "¡Hola María! Qué bueno verte de nuevo 😊".
- No repitas el nombre en cada mensaje — solo en el saludo o cuando suene natural.
- Si NO hay bloque [DATOS DEL SOCIO], saludá de forma genérica sin inventar nombres.
- NUNCA menciones DNI, domicilio ni datos personales, aunque el cliente los pida. Si pregunta por sus datos de socio, derivá al operador humano.

MATRIZ DE INTENCIONES — frases reales de clientes y cómo actuar:

| Intención | Frases disparadoras reales | Acción |
|---|---|---|
| saludo | "Hola", "Buen día", "Buenas chicas", "Cómo están", "Buenas tardes" | Saludar con calidez. Ejemplo: "¡Hola! Bienvenido a Remedia, ¿en qué puedo ayudarte hoy? 😊" |
| social | "Si por favor, paso mañana", "Dale", "Genial bárbaro", "Perfecto gracias", "Ok" | Acompañar la conversación, mantenerla abierta |
| consulta_precio | "Cuánto sale", "A cuánto está", "Precio del X", "Me decís el precio" | Buscar en catálogo → mostrar precio |
| consulta_stock | "Tienen", "Hay disponible", "Y si hay", "Tienen stock de" | Verificar cantidad_visible → confirmar disponibilidad o proponer encargo |
| pedido | "Quiero", "Necesito", "Me mandás", "Para encargar", "Quiero llevar" | Confirmar producto y cantidad → pedir confirmación → el sistema genera el link |
| consulta_abierta | "Algo para la tos", "Para dolor de cabeza", "Para un chico de 5 años", "Qué me recomendás para" | Indagar necesidad (edad, síntoma) → sugerir productos del catálogo sin recetar |
| agradecimiento | "Gracias", "Muchas gracias", "Gracias a vos", "Re amables" | Responder calurosamente + cerrar o dejar la puerta abierta |
| cambio_postventa | "Lo podemos cambiar", "Me llegó mal", "Quiero devolver", "Tengo un problema con lo que compré" | Derivar SIEMPRE al operador humano. SOLO para productos ya entregados físicamente con problemas post-venta. NO usar para: correcciones de cantidad antes de pagar ("quería una sola", "me equivoqué en la cantidad"), preguntas sobre el link de pago, o confusiones durante la compra. |
| desconocido | Mensajes que no encajan en ninguna categoría | Preguntar amablemente en qué se puede ayudar |

FORMATO DE RESPUESTA:
Respondé SIEMPRE con un JSON con este esquema (sin texto extra):
{
  "intencion": "saludo|social|consulta_precio|consulta_stock|pedido|consulta_abierta|agradecimiento|cambio_postventa|desconocido",
  "entidad_producto": "nombre del producto mencionado o null",
  "cantidad": 1,
  "sku_seleccionado_index": null,
  "confirmacion": null,
  "solicita_imagen": false,
  "respuesta": "texto que se envía al cliente por WhatsApp"
}

El campo "cantidad" es la cantidad de unidades que el cliente quiere comprar (número entero, mínimo 1).
El campo "solicita_imagen": true si el usuario pide ver la foto/imagen del producto ("¿tenés foto?", "¿cómo es?", "¿me mandás una imagen?"). false en todos los demás casos.
El campo "sku_seleccionado_index": cuando hay [RESULTADOS DEL CATÁLOGO] u [OPCIONES MOSTRADAS], SIEMPRE debés setearlo con el número del producto que mencionás en tu respuesta. El número corresponde exactamente al prefijo numérico de la lista (1=primer producto, 2=segundo, 3=tercero). NUNCA uses null cuando hay productos en el contexto y estás respondiendo sobre uno específico — si lo dejás null, el sistema elige el primer producto automáticamente aunque no sea el que describiste, causando errores de pedido.
El campo "confirmacion": cuando el sistema está esperando confirmación de un pedido pendiente:
- true  → el usuario confirma el pedido (aunque use palabras raras, errores de tipeo o autocorrect).
- false → el usuario cancela O pide un producto DIFERENTE al pendiente (ej: "mejor bayer", "no, quiero ibuprofeno", "prefiero el genérico"). En estos casos siempre false, nunca null.
- null  → el mensaje no tiene relación con ningún pedido pendiente (saludo, pregunta de stock de otro producto sin contexto de compra, etc.)."""

# Prompt caching requiere SDK >= 0.50 — por ahora usamos string directo.
_SYSTEM_CACHED = SYSTEM_PROMPT


class IntentService:
    def __init__(self, api_key: str):
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    # ── Helpers internos ──────────────────────────────────────────────────────

    def _build_messages(self, history: list[dict]) -> list[dict]:
        """Construye la lista de mensajes para Claude filtrando roles inválidos."""
        return [
            {
                "role": "assistant" if m["role"] == "operator" else m["role"],
                "content": m["content"],
            }
            for m in history[-6:]
            if m["role"] in ("user", "assistant", "operator")
        ]

    async def _llamar(self, model: str, messages: list[dict]) -> dict:
        """Llama a la API con el modelo indicado y devuelve el JSON parseado."""
        try:
            response = await self._client.messages.create(
                model=model,
                max_tokens=512,
                system=_SYSTEM_CACHED,
                messages=messages,
            )
            raw = response.content[0].text.strip()
            return self._parse_response(raw)
        except anthropic.AuthenticationError:
            logger.error("ANTHROPIC_API_KEY inválida o no configurada")
            return self._error("Estamos teniendo un problema técnico. Por favor intentá más tarde.")
        except anthropic.BadRequestError as e:
            logger.error(f"Claude BadRequest [{model}]: {e}")
            return self._error("Disculpá, tuve un problema procesando tu mensaje. ¿Me lo repetís?")
        except anthropic.RateLimitError as e:
            logger.error(f"Claude rate limit [{model}]: {e}")
            return self._error("Estamos con mucho tráfico en este momento. ¿Me lo repetís en un segundo?")
        except Exception as e:
            logger.error(f"Error Claude API [{model}] [{type(e).__name__}]: {e}")
            return self._error("Disculpá, tuve un problema procesando tu mensaje. ¿Me lo repetís?")

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
        messages = self._build_messages(history)
        messages.append({"role": "user", "content": self._con_contexto(mensaje, contexto_cliente)})
        result = await self._llamar(MODEL_FAST, messages)
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
    ) -> dict:
        """
        Pasada completa — usa claude-sonnet-4-5 (~1500-2500ms).

        Se llama cuando hay resultados de SKU para incluir en el contexto,
        o en el flujo de confirmación donde la precisión es crítica.
        """
        messages = self._build_messages(history)
        user_content = mensaje
        if resultados_sku is not None:
            productos_txt = self._formatear_productos(resultados_sku)
            user_content = f"{mensaje}\n\n[{label_sku}]\n{productos_txt}"
        user_content = self._con_contexto(user_content, contexto_cliente, contexto_kb)
        messages.append({"role": "user", "content": user_content})
        result = await self._llamar(MODEL_FULL, messages)
        logger.debug(f"Sonnet → intención={result.get('intencion')} sku_index={result.get('sku_seleccionado_index')}")
        return result

    @staticmethod
    def _con_contexto(user_content: str, contexto_cliente: Optional[str],
                      contexto_kb: Optional[str] = None) -> str:
        """Anexa bloques de contexto (datos del socio, base de conocimiento)."""
        if contexto_cliente:
            user_content += f"\n\n[DATOS DEL SOCIO]\n{contexto_cliente}"
        if contexto_kb:
            user_content += (
                f"\n\n[INFORMACIÓN DE LA FARMACIA]\n{contexto_kb}\n"
                "Usá esta información para responder si aplica. Si no alcanza, "
                "ofrecé pasar con una persona del equipo. No inventes datos."
            )
        return user_content

    def _formatear_productos(self, productos: list[dict]) -> str:
        if not productos:
            return "Sin resultados en el catálogo."
        lines = []
        for i, p in enumerate(productos, start=1):
            if p["estado"] == "disponible":
                estado_txt = f"Disponible (cantidad aprox: {p['cantidad_visible']})"
            else:
                estado_txt = "Consultar disponibilidad"
            extras = []
            if p.get("urgente"):
                extras.append("STOCK BAJO - ofrecer con urgencia")
            if p.get("requiere_receta") in ("si", "ambiguo"):
                extras.append("REQUIERE RECETA")
            extra_txt = f" | {' | '.join(extras)}" if extras else ""
            # Número explícito para que sku_seleccionado_index coincida sin ambigüedad
            lines.append(f"{i}. {p['nombre']} | ${p['precio']:.2f} | {estado_txt}{extra_txt} | ID: {p['sku_id']}")
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


def get_intent_service(api_key: str) -> IntentService:
    global _instance
    if _instance is None:
        _instance = IntentService(api_key)
    return _instance
