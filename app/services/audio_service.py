"""
Transcribe audios de WhatsApp usando OpenAI Whisper API.
Costo aprox: $0.006/minuto de audio.
"""

import io
from typing import Optional
from openai import AsyncOpenAI


class AudioService:
    def __init__(self, api_key: str):
        self._client = AsyncOpenAI(api_key=api_key)

    async def transcribir(self, audio_bytes: bytes, filename: str = "audio.ogg") -> Optional[str]:
        try:
            audio_file = io.BytesIO(audio_bytes)
            audio_file.name = filename
            result = await self._client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="es",
            )
            return result.text
        except Exception:
            return None


_instance: Optional[AudioService] = None


def get_audio_service(api_key: str) -> AudioService:
    global _instance
    if _instance is None:
        _instance = AudioService(api_key)
    return _instance
