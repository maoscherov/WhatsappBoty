"""
Parámetros de §2.1: nombres, ámbito, valor inicial y orden de "más estricto".

"Más estricto" (§2.1):
- plazos (duración y retención, en días o meses): cualquier N > 0 es más
  estricto que 0 (0 = sin plazo) y entre valores > 0 gana el menor;
- tope_ia_mensual_usd: NULL = sin tope (lo más laxo) y entre montos gana el
  menor. El spec deja el valor inicial "a definir en el piloto"; NULL juega
  el papel del 0 de las retenciones (decisión de este plan);
- booleanos: apagar es más estricto que encender;
- enums: perfil sensible > estandar; via_llm sincronica > lotes (§7: la vía
  sincrónica es la que admite retención cero).

El perfil `sensible` PROPONE valores más estrictos (§2.1, §7); no los fuerza.
"""

import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

Ambito = Literal["tenant", "linea"]
Tipo = Literal["plazo", "monto", "booleano", "enum"]
Comparacion = Literal["igual", "mas_estricto", "mas_laxo"]


class ValorInvalido(ValueError):
    pass


@dataclass(frozen=True)
class Parametro:
    nombre: str
    ambito: Ambito
    tipo: Tipo
    inicial: Any
    orden: tuple = field(default=())   # enums: de más laxo a más estricto


PARAMETROS: dict[str, Parametro] = {p.nombre: p for p in (
    Parametro("duracion_vinculo_dias", "linea", "plazo", 0),
    Parametro("retencion_fuente_dias", "linea", "plazo", 0),
    Parametro("retencion_tras_desvinculo_dias", "linea", "plazo", 0),
    Parametro("retencion_fichas_meses", "tenant", "plazo", 12),
    Parametro("tope_ia_mensual_usd", "linea", "monto", None),
    Parametro("perfil_de_datos", "tenant", "enum", "estandar", ("estandar", "sensible")),
    Parametro("retener_fragmentos", "tenant", "booleano", False),
    Parametro("ia_habilitada", "tenant", "booleano", True),
    Parametro("via_llm", "tenant", "enum", "lotes", ("lotes", "sincronica")),
)}

DE_LINEA = [n for n, p in PARAMETROS.items() if p.ambito == "linea"]
DE_TENANT = [n for n, p in PARAMETROS.items() if p.ambito == "tenant"]

# §7, tabla de perfil de datos: qué rubros proponen `sensible` por defecto.
RUBROS_SENSIBLES = frozenset({"farmacia", "salud", "mutual_salud"})


def _parametro(nombre: str) -> Parametro:
    try:
        return PARAMETROS[nombre]
    except KeyError:
        raise ValorInvalido(f"parámetro desconocido: {nombre}") from None


def validar(nombre: str, valor: Any) -> Any:
    p = _parametro(nombre)
    if p.tipo == "plazo":
        if isinstance(valor, bool) or valor is None:
            raise ValorInvalido(f"{nombre}: entero >= 0")
        try:
            v = int(valor)
        except (TypeError, ValueError):
            raise ValorInvalido(f"{nombre}: entero >= 0") from None
        if v < 0:
            raise ValorInvalido(f"{nombre}: entero >= 0")
        return v
    if p.tipo == "monto":
        if valor is None:
            return None
        try:
            v = Decimal(str(valor))
        except InvalidOperation:
            raise ValorInvalido(f"{nombre}: monto >= 0 o null") from None
        if not v.is_finite() or v < 0:
            raise ValorInvalido(f"{nombre}: monto >= 0 o null")
        return v
    if p.tipo == "booleano":
        if not isinstance(valor, bool):
            raise ValorInvalido(f"{nombre}: true o false")
        return valor
    if valor not in p.orden:
        raise ValorInvalido(f"{nombre}: uno de {list(p.orden)}")
    return valor


def _rango(p: Parametro, valor: Any) -> float:
    """Posición en el orden de estrictez: menor = más estricto."""
    if p.tipo == "plazo":
        return math.inf if valor == 0 else float(valor)
    if p.tipo == "monto":
        return math.inf if valor is None else float(valor)
    if p.tipo == "booleano":
        return 1.0 if valor else 0.0
    return float(len(p.orden) - 1 - p.orden.index(valor))


def comparar(nombre: str, actual: Any, nuevo: Any) -> Comparacion:
    p = _parametro(nombre)
    a, n = _rango(p, validar(nombre, actual)), _rango(p, validar(nombre, nuevo))
    if n == a:
        return "igual"
    return "mas_estricto" if n < a else "mas_laxo"


def es_mas_estricto(nombre: str, candidato: Any, referencia: Any) -> bool:
    return comparar(nombre, referencia, candidato) == "mas_estricto"


def iniciales(ambito: str) -> dict:
    return {n: p.inicial for n, p in PARAMETROS.items() if p.ambito == ambito}


def propuesta_para_perfil(perfil: str) -> dict:
    validar("perfil_de_datos", perfil)
    if perfil == "sensible":
        return {"retencion_fuente_dias": 7, "ia_habilitada": False,
                "via_llm": "sincronica", "retener_fragmentos": False}
    return {}


def perfil_por_rubro(rubro: str) -> str:
    return "sensible" if rubro in RUBROS_SENSIBLES else "estandar"


def clasificar_cambios(actuales: dict, nuevos: dict) -> dict[str, Comparacion]:
    """{nombre: comparación} solo para los parámetros cuyo valor cambia.
    Valida cada valor nuevo (lanza ValorInvalido)."""
    resultado: dict[str, Comparacion] = {}
    for nombre, nuevo in nuevos.items():
        c = comparar(nombre, actuales[nombre], nuevo)
        if c != "igual":
            resultado[nombre] = c
    return resultado
