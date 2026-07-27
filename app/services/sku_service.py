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

# Umbrales de búsqueda.
# 66 filtra los falsos positivos por fragmento (ej. "ibuprofeno"→"FREESTYLE LIBRE",
# "vecetam"→"esponja VEGetal") sin perder los aciertos reales.
SCORE_CUTOFF = 66
TOP_N_DEFAULT = 3

# Sinónimos droga↔marca: el catálogo usa nombres de marca, así que el nombre
# genérico que tipea el cliente no siempre matchea. La clave es lo que puede
# escribir el cliente; los valores son términos adicionales a buscar (marcas
# equivalentes presentes en el catálogo). Si algún valor no existe, la búsqueda
# extra simplemente no devuelve nada (inofensivo). Ampliable a medida que surjan.
SINONIMOS: dict[str, list[str]] = {
    "ibuprofeno":   ["ibupirac", "actron"],
    "omeprazol":    ["aziatop"],
    "escopolamina": ["buscapina", "sertal"],
    "butilhioscina":["buscapina", "sertal"],
    "hioscina":     ["buscapina", "sertal"],
    "diclofenac":   ["voltaren"],
    "aspirina":     ["bayaspirina", "aspirineta"],
    "acido acetilsalicilico": ["aspirina", "bayaspirina"],
    "amoxicilina":  ["amoxidal"],
    "paracetamol":  ["tafirol"],
}

# Lista blanca de VENTA LIBRE: drogas/marcas OTC conocidas cuyo flag de receta
# del catálogo suele venir mal (ej. omeprazol/Aziatop marcado "si" o "ambiguo"
# siendo venta libre). Si el nombre contiene alguna de estas, se fuerza
# requiere_receta="no". Ampliable/editable — VALIDAR con criterio farmacéutico.
VENTA_LIBRE: set[str] = {
    # Analgésicos / antipiréticos / AINEs OTC
    "paracetamol", "tafirol", "ibuprofeno", "ibupirac", "actron", "ibuevanol",
    "aspirina", "aspirineta", "bayaspirina", "naproxeno",
    # Antiácidos / IBP / digestivos OTC
    "omeprazol", "aziatop", "esomeprazol", "pantoprazol", "ranitidina",
    "mylanta", "gaviscon", "sertal", "buscapina",
    # Antihistamínicos OTC
    "loratadina",
}


def es_venta_libre(nombre: str) -> bool:
    """True si el nombre del producto corresponde a un OTC de la lista blanca."""
    n = (nombre or "").lower()
    return any(kw in n for kw in VENTA_LIBRE)


def requiere_derivacion(requiere_receta: str, modo: str = "conservador") -> bool:
    """
    Decide si un producto debe derivarse a un humano por requerir receta.
    - "si"      → siempre deriva.
    - "ambiguo" → deriva en modo conservador (default), no en modo estricto.
    - "no"      → nunca deriva.
    """
    r = (requiere_receta or "no").lower()
    if r == "si":
        return True
    if r == "ambiguo":
        return modo != "estricto"
    return False


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
        categoria = row.get("Categoria", "").strip()
        # La receta se deriva de la categoría del propio catálogo: la farmacia
        # ya clasifica los productos como "Medicamentos Bajo Receta". Si el CSV
        # trae una columna requiere_receta explícita, esa tiene prioridad.
        requiere = (row.get("requiere_receta") or "").strip().lower()
        if requiere not in ("si", "ambiguo", "no"):
            requiere = "si" if categoria.lower() == "medicamentos bajo receta" else "no"
        if es_venta_libre(nombre):   # override OTC: nunca requiere receta
            requiere = "no"
        try:
            return SKU(
                sku_id=str(row.get("SKU", "")).strip(),
                barcode=barcode,
                sku_nombre=nombre,
                sku_nombre_original=nombre,
                marca=row.get("Marca", "").strip(),
                laboratorio=row.get("Laboratorio", "").strip(),
                categoria=categoria,
                es_medicamento=_safe_bool(row.get("Es_Medicamento", "")),
                precio_venta=_safe_float(row.get("Precio")) or 0.0,
                cantidad_visible=0,
                imagen_url=row.get("imagen_url", "").strip() or None if has_imagen else None,
                requiere_receta=requiere,
            )
        except Exception:
            return None

    @staticmethod
    def _parse_processed(row: dict, has_imagen: bool = False) -> Optional[SKU]:
        nombre = row.get("sku_nombre", "").strip()
        if not nombre:
            return None
        requiere = (row.get("requiere_receta") or "no").strip().lower()
        if es_venta_libre(nombre):   # override OTC: nunca requiere receta
            requiere = "no"
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
                requiere_receta=requiere,
                clasificacion=(row.get("clasificacion") or "").strip().lower(),
            )
        except Exception:
            return None

    def buscar(self, query: str, top_n: int = TOP_N_DEFAULT, score_cutoff: int = SCORE_CUTOFF) -> list[dict]:
        """
        Busca productos por nombre coloquial, nombre técnico, marca o laboratorio.

        Devuelve hasta top_n resultados no pausados, ordenados por RELEVANCIA
        (score del match) primero, y sólo como desempate por disponibilidad y
        ventas. Si ningún producto supera el umbral, devuelve [] — así el bot
        responde "no lo encontramos" en vez de ofrecer un producto de otro rubro.
        """
        if not self._skus or not query.strip():
            return []

        # Limpiar la query: quitar cantidades ("dame 2"), palabras de intención
        # y de presentación (gotas/crema/jarabe) que arrastran el match a otros
        # productos del mismo formato pero distinto principio.
        import re as _re
        _STOP = (
            r'\b(dame|quiero|necesito|ten[eé]s|tienen|hay|precio|cu[aá]nto|cuanto|sale|'
            r'en|de|para|gotas|gota|comprimidos|comprimido|comp|jarabe|crema|pomada|\d+)\b'
        )
        clean_query = _re.sub(_STOP, '', query.lower())
        clean_query = _re.sub(r'\s+', ' ', clean_query).strip()
        if not clean_query:
            clean_query = query.lower()

        # Expandir con sinónimos: si el cliente escribió un genérico cuyo nombre
        # no está en el catálogo (ej. "ibuprofeno"), agregamos las marcas equivalentes.
        variantes = [clean_query]
        for generico, marcas in SINONIMOS.items():
            if generico in clean_query:
                variantes.extend(marcas)

        # Guardamos el MEJOR score por producto, entre los dos scorers y todas
        # las variantes de la query. token_set_ratio maneja multi-palabra/orden.
        scored: dict[int, float] = {}
        for variante in variantes:
            for scorer in (fuzz.WRatio, fuzz.token_set_ratio):
                for _text, score, idx in process.extract(
                    variante,
                    self._search_index,
                    scorer=scorer,
                    limit=top_n * 8,
                    score_cutoff=score_cutoff,
                ):
                    if score > scored.get(idx, 0):
                        scored[idx] = score

        candidatos = [
            (self._skus[idx], sc) for idx, sc in scored.items()
            if not self._skus[idx].pausado
        ]
        # Relevancia primero; disponibilidad y ventas sólo desempatan matches parejos.
        candidatos.sort(key=lambda x: (-x[1], 0 if x[0].disponible else 1, -(x[0].ventas_mes or 0)))

        return [self._to_response(s) for s, _sc in candidatos[:top_n]]

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
            "requiere_receta": sku.requiere_receta,
            "clasificacion": sku.clasificacion,
            "urgente": sku.urgente,
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
