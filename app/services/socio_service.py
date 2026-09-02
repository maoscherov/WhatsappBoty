"""
Padrón de socios de la mutual — personalización de la conversación.

Carga un padrón (CSV o XLSX importado por backoffice) con columnas:
  APELLIDO | NOMBRE | DNI | SOCIO | CELULAR | DOMICILIO

Permite identificar al cliente por su número de WhatsApp para que el bot
lo salude por nombre y lo trate como socio reconocido.

Matching de teléfono: los números de WA llegan como 549341XXXXXXX y el padrón
tiene el formato local (341XXXXXXX). Se comparan los últimos 10 dígitos
(área + número), con fallback a 8 dígitos por si el padrón viene sin código
de área.

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
    "domicilio": {"domicilio", "direccion"},
}


def _solo_digitos(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


class SocioService:
    def __init__(self, path: str):
        self._path = path
        self._socios: list[dict] = []
        # Índices por sufijo de teléfono
        self._por_tel_10: dict[str, dict] = {}
        self._por_tel_8: dict[str, dict] = {}
        self._por_dni: dict[str, dict] = {}
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
            key = str(col).strip().lower().replace("°", "").replace(".", "")
            for campo, aliases in _COLUMN_ALIASES.items():
                if key in aliases:
                    colmap[campo] = col
                    break

        faltantes = {"nombre", "celular"} - set(colmap)
        if faltantes:
            logger.error(f"Padrón inválido, faltan columnas: {faltantes} (encontradas: {list(df.columns)})")
            return

        self._socios = []
        self._por_tel_10.clear()
        self._por_tel_8.clear()
        self._por_dni.clear()

        for _, row in df.iterrows():
            celular = _solo_digitos(row.get(colmap["celular"]))
            if len(celular) < 8:
                continue
            socio = {
                "nombre": str(row.get(colmap["nombre"]) or "").strip().title(),
                "apellido": str(row.get(colmap.get("apellido"), "") or "").strip().title(),
                "nro_socio": str(row.get(colmap.get("socio"), "") or "").strip(),
                # DNI y domicilio se guardan para el backoffice, NO para el prompt
                "dni": _solo_digitos(row.get(colmap.get("dni"), "")),
                "domicilio": str(row.get(colmap.get("domicilio"), "") or "").strip(),
                "celular": celular,
            }
            self._socios.append(socio)
            self._por_tel_10[celular[-10:]] = socio
            self._por_tel_8[celular[-8:]] = socio
            if socio["dni"]:
                self._por_dni[socio["dni"]] = socio

        logger.info(f"Padrón de socios cargado: {self.total} socios desde {p}")

    def find_by_phone(self, phone: str) -> Optional[dict]:
        """Busca un socio por número de WhatsApp (matching por sufijo)."""
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
        partes = [f"Nombre: {socio['nombre']} {socio['apellido']}".strip()]
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
