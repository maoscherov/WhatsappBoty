"""
Padrón de socios de la mutual — personalización de la conversación.

Carga un padrón (CSV o XLSX importado por backoffice) con columnas:
  APELLIDO | NOMBRE | DNI | SOCIO | CELULAR | DOMICILIO

Permite identificar al cliente por su número de WhatsApp para que el bot
lo salude por nombre y lo trate como socio reconocido.

Matching de teléfono: los números de WA llegan como 549341XXXXXXX. El celular
del padrón (con el formato que sea: con/sin 0, con/sin 9, con/sin +54, con/sin
"15", con/sin código de área) se normaliza a 10 dígitos (área + número) con
`normalizar_celular`, usando `socios_area_default` como área para los
celulares locales cargados sin área. El entrante se normaliza igual y se
busca por esos 10 dígitos, con fallback al matching por sufijo de 10 y de 8
dígitos (compatibilidad con padrones ya cargados).

PRIVACIDAD: al contexto de Claude solo se pasa nombre y N° de socio.
DNI y domicilio se cargan pero NUNCA entran al prompt.
"""

import logging
import re
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Alias aceptados por columna (case-insensitive, sin acentos)
_COLUMN_ALIASES = {
    "apellido":  {"apellido", "apellidos"},
    "nombre":    {"nombre", "nombres"},
    "dni":       {"dni", "documento"},
    "socio":     {"socio", "nro socio", "nro_socio", "numero socio", "n socio"},
    "celular":   {"celular", "telefono", "tel", "movil", "whatsapp"},
    "domicilio": {"domicilio", "direccion", "calle", "domicilio particular", "direccion particular",
                  "domicilio real", "domic", "calle y numero", "calle y nro", "dir"},
}


def _clave_columna(col) -> str:
    """Encabezado normalizado: minúsculas, sin tildes, sin °/./_ extra.
    Antes "DIRECCIÓN" (con tilde) no se reconocía y la columna se ignoraba
    sin avisar (caso 24/9: a una socia se le pidió el domicilio)."""
    import unicodedata
    s = unicodedata.normalize("NFD", str(col).strip().lower())
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = s.replace("°", "").replace("º", "").replace(".", "").replace("_", " ")
    return re.sub(r"\s+", " ", s).strip()


def _celda(row, col) -> str:
    """Valor de una celda como texto; las vacías de Excel/CSV llegan como NaN
    y str(NaN) daba "nan" (un domicilio "Nan")."""
    if col is None:
        return ""
    v = row.get(col)
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


def nombre_de_pila(socio: Optional[dict], orden: str = "") -> str:
    """
    Nombre para saludar. Muchos padrones traen en la columna nombre el nombre
    completo con el apellido adelante ("Muff Claudia Beatriz"): tomar la
    primera palabra saludaba por el apellido (caso real 24/9).
      - "Apellido, Nombre" → lo que va después de la coma.
      - Con columna apellido → se sacan sus palabras y queda la primera.
      - Sin apellido y varias palabras → según `orden` (socios_orden_nombre):
        "apellido_nombre" (default) toma la segunda, "nombre_apellido" la primera.
    """
    if not socio:
        return ""
    nombre = (socio.get("nombre") or "").strip()
    apellido = (socio.get("apellido") or "").strip()
    if not nombre:
        return ""
    if "," in nombre:
        despues = nombre.split(",", 1)[1].strip()
        if despues:
            return despues.split()[0].title()
    toks = nombre.split()
    ap = {t.lower() for t in apellido.replace(",", " ").split()}
    if ap:
        resto = [t for t in toks if t.lower() not in ap]
        return (resto[0] if resto else toks[0]).title()
    if len(toks) >= 2:
        if not orden:
            try:
                from app.config import get_settings
                orden = get_settings().socios_orden_nombre
            except Exception:
                orden = "apellido_nombre"
        if orden != "nombre_apellido":
            return toks[1].title()
    return toks[0].title()


def _solo_digitos(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def analizar_celular(valor: str, area_default: str = "") -> dict:
    """
    Interpreta un celular argentino escrito de cualquier forma habitual
    (con/sin 0, con/sin 9, con/sin +54, con/sin "15", con/sin código de área)
    y lo reduce a 10 dígitos (área + número).

    Devuelve {"normalizado": str|None, "original": str, "regla": ...} —
    "regla" documenta qué transformación se aplicó, para el reporte de carga:
      - "directo":       ya eran 10 dígitos (a lo sumo se sacó 54/9).
      - "sin_0":         se sacó un 0 inicial y quedó en 10 dígitos.
      - "sin_15":        eran 12 dígitos, se identificó el "15" tras el área
                         (código de área único, sin ambigüedad).
      - "ambiguo":       12 dígitos con más de un largo de área posible; se
                         eligió el de 3 dígitos (el más común, Rosario 341).
      - "area_default":  número local (6 a 9 dígitos) al que se le antepuso
                         el área por defecto.
      - "invalido":      no se pudo interpretar.
    """
    original = str(valor or "").strip()
    digitos = _solo_digitos(valor)
    area_default = _solo_digitos(area_default)

    sin_0 = False

    # 1) Prefijo "54" (código de país) y, si tras sacarlo quedan 11 dígitos
    #    que empiezan con "9" (celular), sacar también el "9".
    if digitos.startswith("54"):
        digitos = digitos[2:]
    if len(digitos) == 11 and digitos.startswith("9"):
        digitos = digitos[1:]

    # 2) Un solo "0" inicial (característica interurbana).
    if digitos.startswith("0"):
        digitos = digitos[1:]
        sin_0 = True

    def _resultado(normalizado, regla):
        return {"normalizado": normalizado, "original": original, "regla": regla}

    if len(digitos) == 12:
        # El "15" quedó pegado después del código de área. Los códigos de
        # área argentinos tienen 2, 3 o 4 dígitos y área+número siempre
        # suman 10 — probar los tres largos y ver cuál tiene "15" justo
        # después del área.
        candidatos = {}
        for area_len in (2, 3, 4):
            area = digitos[:area_len]
            resto = digitos[area_len:]
            if resto[:2] == "15":
                candidatos[area_len] = area + resto[2:]
        if not candidatos:
            return _resultado(None, "invalido")
        if len(candidatos) == 1:
            return _resultado(next(iter(candidatos.values())), "sin_15")
        # Ambiguo: preferir el área de 3 dígitos (la más común).
        elegido = candidatos.get(3) or next(iter(candidatos.values()))
        return _resultado(elegido, "ambiguo")

    if len(digitos) == 10:
        return _resultado(digitos, "sin_0" if sin_0 else "directo")

    if len(digitos) in (8, 9) and area_default:
        target = 10 - len(area_default)
        if len(digitos) == target:
            return _resultado(area_default + digitos, "area_default")
        if digitos.startswith("15") and len(digitos) - 2 == target:
            return _resultado(area_default + digitos[2:], "area_default")
        return _resultado(None, "invalido")

    if len(digitos) in (6, 7) and area_default:
        if len(area_default) + len(digitos) == 10:
            return _resultado(area_default + digitos, "area_default")
        return _resultado(None, "invalido")

    return _resultado(None, "invalido")


def normalizar_celular(valor: str, area_default: str = "") -> Optional[str]:
    return analizar_celular(valor, area_default)["normalizado"]


class SocioService:
    def __init__(self, path: str):
        self._path = path
        self._socios: list[dict] = []
        # Índices por sufijo de teléfono
        self._por_tel_10: dict[str, dict] = {}
        self._por_tel_8: dict[str, dict] = {}
        self._por_dni: dict[str, dict] = {}
        self.reporte_carga: dict = {
            "total_filas": 0, "cargados": 0, "normalizados": 0,
            "sin_celular_valido": [], "ambiguos": [], "duplicados": [],
            "columnas_reconocidas": {}, "columnas_ignoradas": [], "con_domicilio": 0,
            "nombre_con_apellido": 0, "nombre_con_apellido_ejemplos": [],
        }
        self._load()

    @property
    def total(self) -> int:
        return len(self._socios)

    def _load(self):
        p = Path(self._path)
        if not p.exists():
            # El import por backoffice puede haber guardado el padrón con la otra
            # extensión (.xlsx vs .csv) — probar la alternativa antes de rendirse.
            alt = p.with_suffix(".xlsx" if p.suffix == ".csv" else ".csv")
            if alt.exists():
                p = alt
                self._path = str(alt)
            else:
                logger.info(f"Padrón de socios no encontrado en {p} — personalización desactivada")
                return

        if p.suffix.lower() in (".xlsx", ".xls"):
            df = pd.read_excel(p, dtype=str)
        else:
            df = pd.read_csv(p, dtype=str)

        # Normalizar nombres de columna y mapear por alias
        colmap = {}
        for col in df.columns:
            key = _clave_columna(col)
            for campo, aliases in _COLUMN_ALIASES.items():
                if key in aliases and campo not in colmap:
                    colmap[campo] = col
                    break
        columnas_ignoradas = [str(c) for c in df.columns if c not in colmap.values()]

        faltantes = {"nombre", "celular"} - set(colmap)
        if faltantes:
            logger.error(f"Padrón inválido, faltan columnas: {faltantes} (encontradas: {list(df.columns)})")
            return

        self._socios = []
        self._por_tel_10.clear()
        self._por_tel_8.clear()
        self._por_dni.clear()

        from app.config import get_settings
        try:
            area_default = get_settings().socios_area_default or "341"
        except Exception:
            area_default = "341"

        sin_celular_valido: list[dict] = []
        ambiguos: list[dict] = []
        normalizados = 0
        contador_tel: dict[str, int] = {}
        con_domicilio = 0
        nombre_con_apellido: list[dict] = []
        nombre_con_apellido_total = 0

        for _, row in df.iterrows():
            raw_cel = row.get(colmap["celular"])
            nombre = _celda(row, colmap["nombre"]).title()
            apellido = _celda(row, colmap.get("apellido")).title()
            analisis = analizar_celular(raw_cel, area_default)
            celular = analisis["normalizado"]

            if not celular:
                if len(sin_celular_valido) < 20:
                    sin_celular_valido.append({
                        "apellido": apellido, "nombre": nombre,
                        "celular": analisis["original"],
                    })
                continue

            if analisis["regla"] == "ambiguo" and len(ambiguos) < 20:
                ambiguos.append({
                    "apellido": apellido, "nombre": nombre,
                    "celular": analisis["original"],
                })
            if _solo_digitos(raw_cel) != celular:
                normalizados += 1

            socio = {
                "nombre": nombre,
                "apellido": apellido,
                "nro_socio": _celda(row, colmap.get("socio")),
                # DNI y domicilio se guardan para el backoffice, NO para el prompt
                "dni": _solo_digitos(_celda(row, colmap.get("dni"))),
                "domicilio": _celda(row, colmap.get("domicilio")),
                "celular": celular,
                "celular_original": analisis["original"],
            }
            socio["nombre_pila"] = nombre_de_pila(socio)
            if socio["domicilio"]:
                con_domicilio += 1
            if socio["apellido"] and nombre and nombre.split() and \
                    nombre.split()[0].lower() in {t.lower() for t in socio["apellido"].split()}:
                if len(nombre_con_apellido) < 20:
                    nombre_con_apellido.append({"nombre": nombre, "apellido": socio["apellido"],
                                                "saluda_como": socio["nombre_pila"]})
                nombre_con_apellido_total += 1
            self._socios.append(socio)
            self._por_tel_10[celular] = socio
            self._por_tel_8[celular[-8:]] = socio
            if socio["dni"]:
                self._por_dni[socio["dni"]] = socio
            contador_tel[celular] = contador_tel.get(celular, 0) + 1

        self.reporte_carga = {
            "total_filas": len(df),
            "cargados": len(self._socios),
            "normalizados": normalizados,
            "sin_celular_valido": sin_celular_valido,
            "ambiguos": ambiguos,
            "duplicados": [t for t, c in contador_tel.items() if c > 1][:20],
            # Calidad de columnas (24/9): domicilio ignorado y nombre con apellido
            "columnas_reconocidas": {k: str(v) for k, v in colmap.items()},
            "columnas_ignoradas": columnas_ignoradas,
            "con_domicilio": con_domicilio,
            "nombre_con_apellido": nombre_con_apellido_total,
            "nombre_con_apellido_ejemplos": nombre_con_apellido,
        }

        logger.info(f"Padrón de socios cargado: {self.total} socios desde {p} "
                    f"({normalizados} normalizados, "
                    f"{len(self.reporte_carga['sin_celular_valido'])} sin celular válido)")

    def buscar_por_nombre(self, q: str, limit: int = 50) -> list[dict]:
        """Socios cuyo nombre/apellido contiene todas las palabras de `q`
        (sin tildes ni mayúsculas). Para el buscador de conversaciones."""
        import unicodedata as _ud

        def _plano(s: str) -> str:
            return "".join(c for c in _ud.normalize("NFD", (s or "").lower())
                           if _ud.category(c) != "Mn")

        palabras = [p for p in _plano(q).split() if len(p) >= 2 and not p.isdigit()]
        if not palabras:
            return []
        out = []
        for s in self._socios:
            texto = _plano(f"{s.get('nombre', '')} {s.get('apellido', '')}")
            if all(p in texto for p in palabras):
                out.append(s)
                if len(out) >= limit:
                    break
        return out

    def find_by_phone(self, phone: str) -> Optional[dict]:
        """Busca un socio por número de WhatsApp: primero normalizando el
        entrante a 10 dígitos (área + número), con fallback al matching por
        sufijo de 10 y de 8 dígitos (padrón cargado sin código de área)."""
        normalizado = normalizar_celular(phone)
        if normalizado and normalizado in self._por_tel_10:
            return self._por_tel_10[normalizado]
        digitos = _solo_digitos(phone)
        if len(digitos) >= 10 and digitos[-10:] in self._por_tel_10:
            return self._por_tel_10[digitos[-10:]]
        if len(digitos) >= 8 and digitos[-8:] in self._por_tel_8:
            return self._por_tel_8[digitos[-8:]]
        return None

    def find_by_dni(self, dni: str) -> Optional[dict]:
        """
        Busca un socio por DNI (acepta con o sin puntos). Se usa para cruzar
        la receta que llega por foto contra el padrón: la receta puede ser de
        otra persona que el teléfono que la manda (una madre por su hija).
        Estos datos van SOLO al backoffice, nunca al prompt.
        """
        digitos = _solo_digitos(dni)
        return self._por_dni.get(digitos) if digitos else None

    def contexto_para_prompt(self, phone: str) -> Optional[str]:
        """
        Contexto de personalización para Claude. Solo nombre y N° de socio —
        DNI y domicilio quedan deliberadamente afuera del prompt.
        """
        socio = self.find_by_phone(phone)
        if not socio:
            return None
        # El nombre de pila va aparte: con "Nombre: Muff Claudia Beatriz Muff"
        # el modelo saludaba por el apellido (24/9).
        partes = [f"Nombre de pila (para saludar): {socio.get('nombre_pila') or nombre_de_pila(socio)}"]
        if socio.get("apellido"):
            partes.append(f"Apellido: {socio['apellido']}")
        if socio["nro_socio"]:
            partes.append(f"N° de socio: {socio['nro_socio']}")
        return " | ".join(partes)


_instance: Optional[SocioService] = None


def get_socio_service(path: str) -> SocioService:
    global _instance
    if _instance is None:
        _instance = SocioService(path)
    return _instance


def reload_socio_service(path: str) -> SocioService:
    """Fuerza recarga del padrón (después de un import por backoffice)."""
    global _instance
    _instance = SocioService(path)
    return _instance
