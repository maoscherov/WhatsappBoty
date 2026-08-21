"""
Expansión de las abreviaturas de góndola del catálogo.

El sistema de la farmacia abrevia el TIPO de producto al final del nombre:
"REXONA EFFIC.TAL.ORIG TAL x 100" es un talco, "DOVE ORIGINAL JAB x 90" es
un jabón. El cliente escribe "talco rexona" o "jabón Dove", que no matchean
esas siglas — y el bot terminaba ofreciendo un desodorante como si fuera el
talco pedido (caso real 21/8).

Se expanden en el índice de búsqueda (el nombre original queda intacto) y se
usan para verificar que el producto encontrado sea del tipo que se pidió.
"""

import re

# Sigla de góndola → palabra que escribe el cliente.
ABREVIATURAS: dict[str, str] = {
    "sha": "shampoo",
    "shamp": "shampoo",
    "aco": "acondicionador",
    "acond": "acondicionador",
    "jab": "jabon",
    "jli": "jabon liquido",
    "des": "desodorante",
    "deo": "desodorante",
    "tal": "talco",
    "cre": "crema",
    "cr": "crema",
    "loc": "locion",
    "gel": "gel",
    "spr": "spray",
    "aer": "aerosol",
    "toa": "toallitas",
    "emu": "emulsion",
    "esp": "espuma",
    "ser": "serum",
    "apo": "apositos",
    "pol": "polvo",
    "pmo": "pomo",
    "com": "comprimidos",
    "cap": "capsulas",
    "caps": "capsulas",
    "tab": "tabletas",
    "gts": "gotas",
    "got": "gotas",
    "jbe": "jarabe",
    "sol": "solucion",
    "sus": "suspension",
    "susp": "suspension",
    "sob": "sobres",
    "amp": "ampollas",
    "ovu": "ovulos",
    "fco": "frasco",
    "ung": "unguento",
    "past": "pastillas",
    "grag": "grageas",
    "iny": "inyectable",
}

# Palabras con las que un cliente nombra el TIPO de producto. Si las usa, el
# producto ofrecido tiene que ser de ese tipo: pedir "talco" y recibir una
# crema del mismo laboratorio no es un match, es un sustituto encubierto.
# Cada tipo agrupa sus sinónimos y su sigla.
TIPOS_PRODUCTO: dict[str, set[str]] = {
    "talco":          {"talco", "tal"},
    "jabon":          {"jabon", "jabón", "jab", "jli"},
    "shampoo":        {"shampoo", "shampu", "champu", "sha"},
    "acondicionador": {"acondicionador", "acond", "aco"},
    "desodorante":    {"desodorante", "des", "deo", "antitranspirante"},
    "crema":          {"crema", "cre"},
    "locion":         {"locion", "loción", "loc"},
    "gel":            {"gel"},
    "toallitas":      {"toallitas", "toallas", "toa"},
    "espuma":         {"espuma", "esp"},
    "polvo":          {"polvo", "pol"},
    "jarabe":         {"jarabe", "jbe"},
    "gotas":          {"gotas", "gota", "gts", "got"},
    "comprimidos":    {"comprimidos", "comprimido", "com"},
    "capsulas":       {"capsulas", "cápsulas", "capsula", "cap"},
    "supositorios":   {"supositorios", "supositorio", "sup"},
    "ovulos":         {"ovulos", "óvulos", "ovu"},
    "ampollas":       {"ampollas", "ampolla", "amp"},
    "aerosol":        {"aerosol", "aer"},
    "spray":          {"spray", "spr"},
}

_TOKEN_RE = re.compile(r"[a-záéíóúñA-ZÁÉÍÓÚÑ0-9]+")


def expandir_abreviaturas(nombre: str) -> str:
    """
    Devuelve el nombre con las expansiones AGREGADAS al final (el original se
    conserva: el índice necesita las dos formas). Sin abreviaturas conocidas,
    devuelve el nombre tal cual.
    """
    extras: list[str] = []
    for tok in _TOKEN_RE.findall(nombre or ""):
        exp = ABREVIATURAS.get(tok.lower())
        if exp and exp not in extras:
            extras.append(exp)
    return f"{nombre} {' '.join(extras)}" if extras else nombre


def tipos_mencionados(texto: str) -> set[str]:
    """Tipos de producto que menciona el texto ('talco rexona' → {'talco'})."""
    tokens = {t.lower() for t in _TOKEN_RE.findall(texto or "")}
    return {tipo for tipo, alias in TIPOS_PRODUCTO.items() if tokens & alias}
