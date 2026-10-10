"""
Normalización de teléfonos argentinos a E.164 para calcular contact_hmac.

Formatos soportados:
  - internacional con +: '+54 9 341 123 4567';
  - dígitos con 54 adelante, como llegan en los ids de WhatsApp:
    '5493411234567' o '5493411234567@c.us';
  - nacional con 0 inicial y 15 opcional: '0341 15 123 4567', '011 15 1234 5678',
    '0341 412 3456' (fijo).

Devuelve '+54' + 10 dígitos (fijo) o '+549' + 10 dígitos (móvil). Cualquier
otro país o forma lanza TelefonoNoSoportado. Es una normalización de formato:
no verifica que la característica exista ni que la línea esté activa.

Resolución del 15: la característica tiene 2, 3 o 4 dígitos, y sin el 15
tienen que quedar 10 dígitos (característica + número). Se prueba en ese orden
y gana la primera que cierra; '11 15 15xxxxxx' es ambiguo y se resuelve como
característica 11, que es lo correcto en la práctica.
"""

import re

_DIGITOS = re.compile(r"\D")


class TelefonoNoSoportado(ValueError):
    pass


def _nacional_sin_cero(d: str) -> str:
    """d = dígitos nacionales sin el 0 inicial. Devuelve los 10 u 11 dígitos
    que siguen al 54 (11 si es móvil, con el 9 adelante)."""
    for largo_area in (2, 3, 4):
        if d[largo_area:largo_area + 2] == "15" and len(d) - 2 == 10:
            return "9" + d[:largo_area] + d[largo_area + 2:]
    if len(d) == 10:
        return d
    raise TelefonoNoSoportado("formato nacional no reconocido")


def normalizar_e164(texto: str) -> str:
    t = (texto or "").strip()
    if "@" in t:
        t = t.split("@", 1)[0]
    con_mas = t.startswith("+")
    d = _DIGITOS.sub("", t)
    if not d:
        raise TelefonoNoSoportado("sin dígitos")

    if con_mas or (d.startswith("54") and len(d) >= 12):
        if not d.startswith("54"):
            raise TelefonoNoSoportado("solo se soportan números de Argentina (+54)")
        nacional = d[2:]
    elif d.startswith("0"):
        nacional = _nacional_sin_cero(d[1:])
    else:
        raise TelefonoNoSoportado("formato no reconocido: usar +54..., 54... o 0...")

    if len(nacional) == 11 and nacional[0] != "9":
        raise TelefonoNoSoportado("11 dígitos nacionales solo con el 9 de móvil")
    if len(nacional) not in (10, 11):
        raise TelefonoNoSoportado("largo inválido")
    return "+54" + nacional
