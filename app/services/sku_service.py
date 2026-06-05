"""
Carga el catálogo y expone búsqueda fuzzy por nombre, marca o laboratorio.

Soporta dos formatos de CSV:
  A) Catálogo base (siemprebien_skus):
       SKU, Nombre, Precio, Marca, Laboratorio, Codigo_Barras_1..4, Categoria, Es_Medicamento
  B) Catálogo procesado (con stock ETL):
       sku_id, barcode, sku_nombre, sku_nombre_original, marca, laboratorio, categoria,
       es_medicamento, precio_venta, stock_actual, ventas_mes, prom_semanal,
       cantidad_visible, tipo_producto, pausado

Si el CSV es el formato B (tiene la columna 'cantidad_visible'), usa los valores calculados.
Si es el formato A (sin stock), todos los productos quedan en estado "consultar" hasta
que se corra el ETL de ObServer.
"""

import csv
import logging
from pathlib import Path
from typing import Optional

from rapidfuzz import fuzz, process

from app.models.sku import SKU

logger = logging.getLogger(__name__)

# Umbrales de búsqueda
SCORE_CUTOFF = 55
TOP_N_DEFAULT = 3


def _safe_float(val) -> Optional[float]:
    try:
        return float(val) if val not in (None, "", "nan") else None
    except (ValueError, TypeError):
        return None


def _safe_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).lower().strip() in ("true", "1", "yes", "si", "sí")


class SKUService:
    def __init__(self, csv_path: str):
        self._skus: list[SKU] = []
        # Índice multi-campo para búsqueda: "nombre marca laboratorio"
        self._search_index: list[str] = []
        self._load(csv_path)

    def _load(self, csv_path: str):
        path = Path(csv_path)
        if not path.exists():
            raise FileNotFoundError(f"Catálogo SKU no encontrado: {csv_path}")

        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            cols = set(reader.fieldnames or [])
            is_processed = "cantidad_visible" in cols
            has_imagen = "imagen_url" in cols

            for row in reader:
                sku = self._parse_processed(row, has_imagen) if is_processed else self._parse_base(row, has_imagen)
                if sku:
                    self._skus.append(sku)
                    # Índice: nombre + marca + laboratorio (todo lower)
                    search_text = " ".join(filter(None, [
                        sku.sku_nombre.lower(),
                        sku.marca.lower(),
                        sku.laboratorio.lower(),
                    ]))
                    self._search_index.append(search_text)

        logger.info(f"SKUService: {len(self._skus)} productos cargados desde {csv_path}")

    @staticmethod
    def _parse_base(row: dict, has_imagen: bool = False) -> Optional[SKU]:
        nombre = row.get("Nombre", "").strip()
        if not nombre or nombre == "nan":
            return None
        barcode = (
            row.get("Codigo_Barras_1", "")
            or row.get("Codigo_Barras_2", "")
            or row.get("Codigo_Barras_3", "")
            or ""
        ).strip()
        try:
            return SKU(
                sku_id=str(row.get("SKU", "")).strip(),
                barcode=barcode,
                sku_nombre=nombre,
                sku_nombre_original=nombre,
                marca=row.get("Marca", "").strip(),
                laboratorio=row.get("Laboratorio", "").strip(),
                categoria=row.get("Categoria", "").strip(),
                es_medicamento=_safe_bool(row.get("Es_Medicamento", "")),
                precio_venta=_safe_float(row.get("Precio")) or 0.0,
                cantidad_visible=0,
                imagen_url=row.get("imagen_url", "").strip() or None if has_imagen else None,
            )
        except Exception:
            return None

    @staticmethod
    def _parse_processed(row: dict, has_imagen: bool = False) -> Optional[SKU]:
        nombre = row.get("sku_nombre", "").strip()
        if not nombre:
            return None
        try:
            return SKU(
                sku_id=row.get("sku_id", "").strip(),
                barcode=row.get("barcode", "").strip(),
                sku_nombre=nombre,
                sku_nombre_original=row.get("sku_nombre_original", nombre).strip(),
                marca=row.get("marca", "").strip(),
                laboratorio=row.get("laboratorio", "").strip(),
                categoria=row.get("categoria", "").strip(),
                es_medicamento=_safe_bool(row.get("es_medicamento", "")),
                precio_venta=_safe_float(row.get("precio_venta")) or 0.0,
                stock_actual=_safe_float(row.get("stock_actual")),
                ventas_mes=_safe_float(row.get("ventas_mes")),
                prom_semanal=_safe_float(row.get("prom_semanal")),
                cantidad_visible=int(row.get("cantidad_visible") or 0),
                tipo_producto=row.get("tipo_producto", "regular"),
                pausado=_safe_bool(row.get("pausado", "False")),
                imagen_url=row.get("imagen_url", "").strip() or None if has_imagen else None,
            )
        except Exception:
            return None

    def buscar(self, query: str, top_n: int = TOP_N_DEFAULT, score_cutoff: int = SCORE_CUTOFF) -> list[dict]:
        """
        Busca productos por nombre coloquial, nombre técnico, marca o laboratorio.
        Devuelve hasta top_n resultados no pausados, ordenados por ventas_mes desc.
        """
        if not self._skus or not query.strip():
            return []

        # Limpiar la query: quitar cantidades ("dame 2", "necesito 3") y
        # palabras de intención que confunden el fuzzy match
        import re as _re
        clean_query = _re.sub(r'\b(dame|quiero|necesito|tenés|hay|tienen|precio|cuánto|sale|\d+)\b', '', query.lower()).strip()
        if not clean_query:
            clean_query = query.lower()

        seen_ids: set[str] = set()
        candidatos: list[SKU] = []

        # Búsqueda con query limpia (scorer partial_ratio para substrings)
        for scorer in [fuzz.WRatio, fuzz.partial_ratio]:
            matches = process.extract(
                clean_query,
                self._search_index,
                scorer=scorer,
                limit=top_n * 6,
                score_cutoff=score_cutoff,
            )
            for _text, _score, idx in matches:
                sku = self._skus[idx]
                if sku.sku_id not in seen_ids and not sku.pausado:
                    candidatos.append(sku)
                    seen_ids.add(sku.sku_id)
            if len(candidatos) >= top_n:
                break

        # Ordenar: disponibles primero, luego por más vendido
        candidatos.sort(key=lambda s: (0 if s.disponible else 1, -(s.ventas_mes or 0)))

        return [self._to_response(s) for s in candidatos[:top_n]]

    def get_by_id(self, sku_id: str) -> Optional[SKU]:
        for sku in self._skus:
            if sku.sku_id == sku_id:
                return sku
        return None

    def get_by_barcode(self, barcode: str) -> Optional[SKU]:
        for sku in self._skus:
            if sku.barcode == barcode:
                return sku
        return None

    @staticmethod
    def _to_response(sku: SKU) -> dict:
        return {
            "sku_id": sku.sku_id,
            "barcode": sku.barcode,
            "nombre": sku.sku_nombre,
            "marca": sku.marca,
            "laboratorio": sku.laboratorio,
            "precio": sku.precio_venta,
            "cantidad_visible": sku.cantidad_visible,
            "estado": sku.estado,
            "categoria": sku.categoria,
            "es_medicamento": sku.es_medicamento,
            "imagen_url": sku.imagen_url,
        }

    @property
    def total(self) -> int:
        return len(self._skus)


_instance: Optional[SKUService] = None


def get_sku_service(csv_path: str = "data/catalogo_base.csv") -> SKUService:
    global _instance
    if _instance is None:
        _instance = SKUService(csv_path)
    return _instance


def reload_sku_service(csv_path: str) -> SKUService:
    """Recarga el catálogo desde disco sin reiniciar el servidor."""
    global _instance
    _instance = SKUService(csv_path)
    logger.info(f"Catálogo recargado: {_instance.total} SKUs")
    return _instance
