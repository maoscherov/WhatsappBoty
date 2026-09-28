"""
Condición de venta (receta sí/no) por código de barras.

Postgres (`receta_referencia`) es la ÚNICA fuente de verdad — ya NO se siembra
desde ningún archivo al arrancar (bug de prod 28/9: el CSV de siembra quedaba
pisado por el catálogo restaurado desde Redis, con otro formato de columnas
—sku_id,barcode,sku_nombre...—, y `filas_desde_csv` leía 0 filas SIN avisar:
la referencia quedó vacía en producción). Un archivo (CSV o Excel de la
farmacia) ahora solo sirve para SINCRONIZAR la referencia, en dos pasos: se
sube y arma una vista previa (nada se aplica, ver `preparar_sincronizacion`)
guardada en `receta_sincronizaciones`, y recién al confirmar
(`confirmar_sincronizacion`) se aplica sobre `receta_referencia` y se
recalcula `catalog_items.requiere_receta`.

En memoria se guarda un dict barcode → flag (`_MAPA`) para que la derivación
al escribir el catálogo (`catalog_rules.derivar_requiere_receta`) sea directa.
"""
import csv
import io
import json
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

_MAPA: dict[str, str] = {}


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


# ══════════════════════════════════════════════════════════════════════════
# Parser de archivos (CSV/Excel) — distintos exports (catálogo base de la
# farmacia, catálogo del bot, planillas ad-hoc) traen encabezados distintos;
# se reconocen por alias (sin acentos, en minúscula) en vez de exigir un
# nombre exacto de columna.
# ══════════════════════════════════════════════════════════════════════════

_ALIAS_NOMBRE = {"nombre", "sku_nombre", "descripcion", "producto", "articulo", "detalle"}
_ALIAS_CATEGORIA = {"categoria"}
_ALIAS_CATEGORIA_FALLBACK = {"rubro"}
_ALIAS_FLAG = {"requiere_receta", "receta", "bajo_receta", "condicion_venta", "condicion_de_venta"}
_ALIAS_VENTA_LIBRE = {"venta_libre"}
_ALIAS_ES_MEDICAMENTO = {"es_medicamento", "medicamento"}
# codigo_barras(_1..n) | codigo_de_barras(_n) | cod_barras(_n) | barcode(_n) |
# codigo_barra(_n) | ean | ean13
_RE_BARCODE_COL = re.compile(
    r"^(codigo_barras|codigo_de_barras|cod_barras|barcode|codigo_barra)(_\d+)?$"
    r"|^ean(13)?$"
)
_RE_CATALOGO_BASE = re.compile(r"^codigo_barras_\d+$")

_VAL_SI = {"si", "s", "true", "1", "x", "bajo receta", "con receta"}
_VAL_NO = {"no", "n", "false", "0", "venta libre", "libre", "otc"}
_VAL_AMBIGUO = {"ambiguo", "a validar"}
_INVERSO = {"si": "no", "no": "si", "ambiguo": "ambiguo"}
# Ante el mismo barcode con flags distintos en el archivo, gana el más
# conservador: más vale derivar de más que vender sin receta (mismo criterio
# que `derivar_requiere_receta`).
_PRIORIDAD_FLAG = {"si": 2, "ambiguo": 1, "no": 0}


def _sin_acentos(s: str) -> str:
    s = unicodedata.normalize("NFD", str(s or ""))
    return "".join(ch for ch in s if unicodedata.category(ch) != "Mn")


def _norm_header(s: str) -> str:
    """'Código de barras' → 'codigo_de_barras'; 'Codigo_Barras_1' → 'codigo_barras_1'."""
    s = _sin_acentos(s).lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def _texto_celda(v) -> str:
    """Celda de Excel a texto, sin '.0' en códigos que Excel guardó como número."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _flag_explicito(valor) -> Optional[str]:
    """Valor de una columna de flag (o de venta_libre, antes de invertir) → si/no/ambiguo/None."""
    s = _sin_acentos(_texto_celda(valor)).strip().lower()
    if s in _VAL_SI:
        return "si"
    if s in _VAL_NO:
        return "no"
    if s in _VAL_AMBIGUO:
        return "ambiguo"
    return None


def _analizar_headers(headers: list[str]) -> tuple[dict, str]:
    """
    headers ORIGINALES (en orden) → ({rol: header, "barcodes": [headers]}, formato).
    Un encabezado cubre como mucho un rol (el primero que matchea, en orden de
    aparición en el archivo).
    """
    cols = {"nombre": None, "categoria": None, "flag": None, "venta_libre": None,
            "es_medicamento": None, "barcodes": []}
    categoria_fallback = None
    normalizados = []
    for h in headers:
        norm = _norm_header(h)
        normalizados.append(norm)
        if not norm:
            continue
        if cols["nombre"] is None and norm in _ALIAS_NOMBRE:
            cols["nombre"] = h
        elif cols["categoria"] is None and norm in _ALIAS_CATEGORIA:
            cols["categoria"] = h
        elif categoria_fallback is None and norm in _ALIAS_CATEGORIA_FALLBACK:
            categoria_fallback = h
        elif cols["flag"] is None and norm in _ALIAS_FLAG:
            cols["flag"] = h
        elif cols["venta_libre"] is None and norm in _ALIAS_VENTA_LIBRE:
            cols["venta_libre"] = h
        elif cols["es_medicamento"] is None and norm in _ALIAS_ES_MEDICAMENTO:
            cols["es_medicamento"] = h
        elif _RE_BARCODE_COL.match(norm):
            cols["barcodes"].append(h)
    if cols["categoria"] is None:
        cols["categoria"] = categoria_fallback

    if any(_RE_CATALOGO_BASE.match(n) for n in normalizados):
        formato = "catalogo_base"
    elif "barcode" in normalizados and "sku_nombre" in normalizados:
        formato = "catalogo_bot"
    else:
        formato = "otro"
    return cols, formato


def _leer_csv(data: bytes) -> tuple[list[str], list[dict]]:
    try:
        texto = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = data.decode("latin-1")
    muestra = texto[:4096]
    try:
        delim = csv.Sniffer().sniff(muestra, delimiters=",;").delimiter
    except csv.Error:
        delim = ";" if muestra.count(";") > muestra.count(",") else ","
    lector = csv.DictReader(io.StringIO(texto), delimiter=delim)
    return list(lector.fieldnames or []), list(lector)


def _leer_xlsx(data: bytes) -> tuple[list[str], list[dict]]:
    """Primera hoja; el encabezado es la primera fila (de las 10 primeras) que
    trae alguna columna de código de barras reconocida."""
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        ws = wb.worksheets[0]
    except Exception as e:
        raise ValueError(f"No se pudo leer el archivo como Excel: {e}")

    filas_iter = ws.iter_rows(values_only=True)
    candidatas = []
    for i, row in enumerate(filas_iter):
        candidatas.append(row)
        if i >= 9:
            break

    headers, header_idx = [], None
    for i, row in enumerate(candidatas):
        crudos = [_texto_celda(c) for c in row]
        normset = {_norm_header(c) for c in crudos if c}
        if any(_RE_BARCODE_COL.match(n) for n in normset):
            headers, header_idx = crudos, i
            break
    if header_idx is None:
        return [], []

    filas = [dict(zip(headers, row)) for row in candidatas[header_idx + 1:]]
    filas += [dict(zip(headers, row)) for row in filas_iter]
    return headers, filas


def leer_archivo(data: bytes, nombre_archivo: str) -> tuple[list[tuple[str, str, str]], dict]:
    """
    (filas, info) desde un CSV o Excel de cualquier farmacia/export conocido.
    filas = [(barcode, nombre, flag)] sin barcodes repetidos (mismo código con
    flags distintos → gana "si", ver _PRIORIDAD_FLAG). info describe qué se
    reconoció en el archivo, para la vista previa y para el mensaje de error
    si no se encontró ningún código.
    """
    from app.services.sku_service import _categoria_sin_receta, es_venta_libre

    es_xlsx = data[:2] == b"PK" or nombre_archivo.lower().endswith((".xlsx", ".xls"))
    headers, filas_raw = _leer_xlsx(data) if es_xlsx else _leer_csv(data)
    cols, formato = _analizar_headers(headers)

    vistos: dict[str, list] = {}         # barcode → [nombre, flag]
    flags_vistos: dict[str, set] = {}    # barcode → {flags distintos vistos en el archivo}
    orden: list[str] = []
    total = 0
    sin_codigo = 0
    codigos_validos = 0
    ajustados = 0
    for row in filas_raw:
        # Fila completamente vacía (relleno al final del Excel): ni cuenta ni
        # ensucia sin_codigo.
        if not any(v not in (None, "") for v in row.values()):
            continue
        total += 1
        nombre = _texto_celda(row.get(cols["nombre"])) if cols["nombre"] else ""
        categoria = _texto_celda(row.get(cols["categoria"])) if cols["categoria"] else ""

        es_med = (_flag_explicito(row.get(cols["es_medicamento"])) == "si"
                  if cols["es_medicamento"] else False)
        cat = categoria.strip().lower()
        # Medicamento sin categoría que lo aclare ("Medicamentos" a secas, o
        # vacía con la marca de medicamento): no se sabe si lleva receta.
        sin_categoria_clara = (("medicament" in cat and cat != "medicamentos bajo receta")
                               or (not cat and es_med))
        otc = es_venta_libre(nombre) or _categoria_sin_receta(categoria)

        flag = _flag_explicito(row.get(cols["flag"])) if cols["flag"] else None
        if flag is None and cols["venta_libre"]:
            inv = _flag_explicito(row.get(cols["venta_libre"]))
            flag = _INVERSO[inv] if inv else None
        if flag == "no" and cols["es_medicamento"] and sin_categoria_clara and not otc:
            # 28/9: en el catálogo del bot, 460 medicamentos sin categoría
            # venían como venta libre por la regla vieja (RACORVAL 160 "no",
            # RACORVAL 80 "si"). Esa marca no es una validación: a validar.
            flag = "ambiguo"
            ajustados += 1
        if flag is None:
            # Categoría explícita "Medicamentos Bajo Receta" → con receta; la
            # lista blanca OTC y el rubro no medicinal → venta libre. Un
            # medicamento sin categoría clara → a validar (antes quedaba como
            # venta libre: 482 en el catálogo base, RACORVAL entre ellos).
            if otc:
                flag = "no"
            elif cat == "medicamentos bajo receta":
                flag = "si"
            elif sin_categoria_clara:
                flag = "ambiguo"
            else:
                flag = "no"

        bcs = []
        for h in cols["barcodes"]:
            b = _norm(row.get(h))
            if len(b) >= 7:
                bcs.append(b)
        if not bcs:
            sin_codigo += 1
            continue
        codigos_validos += len(bcs)
        for b in bcs:
            if b not in vistos:
                vistos[b] = [nombre[:200], flag]
                flags_vistos[b] = {flag}
                orden.append(b)
            else:
                flags_vistos[b].add(flag)
                if _PRIORIDAD_FLAG[flag] > _PRIORIDAD_FLAG[vistos[b][1]]:
                    vistos[b] = [nombre[:200], flag]

    filas = [(b, vistos[b][0], vistos[b][1]) for b in orden]
    conflictos = sum(1 for s in flags_vistos.values() if len(s) > 1)
    info = {
        "formato": formato,
        "columnas": {k: v for k, v in {
            "nombre": cols["nombre"], "categoria": cols["categoria"], "flag": cols["flag"],
            "venta_libre": cols["venta_libre"], "es_medicamento": cols["es_medicamento"],
            "barcodes": ", ".join(cols["barcodes"]) if cols["barcodes"] else None,
        }.items() if v},
        "filas": total,
        "codigos_validos": codigos_validos,
        "sin_codigo": sin_codigo,
        "conflictos_en_archivo": conflictos,
        # Medicamentos sin categoría clara marcados "venta libre" en el
        # archivo que pasaron a "a validar" (ver arriba).
        "ajustados_a_validar": ajustados,
    }
    return filas, info


def filas_desde_csv(data: bytes) -> list[tuple[str, str, str]]:
    """Wrapper fino sobre `leer_archivo` — para no romper a quien ya lo llama."""
    filas, _info = leer_archivo(data, "catalogo.csv")
    return filas


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


# ══════════════════════════════════════════════════════════════════════════
# Sincronización de la referencia desde un archivo: subir → vista previa
# (nada se aplica) → confirmar o descartar. Reemplaza a la carga automática
# desde un CSV al arrancar (ver docstring del módulo).
# ══════════════════════════════════════════════════════════════════════════

MODOS_VALIDOS = ("actualizar", "reemplazar")
VENCIMIENTO_PREVIEW = timedelta(hours=2)


class SincronizacionConflicto(Exception):
    """La sincronización no se puede confirmar/descartar en su estado actual."""


async def _version_ref(db) -> str:
    """'<total filas>|<updated_at más reciente>' — para detectar si la
    referencia cambió entre la vista previa y la confirmación."""
    row = await db.fetchrow(
        "SELECT count(*) AS n, coalesce(max(updated_at)::text, '') AS ts FROM receta_referencia")
    return f"{row['n']}|{row['ts']}" if row else "0|"


async def preparar_sincronizacion(db, data: bytes, nombre_archivo: str, modo: str,
                                  autor: Optional[str] = None) -> dict:
    """
    Arma la vista previa de una sincronización: NADA se aplica todavía. Calcula
    el impacto sobre la referencia y, simulando la referencia NUEVA (sin
    tocar la vigente), sobre el catálogo activo — y guarda un registro
    'pendiente' en receta_sincronizaciones para confirmar (o descartar)
    después. Cualquier pendiente anterior queda 'descartada' (uno solo a la vez).
    """
    from app.services.catalog_rules import derivar_requiere_receta

    if modo not in MODOS_VALIDOS:
        raise ValueError(f"modo inválido: {modo!r} (debe ser 'actualizar' o 'reemplazar')")

    filas, info = leer_archivo(data, nombre_archivo)
    if not filas:
        detalle = ", ".join(f"{rol}: {header}" for rol, header in info["columnas"].items())
        raise ValueError(
            "No se encontró ningún código de barras válido en el archivo. "
            f"Columnas reconocidas: {detalle or 'ninguna'}.")

    # ── Impacto sobre la referencia (contra lo que hay HOY en Postgres) ─────
    # La versión se toma ANTES de leer: si otra sincronización se aplica
    # mientras se calcula esta vista previa, la confirmación la rechaza.
    version_ref = await _version_ref(db)
    actual_rows = await db.fetch("SELECT barcode, nombre, requiere_receta FROM receta_referencia")
    actual = {r["barcode"]: (r["nombre"], r["requiere_receta"]) for r in actual_rows}

    if modo == "actualizar":
        nuevo_map = dict(actual)
        for b, n, f in filas:
            nuevo_map[b] = (n, f)
    else:  # reemplazar: la referencia queda EXACTAMENTE como el archivo
        nuevo_map = {b: (n, f) for b, n, f in filas}

    def _conteo(mapa: dict) -> dict:
        c = {"si": 0, "no": 0, "ambiguo": 0}
        for _n, f in mapa.values():
            c[f] = c.get(f, 0) + 1
        c["total"] = len(mapa)
        return c

    barcodes_archivo = {b for b, _n, _f in filas}
    agrega = len(barcodes_archivo - set(actual))
    quita = len(set(actual) - barcodes_archivo) if modo == "reemplazar" else 0
    cambia = sin_cambios = pasan_a_venta_libre = pasan_a_receta = 0
    ej_venta_libre: list[dict] = []
    ej_receta: list[dict] = []
    ej_agrega: list[dict] = []
    for b, n, f in filas:
        prev = actual.get(b)
        if prev is None:
            if len(ej_agrega) < 10:
                ej_agrega.append({"barcode": b, "nombre": n, "flag": f})
            continue
        prev_flag = prev[1]
        if prev_flag == f:
            sin_cambios += 1
            continue
        cambia += 1
        if prev_flag in ("si", "ambiguo") and f == "no":
            pasan_a_venta_libre += 1
            if len(ej_venta_libre) < 20:
                ej_venta_libre.append({"barcode": b, "nombre": n, "antes": prev_flag, "despues": f})
        if prev_flag in ("no", "ambiguo") and f == "si":
            pasan_a_receta += 1
            if len(ej_receta) < 20:
                ej_receta.append({"barcode": b, "nombre": n, "antes": prev_flag, "despues": f})

    resumen_referencia = {
        "antes": _conteo(actual), "despues": _conteo(nuevo_map),
        "agrega": agrega, "cambia": cambia, "sin_cambios": sin_cambios, "quita": quita,
        "pasan_a_venta_libre": pasan_a_venta_libre, "pasan_a_receta": pasan_a_receta,
        "ejemplos_pasan_a_venta_libre": ej_venta_libre, "ejemplos_pasan_a_receta": ej_receta,
        "ejemplos_agrega": ej_agrega,
    }

    # ── Impacto sobre el catálogo activo, simulando la referencia NUEVA ─────
    nuevo_flags = {b: f for b, (_n, f) in nuevo_map.items()}

    def _referencia_nueva(barcodes):
        for bc in barcodes or []:
            v = nuevo_flags.get(_norm(bc))
            if v:
                return v
        return None

    filas_catalogo = await db.fetch(
        "SELECT ci.branch_id, ci.external_id, ci.name, ci.category, ci.rubro, ci.subrubro, "
        "ci.barcodes, ci.stock, ci.requiere_receta, ce.requiere_receta_override "
        "FROM catalog_items ci LEFT JOIN catalog_extras ce "
        "ON ce.branch_id = ci.branch_id AND ce.external_id = ci.external_id "
        "WHERE ci.active")

    cat_antes = {"si": 0, "no": 0, "ambiguo": 0}
    cat_despues = {"si": 0, "no": 0, "ambiguo": 0}
    cambian = a_validar_antes = a_validar_despues = 0
    ej_cat_venta_libre: list[dict] = []
    ej_cat_receta: list[dict] = []
    conflictos_manuales: list[dict] = []
    conflictos_manuales_total = 0
    for r in filas_catalogo:
        override = r["requiere_receta_override"] or None
        antes = override or r["requiere_receta"]
        computado = derivar_requiere_receta(r["category"], r["rubro"], r["subrubro"], r["name"],
                                            r["barcodes"] or [], referencia=_referencia_nueva)
        despues = override or computado
        cat_antes[antes] = cat_antes.get(antes, 0) + 1
        cat_despues[despues] = cat_despues.get(despues, 0) + 1
        stock = r["stock"] or 0
        if antes == "ambiguo" and stock > 0:
            a_validar_antes += 1
        if despues == "ambiguo" and stock > 0:
            a_validar_despues += 1
        if antes != despues:
            cambian += 1
            if antes in ("si", "ambiguo") and despues == "no" and len(ej_cat_venta_libre) < 20:
                ej_cat_venta_libre.append({"external_id": r["external_id"], "nombre": r["name"],
                                          "antes": antes, "despues": despues})
            if antes in ("no", "ambiguo") and despues == "si" and len(ej_cat_receta) < 20:
                ej_cat_receta.append({"external_id": r["external_id"], "nombre": r["name"],
                                     "antes": antes, "despues": despues})
        # Marca manual en conflicto con lo que diría el archivo: la marca
        # manual SIEMPRE gana, esto es solo para avisar y que alguien revise.
        if override and computado != override:
            conflictos_manuales_total += 1
            if len(conflictos_manuales) < 50:
                conflictos_manuales.append({"external_id": r["external_id"], "nombre": r["name"],
                                           "marca_manual": override, "segun_archivo": computado})

    resumen_catalogo = {
        "antes": cat_antes, "despues": cat_despues, "cambian": cambian,
        "a_validar_con_stock_antes": a_validar_antes, "a_validar_con_stock_despues": a_validar_despues,
        "ejemplos_pasan_a_venta_libre": ej_cat_venta_libre, "ejemplos_pasan_a_receta": ej_cat_receta,
    }

    resumen = {
        "referencia": resumen_referencia,
        "catalogo": resumen_catalogo,
        "marcas_manuales_en_conflicto_total": conflictos_manuales_total,
        "marcas_manuales_en_conflicto": conflictos_manuales,
        "archivo_info": info,
    }

    filas_json = json.dumps([[b, n, f] for b, n, f in filas])
    resumen_json = json.dumps(resumen)
    async with db.transaction() as con:
        # Uno solo pendiente a la vez: el anterior queda descartado.
        await con.execute(
            "UPDATE receta_sincronizaciones SET estado = 'descartada', resuelta_at = now(), "
            "filas = NULL WHERE estado = 'pendiente'")
        fila = await con.fetchrow(
            "INSERT INTO receta_sincronizaciones (archivo, modo, estado, autor, resumen, filas, "
            "version_ref) VALUES ($1, $2, 'pendiente', $3, $4::jsonb, $5::jsonb, $6) "
            "RETURNING id, estado",
            nombre_archivo, modo, autor, resumen_json, filas_json, version_ref)

    return {"id": fila["id"], "estado": fila["estado"], "modo": modo, "archivo": nombre_archivo,
            **resumen}


async def confirmar_sincronizacion(db, id) -> dict:
    """
    Aplica la vista previa `id`: upsert (actualizar) o reemplazo total
    (reemplazar) de receta_referencia, en UNA transacción, y recalcula el
    catálogo. LookupError si no existe; SincronizacionConflicto si ya se
    resolvió, si la vista previa venció (2 h) o si la referencia cambió desde
    entonces (otra sincronización se aplicó primero).
    """
    row = await db.fetchrow("SELECT * FROM receta_sincronizaciones WHERE id = $1", id)
    if not row:
        raise LookupError(f"Sincronización {id} no encontrada")
    if row["estado"] != "pendiente":
        raise SincronizacionConflicto(f"La sincronización ya fue {row['estado']}")
    if datetime.now(timezone.utc) - row["created_at"] > VENCIMIENTO_PREVIEW:
        raise SincronizacionConflicto("La vista previa venció: subí el archivo de nuevo")
    if await _version_ref(db) != row["version_ref"]:
        raise SincronizacionConflicto(
            "La referencia cambió desde la vista previa: generala de nuevo")

    filas_raw = row["filas"]
    filas_raw = json.loads(filas_raw) if isinstance(filas_raw, str) else (filas_raw or [])
    filas = [(b, n, f) for b, n, f in filas_raw]
    fuente = f"sync {id}: {row['archivo']}"[:200]

    async with db.transaction() as con:
        # Doble clic en "Confirmar": el segundo espera acá y ya la encuentra
        # aplicada (no se aplica dos veces).
        bloqueada = await con.fetchrow(
            "SELECT estado FROM receta_sincronizaciones WHERE id = $1 FOR UPDATE", id)
        if not bloqueada or bloqueada["estado"] != "pendiente":
            raise SincronizacionConflicto("La sincronización ya fue resuelta")
        if row["modo"] == "reemplazar":
            await con.execute("DELETE FROM receta_referencia")
        if filas:
            await con.executemany(
                "INSERT INTO receta_referencia (barcode, nombre, requiere_receta, fuente, "
                "updated_at) VALUES ($1, $2, $3, $4, now()) "
                "ON CONFLICT (barcode) DO UPDATE SET nombre = EXCLUDED.nombre, "
                "requiere_receta = EXCLUDED.requiere_receta, fuente = EXCLUDED.fuente, "
                "updated_at = now()",
                [(b, n, f, fuente) for b, n, f in filas])
        await con.execute(
            "UPDATE receta_sincronizaciones SET estado = 'aplicada', resuelta_at = now(), "
            "filas = NULL WHERE id = $1", id)

    total_ref = await cargar_desde_db(db)
    res = await recalcular_catalogo(db)
    return {"id": id, "estado": "aplicada", "referencia": total_ref,
            "cambiados": res["cambiados"], "totales": res["totales"]}


async def descartar_sincronizacion(db, id) -> dict:
    row = await db.fetchrow("SELECT estado FROM receta_sincronizaciones WHERE id = $1", id)
    if not row:
        raise LookupError(f"Sincronización {id} no encontrada")
    if row["estado"] != "pendiente":
        raise SincronizacionConflicto(f"La sincronización ya fue {row['estado']}")
    async with db.transaction() as con:
        await con.execute(
            "UPDATE receta_sincronizaciones SET estado = 'descartada', resuelta_at = now(), "
            "filas = NULL WHERE id = $1", id)
    return {"id": id, "estado": "descartada"}


def _sin_filas(r) -> dict:
    resumen = r["resumen"]
    if isinstance(resumen, str):
        resumen = json.loads(resumen)
    return {
        "id": r["id"], "archivo": r["archivo"], "modo": r["modo"], "estado": r["estado"],
        "autor": r["autor"], "resumen": resumen,
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "resuelta_at": r["resuelta_at"].isoformat() if r["resuelta_at"] else None,
    }


async def listar_sincronizaciones(db, limit: int = 20) -> list[dict]:
    """Más nuevas primero, SIN `filas` (puede ser un archivo grande)."""
    rows = await db.fetch(
        "SELECT id, archivo, modo, estado, autor, resumen, created_at, resuelta_at "
        "FROM receta_sincronizaciones ORDER BY id DESC LIMIT $1", limit)
    return [_sin_filas(r) for r in rows]


async def obtener_sincronizacion(db, id) -> Optional[dict]:
    row = await db.fetchrow(
        "SELECT id, archivo, modo, estado, autor, resumen, created_at, resuelta_at "
        "FROM receta_sincronizaciones WHERE id = $1", id)
    return _sin_filas(row) if row else None


async def inicializar(db) -> dict:
    """
    Al arrancar: Postgres es la ÚNICA fuente de verdad de la referencia — ya
    NO se siembra desde ningún archivo (ver docstring del módulo). Solo carga
    la referencia vigente y recalcula el catálogo ERP con ella.
    """
    n = await cargar_desde_db(db)
    if n == 0:
        logger.warning("Referencia de recetas vacía: sincronizala desde el backoffice "
                       "(Productos · Receta)")
    res = await recalcular_catalogo(db)
    return {"referencia": n, **res}


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
