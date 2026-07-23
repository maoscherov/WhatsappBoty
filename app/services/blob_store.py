"""
Almacén de archivos en Redis para sobrevivir los deploys.

El filesystem de Railway es efímero: se borra en cada deploy. Los archivos
que sube el operador (catálogo de SKU, padrón de socios) se pierden. Este
store guarda una copia en Redis (base64) y la restaura al arrancar el server.

Uso:
  blob = get_blob_store(redis_url)
  await blob.save("socios", content_bytes, ext=".xlsx")
  data = await blob.load("socios")   # -> (bytes, ext) o None
"""

import base64
import logging
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


class BlobStore:
    def __init__(self, redis_url: str):
        self._redis = aioredis.from_url(redis_url, decode_responses=True)

    async def save(self, name: str, data: bytes, ext: str = "") -> bool:
        try:
            await self._redis.set(f"blob:{name}", base64.b64encode(data).decode())
            await self._redis.set(f"blob:{name}:ext", ext or "")
            return True
        except Exception as e:
            logger.warning(f"BlobStore.save({name}) falló: {e}")
            return False

    async def load(self, name: str) -> Optional[tuple[bytes, str]]:
        try:
            raw = await self._redis.get(f"blob:{name}")
            if not raw:
                return None
            ext = await self._redis.get(f"blob:{name}:ext") or ""
            return base64.b64decode(raw), ext
        except Exception as e:
            logger.warning(f"BlobStore.load({name}) falló: {e}")
            return None


_instance: Optional[BlobStore] = None


def get_blob_store(redis_url: str) -> BlobStore:
    global _instance
    if _instance is None:
        _instance = BlobStore(redis_url)
    return _instance
