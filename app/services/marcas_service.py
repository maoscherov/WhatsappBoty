"""
Marcas de error en conversaciones: los operadores señalan cuándo el bot se
equivocó (no derivó, cotizó mal, entendió mal...) para medir qué tan lista
está la operación para migrar sin supervisión humana. Una marca es sobre un
mensaje puntual (`message_id`) o sobre toda la conversación (NULL).

Los "indicadores" (spec de migración) definen una conversación como un día
con actividad del cliente, y la asocian a una marca por la fecha LOCAL
(Argentina) del mensaje marcado, o de la marca misma si es de conversación.
"""

from datetime import date, timedelta
from typing import Optional

from app.services.config_service import TZ_ARG

# Conversión de fecha local en SQL: Argentina no tiene horario de verano
# desde 2009, así que es UTC-3 fijo. Se usa `AT TIME ZONE INTERVAL` (offset)
# en vez del nombre de la zona porque algunas instalaciones de Postgres (p.
# ej. Windows, usadas en test) no traen la base de datos de husos horarios.
TZ_SQL = "AT TIME ZONE INTERVAL '-03:00'"

# Categorías fijas: clave interna → etiqueta para mostrar.
CATEGORIAS: dict[str, str] = {
    "receta_sin_derivar": "Receta sin derivar",
    "descuento_mal": "Descuento mal aplicado",
    "cotizacion_incompleta": "No cotizó todos los productos",
    "link_incompleto": "Link incompleto",
    "no_derivo": "No derivó cuando debía",
    "entendio_mal": "Entendió mal",
    "dato_incorrecto": "Dato incorrecto (precio, stock o dirección)",
    "otro": "Otro",
}

PISO_DIARIO = 20
META_PCT = 85
DIAS_NECESARIOS = 7


def categorias_lista() -> list[dict]:
    return [{"clave": k, "etiqueta": v} for k, v in CATEGORIAS.items()]


def _row_a_dict(r) -> dict:
    return {
        "id": r["id"], "phone": r["phone"], "message_id": r["message_id"],
        "categoria": r["categoria"], "etiqueta": CATEGORIAS.get(r["categoria"], r["categoria"]),
        "observacion": r["observacion"], "autor": r["autor"],
        "ts": r["created_at"].isoformat(),
    }


async def crear(db, phone: str, categoria: str, observacion: str,
                message_id: Optional[int] = None, autor: Optional[str] = None) -> dict:
    """Valida categoría/observación (el llamador traduce ValueError a 422) e inserta."""
    if categoria not in CATEGORIAS:
        raise ValueError(f"Categoría inválida: {categoria}")
    observacion = (observacion or "").strip()
    if not observacion:
        raise ValueError("La observación es obligatoria")
    row = await db.fetchrow(
        "INSERT INTO marcas (phone, message_id, categoria, observacion, autor) "
        "VALUES ($1, $2, $3, $4, $5) "
        "RETURNING id, phone, message_id, categoria, observacion, autor, created_at",
        phone, message_id, categoria, observacion, (autor or None),
    )
    if row is None:
        # La base rechazó el INSERT (queda en el log como "DB fetchrow error").
        raise RuntimeError("No se pudo guardar la marca")
    return _row_a_dict(row)


async def listar(db, phone: Optional[str] = None, desde: Optional[date] = None,
                 hasta: Optional[date] = None, categoria: Optional[str] = None) -> list[dict]:
    """Lista de marcas, más nuevas primero. `desde`/`hasta` son fechas locales
    (Argentina) inclusive, aplicadas sobre `created_at`."""
    conds = []
    args: list = []
    if phone:
        args.append(phone)
        conds.append(f"phone = ${len(args)}")
    if categoria:
        args.append(categoria)
        conds.append(f"categoria = ${len(args)}")
    if desde:
        args.append(desde)
        conds.append(f"(created_at {TZ_SQL})::date >= ${len(args)}")
    if hasta:
        args.append(hasta)
        conds.append(f"(created_at {TZ_SQL})::date <= ${len(args)}")
    where = f"WHERE {' AND '.join(conds)}" if conds else ""
    rows = await db.fetch(
        f"SELECT id, phone, message_id, categoria, observacion, autor, created_at "
        f"FROM marcas {where} ORDER BY created_at DESC",
        *args,
    )
    return [_row_a_dict(r) for r in rows]


async def eliminar(db, marca_id: int) -> bool:
    resultado = await db.execute("DELETE FROM marcas WHERE id = $1", marca_id)
    # asyncpg devuelve "DELETE n"
    return bool(resultado) and not resultado.endswith(" 0")


async def marcas_de_phone(db, phone: str) -> list[dict]:
    """Todas las marcas de un teléfono (para /bo/history): una sola query."""
    rows = await db.fetch(
        "SELECT id, phone, message_id, categoria, observacion, autor, created_at "
        "FROM marcas WHERE phone = $1 ORDER BY created_at DESC",
        phone,
    )
    return [_row_a_dict(r) for r in rows]


async def contar_por_phones(db, phones: list[str]) -> dict[str, int]:
    """Cantidad de marcas por teléfono (para /bo/conversaciones)."""
    if not phones:
        return {}
    rows = await db.fetch(
        "SELECT phone, COUNT(*) AS n FROM marcas WHERE phone = ANY($1) GROUP BY phone",
        phones,
    )
    return {r["phone"]: int(r["n"]) for r in rows}


async def para_export(db, desde: Optional[date] = None, hasta: Optional[date] = None) -> list[dict]:
    """Marcas + contenido del mensaje referenciado (truncado), para el CSV."""
    conds = []
    args: list = []
    if desde:
        args.append(desde)
        conds.append(f"(m.created_at {TZ_SQL})::date >= ${len(args)}")
    if hasta:
        args.append(hasta)
        conds.append(f"(m.created_at {TZ_SQL})::date <= ${len(args)}")
    where = f"WHERE {' AND '.join(conds)}" if conds else ""
    rows = await db.fetch(
        f"SELECT m.id, m.phone, m.message_id, m.categoria, m.observacion, m.autor, "
        f"m.created_at, msg.content AS mensaje "
        f"FROM marcas m LEFT JOIN messages msg ON msg.id = m.message_id "
        f"{where} ORDER BY m.created_at DESC",
        *args,
    )
    out = []
    for r in rows:
        d = _row_a_dict(r)
        mensaje = r["mensaje"] or ""
        d["mensaje"] = mensaje[:300]
        out.append(d)
    return out


def _es_finde(d: date) -> bool:
    return d.weekday() >= 5  # sábado=5, domingo=6


async def indicadores(db, desde: Optional[date] = None, hasta: Optional[date] = None) -> dict:
    """
    Indicadores diarios/de período usados para decidir si la operación puede
    migrar a producción sin supervisión. Se resuelve con dos queries simples
    y se agrega en Python (más legible que una sola query gigante).
    """
    import datetime as _dt
    if hasta is None:
        hasta = _dt.datetime.now(TZ_ARG).date()
    if desde is None:
        desde = hasta - timedelta(days=13)   # 14 días incluyendo `hasta`

    conv_rows = await db.fetch(
        f"SELECT DISTINCT phone, (created_at {TZ_SQL})::date AS fecha "
        f"FROM messages WHERE role = 'user' "
        f"AND (created_at {TZ_SQL})::date BETWEEN $1 AND $2",
        desde, hasta,
    )
    conv_pairs = {(r["phone"], r["fecha"]) for r in conv_rows}

    marca_rows = await db.fetch(
        f"SELECT m.phone, m.categoria, "
        f"COALESCE((msg.created_at {TZ_SQL})::date, "
        f"(m.created_at {TZ_SQL})::date) AS fecha "
        f"FROM marcas m LEFT JOIN messages msg ON msg.id = m.message_id "
        f"WHERE COALESCE((msg.created_at {TZ_SQL})::date, "
        f"(m.created_at {TZ_SQL})::date) BETWEEN $1 AND $2",
        desde, hasta,
    )

    conversaciones_por_dia: dict[date, int] = {}
    for phone, fecha in conv_pairs:
        conversaciones_por_dia[fecha] = conversaciones_por_dia.get(fecha, 0) + 1

    con_error_pares: set[tuple[str, date]] = set()
    recetas_por_dia: dict[date, int] = {}
    for r in marca_rows:
        fecha = r["fecha"]
        if r["categoria"] == "receta_sin_derivar":
            recetas_por_dia[fecha] = recetas_por_dia.get(fecha, 0) + 1
        if (r["phone"], fecha) in conv_pairs:
            con_error_pares.add((r["phone"], fecha))

    con_error_por_dia: dict[date, int] = {}
    for _, fecha in con_error_pares:
        con_error_por_dia[fecha] = con_error_por_dia.get(fecha, 0) + 1

    dias = []
    d = desde
    while d <= hasta:
        conversaciones = conversaciones_por_dia.get(d, 0)
        con_error = con_error_por_dia.get(d, 0)
        dias.append({
            "fecha": d.isoformat(),
            "conversaciones": conversaciones,
            "con_error": con_error,
            "exitosas": conversaciones - con_error,
            "llega_al_piso": conversaciones >= PISO_DIARIO,
            "recetas_sin_derivar": recetas_por_dia.get(d, 0),
        })
        d += timedelta(days=1)

    total_conversaciones = sum(x["conversaciones"] for x in dias)
    total_exitosas = sum(x["exitosas"] for x in dias)
    dias_validos = [x for x in dias if x["llega_al_piso"]]
    conv_validas = sum(x["conversaciones"] for x in dias_validos)
    exitosas_validas = sum(x["exitosas"] for x in dias_validos)
    pct_exitosas = round(100 * exitosas_validas / conv_validas, 1) if conv_validas else 0.0

    # Racha de días hábiles válidos consecutivos, contando desde el más
    # reciente hacia atrás. Sábados/domingos no cuentan ni cortan la racha.
    # El día en curso no corta la racha mientras no llegue al piso. Una receta
    # vendida sin derivar la reinicia: ese día no cuenta (minuta 24/9).
    hoy = _dt.datetime.now(TZ_ARG).date()
    racha = 0
    dias_racha = []
    for x in reversed(dias):
        fecha_d = date.fromisoformat(x["fecha"])
        if _es_finde(fecha_d):
            continue
        if fecha_d == hoy and not x["llega_al_piso"] and not x["recetas_sin_derivar"]:
            continue
        if x["llega_al_piso"] and not x["recetas_sin_derivar"]:
            racha += 1
            dias_racha.append(x)
        else:
            break
    conv_racha = sum(x["conversaciones"] for x in dias_racha)
    pct_racha = (round(100 * sum(x["exitosas"] for x in dias_racha) / conv_racha, 1)
                 if conv_racha else 0.0)

    recetas_sin_derivar_periodo = sum(x["recetas_sin_derivar"] for x in dias)
    reinicia_conteo = recetas_sin_derivar_periodo > 0
    habilita_migracion = racha >= DIAS_NECESARIOS and pct_racha >= META_PCT

    return {
        "dias": dias,
        "total_conversaciones": total_conversaciones,
        "total_exitosas": total_exitosas,
        "pct_exitosas": pct_exitosas,
        "meta_pct": META_PCT,
        "piso_diario": PISO_DIARIO,
        "dias_validos_seguidos": racha,
        "pct_exitosas_racha": pct_racha,   # sobre los días de la racha: el que habilita
        "dias_necesarios": DIAS_NECESARIOS,
        "recetas_sin_derivar": recetas_sin_derivar_periodo,
        "reinicia_conteo": reinicia_conteo,
        "habilita_migracion": habilita_migracion,
    }
