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

SYSTEM_PROMPT = """Sos el asistente virtual de Farmacia Mutual Independencia. Tu nombre es Farma.

IDENTIDAD Y TONO:
- Sos cálido, cercano y profesional. Como las chicas de la farmacia.
- Usás lenguaje informal rioplatense: "hola", "dale", "bárbaro", "listo", "perfecto".
- No sos un bot genérico. Sos parte del equipo de la farmacia.
- Siempre saludás antes de responder la consulta.

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

FORMATO DE RESPUESTA:
Respondé SIEMPRE con un JSON con este esquema (sin texto extra):
{
  "intencion": "saludo|social|consulta_precio|consulta_stock|pedido|consulta_abierta|agradecimiento|cambio_postventa|desconocido",
  "entidad_producto": "nombre del producto mencionado o null",
  "cantidad": 1,
  "sku_seleccionado_index": null,
  "respuesta": "texto que se envía al cliente por WhatsApp"
}

El campo "cantidad" es la cantidad de unidades que el cliente quiere comprar (número entero, mínimo 1).
El campo "sku_seleccionado_index" es el índice (0-based) del producto elegido cuando el mensaje aparece bajo [OPCIONES MOSTRADAS], o null si no seleccionó ninguno específico."""


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
        messages = list(history[-6:])  # últimos 6 turnos para contexto

        user_content = mensaje
        if resultados_sku is not None:
            productos_txt = self._formatear_productos(resultados_sku)
            user_content = f"{mensaje}\n\n[{label_sku}]\n{productos_txt}"

        messages.append({"role": "user", "content": user_content})

        try:
            response = await self._client.messages.create(
                model="claude-sonnet-4-6",
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
        except Exception as e:
            logger.error(f"Error llamando Claude API: {e}")
            return {
                "intencion": "desconocido",
                "entidad_producto": None,
                "respuesta": "Disculpá, tuve un problema procesando tu mensaje. ¿Me lo repetís?",
            }

    def _formatear_productos(self, productos: list[dict]) -> str:
        if not productos:
            return "Sin resultados en el catálogo."
        lines = []
        for p in productos:
            if p["estado"] == "disponible":
                estado_txt = f"Disponible (cantidad aprox: {p['cantidad_visible']})"
            else:
                estado_txt = "Consultar disponibilidad"
            lines.append(f"- {p['nombre']} | ${p['precio']:.2f} | {estado_txt} | ID: {p['sku_id']}")
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
