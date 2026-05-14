"""
Extrae medicamentos de imágenes (recetas, fotos de productos) usando Claude vision.
"""

import base64
import logging
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)


class ImageService:
    def __init__(self, api_key: str):
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def extraer_medicamentos(
        self, image_bytes: bytes, media_type: str = "image/jpeg"
    ) -> Optional[str]:
        """
        Recibe bytes de imagen y retorna los medicamentos encontrados como texto,
        listo para pasar al intent_service.
        Retorna None si no hay medicamentos reconocibles.
        """
        b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
        try:
            response = await self._client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": b64},
                        },
                        {
                            "type": "text",
                            "text": (
                                "Esta imagen puede ser una receta médica o foto de un medicamento. "
                                "Extraé los nombres de los medicamentos o productos de salud que aparecen. "
                                "Respondé SOLO con los nombres separados por coma, sin explicaciones ni texto extra. "
                                "Ejemplo: 'Ibuprofeno 600, Omeprazol 20mg, Amoxicilina 500mg'. "
                                "Si no encontrás medicamentos, respondé únicamente: sin medicamentos."
                            ),
                        },
                    ],
                }],
            )
            result = response.content[0].text.strip()
            if "sin medicamentos" in result.lower():
                return None
            return result
        except Exception as e:
            logger.error(f"Error procesando imagen con Claude: {e}")
            return None


_instance: Optional[ImageService] = None


def get_image_service(api_key: str) -> ImageService:
    global _instance
    if _instance is None:
        _instance = ImageService(api_key)
    return _instance
