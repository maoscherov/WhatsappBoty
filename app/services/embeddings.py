"""
Servicio de embeddings con OpenAI (text-embedding-3-small, 1536 dims).

Degradación elegante: si no hay API key, embed() devuelve [] y las features
de RAG quedan inactivas (el resto del sistema sigue funcionando).
"""

import logging
from typing import Optional

import openai

logger = logging.getLogger(__name__)

MODEL = "text-embedding-3-small"
BATCH = 256   # inputs por request


class EmbeddingService:
    def __init__(self, api_key: str):
        self._enabled = bool(api_key)
        self._client = openai.AsyncOpenAI(api_key=api_key) if api_key else None

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Devuelve un embedding por texto. [] si está deshabilitado o falla."""
        if not self._enabled or not texts:
            return []
        out: list[list[float]] = []
        try:
            for i in range(0, len(texts), BATCH):
                chunk = texts[i:i + BATCH]
                resp = await self._client.embeddings.create(model=MODEL, input=chunk)
                out.extend(d.embedding for d in resp.data)
            return out
        except Exception as e:
            logger.error(f"Embeddings error: {e}")
            return []

    async def embed_one(self, text: str) -> Optional[list[float]]:
        res = await self.embed([text])
        return res[0] if res else None


_instance: Optional[EmbeddingService] = None


def get_embedding_service(api_key: str = "") -> EmbeddingService:
    global _instance
    if _instance is None:
        _instance = EmbeddingService(api_key)
    return _instance
