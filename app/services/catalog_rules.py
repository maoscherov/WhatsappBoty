"""
Reglas de negocio sobre el catálogo que llega del ERP.

`derivar_requiere_receta` reproduce EXACTAMENTE la regla del CSV
(sku_service._parse_base): la categoría "medicamentos bajo receta" marca "si",
y la lista blanca OTC confirmada por la farmacia (VENTA_LIBRE) o una categoría
no medicinal fuerzan "no". Se calcula al ESCRIBIR en catalog_items para que la
lectura sea directa; un cambio de regla requiere re-sync o migración de datos.
"""

from app.services.sku_service import es_venta_libre, _categoria_sin_receta

_BAJO_RECETA = "medicamentos bajo receta"


def derivar_requiere_receta(category: str, rubro: str, subrubro: str, name: str) -> str:
    """"si" | "no" — el ERP no trae flag de receta; se deriva de la clasificación."""
    if es_venta_libre(name) or _categoria_sin_receta(category):
        return "no"
    campos = ((category or ""), (rubro or ""), (subrubro or ""))
    if any(c.strip().lower() == _BAJO_RECETA for c in campos):
        return "si"
    return "no"
