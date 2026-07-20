"""
Cliente SOAP para Mercurio ERP.

ESQUELETO — pendiente de completar cuando tengamos:
  - WSDL del webservice (define nombres exactos de operaciones y tipos)
  - Credenciales de Basic Auth (idealmente de un ambiente de test)
  - Contrato final del JSON de GetPedido (el doc dice que "podría adecuarse")

Métodos del WS según doc "Conexión Mercurio ERP MO":
  Artículos: GetArticuloCantidadPaginas, GetArticuloPaginaType,
             GetGrupoType, GetSubGrupoType, GetMarcaType, GetRubroType
  Stock:     GetArticuloStockDepositos(id_articulo)
  Pedidos:   GetPedido(pedido: json) → idPedido del ERP
  Clientes:  GetClienteType(dni)

Uso previsto:
  svc = get_mercurio_service()
  skus = await svc.sync_catalogo()          # job programado / POST /bo/sku/sync-erp
  stock = await svc.get_stock("391")        # antes de generar link MP
  erp_id = await svc.crear_pedido(order)    # al confirmar pago MP
"""

import asyncio
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# zeep es sincrónico: todas las llamadas SOAP se despachan con asyncio.to_thread
# para no bloquear el event loop de FastAPI.
try:
    from zeep import Client
    from zeep.transports import Transport
    from requests import Session
    from requests.auth import HTTPBasicAuth
    _ZEEP_OK = True
except ImportError:
    _ZEEP_OK = False


class MercurioError(Exception):
    """Error de comunicación o de negocio con el ERP."""


class MercurioService:
    def __init__(self, wsdl_url: str, user: str, password: str, timeout: int = 15):
        self._wsdl_url = wsdl_url
        self._user = user
        self._password = password
        self._timeout = timeout
        self._client: Optional["Client"] = None
        # Cache de taxonomías (id → descripción) para armar nombres buscables
        self._marcas: dict[str, str] = {}
        self._grupos: dict[str, str] = {}
        self._rubros: dict[str, str] = {}
        self._subgrupos: dict[str, str] = {}

    # ── Infraestructura ──────────────────────────────────────────────────────

    def _get_client(self) -> "Client":
        """Crea el cliente zeep en forma lazy (el WSDL se descarga una sola vez)."""
        if not _ZEEP_OK:
            raise MercurioError("zeep no instalado — agregar 'zeep' a requirements.txt")
        if not self._wsdl_url:
            raise MercurioError("MERCURIO_WSDL_URL no configurada")
        if self._client is None:
            session = Session()
            session.auth = HTTPBasicAuth(self._user, self._password)
            transport = Transport(session=session, timeout=self._timeout)
            self._client = Client(self._wsdl_url, transport=transport)
        return self._client

    async def _call(self, operation: str, *args, **kwargs):
        """Ejecuta una operación SOAP en un thread para no bloquear el event loop."""
        def _do():
            client = self._get_client()
            fn = getattr(client.service, operation, None)
            if fn is None:
                raise MercurioError(f"Operación SOAP inexistente: {operation}")
            return fn(*args, **kwargs)
        try:
            return await asyncio.to_thread(_do)
        except MercurioError:
            raise
        except Exception as e:
            logger.error(f"Mercurio SOAP error en {operation}: {type(e).__name__}: {e}")
            raise MercurioError(f"{operation} falló: {e}") from e

    async def health(self) -> bool:
        """Ping liviano para /bo/health — usa la op más barata disponible."""
        try:
            await self._call("GetArticuloCantidadPaginas")
            return True
        except MercurioError:
            return False

    # ── Taxonomías ───────────────────────────────────────────────────────────

    async def _load_taxonomias(self):
        """Carga marcas/grupos/rubros/subgrupos para resolver IDs a nombres."""
        # TODO: confirmar con WSDL el shape exacto de la respuesta
        # (el doc muestra: {'codigo': '5', 'descripcion': 'RAZA', 'id_marca': '1'})
        for op, cache, id_key in [
            ("GetMarcaType", self._marcas, "id_marca"),
            ("GetGrupoType", self._grupos, "id_grupo"),
            ("GetRubroType", self._rubros, "id_rubro"),
            ("GetSubGrupoType", self._subgrupos, "id_subgrupo"),
        ]:
            try:
                rows = await self._call(op)
                cache.clear()
                for r in rows or []:
                    cache[str(r[id_key])] = str(r["descripcion"]).strip()
            except (MercurioError, KeyError, TypeError) as e:
                logger.warning(f"No se pudo cargar taxonomía {op}: {e}")

    # ── Catálogo ─────────────────────────────────────────────────────────────

    def _map_articulo(self, art: dict) -> Optional[dict]:
        """
        Mapea un artículo de Mercurio al modelo SKU del bot.

        Artículo Mercurio (doc):
          codigo, codigo_padre, variacion, descripcion, descripcion_adicional,
          observaciones, precio, stock, id_marca/grupo/rubro/subgrupo,
          codigo_barras, destacado, info_adicional, stock_x_deposito,
          atributos_variacion
        """
        try:
            marca = self._marcas.get(str(art.get("id_marca")), "")
            rubro = self._rubros.get(str(art.get("id_rubro")), "")
            descripcion = str(art.get("descripcion", "")).strip()
            variacion = str(art.get("variacion") or "").strip()

            # Variantes: por ahora se aplanan como SKUs independientes,
            # el nombre incluye la variación para desambiguar en la búsqueda.
            nombre = " ".join(p for p in [marca, descripcion, variacion] if p)

            stock = float(art.get("stock") or 0)
            return {
                "sku_id": str(art["codigo"]),
                "nombre": nombre,
                # Texto extra para el fuzzy search (marca + rubro + código de barras)
                "busqueda": " ".join(p for p in [nombre, rubro, str(art.get("codigo_barras") or "")] if p),
                "precio": float(art.get("precio") or 0),
                "stock": stock,
                "estado": "disponible" if stock > 0 else "sin_stock",
                "codigo_barras": str(art.get("codigo_barras") or ""),
                "destacado": str(art.get("destacado")) == "1",
                # TODO: decidir si observaciones (descripción comercial larga)
                # se le pasa a Claude como contexto del producto
                "observaciones": str(art.get("observaciones") or "").strip(),
            }
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"Artículo Mercurio inválido, se saltea: {e} — {art}")
            return None

    async def sync_catalogo(self) -> list[dict]:
        """
        Descarga el catálogo completo paginado y lo devuelve mapeado al modelo SKU.

        El caller (job de sync / endpoint de backoffice) es responsable de
        persistirlo (CSV o Redis) y llamar reload_sku_service().
        """
        await self._load_taxonomias()

        total_paginas = int(await self._call("GetArticuloCantidadPaginas"))
        logger.info(f"Mercurio sync: {total_paginas} páginas de artículos")

        skus: list[dict] = []
        for pagina in range(1, total_paginas + 1):
            # TODO: confirmar en WSDL la firma exacta (¿nro de página 0-based o 1-based?
            # ¿tamaño de página configurable?)
            articulos = await self._call("GetArticuloPaginaType", pagina)
            for art in articulos or []:
                sku = self._map_articulo(art)
                if sku:
                    skus.append(sku)

        logger.info(f"Mercurio sync: {len(skus)} SKUs mapeados")
        return skus

    # ── Stock en tiempo real ─────────────────────────────────────────────────

    async def get_stock(self, id_articulo: str) -> Optional[float]:
        """
        Stock total en tiempo real para un artículo (suma de depósitos).
        Devuelve None si el WS no responde — el caller decide el fallback
        (usar el stock cacheado del catálogo en vez de frenar la venta).
        """
        try:
            # Respuesta esperada (doc): stock_x_deposito → {1: '10.00', 4: '1.00'}
            result = await self._call("GetArticuloStockDepositos", int(id_articulo))
            if result is None:
                return None
            if isinstance(result, dict):
                return sum(float(v) for v in result.values())
            return float(result)
        except (MercurioError, ValueError, TypeError):
            return None

    # ── Pedidos ──────────────────────────────────────────────────────────────

    def _map_pedido(self, order: dict) -> dict:
        """
        Arma el JSON de GetPedido a partir de un order del bot (order_service).

        Formato según doc (ejemplo pág. 1-3). PENDIENTE de acordar con Mercurio:
          - customer_id cuando no tenemos CUIT/DNI del cliente
          - valores válidos de payment_method y shipping_name
        """
        return {
            "id": order["id"],
            "number": order.get("pickup_code", order["id"]),
            "state": "complete",
            "currency": "ARS",
            "created_at": order.get("created_at"),
            "updated_at": order.get("updated_at", order.get("created_at")),
            "discount_total": 0.0,
            "shipping_name": "Retiro en sucursal",   # TODO: nombre real de la sucursal
            "shipping_total": 0.0,
            "payment_method": "Mercado Pago",        # TODO: valor acordado con Mercurio
            "total": float(order["total"]),
            "customer_id": "",                       # TODO: definir cliente genérico web
            "bill_address": self._direccion_sucursal(order),
            "ship_address": self._direccion_sucursal(order),
            "line_items": [
                {
                    "order_id": order["id"],
                    "name": order["sku_nombre"],
                    "product_id": order["sku_id"],
                    "variant_id": order["sku_id"],
                    "quantity": int(order.get("cantidad", 1)),
                    "subtotal": float(order["total"]),
                    "total": float(order["total"]),
                }
            ],
        }

    @staticmethod
    def _direccion_sucursal(order: dict) -> dict:
        """Dirección para retiro en sucursal — el teléfono es el del cliente WA."""
        # TODO: datos reales de la sucursal (configurables por env o backoffice)
        return {
            "firstname": "Cliente",
            "lastname": "WhatsApp",
            "address1": "",
            "address2": "",
            "city": "",
            "zipcode": "",
            "company": "Remedia",
            "phone": order.get("phone", ""),
            "state": "",
            "country": "Argentina",
        }

    async def crear_pedido(self, order: dict) -> Optional[str]:
        """
        Envía el pedido al ERP. Devuelve el idPedido de Mercurio, o None si falló.

        El caller (mp_webhook) debe encolar reintentos cuando devuelve None:
        el pago ya se cobró y el pedido no se puede perder.
        """
        pedido_json = self._map_pedido(order)
        try:
            # TODO: confirmar en WSDL si GetPedido recibe string JSON o estructura tipada
            erp_id = await self._call("GetPedido", json.dumps(pedido_json))
            logger.info(f"Pedido {order['id']} creado en Mercurio: erp_id={erp_id}")
            return str(erp_id) if erp_id is not None else None
        except MercurioError:
            logger.error(f"Pedido {order['id']} NO pudo enviarse a Mercurio — encolar reintento")
            return None

    # ── Clientes (fase 2) ────────────────────────────────────────────────────

    async def get_cliente(self, dni: str) -> Optional[dict]:
        """Busca un cliente del ERP por DNI. Fase 2 — el bot hoy no pide DNI."""
        try:
            return await self._call("GetClienteType", dni)
        except MercurioError:
            return None


_instance: Optional[MercurioService] = None


def get_mercurio_service(
    wsdl_url: str = "",
    user: str = "",
    password: str = "",
) -> MercurioService:
    global _instance
    if _instance is None:
        _instance = MercurioService(wsdl_url, user, password)
    return _instance
