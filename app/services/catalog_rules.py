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


def derivar_requiere_receta(category: str, rubro: str, subrubro: str, name: str,
                            barcodes=(), referencia=None) -> str:
    """
    "si" | "no" | "ambiguo" — el ERP no trae flag de receta.

    Orden (caso real 24/9: con la regla vieja TODO medicamento del ERP quedaba
    como venta libre, porque Observer dice solo "Medicamentos"):
      1. Venta libre conocida o rubro no medicinal → "no".
      2. Referencia de la farmacia por código de barras (receta_referencia).
      3. Categoría explícita "medicamentos bajo receta" → "si".
      4. Cualquier otro MEDICAMENTO sin referencia → "ambiguo": en modo
         conservador (default) deriva. Más vale derivar de más que vender
         sin receta.
      5. El resto → "no".

    `referencia`: función barcodes → flag|None. Por defecto la referencia
    cargada; la sincronización pasa la NUEVA para simular antes de aplicar.
    """
    return explicar_receta(category, rubro, subrubro, name, barcodes, referencia)[0]


# Origen de la marca, para el backoffice ("¿por qué el bot deriva esto?").
ORIGENES = {
    "venta_libre_conocida": "Venta libre conocida (lista de la farmacia)",
    "no_medicinal": "Rubro no medicinal",
    "referencia": "Referencia de recetas (sincronizada)",
    "categoria_bajo_receta": "Categoría \"Medicamentos bajo receta\"",
    "sin_referencia": "Medicamento sin referencia (a validar)",
    "otro": "No es medicamento",
}


def explicar_receta(category: str, rubro: str, subrubro: str, name: str,
                    barcodes=(), referencia=None) -> tuple[str, str]:
    """(flag, origen) con el mismo orden que `derivar_requiere_receta`."""
    if es_venta_libre(name):
        return "no", "venta_libre_conocida"
    if _categoria_sin_receta(category):
        return "no", "no_medicinal"
    if referencia is None:
        from app.services.receta_referencia import buscar as referencia
    ref = referencia(barcodes)
    if ref in ("si", "no", "ambiguo"):
        return ref, "referencia"
    campos = ((category or ""), (rubro or ""), (subrubro or ""))
    if any(c.strip().lower() == _BAJO_RECETA for c in campos):
        return "si", "categoria_bajo_receta"
    if any("medicament" in c.lower() for c in campos):
        return "ambiguo", "sin_referencia"
    return "no", "otro"
