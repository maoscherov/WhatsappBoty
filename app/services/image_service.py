"""
Analiza imágenes (recetas, credenciales, fotos de producto) con visión.

Soporta Anthropic (Claude) y OpenAI (gpt-4o), con el mismo esquema de fallback
que intent_service: si el proveedor primario falla (ej. sin crédito), cae al otro.

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
import openai

logger = logging.getLogger(__name__)

_VISION_MODELS = {"anthropic": "claude-haiku-4-5-20251001", "openai": "gpt-4o"}

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


_CAMPOS_RECETA = ("paciente", "dni", "obra_social", "nro_afiliado", "plan",
                  "droga", "producto_sugerido", "presentacion", "diagnostico",
                  "nro_receta", "vigencia", "medico", "matricula")

_PROMPT_RECETA = (
    "Esta imagen es una receta médica argentina (impresa, electrónica o "
    "manuscrita). Extraé los datos que se lean con claridad.\n"
    "Respondé SOLO con un JSON (sin texto extra) con este esquema — dejá en "
    "\"\" todo campo que no se lea o no figure, NUNCA inventes un dato:\n"
    "{" + ", ".join(f'"{c}": ""' for c in _CAMPOS_RECETA) + "}\n\n"
    "- paciente: nombre y apellido del paciente.\n"
    "- dni: solo los dígitos, sin puntos.\n"
    "- droga: el principio activo prescripto (puede haber más de uno, separados "
    "por coma).\n"
    "- producto_sugerido: la marca comercial si figura (ej: 'Yasminelle').\n"
    "- presentacion: forma y cantidad (ej: 'comp.rec.x 28').\n"
    "- vigencia: la fecha de inicio de vigencia o de confección."
)


def _parse_receta(raw: str) -> Optional[dict]:
    """JSON del OCR → dict con TODAS las claves (vacías si faltan), o None."""
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    return {c: str(data.get(c) or "").strip() for c in _CAMPOS_RECETA}


class ImageService:
    def __init__(self, anthropic_key: str, openai_key: str = "", provider: str = "anthropic"):
        self._provider = provider if provider in ("anthropic", "openai") else "anthropic"
        self._anthropic = anthropic.AsyncAnthropic(api_key=anthropic_key) if anthropic_key else None
        self._openai = openai.AsyncOpenAI(api_key=openai_key) if openai_key else None

    async def analizar(self, image_bytes: bytes, media_type: str = "image/jpeg") -> dict:
        """
        Clasifica la imagen. Devuelve {"tipo": str, "items": str}.
        Prueba el proveedor primario y cae al otro si falla. Ante error total,
        devuelve {"tipo": "otro", "items": ""}.
        """
        b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
        orden = [self._provider, "anthropic" if self._provider == "openai" else "openai"]
        for prov in orden:
            if prov == "anthropic" and not self._anthropic:
                continue
            if prov == "openai" and not self._openai:
                continue
            try:
                raw = (await self._openai_vision(b64, media_type)) if prov == "openai" \
                    else (await self._anthropic_vision(b64, media_type))
                parsed = self._parse(raw)
                if parsed:
                    return parsed
            except Exception as e:
                logger.warning(f"💥 Visión {prov} falló [{type(e).__name__}]: {str(e)[:200]} — probando fallback")
                continue
        return {"tipo": "otro", "items": ""}

    async def _anthropic_vision(self, b64: str, media_type: str) -> str:
        response = await self._anthropic.messages.create(
            model=_VISION_MODELS["anthropic"],
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": _PROMPT},
                ],
            }],
        )
        return response.content[0].text if getattr(response, "content", None) else ""

    async def _openai_vision(self, b64: str, media_type: str) -> str:
        resp = await self._openai.chat.completions.create(
            model=_VISION_MODELS["openai"],
            max_tokens=256,
            response_format={"type": "json_object"},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": _PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}},
                ],
            }],
        )
        return resp.choices[0].message.content or ""

    @staticmethod
    def _parse(raw: str) -> Optional[dict]:
        match = re.search(r"\{.*\}", raw or "", re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError:
            return None
        tipo = str(data.get("tipo", "otro")).lower().strip()
        if tipo not in ("receta", "credencial", "producto", "otro"):
            tipo = "otro"
        return {"tipo": tipo, "items": str(data.get("items", "")).strip()}

    async def extraer_medicamentos(
        self, image_bytes: bytes, media_type: str = "image/jpeg"
    ) -> Optional[str]:
        """Compatibilidad: devuelve los medicamentos como texto, o None."""
        res = await self.analizar(image_bytes, media_type)
        return res["items"] or None

    async def leer_receta(self, image_bytes: bytes,
                          media_type: str = "image/jpeg") -> Optional[dict]:
        """
        OCR estructurado de una receta (activable con receta_ocr_enabled):
        extrae paciente, DNI, medicamento, obra social, etc. para que el
        operador tenga todo en el backoffice al recibir la derivación.

        Best-effort: ante cualquier fallo devuelve None y la derivación sigue
        como siempre. Estos datos van SOLO al backoffice, nunca al prompt del
        modelo conversacional (misma política que el DNI del padrón).
        """
        b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
        orden = [self._provider, "anthropic" if self._provider == "openai" else "openai"]
        for prov in orden:
            cliente = self._anthropic if prov == "anthropic" else self._openai
            if not cliente:
                continue
            try:
                if prov == "anthropic":
                    r = await self._anthropic.messages.create(
                        model=_VISION_MODELS["anthropic"], max_tokens=512,
                        messages=[{"role": "user", "content": [
                            {"type": "image", "source": {"type": "base64",
                             "media_type": media_type, "data": b64}},
                            {"type": "text", "text": _PROMPT_RECETA},
                        ]}],
                    )
                    raw = r.content[0].text if getattr(r, "content", None) else ""
                else:
                    r = await self._openai.chat.completions.create(
                        model=_VISION_MODELS["openai"], max_tokens=512,
                        response_format={"type": "json_object"},
                        messages=[{"role": "user", "content": [
                            {"type": "text", "text": _PROMPT_RECETA},
                            {"type": "image_url",
                             "image_url": {"url": f"data:{media_type};base64,{b64}"}},
                        ]}],
                    )
                    raw = r.choices[0].message.content or ""
                parsed = _parse_receta(raw)
                if parsed:
                    return parsed
            except Exception as e:
                logger.warning(f"💥 OCR receta {prov} falló [{type(e).__name__}]: "
                               f"{str(e)[:200]} — probando fallback")
        return None


_instance: Optional[ImageService] = None


def get_image_service(anthropic_key: str, openai_key: str = "", provider: str = "anthropic") -> ImageService:
    global _instance
    if _instance is None:
        _instance = ImageService(anthropic_key, openai_key, provider)
    return _instance
