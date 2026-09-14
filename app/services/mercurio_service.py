"""
Cliente REST de Mercurio (ERP de Mascotas del Oeste) + sync de catálogo.

Doc: docs/superpowers/specs/2026-09-14-mercurio-api-v1.md (API v1 del
10/9/2026 + relevamiento contra preproducción del 14/9).

A diferencia de Observer (farmacia), Mercurio es una API en la nube: no hay
agente en el local. El servidor sincroniza directo cada 15 min y escribe en las
MISMAS tablas del catálogo ERP (`branches` / `catalog_items`), así el bot usa
la misma recarga, búsqueda y verificación en vivo.

Estructura del catálogo Mercurio: cada producto viene como un artículo PADRE
(`codigo == codigo_padre`, precio 0, stock null) más sus VARIANTES vendibles
(`codigo` propio, `variacion` = "400 gr" / "Nº 4" / "ROSA", precio y stock).
Se sincronizan solo las variantes; el padre aporta el nombre base y, en el
pedido, `product_id` (variante = `variant_id`).

Alta de pedidos (POST /v1/pedidos): DETRÁS de `mercurio_pedidos_enabled`
hasta que el proveedor confirme estados, `customer_id` y depósito de venta.
"""

import asyncio
import hashlib
import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Optional

import httpx

from app.models.sync import CatalogItemIn, ManifestEntryIn

logger = logging.getLogger(__name__)

BASE_URL_DEFAULT = "https://api.mercurio.com.ar/v1"
CATALOGOS = {
    "rubros": "id_rubro", "marcas": "id_marca", "materiales": "id_material",
    "grupos": "id_grupo", "subgrupos": "id_subgrupo",
    "tamanios-mascota": "id_tamanio", "edades-mascota": "id_edad",
}
SOURCE_MERCURIO = "mercurio"
MAX_REINTENTOS = 3
SEPARADOR_DEPOSITOS = "ç"   # 'ç' — así lo manda Mercurio en stock_x_deposito


class MercurioError(Exception):
    """Error de comunicación o de negocio con Mercurio."""


# ── Parseo de campos (todo llega como string) ─────────────────────────────────

def parsear_stock_x_deposito(valor: Optional[str]) -> dict[str, float]:
    """'1|8.00ç4|0.00ç27|1.00' → {'1': 8.0, '4': 0.0, '27': 1.0}."""
    out: dict[str, float] = {}
    for par in (valor or "").split(SEPARADOR_DEPOSITOS):
        if "|" not in par:
            continue
        dep, cant = par.split("|", 1)
        try:
            out[dep.strip()] = float(cant.strip() or 0)
        except ValueError:
            continue
    return out


def _decimal(valor) -> Optional[Decimal]:
    """'1728.4210000' → Decimal('1728.42'); 0/vacío/None → None (sin precio)."""
    if valor in (None, "", " "):
        return None
    try:
        d = Decimal(str(valor).strip().replace(",", "."))
    except InvalidOperation:
        return None
    if d <= 0:
        return None
    return d.quantize(Decimal("0.01"))


def _entero(valor) -> int:
    try:
        return int(float(str(valor).strip() or 0))
    except ValueError:
        return 0


def _texto(valor) -> str:
    return re.sub(r"\s+", " ", str(valor or "")).strip()


def es_padre(art: dict) -> bool:
    """Artículo agrupador (no vendible): mismo código que su padre y sin precio."""
    codigo = _texto(art.get("codigo"))
    padre = _texto(art.get("codigo_padre"))
    return bool(codigo) and codigo == padre and _decimal(art.get("precio")) is None


def _barcodes(art: dict) -> list[str]:
    """codigo_ean + codigo_barras sin duplicados, solo los que parecen EAN
    (8 a 14 dígitos): los valores internos ("15181000" para un pretal) son
    indistinguibles por forma, pero al menos se filtran los alfanuméricos y
    los cortos."""
    out: list[str] = []
    for k in ("codigo_ean", "codigo_barras"):
        cb = re.sub(r"\s", "", str(art.get(k) or ""))
        if cb.isdigit() and 8 <= len(cb) <= 14 and cb not in out:
            out.append(cb)
    return out


def articulo_a_item(art: dict, tax: dict[str, dict[str, str]]) -> CatalogItemIn:
    """
    Artículo Mercurio → CatalogItemIn (el mismo contrato que el agente de
    farmacia). Mapeo:
      name      = descripcion (espacios colapsados)
      brand     = marca; form = variacion ("400 gr", "Nº 4")
      category  = rubro (ALIMENTOS, ACCESORIOS...)  → requiere_receta "no"
      rubro     = grupo (PERROS, GATOS...)  subrubro = subgrupo (SECOS, CORREAS)
      therapeutic_actions = etapa (material) + edad + tamaño de mascota,
                  para que "cachorro" o "adulto +7" entren a la búsqueda
      price     = precio (Decimal, None si 0)   stock = stock (suma depósitos)
    El hash cubre lo que el bot usa: si no cambia, el upsert no escribe.
    """
    def nombre(cat: str, k: str) -> Optional[str]:
        v = art.get(k)
        return tax.get(cat, {}).get(str(v)) if v not in (None, "") else None

    extras = [x for x in (nombre("materiales", "id_material"),
                          nombre("edades-mascota", "id_edad_mascota"),
                          nombre("tamanios-mascota", "id_tamanio_mascota")) if x]
    campos = {
        "external_id": _texto(art.get("id_articulo_mercurio")),
        "barcodes": _barcodes(art),
        "name": _texto(art.get("descripcion")),
        "brand": nombre("marcas", "id_marca"),
        "form": _texto(art.get("variacion")) or None,
        "category": nombre("rubros", "id_rubro") or "",
        "rubro": nombre("grupos", "id_grupo") or "",
        "subrubro": nombre("subgrupos", "id_subgrupo") or "",
        "therapeutic_actions": extras,
        "price": _decimal(art.get("precio")),
        "stock": _entero(art.get("stock")),
        "visible": True,
        "active": True,
    }
    firma = json.dumps({**campos, "price": str(campos["price"])}, ensure_ascii=False, sort_keys=True)
    return CatalogItemIn(hash=hashlib.sha256(firma.encode()).hexdigest(), troquel=None,
                         drug=None, **campos)


# ── Cliente HTTP ──────────────────────────────────────────────────────────────

class MercurioClient:
    def __init__(self, api_key: str, base_url: str = BASE_URL_DEFAULT, timeout: float = 60.0,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self._base = (base_url or BASE_URL_DEFAULT).rstrip("/")
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout, transport=transport,
        )

    async def aclose(self):
        await self._client.aclose()

    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        """GET con reintentos: 429 respeta Retry-After; 5xx backoff 1-2-4 s."""
        ultimo: Optional[Exception] = None
        for intento in range(MAX_REINTENTOS):
            try:
                r = await self._client.get(f"{self._base}{path}", params=params)
            except httpx.HTTPError as e:
                ultimo = MercurioError(f"{path}: {e}")
                await asyncio.sleep(2 ** intento)
                continue
            if r.status_code == 429:
                espera = int(r.headers.get("Retry-After") or 60)
                logger.warning(f"Mercurio 429 en {path}: espero {espera}s")
                await asyncio.sleep(min(espera, 120))
                continue
            if r.status_code >= 500:
                ultimo = MercurioError(f"{path}: HTTP {r.status_code}")
                await asyncio.sleep(2 ** intento)
                continue
            if r.status_code == 404:
                raise MercurioError(f"{path}: 404")
            if r.status_code >= 400:
                raise MercurioError(f"{path}: HTTP {r.status_code} {r.text[:200]}")
            try:
                return r.json()
            except ValueError as e:
                raise MercurioError(f"{path}: respuesta no JSON ({e})")
        raise ultimo or MercurioError(f"{path}: sin respuesta")

    async def estado(self) -> dict:
        return await self._get("/estado")

    async def catalogo(self, nombre: str) -> dict[str, str]:
        """{id: descripcion} de uno de los 7 catálogos."""
        clave = CATALOGOS[nombre]
        body = await self._get(f"/{nombre}")
        return {str(i.get(clave)): _texto(i.get("descripcion")) for i in body.get("items") or []}

    async def taxonomias(self) -> dict[str, dict[str, str]]:
        return {n: await self.catalogo(n) for n in CATALOGOS}

    async def paginas(self) -> int:
        body = await self._get("/articulos/paginas")
        return int(body.get("paginas") or 0)

    async def articulos(self, pagina: int) -> list[dict]:
        body = await self._get("/articulos", params={"pagina": pagina})
        return body.get("articulos") or []

    async def stock(self, id_articulo: str) -> Optional[dict[str, float]]:
        """Stock por depósito en vivo; None si el artículo no existe."""
        try:
            body = await self._get(f"/articulos/{id_articulo}/stock")
        except MercurioError as e:
            if ": 404" in str(e):
                return None
            raise
        total: dict[str, float] = {}
        for f in body.get("stock_x_deposito") or []:
            total.update(parsear_stock_x_deposito(f.get("stock_x_zona") or f.get("stock_x_deposito")))
        return total


# ── Sync de catálogo ──────────────────────────────────────────────────────────

class MercurioSync:
    """Recorre catálogos + páginas y vuelca las variantes en catalog_items."""

    def __init__(self, client: MercurioClient, branch_id: str, db):
        self._client = client
        self._branch = branch_id
        self._db = db
        self.ultimo: dict = {}

    async def asegurar_sucursal(self):
        """La sucursal Mercurio no tiene agente: se crea sola (token inútil)."""
        from app.services.branch_auth import generar_token
        from app.services.branch_store import get_branch_store
        store = get_branch_store(self._db)
        if not await store.get(self._branch):
            _, token_hash = generar_token()
            await store.crear(self._branch, "Mascotas del Oeste (Mercurio)", token_hash)
            logger.info(f"Sucursal {self._branch} creada para el sync de Mercurio")

    async def sincronizar(self) -> dict:
        """Un ciclo completo. Devuelve el reporte (también en self.ultimo)."""
        import time
        from app.services.catalog_store import get_catalog_store
        from app.services.branch_store import get_branch_store

        t0 = time.perf_counter()
        await self.asegurar_sucursal()
        tax = await self._client.taxonomias()
        paginas = await self._client.paginas()
        items: list[CatalogItemIn] = []
        padres = 0
        for p in range(1, max(paginas, 1) + 1):
            for art in await self._client.articulos(p):
                if es_padre(art):
                    padres += 1
                    continue
                try:
                    items.append(articulo_a_item(art, tax))
                except Exception as e:
                    logger.warning(f"Mercurio: artículo {art.get('id_articulo_mercurio')} omitido: {e}")

        store = get_catalog_store(self._db)
        received = upserted = 0
        for i in range(0, len(items), 500):
            r, u = await store.upsert_items(self._branch, items[i:i + 500], source=SOURCE_MERCURIO)
            received += r
            upserted += u
        # Lo que ya no está en Mercurio se desactiva (mismo mecanismo que el
        # full-manifest del agente).
        deactivated = 0
        if items:
            _, deactivated = await store.full_manifest(
                self._branch, [ManifestEntryIn(external_id=i.external_id, hash=i.hash) for i in items])
        await get_branch_store(self._db).heartbeat(
            self._branch, agent_version="mercurio-rest", erp_version="v1", erp_status="ok",
            catalog_count=len(items), pending_batches=0)

        self.ultimo = {
            "paginas": paginas, "variantes": len(items), "padres_omitidos": padres,
            "upserted": upserted, "unchanged": received - upserted,
            "deactivated": deactivated, "ms": int((time.perf_counter() - t0) * 1000),
        }
        logger.info(f"Mercurio sync: {self.ultimo}")

        if upserted or deactivated:
            try:
                from app.services.catalog_refresher import get_catalog_refresher
                from app.services.catalog_source import resolver_branch_default
                if await resolver_branch_default(forzar=True) == self._branch:
                    get_catalog_refresher().schedule(
                        self._branch, {i.external_id for i in items}, inmediata=True)
            except Exception as e:
                logger.warning(f"Mercurio: no se pudo programar la recarga: {e}")
        return self.ultimo


_client: Optional[MercurioClient] = None
_sync: Optional[MercurioSync] = None


def mercurio_configurado() -> bool:
    from app.config import get_settings
    return bool(get_settings().mercurio_api_key)


def get_mercurio_client() -> MercurioClient:
    global _client
    if _client is None:
        from app.config import get_settings
        s = get_settings()
        if not s.mercurio_api_key:
            raise MercurioError("MERCURIO_API_KEY no configurada")
        _client = MercurioClient(s.mercurio_api_key, s.mercurio_base_url)
    return _client


def get_mercurio_sync() -> MercurioSync:
    global _sync
    if _sync is None:
        from app.config import get_settings
        from app.services.db import get_db
        s = get_settings()
        _sync = MercurioSync(get_mercurio_client(), s.mercurio_branch_id, get_db(s.database_url))
    return _sync
