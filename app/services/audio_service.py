"""
Transcripción de audios de WhatsApp.

Soporta dos providers configurables vía AUDIO_PROVIDER:
  - "groq"   → Groq Whisper (large-v3-turbo), gratis hasta 28hs/día, latencia ~1s
  - "openai" → OpenAI Whisper (whisper-1), ~$0.006/min

Groq usa la misma API compatible con OpenAI, solo cambia la base_url y el modelo.
"""

import io
import logging
from typing import Optional

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

PROVIDERS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "whisper-large-v3-turbo",
    },
    "openai": {
        "base_url": None,   # usa el default de OpenAI
        "model": "whisper-1",
    },
}


class AudioService:
    def __init__(self, api_key: str, provider: str = "groq"):
        cfg = PROVIDERS.get(provider, PROVIDERS["groq"])
        kwargs = {"api_key": api_key}
        if cfg["base_url"]:
            kwargs["base_url"] = cfg["base_url"]
        self._client = AsyncOpenAI(**kwargs)
        self._model = cfg["model"]
        self._provider = provider
        logger.info(f"AudioService inicializado con provider={provider} model={self._model}")

    async def transcribir(self, audio_bytes: bytes, filename: str = "audio.ogg") -> Optional[str]:
        try:
            audio_file = io.BytesIO(audio_bytes)
            audio_file.name = filename
            result = await self._client.audio.transcriptions.create(
                model=self._model,
                file=audio_file,
                language="es",
            )
            texto = result.text.strip()
            logger.info(f"Audio transcripto ({self._provider}): {texto[:80]}...")
            return texto or None
        except Exception as e:
            logger.error(f"Error transcribiendo audio con {self._provider}: {e}")
            return None


_instance: Optional[AudioService] = None


def get_audio_service(api_key: str, provider: str = "groq") -> AudioService:
    global _instance
    if _instance is None or _instance._provider != provider:
        _instance = AudioService(api_key, provider)
    return _instance
