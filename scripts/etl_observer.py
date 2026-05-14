"""
ETL: catalogo_base.csv + reporte mensual ObServer → catalogo_procesado.csv con cantidad_visible.

Flujo:
  1. Lee el catálogo base (siemprebien_skus / catalogo_base.csv)
  2. Lee el reporte ObServer (ventas + stock por producto)
  3. Hace JOIN por nombre normalizado o código de barras
  4. Calcula cantidad_visible por producto
  5. Exporta catalogo_procesado.csv listo para el bot

Uso:
  # Primera corrida (genera catálogo procesado desde cero):
  python scripts/etl_observer.py --catalogo data/catalogo_base.csv --observer reporte.xlsx

  # Corridas siguientes (preserva tipo_producto y pausado manuales):
  python scripts/etl_observer.py --catalogo data/catalogo_base.csv --observer reporte.xlsx \\
      --merge-existing data/catalogo_procesado.csv

  # Salida personalizada:
  python scripts/etl_observer.py --catalogo data/catalogo_base.csv --observer reporte.xlsx \\
      --output data/catalogo_procesado.csv
"""

import argparse
import math
import re
import datetime
import pandas as pd
from pathlib import Path

# ── Columnas del catálogo base (siemprebien_skus) ────────────────────────────
CAT_SKU_ID = "SKU"
CAT_NOMBRE = "Nombre"
CAT_PRECIO = "Precio"
CAT_MARCA = "Marca"
CAT_LABORATORIO = "Laboratorio"
CAT_BARCODE = "Codigo_Barras_1"
CAT_CATEGORIA = "Categoria"
CAT_ES_MED = "Es_Medicamento"

# ── Columnas del reporte ObServer ─────────────────────────────────────────────
OBS_NOMBRE = "Producto"          # nombre largo, ej. "G-IBUPROFENO 600 COM x 10"
OBS_VENTAS = "Envases"           # unidades vendidas en el mes
OBS_STOCK = "Stock Disp."        # existencia física
OBS_BARCODE = "Cód. Barra"       # código de barras (para join preferido)

# Meses activos para productos estacionales (1-indexed)
ESTACIONAL_MESES = {5, 6, 7}


def _normalizar(nombre: str) -> str:
    """Limpia prefijos de lab ('G-', 'R-') y normaliza para join fuzzy."""
    s = re.sub(r"^[A-Z]{1,4}-", "", nombre.strip())
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def calcular_cantidad_visible(stock_actual, ventas_mes) -> int:
    prom = (ventas_mes / 4) if ventas_mes and ventas_mes > 0 else 0
    if prom == 0:
        return 0
    if stock_actual is not None and not (isinstance(stock_actual, float) and math.isnan(stock_actual)):
        return math.floor(min(stock_actual, prom) * 0.70)
    return math.floor(prom * 0.50)


def load_existing_overrides(path: Path) -> dict:
    """Devuelve {sku_id: {tipo_producto, pausado}} del CSV procesado anterior."""
    if not path.exists():
        return {}
    df = pd.read_csv(path, dtype=str)
    out = {}
    for _, row in df.iterrows():
        sid = str(row.get("sku_id", "")).strip()
        if sid:
            out[sid] = {
                "tipo_producto": row.get("tipo_producto", "regular"),
                "pausado": row.get("pausado", "False"),
            }
    return out


def run(catalogo_path: Path, observer_path: Path, output_path: Path,
        existing_path: Path | None, mes_actual: int):

    print(f"[1/4] Cargando catálogo base: {catalogo_path}")
    cat = pd.read_csv(catalogo_path, dtype=str, encoding="utf-8-sig")
    # Normalizar columnas por si vienen con encoding raro
    cat.columns = [c.strip() for c in cat.columns]

    print(f"[2/4] Cargando reporte ObServer: {observer_path}")
    if observer_path.suffix.lower() in (".xlsx", ".xls"):
        obs = pd.read_excel(observer_path)
    else:
        obs = pd.read_csv(observer_path)
    obs.columns = [c.strip() for c in obs.columns]

    # Verificar columnas mínimas del reporte
    obs_required = [OBS_NOMBRE, OBS_VENTAS]
    missing = [c for c in obs_required if c not in obs.columns]
    if missing:
        raise ValueError(f"Columnas faltantes en el reporte ObServer: {missing}\nColumnas: {list(obs.columns)}")

    print(f"[3/4] Haciendo JOIN catálogo ({len(cat)} SKUs) ↔ reporte ({len(obs)} filas)")

    # Construir índice de búsqueda del reporte: barcode y nombre normalizado
    obs["_nombre_norm"] = obs[OBS_NOMBRE].astype(str).apply(_normalizar)
    obs["_ventas"] = pd.to_numeric(obs[OBS_VENTAS], errors="coerce").fillna(0)
    obs["_stock"] = pd.to_numeric(obs.get(OBS_STOCK, pd.Series()), errors="coerce") if OBS_STOCK in obs.columns else None

    # Índice barcode → fila observer
    obs_by_barcode: dict[str, dict] = {}
    if OBS_BARCODE in obs.columns:
        for _, row in obs.iterrows():
            bc = str(row.get(OBS_BARCODE, "")).strip()
            if bc:
                obs_by_barcode[bc] = row.to_dict()

    # Índice nombre normalizado → fila observer
    obs_by_nombre: dict[str, dict] = {}
    for _, row in obs.iterrows():
        obs_by_nombre[row["_nombre_norm"]] = row.to_dict()

    overrides = load_existing_overrides(existing_path) if existing_path else {}

    rows = []
    matched = 0

    for _, cat_row in cat.iterrows():
        sku_id = str(cat_row.get(CAT_SKU_ID, "")).strip()
        nombre = str(cat_row.get(CAT_NOMBRE, "")).strip()
        barcode = str(cat_row.get(CAT_BARCODE, "")).strip()
        marca = str(cat_row.get(CAT_MARCA, "")).strip()
        laboratorio = str(cat_row.get(CAT_LABORATORIO, "")).strip()
        categoria = str(cat_row.get(CAT_CATEGORIA, "")).strip()
        es_med = str(cat_row.get(CAT_ES_MED, "")).strip()
        precio = 0.0
        try:
            precio = float(cat_row.get(CAT_PRECIO, 0) or 0)
        except (ValueError, TypeError):
            pass

        if not nombre or nombre == "nan":
            continue

        # Intentar JOIN: barcode primero, luego nombre normalizado
        obs_match = obs_by_barcode.get(barcode)
        if obs_match is None:
            nombre_norm = _normalizar(nombre)
            obs_match = obs_by_nombre.get(nombre_norm)

        ventas_mes = 0.0
        stock_actual = None
        if obs_match:
            ventas_mes = float(obs_match.get("_ventas") or 0)
            stock_raw = obs_match.get("_stock")
            if stock_raw is not None and not (isinstance(stock_raw, float) and math.isnan(stock_raw)):
                stock_actual = float(stock_raw)
            matched += 1

        override = overrides.get(sku_id, {})
        tipo_producto = override.get("tipo_producto", "regular")
        pausado = override.get("pausado", "False").lower() == "true"

        prom_semanal = round(ventas_mes / 4, 2) if ventas_mes else 0

        if tipo_producto == "estacional" and mes_actual not in ESTACIONAL_MESES:
            cantidad_visible = 0
        else:
            cantidad_visible = calcular_cantidad_visible(stock_actual, ventas_mes)

        rows.append({
            "sku_id": sku_id,
            "barcode": barcode,
            "sku_nombre": nombre,
            "sku_nombre_original": nombre,
            "marca": marca,
            "laboratorio": laboratorio,
            "categoria": categoria,
            "es_medicamento": es_med,
            "precio_venta": precio,
            "stock_actual": "" if stock_actual is None else stock_actual,
            "ventas_mes": ventas_mes,
            "prom_semanal": prom_semanal,
            "cantidad_visible": cantidad_visible,
            "tipo_producto": tipo_producto,
            "pausado": pausado,
        })

    result = pd.DataFrame(rows)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_path, index=False, encoding="utf-8")

    total = len(result)
    disponibles = (result["cantidad_visible"] > 0).sum()
    print(f"[4/4] Listo:")
    print(f"  Total SKUs procesados : {total}")
    print(f"  Con datos de ObServer : {matched}  ({100*matched//total if total else 0}% del catálogo)")
    print(f"  Disponibles (bot)     : {disponibles}")
    print(f"  Solo 'Consultar'      : {total - disponibles}")
    print(f"  Guardado en           : {output_path}")


def main():
    parser = argparse.ArgumentParser(description="ETL catálogo base + reporte ObServer → CSV procesado")
    parser.add_argument("--catalogo", required=True, help="Catálogo base (siemprebien_skus.csv)")
    parser.add_argument("--observer", required=True, help="Reporte mensual ObServer (.xlsx o .csv)")
    parser.add_argument("--output", default="data/catalogo_procesado.csv")
    parser.add_argument("--merge-existing", dest="existing", default=None,
                        help="CSV procesado anterior (preserva tipo_producto y pausado)")
    parser.add_argument("--mes", type=int, default=datetime.date.today().month)
    args = parser.parse_args()

    run(
        catalogo_path=Path(args.catalogo),
        observer_path=Path(args.observer),
        output_path=Path(args.output),
        existing_path=Path(args.existing) if args.existing else None,
        mes_actual=args.mes,
    )


if __name__ == "__main__":
    main()
