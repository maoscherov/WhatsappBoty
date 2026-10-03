"""
Alias de productos (capa B, 3/10): nombre legible + términos de cliente por
SKU, generados con un prompt por tandas e importados como CSV:

    id;nombre_legible;tipo;terminos;seguro

Entran al índice de búsqueda (SKUService.from_rows) solo mientras el nombre
del ERP siga siendo el que se tradujo (nombre_base).
"""

import csv
import io
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

COLUMNAS = ("id", "nombre_legible", "tipo", "terminos", "seguro")


def parsear_csv(contenido: bytes | str) -> tuple[list[dict], list[str]]:
    """
    Lee la salida del prompt. Tolera BOM, bloque de código (```), espacios y
    líneas en blanco. Devuelve (filas válidas, errores legibles).
    """
    texto = contenido.decode("utf-8-sig", errors="replace") if isinstance(contenido, bytes) \
        else contenido.lstrip("﻿")
    lineas = [l for l in texto.splitlines()
              if l.strip() and not l.strip().startswith("```")]
    if not lineas:
        return [], ["El archivo está vacío"]
    lector = csv.reader(lineas, delimiter=";")
    encabezado = [c.strip().lower() for c in next(lector)]
    if encabezado[:5] != list(COLUMNAS):
        return [], [f"Encabezado inesperado: {';'.join(encabezado)} "
                    f"(se espera {';'.join(COLUMNAS)})"]
    filas, errores, vistos = [], [], set()
    for n, campos in enumerate(lector, start=2):
        campos = [c.strip() for c in campos]
        if len(campos) < 2:
            errores.append(f"Línea {n}: faltan columnas")
            continue
        campos += [""] * (5 - len(campos))
        id_, legible, tipo, terminos, seguro = campos[:5]
        if not id_ or not legible:
            errores.append(f"Línea {n}: falta id o nombre_legible")
            continue
        if id_ in vistos:
            errores.append(f"Línea {n}: id {id_} repetido (vale el primero)")
            continue
        vistos.add(id_)
        terminos = ", ".join(t.strip() for t in re.split(r"[,|]", terminos) if t.strip())
        filas.append({"external_id": id_, "nombre_legible": legible[:300],
                      "tipo": tipo[:80] or None, "terminos": terminos[:500] or None,
                      "seguro": seguro.strip().upper() not in ("NO", "N", "FALSE", "0")})
    return filas, errores


def texto_indice(alias: Optional[dict], nombre_actual: str) -> str:
    """Texto que suma al índice, o "" si no hay alias o quedó viejo."""
    if not alias or (alias.get("nombre_base") or "").strip() != (nombre_actual or "").strip():
        return ""
    partes = [alias.get("nombre_legible") or "", alias.get("tipo") or ""]
    if alias.get("seguro", True):
        partes.append((alias.get("terminos") or "").replace(",", " "))
    return " ".join(p for p in partes if p)


async def importar(db, branch: str, filas: list[dict], nombres: dict[str, str],
                   autor: Optional[str] = None, origen: str = "prompt") -> dict:
    """
    Upsert de los alias. `nombres` = nombre actual del ERP por external_id: los
    ids que no están en el catálogo se informan y no se cargan.
    """
    conocidas = [f for f in filas if f["external_id"] in nombres]
    desconocidos = [f["external_id"] for f in filas if f["external_id"] not in nombres]
    previos = {r["external_id"] for r in await db.fetch(
        "SELECT external_id FROM sku_alias WHERE branch_id = $1 AND external_id = ANY($2::text[])",
        branch, [f["external_id"] for f in conocidas])} if conocidas else set()
    async with db.transaction() as con:
        await con.executemany(
            """INSERT INTO sku_alias (branch_id, external_id, nombre_base, nombre_legible, tipo,
                                      terminos, seguro, origen, autor, updated_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now())
               ON CONFLICT (branch_id, external_id) DO UPDATE SET
                   nombre_base = EXCLUDED.nombre_base, nombre_legible = EXCLUDED.nombre_legible,
                   tipo = EXCLUDED.tipo, terminos = EXCLUDED.terminos, seguro = EXCLUDED.seguro,
                   origen = EXCLUDED.origen, autor = EXCLUDED.autor, updated_at = now()""",
            [(branch, f["external_id"], nombres[f["external_id"]], f["nombre_legible"], f["tipo"],
              f["terminos"], f["seguro"], origen, autor) for f in conocidas])
    return {"importados": len(conocidas), "nuevos": len(conocidas) - len(previos),
            "actualizados": len(previos), "dudosos": sum(1 for f in conocidas if not f["seguro"]),
            "ids_desconocidos": desconocidos[:50], "total_desconocidos": len(desconocidos)}


async def listar(db, branch: str, q: str = "", solo_dudosos: bool = False,
                 page: int = 1, page_size: int = 50) -> dict:
    cond, args = ["a.branch_id = $1"], [branch]
    if q.strip():
        args.append(f"%{q.strip().lower()}%")
        cond.append(f"(lower(a.nombre_legible) LIKE ${len(args)} OR lower(a.nombre_base) LIKE ${len(args)}"
                    f" OR lower(coalesce(a.terminos, '')) LIKE ${len(args)})")
    if solo_dudosos:
        cond.append("NOT a.seguro")
    where = " AND ".join(cond)
    total = await db.fetchrow(f"SELECT count(*) AS n FROM sku_alias a WHERE {where}", *args)
    args += [page_size, (page - 1) * page_size]
    filas = await db.fetch(
        f"""SELECT a.external_id, a.nombre_base, a.nombre_legible, a.tipo, a.terminos, a.seguro,
                   a.origen, a.autor, a.updated_at, i.name AS nombre_actual
            FROM sku_alias a LEFT JOIN catalog_items i
              ON i.branch_id = a.branch_id AND i.external_id = a.external_id
            WHERE {where} ORDER BY a.nombre_legible
            LIMIT ${len(args) - 1} OFFSET ${len(args)}""", *args)
    items = []
    for f in filas:
        d = dict(f)
        d["vigente"] = (d.get("nombre_actual") or "").strip() == (d.get("nombre_base") or "").strip()
        d["updated_at"] = d["updated_at"].isoformat() if d.get("updated_at") else None
        items.append(d)
    return {"total": int(total["n"]) if total else 0, "page": page, "items": items}


async def cobertura(db, branch: str) -> dict:
    """Cuántos productos con stock tienen alias vigente."""
    r = await db.fetchrow(
        """SELECT count(*) FILTER (WHERE i.stock > 0 AND i.active) AS con_stock,
                  count(a.external_id) FILTER (WHERE i.stock > 0 AND i.active
                                               AND a.nombre_base = i.name) AS con_alias
           FROM catalog_items i LEFT JOIN sku_alias a
             ON a.branch_id = i.branch_id AND a.external_id = i.external_id
           WHERE i.branch_id = $1""", branch)
    return {"con_stock": int(r["con_stock"] or 0), "con_alias": int(r["con_alias"] or 0)} if r \
        else {"con_stock": 0, "con_alias": 0}
