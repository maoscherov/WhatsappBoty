"""
Analiza imágenes (recetas, credenciales, fotos de producto) con Claude vision.

analizar() clasifica el tipo de imagen para que el webhook decida:
  - receta / credencial  → derivar a una persona (no vender automáticamente)
  - producto             → seguir el flujo normal con el nombre extraído
  - otro                 → pedir que lo escriba
"""

import base64
import json
import logging
import re
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)

_VISION_MODEL = "claude-haiku-4-5-20251001"

_PROMPT = (
    "Analizá esta imagen enviada a una farmacia por WhatsApp y clasificala.\n"
    "Respondé SOLO con un JSON (sin texto extra) con este esquema:\n"
    '{"tipo": "receta|credencial|producto|otro", "items": "nombres separados por coma o vacío"}\n\n'
    "- receta: es una receta o prescripción médica (manuscrita o impresa, con indicaciones).\n"
    "- credencial: es una credencial/carnet de obra social o prepaga (PAMI, IOMA, etc.).\n"
    "- producto: es la foto de un medicamento o producto de salud. Poné su nombre en items.\n"
    "- otro: cualquier otra cosa que no encaje.\n"
    "En items va SOLO cuando hay productos identificables (ej: 'Ibuprofeno 600, Omeprazol 20mg'); "
    "si no, dejalo vacío."
)


class ImageService:
    def __init__(self, api_key: str):
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def analizar(self, image_bytes: bytes, media_type: str = "image/jpeg") -> dict:
        """
        Clasifica la imagen. Devuelve {"tipo": str, "items": str}.
        Ante error, devuelve {"tipo": "otro", "items": ""}.
        """
        b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
        try:
            response = await self._client.messages.create(
                model=_VISION_MODEL,
                max_tokens=256,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                        {"type": "text", "text": _PROMPT},
                    ],
                }],
            )
            raw = response.content[0].text.strip()
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                data = json.loads(match.group())
                tipo = str(data.get("tipo", "otro")).lower().strip()
                if tipo not in ("receta", "credencial", "producto", "otro"):
                    tipo = "otro"
                return {"tipo": tipo, "items": str(data.get("items", "")).strip()}
        except Exception as e:
            logger.error(f"Error procesando imagen con Claude: {e}")
        return {"tipo": "otro", "items": ""}

    async def extraer_medicamentos(
        self, image_bytes: bytes, media_type: str = "image/jpeg"
    ) -> Optional[str]:
        """Compatibilidad: devuelve los medicamentos como texto, o None."""
        res = await self.analizar(image_bytes, media_type)
        return res["items"] or None


_instance: Optional[ImageService] = None


def get_image_service(api_key: str) -> ImageService:
    global _instance
    if _instance is None:
        _instance = ImageService(api_key)
    return _instance
