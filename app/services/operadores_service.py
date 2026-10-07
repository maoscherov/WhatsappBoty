"""
Operadores del backoffice (6/10): la lista de quién usa el sistema.

`canonico(nombre)` traduce un nombre o alias ("lorena", "LORE ") al nombre de
la lista; se aplica al registrar acciones (tomar, marcar, enviar, armar
pedidos) y al mostrarlas, así lo viejo y lo nuevo quedan unificados. Un
nombre que no está en la lista se deja tal cual (nunca se rechaza una acción
por eso).
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_CANON: dict[str, str] = {}          # nombre/alias en minúsculas → nombre de la lista
_COLS = "id, nombre, aliases, activo, nota, created_at, updated_at"


def _k(nombre: Optional[str]) -> str:
    return " ".join((nombre or "").split()).lower()


def canonico(nombre: Optional[str]) -> Optional[str]:
    if not nombre or not str(nombre).strip():
        return nombre
    return _CANON.get(_k(nombre), " ".join(str(nombre).split()))


def _aplicar(filas) -> None:
    _CANON.clear()
    for f in filas or []:
        _CANON[_k(f["nombre"])] = f["nombre"]
        for a in f["aliases"] or []:
            _CANON[_k(a)] = f["nombre"]


async def cargar(db) -> int:
    if db is None or not db.available():
        return 0
    filas = await db.fetch(f"SELECT {_COLS} FROM operadores")
    _aplicar(filas)
    return len(filas or [])


def _a_dict(f) -> dict:
    d = dict(f)
    d["aliases"] = list(d.get("aliases") or [])
    for k in ("created_at", "updated_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    return d


async def listar(db, todos: bool = False) -> list[dict]:
    where = "" if todos else "WHERE activo"
    filas = await db.fetch(f"SELECT {_COLS} FROM operadores {where} ORDER BY lower(nombre)")
    return [_a_dict(f) for f in filas or []]


async def crear(db, nombre: str, aliases: Optional[list[str]] = None,
                nota: Optional[str] = None) -> dict:
    nombre = " ".join((nombre or "").split())
    if not nombre:
        raise ValueError("Falta el nombre")
    async with db.transaction() as con:
        if await con.fetchrow("SELECT 1 FROM operadores WHERE lower(nombre) = lower($1)", nombre):
            raise ValueError(f"Ya existe un operador llamado {nombre}")
        f = await con.fetchrow(
            f"INSERT INTO operadores (nombre, aliases, nota) VALUES ($1, $2, $3) RETURNING {_COLS}",
            nombre, [a.strip() for a in (aliases or []) if a.strip()], nota)
    await cargar(db)
    return _a_dict(f)


async def actualizar(db, id_: int, nombre: Optional[str] = None, activo: Optional[bool] = None,
                     aliases: Optional[list[str]] = None, nota: Optional[str] = None) -> Optional[dict]:
    """Renombrar guarda el nombre viejo como alias (lo registrado antes sigue
    apuntando a esta persona)."""
    async with db.transaction() as con:
        actual = await con.fetchrow(f"SELECT {_COLS} FROM operadores WHERE id = $1", id_)
        if actual is None:
            return None
        nuevos_alias = list(actual["aliases"] or [])
        if aliases is not None:
            nuevos_alias = [a.strip() for a in aliases if a.strip()]
        if nombre is not None:
            nombre = " ".join(nombre.split())
            if not nombre:
                raise ValueError("El nombre no puede quedar vacío")
            otro = await con.fetchrow(
                "SELECT id FROM operadores WHERE lower(nombre) = lower($1) AND id <> $2", nombre, id_)
            if otro:
                raise ValueError(f"Ya existe {nombre}: usá Fusionar")
            if _k(nombre) != _k(actual["nombre"]) and actual["nombre"] not in nuevos_alias:
                nuevos_alias.append(actual["nombre"])
        f = await con.fetchrow(
            f"""UPDATE operadores SET nombre = COALESCE($2, nombre), activo = COALESCE($3, activo),
                    aliases = $4, nota = COALESCE($5, nota), updated_at = now()
                WHERE id = $1 RETURNING {_COLS}""",
            id_, nombre, activo, nuevos_alias, nota)
    await cargar(db)
    return _a_dict(f)


async def fusionar(db, origen_id: int, destino_id: int) -> Optional[dict]:
    """"lorena" es "Lore": el origen pasa a ser alias del destino y se borra."""
    if origen_id == destino_id:
        raise ValueError("Elegí dos operadores distintos")
    async with db.transaction() as con:
        o = await con.fetchrow(f"SELECT {_COLS} FROM operadores WHERE id = $1", origen_id)
        d = await con.fetchrow(f"SELECT {_COLS} FROM operadores WHERE id = $1", destino_id)
        if o is None or d is None:
            return None
        aliases = list(dict.fromkeys(list(d["aliases"] or []) + [o["nombre"]] + list(o["aliases"] or [])))
        await con.execute("DELETE FROM operadores WHERE id = $1", origen_id)
        f = await con.fetchrow(
            f"UPDATE operadores SET aliases = $2, updated_at = now() WHERE id = $1 RETURNING {_COLS}",
            destino_id, aliases)
    await cargar(db)
    return _a_dict(f)


async def actividad(db, dias: int = 7) -> list[dict]:
    """
    Por operador: última acción (tomar, derivación atendida, pedido, mensaje,
    marca) y cuántas hizo hoy. Sirve para ver quién está activo ahora.
    """
    ops = await listar(db, todos=False)
    out = []
    for o in ops:
        nombres = [o["nombre"]] + o["aliases"]
        r = await db.fetchrow(
            """
            WITH acc AS (
                SELECT created_at FROM eventos
                 WHERE ref = ANY($1::text[]) AND tipo IN
                       ('conversacion_tomada', 'derivacion_atendida', 'pedido_operador',
                        'conversacion_devuelta')
                   AND created_at > now() - make_interval(days => $2)
                UNION ALL
                SELECT created_at FROM messages
                 WHERE role = 'operator' AND autor = ANY($1::text[])
                   AND created_at > now() - make_interval(days => $2)
                UNION ALL
                SELECT created_at FROM marcas
                 WHERE autor = ANY($1::text[]) AND created_at > now() - make_interval(days => $2)
            )
            SELECT max(created_at) AS ultima,
                   count(*) FILTER (WHERE (created_at AT TIME ZONE INTERVAL '-03:00')::date
                                    = (now() AT TIME ZONE INTERVAL '-03:00')::date) AS hoy
            FROM acc""", nombres, dias)
        out.append({**o, "ultima_actividad": r["ultima"].isoformat() if r and r["ultima"] else None,
                    "acciones_hoy": int(r["hoy"] or 0) if r else 0})
    out.sort(key=lambda x: x["ultima_actividad"] or "", reverse=True)
    return out
