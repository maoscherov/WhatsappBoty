"""
Excepciones de pago con cuenta corriente (minuta 79, punto 6).

Regla: TODO socio activo puede pagar con cuenta corriente, salvo que figure en
la lista de excepciones que manda la farmacia. Hasta que esa lista llegue, el
conjunto está vacío y todos quedan habilitados.

La lista se sube como CSV/Excel-CSV desde el backoffice (/bo/cc/excepciones) y
persiste en el blob store de Redis (el filesystem de Railway es efímero). Se
reconoce por DNI o N° de socio: cualquier grupo de 4+ dígitos por línea entra
al conjunto — así el formato exacto de columnas de la farmacia no importa.
"""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

_BLOB_KEY = "cc_excepciones"


def _parsear(data: bytes) -> set[str]:
    """Grupos de 4+ dígitos por línea (DNI, N° socio, CUIL sin guiones)."""
    try:
        texto = data.decode("utf-8-sig", errors="replace")
    except Exception:
        return set()
    numeros: set[str] = set()
    for linea in texto.splitlines():
        for grupo in re.findall(r"\d{4,}", linea):
            numeros.add(grupo)
    return numeros


class CCService:
    def __init__(self, redis_url: str):
        self._redis_url = redis_url
        self._excepciones: Optional[set[str]] = None

    async def excepciones(self) -> set[str]:
        """Conjunto en memoria; se hidrata del blob la primera vez."""
        if self._excepciones is None:
            try:
                from app.services.blob_store import get_blob_store
                data = await get_blob_store(self._redis_url).load(_BLOB_KEY)
                self._excepciones = _parsear(data[0]) if data else set()
                if self._excepciones:
                    logger.info(f"CC: {len(self._excepciones)} excepciones cargadas")
            except Exception as e:
                logger.warning(f"CC: no se pudieron cargar las excepciones: {e}")
                self._excepciones = set()
        return self._excepciones

    async def cargar(self, data: bytes) -> int:
        """Reemplaza la lista con el archivo subido. Devuelve la cantidad."""
        numeros = _parsear(data)
        from app.services.blob_store import get_blob_store
        await get_blob_store(self._redis_url).save(_BLOB_KEY, data, ".csv")
        self._excepciones = numeros
        logger.info(f"CC: lista de excepciones actualizada ({len(numeros)} números)")
        return len(numeros)

    async def es_excepcion(self, socio: dict) -> bool:
        """True si el DNI o el N° de socio figura en la lista de la farmacia."""
        if not socio:
            return False
        exc = await self.excepciones()
        if not exc:
            return False
        dni = re.sub(r"\D", "", str(socio.get("dni") or ""))
        nro = re.sub(r"\D", "", str(socio.get("nro_socio") or ""))
        return bool((dni and dni in exc) or (nro and nro in exc))


_instance: Optional[CCService] = None


def get_cc_service(redis_url: str = "redis://localhost:6379") -> CCService:
    global _instance
    if _instance is None:
        _instance = CCService(redis_url)
    return _instance
