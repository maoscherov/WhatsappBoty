"""
Listado de empleados — tienen 20% de descuento (no acumulable con el de
socio: si el teléfono es de un empleado, se aplica el descuento de empleado).

Se cargan por planilla (XLSX/CSV) desde el backoffice, agrupados por `grupo`
(p.ej. distintas sucursales/entidades cargan su propia lista sin pisar la de
las demás) y persisten en Postgres (tabla `empleados`).

CONTRATO: `get_empleado_service()` y `EmpleadoService.find_by_phone()` los
importa el módulo de checkout para aplicar el descuento — no renombrar.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class EmpleadoService:
    def __init__(self):
        self._empleados: list[dict] = []
        self._por_tel_10: dict[str, dict] = {}
        self._por_tel_8: dict[str, dict] = {}
        self.reporte_carga: dict = {
            "total_filas": 0, "cargados": 0,
            "sin_celular_valido": [], "duplicados": [],
            "columnas_reconocidas": {},
        }

    @property
    def total(self) -> int:
        return len(self._empleados)

    def cargar_desde_lista(self, empleados: list[dict]):
        """Construye los índices en memoria a partir de una lista de dicts
        (nombre, apellido, nombre_pila, grupo, celular, celular_original,
        activo). Se usa tanto tras `parsear_planilla` como al cargar desde DB."""
        self._empleados = []
        self._por_tel_10.clear()
        self._por_tel_8.clear()
        for emp in empleados:
            emp = dict(emp)
            if not emp.get("celular"):
                continue
            emp.setdefault("activo", True)
            self._empleados.append(emp)
            self._por_tel_10[emp["celular"]] = emp
            self._por_tel_8[emp["celular"][-8:]] = emp

    def find_by_phone(self, phone: str) -> Optional[dict]:
        """Busca un empleado ACTIVO por número de WhatsApp: normaliza el
        entrante a 10 dígitos (misma lógica que el padrón de socios), con
        fallback al matching por sufijo de 8 dígitos."""
        from app.services.socio_service import normalizar_celular, _solo_digitos

        normalizado = normalizar_celular(phone)
        emp = self._por_tel_10.get(normalizado) if normalizado else None
        if emp is None:
            digitos = _solo_digitos(phone)
            if len(digitos) >= 10:
                emp = self._por_tel_10.get(digitos[-10:])
            if emp is None and len(digitos) >= 8:
                emp = self._por_tel_8.get(digitos[-8:])
        if emp and emp.get("activo", True):
            return emp
        return None

    def listar(self, q: str = "", page: int = 1, page_size: int = 50) -> tuple[list[dict], int]:
        """Listado paginado, opcionalmente filtrado por nombre/apellido/grupo
        (sin tildes ni mayúsculas). Devuelve (página, total)."""
        import unicodedata as _ud

        def _plano(s: str) -> str:
            return "".join(c for c in _ud.normalize("NFD", (s or "").lower())
                           if _ud.category(c) != "Mn")

        q = q.strip()
        if q:
            palabras = [p for p in _plano(q).split() if p]
            candidatos = [
                e for e in self._empleados
                if all(p in _plano(f"{e.get('nombre','')} {e.get('apellido','')} "
                                    f"{e.get('grupo','')}") for p in palabras)
            ]
        else:
            candidatos = list(self._empleados)

        total = len(candidatos)
        inicio = (page - 1) * page_size
        return candidatos[inicio:inicio + page_size], total


_instance: Optional[EmpleadoService] = None


def get_empleado_service() -> EmpleadoService:
    global _instance
    if _instance is None:
        _instance = EmpleadoService()
    return _instance


def reload_empleado_service() -> EmpleadoService:
    """Fuerza una instancia nueva y vacía (se repuebla llamando a
    `cargar_desde_lista`/`cargar_desde_db` después)."""
    global _instance
    _instance = EmpleadoService()
    return _instance


# ── Planilla (XLSX/CSV) ──────────────────────────────────────────────────────

def _celda(row, idx) -> str:
    if idx is None or idx >= len(row):
        return ""
    v = row[idx]
    if v is None:
        return ""
    try:
        import math
        if isinstance(v, float) and math.isnan(v):
            return ""
    except Exception:
        pass
    s = str(v).strip()
    return "" if s.lower() in ("nan", "none", "null") else s


def _clave_columna(col) -> str:
    import re
    import unicodedata
    s = unicodedata.normalize("NFD", str(col or "").strip().lower())
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = s.replace("°", "").replace("º", "").replace(".", "").replace("_", " ")
    return re.sub(r"\s+", " ", s).strip()


_ALIAS_CELULAR = {"celular", "telefono", "tel", "movil", "whatsapp"}
_ALIAS_NOMBRE_COMPLETO = {"apellido y nombre", "nombre y apellido", "apellido, nombre", "empleado"}
_ALIAS_APELLIDO = {"apellido", "apellidos"}
_ALIAS_NOMBRE = {"nombre", "nombres"}
_ALIAS_ORDEN = {"orden", "nro", "n", "item", "legajo"}


def _leer_filas(data: bytes, filename: str) -> list[list]:
    """Filas crudas (lista de listas) del XLSX o CSV, sin asumir dónde
    empieza el encabezado."""
    filename = (filename or "").lower()
    if filename.endswith((".xlsx", ".xls")):
        import io as _io

        import openpyxl
        wb = openpyxl.load_workbook(_io.BytesIO(data), data_only=True)
        ws = wb.active
        return [list(row) for row in ws.iter_rows(values_only=True)]
    else:
        import csv
        import io as _io
        texto = data.decode("utf-8-sig", errors="replace")
        return [row for row in csv.reader(_io.StringIO(texto))]


def _encontrar_encabezado(filas: list[list]) -> tuple[int, dict]:
    """
    Busca la fila de encabezados: la planilla real trae filas vacías arriba
    antes del encabezado. Se identifica por tener una celda que reconoce
    como celular/teléfono Y otra que reconoce como nombre (columna de
    "Apellido y Nombre" combinada, o "nombre" separado).
    """
    for i, fila in enumerate(filas):
        colmap: dict[str, int] = {}
        for j, celda in enumerate(fila):
            clave = _clave_columna(celda)
            if not clave:
                continue
            if clave in _ALIAS_CELULAR and "celular" not in colmap:
                colmap["celular"] = j
            elif clave in _ALIAS_NOMBRE_COMPLETO and "nombre_completo" not in colmap:
                colmap["nombre_completo"] = j
            elif clave in _ALIAS_APELLIDO and "apellido" not in colmap:
                colmap["apellido"] = j
            elif clave in _ALIAS_NOMBRE and "nombre" not in colmap:
                colmap["nombre"] = j
            elif clave in _ALIAS_ORDEN and "orden" not in colmap:
                colmap["orden"] = j
        tiene_celular = "celular" in colmap
        tiene_nombre = "nombre_completo" in colmap or "nombre" in colmap
        if tiene_celular and tiene_nombre:
            return i, colmap
    return -1, {}


def parsear_planilla(data: bytes, filename: str, grupo: str) -> tuple[list[dict], dict]:
    """
    Parsea una planilla de empleados (XLSX o CSV) con filas vacías arriba,
    encabezado en cualquier fila, columna de orden, "Apellido y Nombre"
    ("APELLIDO, NOMBRE") o columnas separadas Apellido/Nombre, y una columna
    de celular. Devuelve (lista_empleados, reporte).
    """
    from app.config import get_settings
    from app.services.socio_service import analizar_celular, nombre_de_pila

    try:
        area_default = get_settings().socios_area_default or "341"
    except Exception:
        area_default = "341"

    filas = _leer_filas(data, filename)
    idx_header, colmap = _encontrar_encabezado(filas)

    reporte = {
        "total_filas": 0, "cargados": 0,
        "sin_celular_valido": [], "duplicados": [],
        "columnas_reconocidas": {k: v for k, v in colmap.items()},
    }
    if idx_header < 0:
        logger.error("Planilla de empleados inválida: no se encontró encabezado "
                     "con celular/teléfono y nombre")
        return [], reporte

    filas_datos = filas[idx_header + 1:]
    reporte["total_filas"] = len(
        [f for f in filas_datos if any(_celda(f, j) for j in range(len(f)))])

    empleados: list[dict] = []
    contador_tel: dict[str, int] = {}
    sin_celular: list[dict] = []

    for fila in filas_datos:
        if not any(_celda(fila, j) for j in range(len(fila))):
            continue  # fila totalmente vacía

        if "nombre_completo" in colmap:
            completo = _celda(fila, colmap["nombre_completo"])
            if "," in completo:
                apellido, nombre = (p.strip() for p in completo.split(",", 1))
            else:
                partes = completo.split()
                apellido = partes[0].title() if partes else ""
                nombre = " ".join(partes[1:]) if len(partes) > 1 else ""
        else:
            apellido = _celda(fila, colmap.get("apellido"))
            nombre = _celda(fila, colmap.get("nombre"))

        apellido = apellido.title()
        nombre = nombre.title()
        if not apellido and not nombre:
            continue

        raw_cel = _celda(fila, colmap.get("celular"))
        analisis = analizar_celular(raw_cel, area_default)
        celular = analisis["normalizado"]

        if not celular:
            if len(sin_celular) < 20:
                sin_celular.append({"nombre": f"{apellido} {nombre}".strip(),
                                    "celular": analisis["original"]})
            continue

        emp = {
            "nombre": nombre, "apellido": apellido,
            "grupo": grupo, "celular": celular,
            "celular_original": analisis["original"], "activo": True,
        }
        emp["nombre_pila"] = nombre_de_pila({"nombre": nombre or apellido, "apellido": apellido})
        empleados.append(emp)
        contador_tel[celular] = contador_tel.get(celular, 0) + 1

    reporte["cargados"] = len(empleados)
    reporte["sin_celular_valido"] = sin_celular
    reporte["duplicados"] = [t for t, c in contador_tel.items() if c > 1][:20]

    logger.info(f"Planilla de empleados '{grupo}' parseada: {len(empleados)} cargados "
                f"({len(sin_celular)} sin celular válido)")
    return empleados, reporte


# ── Persistencia (Postgres) ──────────────────────────────────────────────────

async def guardar_en_db(db, empleados: list[dict], grupo: str) -> int:
    """Reemplaza SOLO los empleados del grupo dado (transacción) — así cada
    grupo (p.ej. mutual y cooperativa) se carga por separado sin pisar a los
    demás."""
    filas = [
        (e.get("celular", ""), e.get("celular_original", ""), e.get("nombre", ""),
         e.get("apellido", ""), e.get("nombre_pila", ""), grupo, bool(e.get("activo", True)))
        for e in empleados
    ]
    async with db.transaction() as con:
        await con.execute("DELETE FROM empleados WHERE grupo = $1", grupo)
        if filas:
            await con.executemany(
                "INSERT INTO empleados (celular, celular_original, nombre, apellido, "
                "nombre_pila, grupo, activo) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                filas)
    return len(filas)


async def cargar_desde_db(db, svc: "EmpleadoService") -> int:
    """Llena `svc` desde la tabla `empleados` completa (todos los grupos)."""
    rows = await db.fetch(
        "SELECT celular, celular_original, nombre, apellido, nombre_pila, grupo, activo "
        "FROM empleados")
    if not rows:
        return 0
    empleados = [{
        "celular": r["celular"], "celular_original": r["celular_original"],
        "nombre": r["nombre"], "apellido": r["apellido"], "nombre_pila": r["nombre_pila"],
        "grupo": r["grupo"], "activo": r["activo"],
    } for r in rows]
    svc.cargar_desde_lista(empleados)
    return svc.total
