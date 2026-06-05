"""
Clasificador de intenciones y generador de respuestas usando Claude API.

Intenciones reconocidas (según documento):
  saludo | social | consulta_precio | consulta_stock | pedido |
  consulta_abierta | agradecimiento | cambio_postventa | desconocido

Claude hace dos cosas en un único llamado:
  1. Clasifica la intención.
  2. Extrae la entidad (nombre del producto si aplica).
  3. Genera la respuesta del bot.
"""

import json
import logging
import re
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Sos el asistente virtual de Remedia.

IDENTIDAD Y TONO:
- Sos cálido, cercano y profesional. Como el equipo de Remedia.
- Usás lenguaje informal rioplatense: "hola", "dale", "bárbaro", "listo", "perfecto".
- No sos un bot genérico. Sos parte del equipo de Remedia.
- Siempre saludás antes de responder la consulta.
- El canal es relacional antes de transaccional: primero conectás, después vendés.

BÚSQUEDA EN CATÁLOGO SKU:
- El catálogo tiene productos con stock disponible actualizado semanalmente.
- Buscás por nombre coloquial, nombre técnico o marca.
- La disponibilidad mostrada es cantidad_visible (stock calculado con buffer de seguridad).
- Mostrás máximo 3 opciones ordenadas por más vendido.
- Si cantidad_visible = 0: "No tenemos en este momento, ¿lo encargamos?"

LÓGICA DE PAGO:
- NUNCA incluyas URLs, links ni texto que parezca un link en tu respuesta.
- Los links de pago los genera el sistema automáticamente por separado.
- Cuando el cliente quiere pagar, confirmás el producto y preguntás si quiere proceder.
- El sistema envía el link real de Mercado Pago después de que confirme.

DERIVACIÓN:
- Para cambios, devoluciones o problemas: derivás al operador humano siempre.

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
| cambio_postventa | "Lo podemos cambiar", "Tengo un problema", "Me llegó mal", "Quiero devolver" | Derivar SIEMPRE al operador humano, no intentar resolver |
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


class IntentService:
    def __init__(self, api_key: str):
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def procesar(
        self,
        mensaje: str,
        history: list[dict],
        resultados_sku: Optional[list[dict]] = None,
        label_sku: str = "RESULTADOS DEL CATÁLOGO",
    ) -> dict:
        """
        Clasifica intención y genera respuesta.
        Si resultados_sku está presente, se los inyectamos al contexto.
        """
        # Claude solo acepta roles "user" y "assistant".
        # Los mensajes de operador se convierten a "assistant" para mantener contexto.
        messages = [
            {"role": "assistant" if m["role"] == "operator" else m["role"],
             "content": m["content"]}
            for m in history[-6:]
            if m["role"] in ("user", "assistant", "operator")
        ]

        user_content = mensaje
        if resultados_sku is not None:
            productos_txt = self._formatear_productos(resultados_sku)
            user_content = f"{mensaje}\n\n[{label_sku}]\n{productos_txt}"

        messages.append({"role": "user", "content": user_content})

        try:
            response = await self._client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=512,
                system=SYSTEM_PROMPT,
                messages=messages,
            )
            raw = response.content[0].text.strip()
            return self._parse_response(raw)
        except anthropic.AuthenticationError:
            logger.error("ANTHROPIC_API_KEY inválida o no configurada")
            return {
                "intencion": "desconocido",
                "entidad_producto": None,
                "respuesta": "Estamos teniendo un problema técnico. Por favor intentá más tarde.",
            }
        except anthropic.BadRequestError as e:
            logger.error(f"Claude BadRequest (model/prompt inválido): {e}")
            return {
                "intencion": "desconocido",
                "entidad_producto": None,
                "respuesta": "Disculpá, tuve un problema procesando tu mensaje. ¿Me lo repetís?",
            }
        except anthropic.RateLimitError as e:
            logger.error(f"Claude rate limit alcanzado: {e}")
            return {
                "intencion": "desconocido",
                "entidad_producto": None,
                "respuesta": "Estamos con mucho tráfico en este momento. ¿Me lo repetís en un segundo?",
            }
        except Exception as e:
            logger.error(f"Error llamando Claude API [{type(e).__name__}]: {e}")
            return {
                "intencion": "desconocido",
                "entidad_producto": None,
                "respuesta": "Disculpá, tuve un problema procesando tu mensaje. ¿Me lo repetís?",
            }

    def _formatear_productos(self, productos: list[dict]) -> str:
        if not productos:
            return "Sin resultados en el catálogo."
        lines = []
        for i, p in enumerate(productos, start=1):
            if p["estado"] == "disponible":
                estado_txt = f"Disponible (cantidad aprox: {p['cantidad_visible']})"
            else:
                estado_txt = "Consultar disponibilidad"
            # Número explícito para que sku_seleccionado_index coincida sin ambigüedad
            lines.append(f"{i}. {p['nombre']} | ${p['precio']:.2f} | {estado_txt} | ID: {p['sku_id']}")
        return "\n".join(lines)

    @staticmethod
    def _parse_response(raw: str) -> dict:
        try:
            # Claude a veces envuelve el JSON en ```json ... ```
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
