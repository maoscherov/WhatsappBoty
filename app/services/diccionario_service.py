"""
Diccionario del catálogo: abreviaturas de góndola y sinónimos de cliente.

Postgres (tabla catalogo_diccionario) es la fuente; las listas del código
(catalogo_enriquecido.ABREVIATURAS, sku_service.SINONIMOS) quedan como base
por si la base no está disponible. Al cargar se parte SIEMPRE de esa base y
se aplica la tabla encima: una entrada `activa` agrega o reemplaza, una
`descartada` saca una de la base, una `propuesta` no se usa todavía.

Las abreviaturas entran al índice de búsqueda, que se arma al cargar el
catálogo: después de cambiarlas hay que recargarlo (lo hace el router).
"""

import logging
import re
from collections import Counter
from typing import Optional

from app.services import catalogo_enriquecido as ce
from app.services import sku_service as ss

logger = logging.getLogger(__name__)

TIPOS = ("abreviatura", "sinonimo")
ESTADOS = ("activa", "propuesta", "descartada")

# Base del código, congelada al importar: cada carga parte de acá.
_BASE_ABREV: dict[str, str] = dict(ce.ABREVIATURAS)
_BASE_SINON: dict[str, list[str]] = {k: list(v) for k, v in ss.SINONIMOS.items()}

_COLUMNAS = ("id, tipo, termino, equivale, estado, origen, nota, autor, "
             "created_at, updated_at")


def normalizar_termino(tipo: str, termino: str) -> str:
    t = re.sub(r"\s+", " ", (termino or "").strip().lower())
    if tipo == "abreviatura":
        t = t.strip(".")
    return t


def _lista(equivale: str) -> list[str]:
    return [p.strip().lower() for p in (equivale or "").split(",") if p.strip()]


def aplicar(filas: list[dict]) -> dict:
    """Arma los diccionarios en memoria desde la base del código + `filas`."""
    abrev = dict(_BASE_ABREV)
    sinon = {k: list(v) for k, v in _BASE_SINON.items()}
    for f in filas:
        tipo, termino, estado = f["tipo"], normalizar_termino(f["tipo"], f["termino"]), f["estado"]
        if tipo == "abreviatura":
            if estado == "activa":
                abrev[termino] = (f["equivale"] or "").strip().lower()
            elif estado == "descartada":
                abrev.pop(termino, None)
        elif tipo == "sinonimo":
            if estado == "activa":
                sinon[termino] = _lista(f["equivale"])
            elif estado == "descartada":
                sinon.pop(termino, None)
    # Se reemplaza el CONTENIDO (no el objeto): los módulos que los importaron
    # ven el cambio sin re-importar.
    ce.ABREVIATURAS.clear()
    ce.ABREVIATURAS.update(abrev)
    ss.SINONIMOS.clear()
    ss.SINONIMOS.update(sinon)
    return {"abreviaturas": len(abrev), "sinonimos": len(sinon)}


async def cargar(db) -> dict:
    """Lee la tabla y la aplica. Sin base, queda la lista del código."""
    if db is None or not db.available():
        return aplicar([])
    filas = await db.fetch(f"SELECT {_COLUMNAS} FROM catalogo_diccionario")
    res = aplicar([dict(f) for f in filas or []])
    logger.info(f"Diccionario del catálogo: {res}")
    return res


async def listar(db, tipo: Optional[str] = None, estado: Optional[str] = None,
                 q: str = "") -> list[dict]:
    cond, args = [], []
    if tipo:
        args.append(tipo)
        cond.append(f"tipo = ${len(args)}")
    if estado:
        args.append(estado)
        cond.append(f"estado = ${len(args)}")
    if q.strip():
        args.append(f"%{q.strip().lower()}%")
        cond.append(f"(lower(termino) LIKE ${len(args)} OR lower(equivale) LIKE ${len(args)})")
    where = f"WHERE {' AND '.join(cond)}" if cond else ""
    filas = await db.fetch(
        f"SELECT {_COLUMNAS} FROM catalogo_diccionario {where} "
        "ORDER BY CASE estado WHEN 'propuesta' THEN 0 WHEN 'activa' THEN 1 ELSE 2 END, "
        "tipo, termino", *args)
    return [_a_dict(f) for f in filas or []]


def _a_dict(f) -> dict:
    d = dict(f)
    for k in ("created_at", "updated_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    return d


async def guardar(db, tipo: str, termino: str, equivale: str, estado: str = "activa",
                  nota: Optional[str] = None, autor: Optional[str] = None,
                  origen: str = "farmacia") -> dict:
    """Alta o modificación por (tipo, termino). Lanza ValueError si no es válido."""
    if tipo not in TIPOS:
        raise ValueError("tipo: abreviatura | sinonimo")
    if estado not in ESTADOS:
        raise ValueError("estado: activa | propuesta | descartada")
    termino = normalizar_termino(tipo, termino)
    equivale = ", ".join(_lista(equivale)) if tipo == "sinonimo" else (equivale or "").strip().lower()
    if not termino or not equivale:
        raise ValueError("Faltan el término y su equivalencia")
    if tipo == "abreviatura" and not re.fullmatch(r"[a-z0-9áéíóúñ]+", termino):
        raise ValueError("La abreviatura es una sola palabra, sin espacios ni signos")
    async with db.transaction() as conn:
        fila = await conn.fetchrow(
            f"""INSERT INTO catalogo_diccionario (tipo, termino, equivale, estado, origen, nota, autor)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (tipo, termino) DO UPDATE SET
                    equivale = EXCLUDED.equivale, estado = EXCLUDED.estado,
                    nota = COALESCE(EXCLUDED.nota, catalogo_diccionario.nota),
                    autor = EXCLUDED.autor, updated_at = now()
                RETURNING {_COLUMNAS}""",
            tipo, termino, equivale, estado, origen, nota, autor)
    return _a_dict(fila)


async def actualizar(db, id_: int, estado: Optional[str] = None, equivale: Optional[str] = None,
                     nota: Optional[str] = None, autor: Optional[str] = None) -> Optional[dict]:
    if estado is not None and estado not in ESTADOS:
        raise ValueError("estado: activa | propuesta | descartada")
    async with db.transaction() as conn:
        actual = await conn.fetchrow(f"SELECT {_COLUMNAS} FROM catalogo_diccionario WHERE id = $1", id_)
        if actual is None:
            return None
        if equivale is not None:
            equivale = (", ".join(_lista(equivale)) if actual["tipo"] == "sinonimo"
                        else equivale.strip().lower())
            if not equivale:
                raise ValueError("La equivalencia no puede quedar vacía")
        fila = await conn.fetchrow(
            f"""UPDATE catalogo_diccionario SET
                    estado = COALESCE($2, estado), equivale = COALESCE($3, equivale),
                    nota = COALESCE($4, nota), autor = COALESCE($5, autor), updated_at = now()
                WHERE id = $1 RETURNING {_COLUMNAS}""",
            id_, estado, equivale, nota, autor)
    return _a_dict(fila)


async def eliminar(db, id_: int) -> bool:
    async with db.transaction() as conn:
        r = await conn.execute("DELETE FROM catalogo_diccionario WHERE id = $1", id_)
    return r.endswith(" 1")


def sugerencias(sku_svc, minimo: int = 5, limite: int = 60) -> list[dict]:
    """
    Siglas cortas que aparecen seguido en los nombres de los productos con
    stock y que el diccionario no conoce: candidatas para que la farmacia
    las cargue. Con 3 ejemplos de nombre cada una.
    """
    conocidas = set(ce.ABREVIATURAS)
    conteo: Counter = Counter()
    ejemplos: dict[str, list[str]] = {}
    for sku in getattr(sku_svc, "_skus", []):
        if getattr(sku, "sin_stock", False) or getattr(sku, "pausado", False):
            continue
        nombre = sku.sku_nombre_original or sku.sku_nombre or ""
        for tok in {t.lower() for t in ce._TOKEN_RE.findall(nombre)}:
            if 2 <= len(tok) <= 5 and not tok.isdigit() and tok not in conocidas:
                conteo[tok] += 1
                ej = ejemplos.setdefault(tok, [])
                if len(ej) < 3:
                    ej.append(nombre)
    # Palabras comunes que no son siglas: no se sugieren.
    comunes = {"de", "la", "las", "los", "el", "con", "sin", "para", "por", "en", "y", "x",
               "mg", "ml", "gr", "ui", "mcg", "cm", "env", "plus", "max", "ultra", "kids",
               "baby", "body", "men", "fem", "agua", "color", "dia", "noche", "oil", "set",
               "kit", "duo", "pack", "mini", "extra", "super", "total", "care", "fresh",
               "forte", "new", "the", "by", "on", "oral", "rec", "pro", "bl", "art"}
    salida = []
    for tok, n in conteo.most_common():
        if n < minimo:
            break
        if tok in comunes:
            continue
        salida.append({"termino": tok, "productos": n, "ejemplos": ejemplos.get(tok, [])})
        if len(salida) >= limite:
            break
    return salida
