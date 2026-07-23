"""
Conversor one-off: Excel de muestra "Farmacia MUTUAL Base Predictiva" → CSV del bot.

Uso:
    python scripts/convert_muestra_excel.py "ruta/al/archivo.xlsx" [salida.csv]

Genera un CSV en formato "procesado" (el que ya lee sku_service) con dos
columnas extra para esta muestra:
    - requiere_receta  : "si" | "ambiguo" | "no"
        si      → el cruce marcó "Si" explícito
        ambiguo → "No encontrado en cruce" / "Indeterminado" (solo medicamentos)
        no      → venta libre, no aplica, o no es medicamento
      El bot decide si "ambiguo" deriva o no según el modo configurado
      (config bot: receta_mode = conservador | estricto).
    - clasificacion    : critico | riesgo_alto | riesgo_medio | saludable | sin_rotacion

Filtra filas no vendibles: precio <= 0, stock <= 0, y placeholders (barcode "1").

NOTA: es un script de muestra/demo, no el pipeline definitivo de catálogo.
"""

import csv
import sys
from pathlib import Path

import pandas as pd

# Hojas de producto (las de reportes se ignoran)
HOJAS_PRODUCTO = ["Medicamentos", "General", "Cosmeticos", "Alimentos"]

# Mapeo de "Clasificacion disponibilidad" → código corto
_CLASIF_MAP = {
    "critico - quiebre inminente": "critico",
    "riesgo alto": "riesgo_alto",
    "riesgo medio": "riesgo_medio",
    "cobertura saludable": "saludable",
    "sin rotacion (ult. 4 sem)": "sin_rotacion",
    "revisar stock (valor negativo)": "revisar",
}

# "Requiere Receta (cruce SKU)" → nivel de receta.
# Solo aplica a la hoja Medicamentos; el resto nunca requiere receta.
_RECETA_AMBIGUO = {"no encontrado en cruce", "indeterminado - revisar"}


def _clasif(valor: str) -> str:
    return _CLASIF_MAP.get(str(valor).strip().lower(), "saludable")


def _receta(valor: str, es_medicamento: bool) -> str:
    if not es_medicamento:
        return "no"
    v = str(valor).strip().lower()
    if v == "si":
        return "si"
    if v in _RECETA_AMBIGUO:
        return "ambiguo"
    return "no"


def convertir(xlsx_path: str, out_path: str) -> int:
    filas = []
    idx = 0
    for hoja in HOJAS_PRODUCTO:
        try:
            df = pd.read_excel(xlsx_path, sheet_name=hoja)
        except ValueError:
            print(f"  (hoja '{hoja}' no encontrada, se saltea)")
            continue

        es_medicamento = hoja == "Medicamentos"
        incluidas = 0
        for _, row in df.iterrows():
            precio = row.get("Precio venta unit.")
            stock = row.get("Stock actual")
            barcode = str(row.get("Codigo de barra", "")).strip()

            # Filtros de datos no vendibles
            try:
                precio = float(precio)
                stock = float(stock)
            except (ValueError, TypeError):
                continue
            if precio <= 0 or stock <= 0 or barcode in ("", "1", "nan"):
                continue

            clasif = _clasif(row.get("Clasificacion disponibilidad"))
            if clasif == "revisar":     # stock negativo / error de carga
                continue

            idx += 1
            nombre = str(row.get("Descripcion", "")).strip()
            filas.append({
                "sku_id": barcode or f"MUESTRA-{idx}",
                "barcode": barcode,
                "sku_nombre": nombre,
                "sku_nombre_original": nombre,
                "marca": "",
                "laboratorio": nombre.split(" ")[0] if nombre else "",
                "categoria": hoja,
                "es_medicamento": "si" if es_medicamento else "no",
                "precio_venta": round(precio, 2),
                "stock_actual": int(stock),
                "ventas_mes": round(float(row.get("Prom. venta semanal (4 sem)") or 0) * 4, 1),
                "prom_semanal": round(float(row.get("Prom. venta semanal (4 sem)") or 0), 1),
                "cantidad_visible": int(stock),
                "tipo_producto": "regular",
                "pausado": "False",
                "requiere_receta": _receta(row.get("Requiere Receta (cruce SKU)"), es_medicamento),
                "clasificacion": clasif,
            })
            incluidas += 1
        print(f"  {hoja}: {incluidas} productos incluidos (de {len(df)})")

    if not filas:
        print("No se generó ninguna fila — revisá el archivo de entrada.")
        return 0

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(filas[0].keys()))
        writer.writeheader()
        writer.writerows(filas)

    receta_si = sum(1 for r in filas if r["requiere_receta"] == "si")
    receta_amb = sum(1 for r in filas if r["requiere_receta"] == "ambiguo")
    print(f"\nTotal: {len(filas)} productos → {out_path}")
    print(f"  receta 'si' (explícito): {receta_si}")
    print(f"  receta 'ambiguo':        {receta_amb}")
    return len(filas)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    xlsx = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "data/catalogo_muestra.csv"
    convertir(xlsx, out)
