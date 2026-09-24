"""
Condición de venta (receta sí/no) por código de barras — caso real 24/9.

El ERP no trae si un producto requiere receta y sus categorías dicen solo
"Medicamentos": la regla por categoría dejaba TODO como venta libre y el bot
no derivaba ninguna receta. La referencia sale del catálogo de la farmacia
(CSV con categoría "Medicamentos Bajo Receta" / "Venta Libre") y vive en
Postgres (`receta_referencia`); en memoria se guarda un dict barcode → flag
para que la derivación al escribir el catálogo sea instantánea.
"""
import csv
import io
import logging
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

_MAPA: dict[str, str] = {}
FUENTE_INICIAL = "data/catalogo_base.csv"


def buscar(barcodes: Iterable[str]) -> Optional[str]:
    """Flag de la referencia para el primer código de barras que cruce."""
    for b in barcodes or []:
        v = _MAPA.get(_norm(b))
        if v:
            return v
    return None


def cargada() -> int:
    return len(_MAPA)


def _norm(b) -> str:
    return "".join(ch for ch in str(b or "") if ch.isdigit()).lstrip("0")


def filas_desde_csv(data: bytes) -> list[tuple[str, str, str]]:
    """
    (barcode, nombre, flag) desde un catálogo con el formato base de la
    farmacia: Nombre, Codigo_Barras_1..4, Categoria. Misma regla que el CSV
    de siempre: categoría "Medicamentos Bajo Receta" → "si"; venta libre
    conocida o rubro no medicinal → "no". Si trae una columna
    requiere_receta explícita, esa manda.
    """
    from app.services.sku_service import _categoria_sin_receta, es_venta_libre
    texto = data.decode("utf-8-sig", errors="replace")
    lector = csv.DictReader(io.StringIO(texto))
    out: dict[str, tuple[str, str, str]] = {}
    for row in lector:
        nombre = (row.get("Nombre") or "").strip()
        categoria = (row.get("Categoria") or "").strip()
        explicito = (row.get("requiere_receta") or "").strip().lower()
        if explicito in ("si", "no", "ambiguo"):
            flag = explicito
        else:
            flag = "si" if categoria.lower() == "medicamentos bajo receta" else "no"
            if es_venta_libre(nombre) or _categoria_sin_receta(categoria):
                flag = "no"
        for col in ("Codigo_Barras_1", "Codigo_Barras_2", "Codigo_Barras_3", "Codigo_Barras_4"):
            b = _norm(row.get(col))
            if len(b) >= 7:
                # Ante duplicados gana "si": más vale derivar de más.
                if b not in out or flag == "si":
                    out[b] = (b, nombre[:200], flag)
    return list(out.values())


async def cargar_desde_db(db) -> int:
    global _MAPA
    rows = await db.fetch("SELECT barcode, requiere_receta FROM receta_referencia")
    _MAPA = {r["barcode"]: r["requiere_receta"] for r in rows}
    return len(_MAPA)


async def reemplazar(db, filas: list[tuple[str, str, str]], fuente: str) -> int:
    """Reemplaza la referencia completa (transacción) y actualiza la memoria."""
    async with db.transaction() as con:
        await con.execute("DELETE FROM receta_referencia")
        await con.executemany(
            "INSERT INTO receta_referencia (barcode, nombre, requiere_receta, fuente) "
            "VALUES ($1, $2, $3, $4)",
            [(b, n, f, fuente) for b, n, f in filas])
    return await cargar_desde_db(db)


async def recalcular_catalogo(db) -> dict:
    """
    Recalcula requiere_receta de TODO el catálogo ERP con la regla vigente
    (referencia + criterio conservador). Solo escribe las filas que cambian.
    """
    from app.services.catalog_rules import derivar_requiere_receta
    rows = await db.fetch(
        "SELECT branch_id, external_id, barcodes, name, category, rubro, subrubro, "
        "requiere_receta FROM catalog_items")
    cambios = []
    conteo = {"si": 0, "no": 0, "ambiguo": 0}
    for r in rows:
        nuevo = derivar_requiere_receta(r["category"], r["rubro"], r["subrubro"], r["name"],
                                        r["barcodes"] or [])
        conteo[nuevo] = conteo.get(nuevo, 0) + 1
        if nuevo != r["requiere_receta"]:
            cambios.append((nuevo, r["branch_id"], r["external_id"]))
    if cambios:
        await db.executemany(
            "UPDATE catalog_items SET requiere_receta = $1 WHERE branch_id = $2 AND external_id = $3",
            cambios)
    logger.info(f"Receta recalculada: {len(cambios)} productos cambiaron; totales {conteo}")
    return {"cambiados": len(cambios), "totales": conteo}


async def inicializar(db, ruta_csv: str = FUENTE_INICIAL) -> dict:
    """Al arrancar: carga la referencia; si la tabla está vacía, la siembra
    con el catálogo de la farmacia; y recalcula el catálogo ERP."""
    n = await cargar_desde_db(db)
    sembrado = False
    if n == 0:
        try:
            with open(ruta_csv, "rb") as f:
                filas = filas_desde_csv(f.read())
            n = await reemplazar(db, filas, fuente=ruta_csv)
            sembrado = True
        except FileNotFoundError:
            logger.warning(f"Sin referencia de receta: no está {ruta_csv}")
    res = await recalcular_catalogo(db)
    return {"referencia": n, "sembrado": sembrado, **res}


async def estado(db) -> dict:
    """Para el backoffice: tamaño de la referencia y cómo quedó el catálogo."""
    ref = await db.fetch(
        "SELECT requiere_receta, COUNT(*) AS n FROM receta_referencia GROUP BY 1")
    cat = await db.fetch(
        "SELECT requiere_receta, COUNT(*) AS n FROM catalog_items WHERE active GROUP BY 1")
    a_validar = await db.fetch(
        "SELECT name FROM catalog_items WHERE active AND requiere_receta = 'ambiguo' "
        "ORDER BY name LIMIT 50")
    return {
        "referencia": {r["requiere_receta"]: r["n"] for r in ref},
        "catalogo": {r["requiere_receta"]: r["n"] for r in cat},
        "a_validar_ejemplos": [r["name"] for r in a_validar],
    }
