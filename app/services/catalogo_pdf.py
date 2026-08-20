"""
Convierte los PDFs "Informe de existencias" del sistema de la farmacia
(Zetti/similar) al CSV de catálogo formato B que consume SKUService.

CLI: python -m scripts.catalogo_desde_pdf a.pdf [b.pdf ...] -o data/catalogo_pdf.csv
    (convierte a CSV nuevo, sin fusionar — para inspeccionar el resultado)
Backoffice: POST /bo/sku/import-pdf (uno o más PDFs; FUSIONA con el catálogo
    actual por código de barras — actualiza precio/stock de lo que aparece
    en los PDF, deja el resto intacto, nunca toca el flag de receta de un
    producto existente).

Cada PDF trae la misma tabla: Laboratorio, Producto, Frac., Troquel,
Código de barra, Stock, Unidades, ..., Prom. vta, Valor. La categoría se
lee del filtro del encabezado ("Categoría igual a 'Alimentos'").

El parseo es por POSICIÓN de columnas (coordenadas x del encabezado), no por
regex sobre el texto plano: los nombres de producto y laboratorio contienen
espacios y números, y partir la línea por espacios mezclaba columnas.
"""

import csv
import io
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

COLUMNAS_B = [
    "sku_id", "barcode", "sku_nombre", "sku_nombre_original", "marca",
    "laboratorio", "categoria", "es_medicamento", "precio_venta",
    "stock_actual", "ventas_mes", "prom_semanal", "cantidad_visible",
    "tipo_producto", "pausado", "requiere_receta", "clasificacion",
]

# Encabezados del reporte, en orden. Se usan para ubicar los límites x.
_HEADERS = ["Laboratorio", "Producto", "Frac.", "Troquel", "Código de barra",
            "Stock", "Unidades", "Método reposición", "Cálculo mín.", "Mín",
            "Max", "Frecuencia", "Cant.", "Prom. vta", "Valor"]

_CAT_RE = re.compile(r'Categor[ií]a\s+igual\s+a\s+"([^"]+)"', re.IGNORECASE)


def _num(s: str) -> float:
    """'20497,29' → 20497.29 · '1.234,56' → 1234.56 · '' → 0.0"""
    s = (s or "").strip().replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _lineas(page) -> list[list[dict]]:
    """Palabras agrupadas por renglón (tolerancia vertical de 2pt)."""
    words = page.extract_words(keep_blank_chars=False)
    lineas: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lineas and abs(lineas[-1][0]["top"] - w["top"]) <= 2:
            lineas[-1].append(w)
        else:
            lineas.append([w])
    return lineas


def _limites_columnas(linea_header: list[dict]) -> list[float] | None:
    """
    Devuelve los x0 de cada columna del encabezado (15 valores) o None si el
    renglón no es el encabezado completo. Los encabezados multi-palabra
    ("Código de barra") se identifican por su primera palabra.
    """
    primeras = [h.split()[0] for h in _HEADERS]
    xs, i = [], 0
    for w in sorted(linea_header, key=lambda w: w["x0"]):
        if i < len(primeras) and w["text"].strip().rstrip(".").lower().startswith(
                primeras[i].rstrip(".").lower()[:5]):
            xs.append(w["x0"])
            i += 1
    return xs if len(xs) == len(_HEADERS) else None


def _celdas(linea: list[dict], limites: list[float]) -> list[str]:
    """Asigna cada palabra a su columna según x0 y concatena por columna."""
    celdas = [""] * len(limites)
    for w in sorted(linea, key=lambda w: w["x0"]):
        col = 0
        for i, x in enumerate(limites):
            # margen de 3pt: los valores numéricos alinean a la derecha y
            # pueden arrancar apenas antes del x0 del título de su columna
            if w["x0"] >= x - 3:
                col = i
        celdas[col] = f"{celdas[col]} {w['text']}".strip()
    return celdas


def parsear_pdf(origen, nombre: str = "") -> tuple[str, list[dict]]:
    """
    Devuelve (categoria, filas). Cada fila: laboratorio, producto, troquel,
    barcode, stock, prom_vta, valor. `origen` puede ser un path o bytes
    (upload del backoffice).
    """
    import pdfplumber

    if isinstance(origen, bytes):
        origen = io.BytesIO(origen)
    else:
        nombre = nombre or Path(str(origen)).stem
    categoria, filas = "", []
    with pdfplumber.open(origen) as pdf:
        limites = None
        for page in pdf.pages:
            if not categoria:
                m = _CAT_RE.search(page.extract_text() or "")
                if m:
                    categoria = m.group(1).strip()
            for linea in _lineas(page):
                nuevos = _limites_columnas(linea)
                if nuevos:
                    limites = nuevos
                    continue
                if not limites:
                    continue
                c = _celdas(linea, limites)
                barcode = c[4].replace(" ", "")
                if not re.fullmatch(r"\d{8,14}", barcode):
                    continue
                # "Prom. vta" y "Valor" alinean a la derecha y suelen caer
                # juntos en la misma celda: se toman los dos últimos números.
                cola = f"{c[13]} {c[14]}".split()
                if len(cola) < 2:
                    continue
                filas.append({
                    "laboratorio": c[0],
                    "producto": c[1],
                    "troquel": c[3].strip(),
                    "barcode": barcode,
                    "stock": _num(c[5]),
                    "prom_vta": _num(cola[0]),
                    "valor": _num(cola[-1]),
                })
    return categoria or nombre or "Sin categoría", filas


def a_fila_catalogo(r: dict, categoria: str) -> dict:
    es_medicamento = categoria.strip().lower().startswith("medicamento")
    nombre = f"{r['laboratorio']} {r['producto']}".strip()
    sku_id = r["troquel"] if r["troquel"] not in ("", "0") else r["barcode"]
    # "Valor" del reporte es la valorización TOTAL (stock × precio unitario):
    # verificado contra ventas reales (Koleston: stock 2, valor 19110.56 =
    # 2 × $9555.28). El unitario sale de dividir; stock negativo (ajustes de
    # inventario) divide igual y como disponibilidad cuenta 0.
    stock = r["stock"]
    precio = round(r["valor"] / stock, 2) if stock else 0.0
    return {
        "sku_id": sku_id,
        "barcode": r["barcode"],
        "sku_nombre": nombre,
        "sku_nombre_original": nombre,
        "marca": "",
        "laboratorio": r["laboratorio"],
        "categoria": categoria,
        "es_medicamento": "si" if es_medicamento else "no",
        "precio_venta": f"{precio:.2f}",
        "stock_actual": f"{max(stock, 0):g}",
        "ventas_mes": f"{r['prom_vta'] * 4:.1f}",
        "prom_semanal": f"{r['prom_vta']:.2f}",
        "cantidad_visible": f"{max(int(stock), 0)}",
        "tipo_producto": "regular",
        "pausado": "False",
        # "ambiguo" en medicamentos: deriva salvo lista blanca OTC (modo
        # conservador). El resto de las categorías es venta libre.
        "requiere_receta": "ambiguo" if es_medicamento else "no",
        "clasificacion": "",
    }


def fusionar_con_catalogo(catalogo_actual: list, pdfs: list[tuple[bytes | str, str]]) -> tuple[str, dict]:
    """
    Actualiza precio/stock de los productos que aparecen en los PDFs y deja
    intacto todo lo demás del catálogo actual — nunca reemplaza la lista
    completa. El cruce es por código de barras.

    El campo `requiere_receta` de un producto YA EXISTENTE nunca se toca: lo
    define la farmacia/el catálogo curado, no el reporte de stock. Solo a un
    producto NUEVO (que no estaba en el catálogo) se le asigna receta con la
    heurística por categoría (ambiguo en medicamentos, no en el resto).

    `catalogo_actual`: lista de objetos SKU (SKUService.todos()).
    Devuelve (csv_completo, resumen) — el CSV trae el catálogo COMPLETO
    (existentes + actualizados + nuevos), listo para reemplazar el archivo.
    """
    por_barcode: dict[str, dict] = {}
    for sku in catalogo_actual:
        por_barcode[sku.barcode] = {
            "sku_id": sku.sku_id, "barcode": sku.barcode,
            "sku_nombre": sku.sku_nombre, "sku_nombre_original": sku.sku_nombre_original,
            "marca": sku.marca, "laboratorio": sku.laboratorio, "categoria": sku.categoria,
            "es_medicamento": "si" if sku.es_medicamento else "no",
            "precio_venta": f"{sku.precio_venta:.2f}",
            "stock_actual": "" if sku.stock_actual is None else f"{sku.stock_actual:g}",
            "ventas_mes": "" if sku.ventas_mes is None else f"{sku.ventas_mes:.1f}",
            "prom_semanal": "" if sku.prom_semanal is None else f"{sku.prom_semanal:.2f}",
            "cantidad_visible": f"{sku.cantidad_visible}",
            "tipo_producto": sku.tipo_producto, "pausado": str(sku.pausado),
            "requiere_receta": sku.requiere_receta, "clasificacion": sku.clasificacion,
        }

    barcodes_originales = set(por_barcode.keys())
    actualizados: set[str] = set()
    nuevos = 0
    resumen: dict[str, int] = {}
    for origen, nombre in pdfs:
        categoria, filas = parsear_pdf(origen, nombre)
        resumen[f"{nombre} ({categoria})"] = len(filas)
        for r in filas:
            existente = por_barcode.get(r["barcode"])
            if existente:
                # Solo precio y stock — el resto (nombre, receta, categoría,
                # clasificación) lo conserva el catálogo actual.
                stock = r["stock"]
                precio = round(r["valor"] / stock, 2) if stock else 0.0
                existente["precio_venta"] = f"{precio:.2f}"
                existente["stock_actual"] = f"{max(stock, 0):g}"
                existente["cantidad_visible"] = f"{max(int(stock), 0)}"
                existente["ventas_mes"] = f"{r['prom_vta'] * 4:.1f}"
                existente["prom_semanal"] = f"{r['prom_vta']:.2f}"
                actualizados.add(r["barcode"])
            else:
                por_barcode[r["barcode"]] = a_fila_catalogo(r, categoria)
                nuevos += 1

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNAS_B, lineterminator="\n")
    w.writeheader()
    for fila in por_barcode.values():
        w.writerow(fila)
    resumen["Actualizados (precio/stock)"] = len(actualizados)
    resumen["Nuevos (no estaban en el catálogo)"] = nuevos
    resumen["Sin cambios (no vinieron en los PDF)"] = len(barcodes_originales - actualizados)
    resumen["TOTAL catálogo resultante"] = len(por_barcode)
    return buf.getvalue(), resumen


def convertir_a_csv(pdfs: list[tuple[bytes | str, str]]) -> tuple[str, dict]:
    """
    Parsea todos los PDFs [(bytes_o_path, nombre), ...] y devuelve
    (contenido_csv, resumen). Deduplica por código de barras (el último pisa).
    """
    todas: dict[str, dict] = {}
    resumen: dict[str, int] = {}
    for origen, nombre in pdfs:
        categoria, filas = parsear_pdf(origen, nombre)
        resumen[f"{nombre} ({categoria})"] = len(filas)
        for r in filas:
            todas[r["barcode"]] = a_fila_catalogo(r, categoria)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNAS_B, lineterminator="\n")
    w.writeheader()
    for fila in todas.values():
        w.writerow(fila)
    resumen["TOTAL (sin duplicados)"] = len(todas)
    return buf.getvalue(), resumen


def convertir(pdfs: list[str], salida: str) -> dict:
    """Versión CLI: escribe el CSV a disco. Devuelve el resumen."""
    contenido, resumen = convertir_a_csv([(p, Path(p).name) for p in pdfs])
    Path(salida).write_text(contenido, encoding="utf-8")
    return resumen
