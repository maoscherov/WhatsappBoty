"""
Saca de las respuestas del modelo los rasgos que delatan que las escribió una IA.

El bot escribía con la firma tipográfica del texto generado: negritas y viñetas
en un chat de WhatsApp, un emoji ritual por mensaje siempre en la misma
posición, apertura entusiasta en cada turno ("¡Genial!") y cierre de call center
("¿Hay algo más en lo que pueda ayudarte?"). Los patrones son los que cataloga
Wikipedia en "Signs of AI writing", adaptados al castellano rioplatense.

Por qué determinista y no sólo prompt: las frases de espera estaban prohibidas
en el prompt y se colaban igual — hubo que sacarlas con
`quitar_frases_de_espera`. Mismo criterio acá, y mismo patrón de implementación.

INVARIANTE: los importes quedan textualmente intactos. La regla del negocio es
"el precio que el bot dice es el que cobra", y aguas abajo `productos_con_precio`
busca esos importes dentro del texto. Si el filtro los altera, o si el resultado
queda vacío, se devuelve el original.

NO se aplica a los textos que arma el código (simulador, AMT, mensajes del
backoffice): esos ya los escribió una persona y usan formato a propósito.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Importes: se usan para dos cosas — como red de seguridad final (comparación
# textual, ordenada) y como guarda para no borrar nunca una oración que los
# contenga. La comparación NO se hace por valor: "$1762.96" y "$1,762.96" son
# el mismo número pero rompen igual el matcheo aguas abajo.
# Terminan SIEMPRE en dígito: si no, al sacar una negrita el punto final queda
# pegado al número ("*$16.827,20*." → "$16.827,20.") y la comparación de la red
# de seguridad daba falso positivo.
_IMPORTE_RE = re.compile(
    r"\$\s?\d(?:[\d.,]*\d)?|\b\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{2})?\b|\b\d+[.,]\d{2}\b"
)

_DECIMAL = re.compile(r"(?<=\d)\.(?=\d)")
_URL = re.compile(r"https?://\S+|www\.\S+")
_ALIAS = re.compile(r"\b\w+\.\w+\.\w+\b")

# Negritas de WhatsApp: sólo pares en una misma línea, con contenido pegado a
# los asteriscos (así "2 * 3" o un asterisco suelto quedan intactos).
_NEGRITA = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")

# Viñetas al inicio de línea. Exige espacio después: "-15%" no es una viñeta.
_VINETA = re.compile(r"(?m)^[ \t]*[•·‣▪\-–—]\s+")

# Guiones largos usados como puntuación. Los lookarounds protegen los rangos de
# importes ("de $50.000 – $2.000.000") y los años ("2020–2024").
_GUION = re.compile(r"(?<![\d$%])(?<![\d$%]\s)\s*[—–]\s*(?![\d$])(?!\s?[\d$])")

# Aperturas entusiastas rituales. "Dale" y "Claro" quedan afuera a propósito:
# son rioplatense legítimo y los usan los propios mensajes del equipo.
_APERTURA = re.compile(
    r"^\s*[¡!]?\s*(?:genial|perfecto|excelente|buen[ií]sim[oa]|b[áa]rbaro|"
    r"joya|impecable)\s*(?:[!.…]+|,)\s*",
    re.IGNORECASE,
)

# Preguntas de cierre de call center. OJO con el verbo: dice "proceder", nunca
# "avanzar" ni "continuar" — "¿querés avanzar con el préstamo?" es el próximo
# paso del negocio, no un ritual.
_CIERRE_RITUAL = re.compile(
    r"(?:^|(?<=[.!?…\n]))\s*¿?\s*(?:"
    r"(?:te\s+gustar[ií]a|desea[sr]?|quer[eé]s)\s+proceder\s+con\s+(?:la|el)\s+\w+|"
    r"hay\s+algo\s+m[aá]s\s+en\s+(?:lo|el)\s+que\s+(?:pueda|puedo)\s+"
    r"(?:ayudarte|asistirte|colaborar)|"
    r"(?:en\s+)?qu[eé]\s+m[aá]s\s+puedo\s+(?:hacer|ayudarte)(?:\s+por\s+(?:vos|ti))?|"
    r"puedo\s+ayudarte\s+en\s+(?:algo|alguna\s+otra\s+cosa)\s+m[aá]s"
    r")\s*[?!.…]*\s*",
    re.IGNORECASE,
)

# Muletillas conectoras: se borra el conector y sobrevive la oración.
# "Es importante QUE traigas el DNI" no matchea (falta destacar/mencionar): es
# contenido real. "Por otro lado" exige la coma, para no comerse "por otro lado
# del mostrador".
_MULETILLA_CONECTOR = re.compile(
    r"(?:^|(?<=[.!?…\n]))\s*(?:"
    r"(?:es\s+importante|cabe)\s+(?:destacar|mencionar|se[ñn]alar|notar|resaltar)\s+que\s+|"
    r"(?:adicionalmente|asimismo)\s*,?\s+|"
    r"(?:por\s+otro\s+lado|en\s+resumen|en\s+s[ií]ntesis|en\s+conclusi[oó]n)\s*,\s+"
    r")",
    re.IGNORECASE,
)

# Oraciones de relleno completas.
_FRASE_IA = re.compile(
    r"(?:^|(?<=[.!?…\n]))\s*[¡!]?\s*(?:"
    r"no\s+dudes\s+en\s+[^.!?…\n]*|"
    r"espero\s+que\s+(?:esto\s+)?te\s+(?:sea|resulte|haya\s+sido)\s+[^.!?…\n]*|"
    r"estoy\s+(?:aqu[ií]|ac[aá])\s+para\s+[^.!?…\n]*"
    r")\s*[!.…?]*\s*",
    re.IGNORECASE,
)

# Paralelismo negativo. Opt-in: es la única transformación que toca gramática.
_PARALELISMO = re.compile(
    r"\bno\s+s[oó]?l[oa]?\b\s*(?P<x>[^.!?…\n]*?)\s*,?\s*sino\s+(?:tambi[eé]n|adem[aá]s)\s+",
    re.IGNORECASE,
)

# Emojis. El rango de flechas (U+2190-21FF) queda EXCLUIDO a propósito: "→" se
# usa como puntuación en los textos del simulador.
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U0001F1E6-\U0001F1FF☀-➿⬀-⯿"
    "️‍]+"
)


def _hay_segunda_senal(texto: str) -> bool:
    """
    True si el mensaje acumula más de una marca de entusiasmo/ritual.

    Un "¡Genial!" solo suena humano; lo que delata es la acumulación
    ("¡Perfecto! ... ¡Espero que te sirva! 😊"). Se mide sobre el texto
    ORIGINAL: si se midiera después de borrar el cierre ritual, la apertura
    sobreviviría siempre.
    """
    resto = _APERTURA.sub("", texto, count=1)
    return bool(
        "!" in resto
        or _EMOJI.search(resto)
        or _CIERRE_RITUAL.search(resto)
        or _FRASE_IA.search(resto)
        or _MULETILLA_CONECTOR.search(resto)
    )


def _borrar_sin_importe(patron: re.Pattern, texto: str) -> str:
    """Borra lo que matchea el patrón, salvo que contenga un importe."""
    return patron.sub(
        lambda m: m.group(0) if _IMPORTE_RE.search(m.group(0)) else " ", texto
    )


def _capitalizar(texto: str) -> str:
    """Recapitaliza el inicio del mensaje y lo que sigue a un punto."""
    def _upper(m):
        return m.group(0)[:-1] + m.group(0)[-1].upper()
    texto = re.sub(r"^\s*[a-záéíóúñ]", lambda m: m.group(0).upper(), texto)
    return re.sub(r"(?<=[.!?…])\s+[a-záéíóúñ]", _upper, texto)


def _solo_un_emoji(texto: str) -> str:
    """Deja el primer emoji del mensaje y borra el resto."""
    vistos = [0]

    def _uno(m):
        vistos[0] += 1
        return m.group(0) if vistos[0] == 1 else ""

    return _EMOJI.sub(_uno, texto)


def humanizar(texto: str, paralelismo: bool = False) -> str:
    """
    Devuelve el texto sin los rasgos que delatan escritura de IA.

    `paralelismo=True` además reescribe "no solo X sino también Y" — apagado por
    defecto porque es la única transformación que toca la gramática.

    Si el resultado queda vacío o alteraría un importe, devuelve el original.
    """
    if not texto:
        return texto

    original = texto
    importes = _IMPORTE_RE.findall(original)
    sacar_apertura = _hay_segunda_senal(original)

    # Proteger lo que no se puede tocar ni partir: decimales de importes, URLs y
    # alias. Sin esto, el punto de "$1762.96" hace de fin de oración.
    guardados: list[str] = []

    def _guardar(m):
        guardados.append(m.group(0))
        return f"\x01{len(guardados) - 1}\x01"

    t = _URL.sub(_guardar, texto)
    t = _ALIAS.sub(_guardar, t)
    t = _DECIMAL.sub("\x00", t)

    # Forma primero: las negritas tienen que caer antes de que la apertura
    # intente anclar en "^¡Perfecto!", y "• *Online*" necesita las dos pasadas.
    t = _NEGRITA.sub(r"\1", t)
    t = _VINETA.sub("", t)
    # El guion largo se convierte en puntuación, y con eso CREA el límite de
    # oración donde después anclan las muletillas y los cierres.
    t = _GUION.sub(lambda m: ". " if _proximo_es_mayuscula(t, m.end()) else ", ", t)

    # Recorte quirúrgico antes de los borrados grandes, para no dejar huérfano
    # el conector.
    t = _MULETILLA_CONECTOR.sub("", t)

    # Borrados de oración completa (nunca una que contenga un importe).
    t = _borrar_sin_importe(_CIERRE_RITUAL, t)
    t = _borrar_sin_importe(_FRASE_IA, t)
    if paralelismo:
        t = _PARALELISMO.sub(r"\g<x> y ", t)

    if sacar_apertura:
        t = _APERTURA.sub("", t, count=1)

    # Último de contenido: si el segundo emoji vivía en una oración ya borrada,
    # no debe consumir la cuota.
    t = _solo_un_emoji(t)

    t = _capitalizar(t)

    # Limpieza de espacios. NUNCA \s{2,} → " ": eso mataría los \n\n que
    # estructuran el mensaje.
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"(?m)[ \t]+$", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()

    t = t.replace("\x00", ".")
    for i, g in enumerate(guardados):
        t = t.replace(f"\x01{i}\x01", g)

    if not t:
        return original
    if _IMPORTE_RE.findall(t) != importes:
        logger.warning("humanizar alteró un importe — se devuelve el original")
        return original
    return t


def _proximo_es_mayuscula(texto: str, pos: int) -> bool:
    resto = texto[pos:].lstrip()
    return bool(resto) and resto[0].isupper()
