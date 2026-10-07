"""
Helpers compartidos del flujo de checkout: derivación por receta y
finalización de compra (link de pago + modo de entrega).

Usados por webhook.py (WhatsApp real) y simulate.py (testing) para no
duplicar la lógica de negocio.
"""

import logging
import re
from typing import Optional
from urllib.parse import urlsplit

from app.services.perfil import get_perfil
from app.services.sku_service import requiere_derivacion

logger = logging.getLogger(__name__)

# Detección de modo de entrega (compartida por webhook y simulate)
_RETIRO = [r"\bretiro\b", r"\bretirar\b", r"\bsucursal\b", r"\bpaso\b", r"\bbusco\b",
           r"\bvoy\b", r"\bretiro yo\b", r"\ben el local\b", r"\bpasar\b"]
_ENVIO  = [r"\benv[ií]o\b", r"\benviar\b", r"\benv[ií]en\b", r"\bdomicilio\b",
           r"\bmand[aá]\b", r"\bmandame\b", r"\bmanden\b", r"\bcasa\b", r"\bdelivery\b",
           r"\ba domicilio\b",
           # 5/10 (C-3854, C-3912): "mandámelo", "me lo envías", "me lo podés
           # enviar", "traémelo" se tomaban como otra cosa y el bot repreguntaba.
           r"\bmand[aá](lo|melo|mela)\b", r"\benvi[aá](lo|melo|mela|me)\b",
           r"\btra[eé](lo|melo|mela)\b",
           r"\bme\s+lo\s+(env[ií]\w*|mand\w*|tra[eé]\w*)\b",
           r"\bme\s+lo\s+\w+\s+(enviar|mandar|traer)\b"]


def match_retiro(t: str) -> bool:
    return any(re.search(p, t, re.IGNORECASE) for p in _RETIRO)


def match_envio(t: str) -> bool:
    # "mandame el link" no es un envío a domicilio.
    if re.search(r"\b(link|pago|comprobante|foto|lista)\b", t or "", re.IGNORECASE) and \
            not re.search(r"\b(env[ií]o|domicilio|delivery|casa)\b", t or "", re.IGNORECASE):
        return False
    return any(re.search(p, t, re.IGNORECASE) for p in _ENVIO)


# Afirmación de la dirección propuesta ("sí, ahí", "dale a esa", "a mi domicilio").
# Se usa cuando el bot ya ofreció la dirección del socio y el cliente la acepta.
_AFIRMA  = [r"\bs[ií]\b", r"\bdale\b", r"\bok\b", r"\bbueno\b", r"\bperfecto\b",
            r"\blisto\b", r"\bok\b", r"\bahi\b", r"\bahí\b"]
_DIR_CUE = [r"\bah[ií]\b", r"\besa\b", r"\bese\b", r"\bdomicilio\b", r"\bcasa\b",
            r"\besa direcci[oó]n\b", r"\bmi domicilio\b"]

def afirma_envio(t: str) -> bool:
    """True si el cliente acepta la dirección de envío propuesta (ej: 'sí, ahí')."""
    tiene_afirma = any(re.search(p, t, re.IGNORECASE) for p in _AFIRMA)
    tiene_cue    = any(re.search(p, t, re.IGNORECASE) for p in _DIR_CUE)
    return tiene_afirma and tiene_cue


# Cambio de dirección: el cliente quiere enviar a otra parte.
_CAMBIO_DIR = [r"otra direcci[oó]n", r"cambiar.{0,12}direcci[oó]n", r"distinta direcci[oó]n",
               r"a otra parte", r"a otro lado", r"otro domicilio", r"cambiar.{0,8}env[ií]o",
               r"nueva direcci[oó]n"]
# Una dirección escrita: nombre de calle + número de 2 a 5 dígitos (ej: "donado
# 608", "16 de enero 9279"). Antes bastaba "palabra + número" y se tomaban como
# domicilio "te pedi 1 blister", "sertal 10 comprimidos" u "optiser de 20 mg":
# el link salía "a domicilio a *Esta perfecto, pero solo te pedi 1 blister*"
# (casos reales 6/8, 26/8, 1/10).
_NO_DIR = re.compile(
    r"\?|\d\s*(mg|ml|gr?s?|cc|mcg|ui|%)\b|\bx\s*\d+|"
    r"\b(comprimid\w*|comp|c[aá]psul\w*|bl[ií]ster\w*|caja\w*|tiras?|unidad\w*|frasco\w*|"
    r"ped[ií]\w*|quiero|quer[ií]a|precio\w*|link|cu[aá]nto|stock|ten[eé]s|tendr[aá]s|"
    r"receta\w*|veces|producto\w*)\b",
    re.IGNORECASE)
# Palabras que cortan la calle hacia atrás: "por favor me lo envías san javier
# 837" → "san javier 837".
_STOP_DIR = {
    "por", "favor", "me", "lo", "la", "el", "a", "al", "en", "mi", "es", "y", "que",
    "te", "seria", "sería", "ahora", "ok", "dale", "si", "sí", "bueno", "perfecto",
    "mejor", "entonces", "para", "otra", "nueva", "distinta", "direccion", "dirección",
    "domicilio", "envias", "envías", "envia", "envía", "envialo", "envíalo", "enviamelo",
    "envíamelo", "enviame", "envíame", "enviar", "enviarlo", "manda", "mandá", "mandalo",
    "mandálo", "mandamelo", "mandámelo", "mandame", "mandar", "mandarlo", "traelo",
    "traémelo", "traemelo", "vivo", "casa", "queda",
}
_SUFIJO_DIR = {"bis", "piso", "dto", "dpto", "depto", "timbre", "casa", "lote"}
# Esquina: solo con "esquina" explícito o el lado "9 de julio" — "jabón y crema"
# no es una dirección.
_ESQUINA_RE = re.compile(
    r"\b((?:calle|av\.?|avenida|bv\.?|bulevar|pje\.?|pasaje)?\s*[a-záéíóúñ.]{3,}(?:\s+[a-záéíóúñ.]{2,})?)"
    r"\s+(?:y|e|esquina)\s+(\d{1,2}\s+de\s+[a-záéíóúñ]+|[a-záéíóúñ.]{3,}(?:\s+[a-záéíóúñ.]{2,})?)",
    re.IGNORECASE)


def quiere_cambiar_direccion(t: str) -> bool:
    return any(re.search(p, t, re.IGNORECASE) for p in _CAMBIO_DIR)


def parece_direccion(t: str) -> bool:
    return extraer_direccion_de(t) is not None


def extraer_direccion_de(t: str) -> Optional[str]:
    """
    Extrae SOLO la dirección escrita en el mensaje (calle + número, con piso /
    depto / bis, o una esquina). Devuelve None si el mensaje no la contiene o
    habla de productos, cantidades o precios: un texto cualquiera nunca se
    toma como domicilio de entrega.
    """
    if not t or _NO_DIR.search(t):
        return None
    toks = re.findall(r"[\wáéíóúñÁÉÍÓÚÑ.°º]+", t)
    for i in range(len(toks) - 1, 0, -1):
        if not re.fullmatch(r"\d{2,5}", toks[i]):
            continue
        k = i - 1
        while k >= 0 and i - k <= 4 and toks[k].lower().strip(".") not in _STOP_DIR:
            k -= 1
        calle = toks[k + 1:i]
        if not calle or not re.search(r"[a-záéíóúñ]{2,}", " ".join(calle), re.IGNORECASE):
            continue
        fin = i + 1
        while fin < len(toks) and (
                toks[fin].lower().strip(".") in _SUFIJO_DIR
                or (re.fullmatch(r"\w{1,3}", toks[fin])
                    and toks[fin - 1].lower().strip(".") in _SUFIJO_DIR)):
            fin += 1
        return " ".join(toks[k + 1:fin])
    m = _ESQUINA_RE.search(t)
    if m and len(toks) <= 8 and (
            re.search(r"\besquina\b", t, re.IGNORECASE)
            or re.match(r"\d{1,2}\s+de\s+", m.group(2))
            or re.match(r"(calle|av|avenida|bv|bulevar|pje|pasaje)\b", m.group(1).strip(), re.IGNORECASE)):
        lado1 = " ".join(w for w in m.group(1).split() if w.lower().strip(".") not in _STOP_DIR)
        if lado1:
            return f"{lado1} y {m.group(2)}".strip()
    return None


# Pedido explícito de hablar con una persona → derivar al operador.
# Patrones que exigen un verbo de contacto + destinatario humano, para no
# dispararse con "algo para una persona mayor".
_HUMANO = [
    r"\basesor(a|es)?\b",
    r"\b(hablar|pasame|pas[aá]s|pasar|paso|comunicar\w*|comunicame|deriv\w+|atienda|atiende|atenderme)\b"
    r".{0,20}\b(persona|alguien|humano|humana|operador|asesor|encargad|vendedor|farmac\w+)\b",
    r"\bpersona real\b",
    r"\bun humano\b",
    r"\bcon alguien\b",
    r"\batenci[oó]n humana\b",
    r"\bquiero hablar con\b",
    # Feedback 56 (15/9): "derivame", "atención personalizada" (así la llama
    # la farmacia en sus mensajes y los clientes la repiten) y "pasame con el
    # equipo" no matcheaban y el bot repetía el mensaje de sin stock.
    r"\bderiv(a|ame|arme|ar|anme|alo)\b",
    r"\batenci[oó]n personalizada\b",
    r"\b(hablar|pasame|pas[aá]s|pasar|paso|comunicar\w*|comunicame)\b.{0,20}\bequipo\b",
    # Caso real 23/9: "¿me pasás con las chicas?" — así nombran los clientes a
    # las empleadas de la farmacia.
    r"\b(hablar|pasame|pas[aá]s|pasar|paso|comunicar\w*|comunicame|atienda|atiendan)\b"
    r".{0,25}\b(las?\s+chicas?|los\s+chicos|las?\s+farmac[eé]utic[oa]s?|alguien\s+de\s+la\s+farmacia|"
    r"el\s+personal|una\s+empleada)\b",
    r"\bhablar\s+con\s+alguien\b",
]


def pide_humano(t: str) -> bool:
    """True si el cliente pide explícitamente ser atendido por una persona."""
    return any(re.search(p, t, re.IGNORECASE) for p in _HUMANO)


_PRECIO_RE = re.compile(r"\$?\s?(\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{2})?|\d+[.,]\d{2}|\d{3,})")


def precios_mencionados(texto: str) -> set[float]:
    """
    Precios que aparecen en un texto, tolerando los formatos que mezcla el LLM
    ($18,057.61 / $18.057,61 / 18057.61). Se usa para verificar que la respuesta
    enviada al cliente realmente ofrece un producto concreto.
    """
    out: set[float] = set()
    for crudo in _PRECIO_RE.findall(texto or ""):
        s = crudo.strip()
        # El último separador es el decimal sólo si le siguen exactamente 2 dígitos.
        if len(s) > 3 and s[-3] in ".," :
            entero = re.sub(r"[.,]", "", s[:-3])
            s = f"{entero}.{s[-2:]}"
        else:
            s = re.sub(r"[.,]", "", s)
        try:
            out.add(round(float(s), 2))
        except ValueError:
            continue
    return out


def productos_con_precio(respuesta: str, resultados: list[dict]) -> list[dict]:
    """Productos de `resultados` cuyo precio aparece en la respuesta enviada."""
    if not respuesta or not resultados:
        return []
    precios = precios_mencionados(respuesta)
    if not precios:
        return []
    out = []
    for r in resultados:
        try:
            if round(float(r.get("precio") or 0), 2) in precios:
                out.append(r)
        except (TypeError, ValueError):
            continue
    return out


def producto_respaldado(respuesta: str, resultados: list[dict]) -> Optional[dict]:
    """
    El ÚNICO producto cuyo precio aparece en la respuesta, o None.

    Exactamente uno: si la respuesta menciona varios precios es una LISTA de
    opciones ("elegí cuál"), no la oferta de un producto — dejar el primero
    como pendiente hizo que "perfecto, rubio oscuro" confirmara la opción 1
    en vez de la 2 (caso real, link de $33.437 por el producto equivocado).
    """
    matches = productos_con_precio(respuesta, resultados)
    return matches[0] if len(matches) == 1 else None


_PAGO_MANUAL = [
    r"\btransferencia\b", r"\btransferir\b", r"\btransfiero\b", r"\btransferis\b",
    r"\befectivo\b", r"\bcbu\b", r"\balias\b", r"\bmercado\s*pago\b",
    # Casos 29 y 31: "lo pago en la sucursal cuando retiro" y "cuenta corriente"
    # recibían link de pago igual. Son medios que coordina una persona.
    r"\bcuenta\s+corriente\b",
    r"\bpag\w+\b.{0,30}\b(sucursal|local|farmacia|caja|retir\w+|ah[ií]|all[aá])\b",
    r"\b(retir\w+|sucursal)\b.{0,30}\bpag\w+",
]


def pide_pago_manual(t: str) -> bool:
    """
    True si el cliente pide pagar por transferencia o efectivo. Regla de negocio
    (minuta 2026-07-31): esos medios se derivan SIEMPRE a una persona — la
    transferencia requiere validar comprobante, el efectivo no se ofrece por el bot.
    """
    return any(re.search(p, t, re.IGNORECASE) for p in _PAGO_MANUAL)


_CUENTA_CORRIENTE = [
    r"\bcuenta\s+corriente\b",
    r"\bcta\.?\s*(cte\.?|corriente)\b",
    # Cualquier persona del verbo: "anotalo", "lo anoto", "lo cargo", "lo
    # pongo en la cuenta" (5/10: "lo anoto en la cuenta" no matcheaba y caía
    # al modelo). "anotado"/"cargado" no son pedidos.
    r"\b(carg(?!ad)\w*|anot(?!ad)\w*|sum[aáo]\w*|pon[eéégo]\w*|pong\w*)\b.{0,30}\b(mi\s+|la\s+)?cuenta\b",
    r"\ba\s+la\s+cuenta\b",
]


def pide_cuenta_corriente(t: str) -> bool:
    """
    True si el cliente pide pagar con cuenta corriente. Minuta 79: los socios
    activos la tienen habilitada por default — este medio ya NO deriva
    (pide_pago_manual sigue matcheando "cuenta corriente" como red de
    seguridad para no-socios y excepciones, que sí van a una persona).
    """
    # Sin tildes: "Anótamelo en la cuenta" / "anotámelo" no matcheaban el
    # patrón "anot[aá]" y el modelo improvisaba "no puedo anotarlo" (caso real
    # 18/9, Mauricio, socio).
    s = _sin_tildes(t)
    return any(re.search(p, s, re.IGNORECASE) for p in _CUENTA_CORRIENTE)


def _sin_tildes(t: str) -> str:
    s = t or ""
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"),
                 ("Á", "A"), ("É", "E"), ("Í", "I"), ("Ó", "O"), ("Ú", "U")):
        s = s.replace(a, b)
    return s


# Consulta de saldo / deuda de cuenta corriente. El bot no tiene esos datos:
# "me dirían lo que debo", "saldo de mi cuenta corriente", "resumen de cuenta"
# recibían "cuando armemos tu pedido lo cargamos a tu cuenta" o un "no te
# entendí" (~10 casos reales entre 25/9 y 2/10). Va siempre a una persona.
_SALDO_FUERTE = [
    r"\bsaldo\b",
    r"\bresumen\b.{0,25}\bcuenta\b",
    r"\bestado\s+de\s+(mi\s+)?cuenta\b",
    r"\bmi\s+deuda\b",
    r"\bcuota\b.{0,15}\bsocio\b",
    r"\b(les|le|te)\s+debo\s+(algo|plata|recetas?|de\s+antes)\b",
]
# "Cuánto te debo" con un pedido en curso es el total de ESE pedido: solo
# cuenta como consulta de saldo sin compra abierta.
_SALDO_DEBIL = [
    r"\bcu[aá]nto\s+(te\s+|les\s+|le\s+)?deb\w*",
    # "lo q / lo qe / lo qie debo" (C-4115); "lo que debo tomar" es otra cosa.
    r"\blo\s+q\w{0,3}\s+(te\s+|les\s+|le\s+)?debo\b(?!\s+(tomar|hacer|usar|comprar|pedir))",
    r"\b(les|le|te)\s+debo\b",
    r"\bdebo\s+algo\b",
]


def consulta_saldo(t: str, hay_pedido: bool = False) -> bool:
    """True si el cliente pregunta su saldo / deuda de cuenta corriente."""
    s = _sin_tildes(t or "").lower()
    if any(re.search(p, s) for p in _SALDO_FUERTE):
        return True
    return not hay_pedido and any(re.search(p, s) for p in _SALDO_DEBIL)


# "anotalo", "anotame", "lo anoto" (5/10); "anotado" no es un pedido.
_ANOTAR = r"\b(anot(?!ad)\w*|apunt(?!ad)\w*)\b"
_ANOTAR_OTRA_COSA = r"\b(direccion|domicilio|telefono|numero|nombre|receta|mail|correo)\b"


def pide_anotar(t: str) -> bool:
    """
    "Anotalo", "anotámelo", "me lo anotás": así piden cuenta corriente los
    socios de la mutual, sin decir "cuenta" (feedback 29, 42, 49). SOLO vale
    como cuenta corriente con un pedido en curso — lo decide quien llama.
    """
    s = _sin_tildes(t).lower()
    return bool(re.search(_ANOTAR, s)) and not re.search(_ANOTAR_OTRA_COSA, s)


def entrega_ya_elegida(session: dict) -> Optional[tuple[str, Optional[str]]]:
    """
    (tipo_entrega, direccion) si el cliente ya eligió cómo recibir el pedido
    en curso, o None. Evita volver a preguntar retiro/envío y la dirección
    cuando cambia el medio de pago con el link ya enviado (caso real 18/9).
    """
    tipo = session.get("tipo_entrega")
    if tipo == "retiro":
        return "retiro", None
    if tipo == "envio" and (session.get("direccion_envio") or "").strip():
        return "envio", session["direccion_envio"]
    return None


async def habilitado_cc(phone: str, cfg: dict, socio_svc, monto: float = 0.0) -> Optional[dict]:
    """
    El socio del padrón (o el empleado) si puede pagar con cuenta corriente,
    o None. None si: la función está apagada (cc_enabled), el teléfono no es
    socio ni empleado, figura en la lista de excepciones de la farmacia, o
    supera el tope (cc_tope_monto, 0 = sin tope).

    Empleados (5/10): tienen cuenta corriente como los socios, con el mismo
    tope y excepciones. Antes solo valía el padrón de socios y una empleada
    que no era socia no podía anotar ("lo anoto en la cuenta" se perdía).
    """
    if str(cfg.get("cc_enabled", "true")).lower() != "true":
        return None
    try:
        socio = socio_svc.find_by_phone(phone) if socio_svc else None
    except Exception:
        socio = None
    if not socio:
        try:
            from app.services.empleado_service import get_empleado_service
            emp = get_empleado_service().find_by_phone(phone)
        except Exception:
            emp = None
        if emp:
            socio = {**emp, "empleado": True}
    if not socio:
        return None
    try:
        from app.config import get_settings as _gs
        from app.services.cc_service import get_cc_service
        if await get_cc_service(_gs().redis_url).es_excepcion(socio):
            return None
    except Exception as e:
        logger.warning(f"CC: no se pudo chequear la excepción de {phone}: {e}")
        return None   # ante la duda, que lo coordine una persona
    try:
        tope = float(cfg.get("cc_tope_monto") or 0)
    except (TypeError, ValueError):
        tope = 0.0
    if tope > 0 and monto > tope:
        return None
    return socio


# Frases de espera que el modelo promete y nunca cumple ("ahora verifico...").
# El prompt las prohíbe pero a veces se cuelan: se eliminan por oración.
# El recorte arranca EN la palabra disparadora (no al inicio de la oración):
# el modelo a veces pega la promesa a la oferta sin punto ("...por $1762.96
# Ahora voy a buscar...") y borrar la oración entera se llevaba la oferta.
_FRASE_ESPERA = re.compile(
    r"\b(ahora|voy\s+a|dejame|d[eé]jame|en\s+un\s+momento|ya\s+te|luego\s+te|"
    r"despu[eé]s\s+te|un\s+segundito|aguardame|esperame)\b"
    r"[^.!?…]*\b(verific\w+|chequ\w+|consulto|confirmo|busc\w+|averig\w+|revis\w+|fijo)\w*"
    r"[^.!?…]*[.!?…]?",
    re.IGNORECASE,
)

# Cortesías de espera SOLAS ("Un momento, por favor."): prometen algo que no
# llega. Solo se elimina la oración compuesta únicamente por la cortesía —
# "En un momento te contactamos" tiene verbo y sobrevive.
_CORTESIA_ESPERA = re.compile(
    r"(?:^|(?<=[.!?…]))\s*(un\s+moment(?:o|ito)|un\s+segund(?:o|ito)|"
    r"aguard[aá]\w*|esper[aá](?:me|mos)?)\s*,?\s*(por\s+favor)?\s*[.!?…]",
    re.IGNORECASE,
)


def quitar_frases_de_espera(texto: str) -> str:
    """
    Elimina oraciones tipo "Ahora verifico X para vos": prometen una
    verificación que nunca llega (no hay segundo mensaje). Si el resultado
    queda vacío, se devuelve el original — mejor una promesa fea que silencio.
    """
    # El punto decimal de un precio ("$1762.96") NO es fin de oración: se
    # protege antes de segmentar para no partir el número.
    protegido = re.sub(r"(?<=\d)\.(?=\d)", "\x00", texto or "")
    limpio = _FRASE_ESPERA.sub(" ", protegido)
    limpio = _CORTESIA_ESPERA.sub(" ", limpio).strip()
    limpio = re.sub(r"\s{2,}", " ", limpio).replace("\x00", ".")
    return limpio if limpio else (texto or "")


# Anuncios de compra/confirmación que SOLO puede hacer el sistema (cuando arma
# el carrito o genera el link). El modelo declaró "tu pedido queda confirmado
# ... ¡gracias por tu compra!" arrastrando el contexto de una charla vieja
# (caso real 1/9) — si estas frases vienen de él, se recorta la oración.
_CONFIRMACION_FANTASMA = re.compile(
    r"(?:^|(?<=[.!?…]))[^.!?…$]*"
    r"\b(?:(?:pedido|compra)[^.!?…$]{0,30}confirmad\w+|"
    r"queda(?:r[aá])?\s+confirmad\w+|"
    r"gracias\s+por\s+tu\s+compra|"
    r"compra\s+(?:realizada|exitosa)|"
    r"te\s+esperamos\b[^.!?…$]{0,40}\bretirar\w*|"
    # Reservas/urgencia (11/9): no existe flujo de reserva; "lo reservamos" y
    # "quedan pocas, conviene reservarlo" son promesas que nadie cumple.
    r"\breserv(?:a|amos|ar\w*|ad\w+|o)\b|"
    r"\bapart(?:amos|ar\w*|ad\w+)\b|"
    r"quedan?\s+poc[oa]s?\s+unidad\w*)"
    r"[^.!?…$]*[.!?…]?",
    re.IGNORECASE,
)


def quitar_confirmaciones_fantasma(texto: str) -> str:
    """
    Recorta oraciones donde el modelo anuncia una compra, confirmación o
    RESERVA que el sistema no hizo (no hay flujo de reserva: "lo reservamos" y
    "quedan pocas unidades, conviene reservarlo" quedan afuera). Las oraciones con importes nunca se tocan (el precio que
    el bot dice es el que cobra), y si el resultado queda vacío se devuelve el
    original — mejor un anuncio de más que un bot mudo.
    """
    protegido = re.sub(r"(?<=\d)\.(?=\d)", "\x00", texto or "")
    limpio = _CONFIRMACION_FANTASMA.sub(" ", protegido)
    limpio = re.sub(r"\s{2,}", " ", limpio).strip().replace("\x00", ".")
    return limpio if limpio else (texto or "")


def personalizar_nombre(texto: str, nombre: str = "") -> str:
    """
    Resuelve el placeholder {nombre} en los mensajes fijos (recepción de
    receta/credencial): con nombre lo reemplaza; sin nombre elimina el saludo
    que lo contiene ("¡Hola {nombre}! ...") para que no quede "¡Hola !". Si el
    texto no trae placeholder, sale intacto — las configs viejas siguen
    funcionando igual.
    """
    if "{nombre}" not in (texto or ""):
        return texto or ""
    if nombre:
        return texto.replace("{nombre}", nombre)
    limpio = re.sub(r"¡?\s*hola,?\s*\{nombre\}\s*[!,.]?\s*", "", texto, flags=re.IGNORECASE)
    limpio = limpio.replace("{nombre}", "").strip()
    limpio = re.sub(r"\s{2,}", " ", limpio)
    return limpio or texto.replace("{nombre}", "").strip()


_PIDE_FOTO = [
    r"\b(mand|pas|env[ií]|ten[eé]|hay|ver|mostr|sac)\w*\b.{0,25}\b(foto|fotos|imagen|im[aá]genes)\b",
    r"\b(foto|fotos|imagen|im[aá]genes)\b.{0,25}\b(mand|pas|env[ií]|ten[eé]|mostr)\w*\b",
    r"\bc[oó]mo\s+(es|viene|se\s+ve)\b.{0,20}\?",
]


def pide_foto(t: str) -> bool:
    """
    True si el cliente pide ver una foto/imagen de un producto. Regla de
    negocio: eso lo atiende una persona (saca la foto real del producto y se
    la manda), no el bot.
    """
    return any(re.search(p, t, re.IGNORECASE) for p in _PIDE_FOTO)


# Texto que solo SEÑALA ("necesito esos productos", "los de la foto") sin
# nombrar nada. Solo, no dice qué quiere el cliente — la referencia es una
# imagen. Palabras funcionales alrededor permitidas; cualquier sustantivo
# concreto ("...y un tafirol") lo saca de esta categoría.
_DEICTICO_RE = re.compile(
    r"^\s*(?:hola[,!\s]*)?"
    r"(?:necesito|quiero|dame|me\s+(?:das|mand[aá]s|env[ií][aá]s)|te\s+encargo)?\s*"
    r"(?:esos?|estos?|esas?|estas?|eso|aquellos?)\s*"
    r"(?:productos?|art[ií]culos?|cosas?|[ií]tems?)?\s*"
    r"(?:de\s+la\s+foto|que\s+te\s+mand[eé])?\s*[?!.]*\s*$"
    r"|^\s*(?:necesito\s+|quiero\s+|dame\s+)?los\s+(?:productos\s+)?de\s+la\s+foto\s*[?!.]*\s*$",
    re.IGNORECASE,
)


def texto_deictico(t: str) -> bool:
    """
    True si el mensaje solo señala productos sin nombrarlos ("esos productos").

    Caso real (31/8): la foto de los productos y este texto llegan como dos
    mensajes; el texto suele llegar primero (la imagen tarda en subir) y el
    bot preguntaba "¿podrías especificar?" un segundo antes de responder todo
    con la imagen. Si el lote trae imagen + texto deíctico, el texto se
    descarta: la imagen es el pedido.
    """
    return bool(_DEICTICO_RE.match(t or ""))


_TODOS = [
    r"\b(todos|todas|todo)\b",
    r"\blos\s+(dos|tres|cuatro)\b", r"\blas\s+(dos|tres|cuatro)\b",
    r"\bambos\b", r"\bambas\b",
]


def pide_todos(t: str) -> bool:
    """
    True si el cliente quiere TODOS los productos ofrecidos ("mandame todos",
    "los tres", "ambos").

    Regresión (27/8): pidió tres productos, dijo "Si mándame todos" y el link
    salió por uno. Un mensaje que arranca con "no" nunca cuenta como pedido de
    todo — "no, todos no" es lo contrario.
    """
    texto = t or ""
    if re.match(r"^\s*no\b", texto, re.IGNORECASE):
        return False
    return any(re.search(p, texto, re.IGNORECASE) for p in _TODOS)


_RECETA_NUBE = [
    r"\breceta\w*\b.{0,40}\b(nube|sistema|cargad\w+|electr[oó]nic\w+)",
    r"\b(nube|sistema)\b.{0,40}\breceta",
]


def pide_receta_nube(t: str) -> bool:
    """
    True si el cliente refiere a recetas "en la nube" / electrónicas / cargadas
    en el sistema. El bot no accede a ese sistema: deriva SIEMPRE a una persona.
    (Caso real 19/8: "un momento, por favor" y silencio hasta el cierre.)
    """
    return any(re.search(p, t, re.IGNORECASE) for p in _RECETA_NUBE)


_DESCUENTO = [r"\bdescuent\w+", r"\bprecio\s+de\s+socio\b"]


def pregunta_descuento(t: str) -> bool:
    """
    True si el mensaje menciona descuentos. La respuesta es SIEMPRE texto fijo
    según la config — el modelo inventó un descuento de socia con un precio
    inexistente (caso 29): nunca más redacta él sobre descuentos.
    """
    return any(re.search(p, t, re.IGNORECASE) for p in _DESCUENTO)


_LINK_RE = re.compile(r"(https?://\S+|www\.\S+|\S+\.(?:pdf|jpg|jpeg|png)\b)", re.IGNORECASE)


# Sufijos genéricos de segundo nivel: en "bot.mascotasdeloeste.com.ar" el
# dominio propio es el host entero, nunca "com.ar".
_SLD_GENERICOS = {"com", "net", "org", "gob", "gov", "edu", "co"}


def dominio_propio(base_url: str) -> str:
    """
    Dominio de los links propios, a partir de PUBLIC_BASE_URL: el host en
    minúsculas; con 3 o más etiquetas que no terminan en un sufijo genérico
    de segundo nivel (com.ar, gob.ar, co.uk...), las dos últimas
    ("cerca.remedia.ar" → "remedia.ar"). Sin URL, "".
    """
    s = (base_url or "").strip()
    if not s:
        return ""
    try:
        host = (urlsplit(s if "://" in s else "//" + s).hostname or "").lower().rstrip(".")
    except ValueError:                       # URL mal cargada: sin dominio propio
        return ""
    if not host or host.replace(".", "").isdigit():      # IP de desarrollo: tal cual
        return host
    partes = host.split(".")
    if len(partes) >= 3 and not (partes[-2] in _SLD_GENERICOS and len(partes[-1]) == 2):
        return ".".join(partes[-2:])
    return host


def contiene_link(t: str, dominio_propio: str = "") -> bool:
    """
    True si el mensaje trae una URL o referencia a un archivo (receta/bono
    enviado como link en vez de foto) → se deriva a una persona, igual que
    una imagen de receta. Excluye los links de pago (/pay/) y, si viene, los
    del dominio propio del deploy (ver `dominio_propio`).
    """
    m = _LINK_RE.search(t or "")
    if not m:
        return False
    link = m.group(0).lower()
    if "/pay/" in link:
        return False
    dominio = (dominio_propio or "").strip().lower()
    return not (dominio and dominio in link)


def necesita_receta(sku_svc, sku_id: str, modo: str) -> bool:
    """True si el producto pendiente requiere derivación por receta."""
    # Llave única de la derivación por receta. Sin recetas (petshop) quedan
    # apagados sin tocarlos: confirmar_pedido, derivar_si_receta,
    # _sumar_productos_nuevos, referencia_ambigua_bloquea, "agregame" y
    # simulate; y quitar_receta_inventada corre con todo producto ofrecido.
    if not get_perfil().recetas:
        return False
    if not sku_id:
        return False
    sku = sku_svc.get_by_id(sku_id)
    if not sku:
        return False
    return requiere_derivacion(sku.requiere_receta, modo)


async def derivar_si_receta(sku_svc, session_svc, cfg: dict, phone: str, sku_id: str,
                            nombre: str = "", extras: Optional[list[dict]] = None):
    """
    Si el producto recién elegido requiere receta, deriva a una persona en el
    acto (sin ofrecer link de pago) y devuelve el mensaje para el cliente.
    Si no, devuelve None y el flujo sigue normal.

    Los productos de venta libre del mismo pedido (el carrito en curso y los
    `extras` que pidió en el mismo mensaje) pasan al operador ya elegidos:
    quedan en el pedido de la sesión, que el backoffice muestra. Antes la
    derivación borraba todo y "envíame lo otro" quedaba sin respuesta
    (decisión 3/10: con un producto con receta, todo va al operador).
    """
    modo = cfg.get("receta_mode", "conservador")
    if necesita_receta(sku_svc, sku_id, modo):
        # Cotizado por el operador (receta ya vista): no se re-deriva.
        _s = await session_svc.get(phone)
        if _s.get("receta_validada") and _s.get("pending_sku_id") == sku_id:
            return None
        # Hand-off limpio: sin producto pendiente (no se puede vender) y en
        # modo operador. Así no queda un pending que re-dispare la derivación.
        # Feedback 49 (5/9): si el turno anterior ya le dijimos que lleva
        # receta y ahora pide que se lo anotemos, "te derivo" alcanza —
        # repetir "requiere receta" suena a que no lo escuchamos.
        _ya_dicho = receta_ya_mencionada(_s.get("history") or [])
        carrito = list(_s.get("pending_items") or [])
        if not carrito and _s.get("pending_sku_id"):
            carrito = [{"sku_id": _s["pending_sku_id"], "nombre": _s.get("pending_sku_nombre") or "",
                        "precio": _s.get("pending_precio") or 0,
                        "cantidad": _s.get("pending_cantidad", 1)}]
        from app.services.receta_marcas import recordar_producto_por_receta
        conservar: list[dict] = []
        for it in carrito + list(extras or []):
            sid = str(it.get("sku_id") or "")
            if not sid or sid == str(sku_id) or any(c["sku_id"] == sid for c in conservar):
                continue
            if necesita_receta(sku_svc, sid, modo):
                # Otro del pedido que también lleva receta: el operador lo ve.
                await recordar_producto_por_receta(session_svc, sku_svc, phone, sid)
                continue
            conservar.append({"sku_id": sid, "nombre": it.get("nombre") or "",
                              "precio": float(it.get("precio") or 0),
                              "cantidad": int(it.get("cantidad", 1) or 1)})
        # Qué producto frenó la venta: el operador lo marca desde el chat (28/9).
        await recordar_producto_por_receta(session_svc, sku_svc, phone, sku_id)
        await session_svc.clear_pending(phone)
        await session_svc.set_estado(phone, "operador", motivo="receta")
        if conservar:
            _s2 = await session_svc.get(phone)
            primero = conservar[0]
            _s2.update({
                "pending_sku_id": primero["sku_id"], "pending_sku_nombre": primero["nombre"],
                "pending_precio": primero["precio"], "pending_cantidad": primero["cantidad"],
                "pending_items": conservar, "pending_opciones": [],
            })
            await session_svc.save(phone, _s2)          # sigue en modo operador
            sku = sku_svc.get_by_id(sku_id)
            prod = (getattr(sku, "sku_nombre_original", None) or getattr(sku, "sku_nombre", None)
                    or "ese producto") if sku else "ese producto"
            lineas = "\n".join(f"• {c['nombre']} — ${c['precio'] * c['cantidad']:,.2f}"
                               for c in conservar)
            saludo = f"{nombre}, el" if nombre else "El"
            return (f"{saludo} {prod} requiere receta 🩺. Te paso con alguien del equipo "
                    f"con todo tu pedido, así lo gestiona junto con lo demás:\n{lineas}\n\n"
                    "¡En un momento te contactamos!")
        if _ya_dicho:
            inicio = f"Dale {nombre}, te" if nombre else "Dale, te"
            return f"{inicio} paso con alguien del equipo para gestionarlo con vos. ¡En un momento te contactamos!"
        inicio = f"{nombre}, ese" if nombre else "Ese"
        return (
            f"{inicio} producto requiere receta 🩺. Te paso con alguien del equipo "
            "para gestionarlo con vos. ¡En un momento te contactamos!"
        )
    return None


def receta_ya_mencionada(history: list) -> bool:
    """True si el último mensaje del bot ya avisó que el producto lleva receta."""
    for m in reversed(history or []):
        if m.get("role") == "assistant":
            return "receta" in (m.get("content") or "").lower()
    return False


def descuento_para(phone: str, cfg: dict, socio_svc=None) -> tuple[float, str]:
    """
    (pct, tipo) del descuento que corresponde al teléfono: "empleado",
    "socio" o "" (ninguno). El de empleado NO se acumula con el de socio:
    un empleado que además es socio tiene el de empleado (decisión 25/9).
    Único lugar que decide el descuento: catálogo, texto al modelo, respuesta
    a "¿tengo descuento?", link de pago y cotización de recetas.
    """
    try:
        from app.services.empleado_service import get_empleado_service
        if get_empleado_service().find_by_phone(phone):
            pct = float(cfg.get("empleado_discount_pct") or 0)
            if pct > 0:
                return pct, "empleado"
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"No se pudo evaluar si {phone} es empleado: {e}")
    try:
        pct = float(cfg.get("socio_discount_pct") or 0)
    except (TypeError, ValueError):
        pct = 0.0
    if pct <= 0:
        return 0.0, ""
    try:
        if socio_svc is None:
            from app.config import get_settings as _gs
            from app.services.socio_service import get_socio_service as _gss
            socio_svc = _gss(_gs().socios_path)
        if socio_svc.find_by_phone(phone):
            return pct, "socio"
    except Exception as e:
        logger.warning(f"No se pudo evaluar el descuento de socio para {phone}: {e}")
    return 0.0, ""


def aplicar_descuento_socio(resultados: list[dict], phone: str, cfg: dict,
                            socio_svc=None, incluir_receta: bool = False
                            ) -> tuple[list[dict], float]:
    """
    Devuelve (resultados_con_descuento, pct_aplicado) para un socio del padrón.

    Se llama APENAS se buscan los productos, no al armar el link: así el precio
    con descuento es el único que circula (lo ve el modelo, se matchea contra
    él la regla del precio, se guarda en el pendiente y llega al pago). Aplicar
    el descuento en dos lugares cobraría dos veces el mismo beneficio.

    No toca los productos que requieren receta: ésos derivan a una persona y el
    precio lo resuelve el mostrador. Devuelve copias — no muta el catálogo.

    pct = 0 significa "no se aplicó nada" (no es socio, descuento apagado, o la
    config `socio_discount_en_catalogo` está en false).
    """
    if not resultados:
        return resultados, 0.0
    if str(cfg.get("socio_discount_en_catalogo", "true")).lower() != "true":
        return resultados, 0.0
    pct, _tipo = descuento_para(phone, cfg, socio_svc)
    if pct <= 0:
        return resultados, 0.0

    modo = cfg.get("receta_mode", "conservador")
    salida = []
    for r in resultados:
        item = dict(r)
        # incluir_receta: el operador ya validó la receta (pedido armado desde
        # el backoffice) y el descuento va igual que en la cotización.
        if incluir_receta or not requiere_derivacion(item.get("requiere_receta", "no"), modo):
            lista = item.get("precio") or 0.0
            if lista > 0:
                item["precio_lista"] = lista
                item["precio"] = round(lista * (1 - pct / 100), 2)
        salida.append(item)
    return salida, pct


def costo_envio_de(cfg: dict) -> float:
    """Costo del envío a domicilio (config envio_costo, 0 = gratis)."""
    try:
        return max(0.0, float(cfg.get("envio_costo") or 0))
    except (TypeError, ValueError):
        return 0.0


# Última dirección de envío por teléfono (tabla direcciones_cliente, se carga
# al arrancar y se actualiza en cada envío). Minuta 24/9 punto 8 y C-3854:
# una empleada pidió envío y el bot le volvió a pedir la dirección.
_ULTIMA_DIRECCION: dict[str, str] = {}


def recordar_direccion(phone: str, direccion: Optional[str]) -> None:
    if phone and direccion and parece_direccion(direccion):
        _ULTIMA_DIRECCION[phone] = direccion.strip()


async def cargar_direcciones(db) -> int:
    if db is None or not db.available():
        return 0
    filas = await db.fetch("SELECT phone, direccion FROM direcciones_cliente")
    for f in filas or []:
        _ULTIMA_DIRECCION[f["phone"]] = f["direccion"]
    return len(filas or [])


async def guardar_direccion(db, phone: str, direccion: Optional[str]) -> None:
    """Persiste la última dirección de envío (best-effort)."""
    if not (phone and direccion and parece_direccion(direccion)):
        return
    recordar_direccion(phone, direccion)
    if db is not None and db.available():
        await db.execute(
            "INSERT INTO direcciones_cliente (phone, direccion, updated_at) VALUES ($1, $2, now()) "
            "ON CONFLICT (phone) DO UPDATE SET direccion = EXCLUDED.direccion, updated_at = now()",
            phone, direccion.strip())


def domicilio_de(phone: Optional[str], socio_svc=None) -> str:
    """
    Domicilio para ofrecer el envío: el del padrón de socios o, si no hay, la
    última dirección a la que se le mandó un pedido a ese teléfono. "" si no
    hay ninguno.
    """
    if not phone:
        return ""
    try:
        if socio_svc is None:
            from app.config import get_settings as _gs
            from app.services.socio_service import get_socio_service as _gss
            socio_svc = _gss(_gs().socios_path)
        socio = socio_svc.find_by_phone(phone)
        dom = ((socio or {}).get("domicilio") or "").strip()
        if dom:
            return dom
    except Exception:
        pass
    return _ULTIMA_DIRECCION.get(phone, "")


def pregunta_entrega(cfg: dict, extra: str = "", saludo: bool = True,
                     phone: Optional[str] = None, socio_svc=None) -> str:
    """
    La pregunta retiro/envío, con el costo del envío A LA VISTA si existe:
    el cliente lo ve antes de elegir, nunca como sorpresa en el link. Si
    quien escribe es socio con domicilio, la dirección va EN la pregunta
    (pedido 25/9: antes salía solo en uno de los cuatro caminos).
    """
    costo = costo_envio_de(cfg)
    dom = domicilio_de(phone, socio_svc)
    destino = f"envío a {dom}" if dom else "envío a domicilio"
    envio_txt = (f"*{destino}* (+${costo:,.0f})" if costo > 0 else f"*{destino}*")
    if dom:
        extra = f"{extra} Si es a otra dirección, decímela.".rstrip() if extra else " Si es a otra dirección, decímela."
    if saludo:
        return f"¡Genial! ¿Cómo preferís recibirlo: *retiro en sucursal* o {envio_txt}?{extra}"
    return f"¿Preferís *retiro en sucursal* o {envio_txt}? 🙂{extra}"


def texto_entrega(tipo: str, direccion: Optional[str], costo_envio: float = 0) -> str:
    """Línea que describe la entrega elegida, para el mensaje del link de pago."""
    if tipo == "envio":
        dir_txt = f" a *{direccion}*" if direccion else ""
        costo_txt = (f" Incluye el envío (${costo_envio:,.2f})."
                     if costo_envio > 0 else "")
        return f"🚚 Te lo enviamos a domicilio{dir_txt}.{costo_txt}"
    return "🏪 Lo retirás en la sucursal (te enviamos el código al confirmar el pago)."


async def _chequear_stock_vivo(session: dict, phone: str, session_svc,
                               cfg: dict) -> tuple[Optional[str], Optional[float]]:
    """
    Consulta el stock REAL al agente de la sucursal antes de generar el link.

    Devuelve (mensaje_freno, precio_erp):
      - mensaje_freno: texto para el cliente si NO hay stock (la venta se
        frena, clear_pending; con sin_stock_mode=derivar pasa a operador).
        None = seguir al link.
      - precio_erp: precio del ERP si difiere del cotizado (solo informativo:
        SIEMPRE se cobra el precio que el bot dijo — D6).

    Best-effort por diseño: sin agente conectado, timeout o cualquier error →
    (None, None) y se cobra con el dato cacheado de 15 minutos.
    """
    from app.config import get_settings as _gs
    settings = _gs()
    if settings.live_stock_check != "stock":
        return None, None
    from app.services.catalog_source import resolver_branch_default
    branch = await resolver_branch_default()
    if not branch:
        return None, None

    items = session.get("pending_items") or []
    if not items and session.get("pending_sku_id"):
        items = [{"sku_id": session["pending_sku_id"],
                  "nombre": session.get("pending_sku_nombre") or "",
                  "precio": session.get("pending_precio") or 0,
                  "cantidad": session.get("pending_cantidad", 1)}]
    if not items:
        return None, None

    from app.services.catalog_live import lookup_vivo
    res = await lookup_vivo(branch, [str(i["sku_id"]) for i in items],
                            timeout=settings.live_lookup_timeout_s)
    if res is None:
        logger.warning(f"Stock en vivo: sin respuesta del agente para {phone} — "
                       "se cobra con el dato cacheado")
        return None, None

    missing = {str(m) for m in res.missing}
    precio_erp_distinto = None

    # Refrescar el cache (memoria + Postgres) con lo que dijo el ERP.
    from app.services.catalog_live import aplicar_items_vivos
    vivos = await aplicar_items_vivos(res.items, branch)

    for pedido in items:
        sid = str(pedido["sku_id"])
        vivo = vivos.get(sid)
        stock_vivo = None
        if vivo is not None:
            stock_vivo = int(vivo.get("stock") or 0)
            precio_raw = vivo.get("price")
            if precio_raw not in (None, ""):
                precio_vivo = float(precio_raw)
                cotizado = float(pedido.get("precio") or 0)
                if cotizado and abs(precio_vivo - cotizado) >= 0.01:
                    precio_erp_distinto = precio_vivo
                    logger.warning(
                        f"Precio ERP distinto para {sid}: cotizado "
                        f"${cotizado:,.2f}, ERP ${precio_vivo:,.2f} — "
                        "se cobra el cotizado")
        elif sid in missing:
            # El agente mete en `missing` tanto lo que el ERP no conoce como
            # los lookups que FALLARON (timeout mientras corre el pase de
            # verdad). Caso real 11/9: "no nos queda stock" de un producto
            # con 2 unidades. Desconocido → se sigue con el cache (fail-open).
            logger.warning(f"Stock en vivo: {sid} sin respuesta del ERP (missing) — "
                           "se cobra con el dato cacheado")

        if stock_vivo is not None and stock_vivo < int(pedido.get("cantidad", 1)):
            nombre = pedido.get("nombre") or "ese producto"
            plantilla = cfg.get("live_sin_stock_message") or (
                "Justo me fijé y no nos queda stock de {producto}. "
                "¿Querés que lo consultemos con el equipo?")
            await session_svc.clear_pending(phone)
            if (cfg.get("sin_stock_mode") or "preguntar") == "derivar":
                await session_svc.set_estado(phone, "operador", motivo="sin_stock_vivo")
            else:
                # "¿Querés que lo consultemos?" → el próximo "sí" deriva (mismo
                # flag que el flujo de sin-stock del webhook). Sin esto el "sí"
                # caía al modelo, que re-ofrecía el producto en bucle.
                _s_off = await session_svc.get(phone)
                _s_off["derivacion_ofrecida"] = nombre[:60]
                await session_svc.save(phone, _s_off)
            logger.info(f"Venta frenada por stock en vivo: {sid} stock={stock_vivo} "
                        f"pedido={pedido.get('cantidad', 1)} phone={phone}")
            return plantilla.replace("{producto}", nombre), None

    return None, precio_erp_distinto


async def _cerrar_venta_cc(session_svc, phone: str, session: dict,
                           tipo_entrega: str, direccion: Optional[str],
                           total: float, costo_envio: float = 0.0,
                           link_previo: bool = False,
                           extra_pedido: Optional[dict] = None) -> str:
    """
    Cierra una venta con CUENTA CORRIENTE: crea el pedido (pago="cuenta_corriente",
    entra al backoffice como cualquier pedido pagado, con código de retiro) y
    devuelve el mensaje de confirmación. El bot no maneja saldos: solo registra;
    el asiento contable lo marca la farmacia (cc-cargado).
    """
    from app.config import get_settings as _gs
    from app.services.order_service import get_order_service
    settings = _gs()

    items = session.get("pending_items") or []
    if len(items) > 1:
        sku_id = "MULTI"
        nombre = " + ".join(
            i["nombre"] + (f" x{i.get('cantidad', 1)}" if i.get("cantidad", 1) > 1 else "")
            for i in items)
        cantidad = 1
    else:
        sku_id = session.get("pending_sku_id") or ""
        cantidad = int(session.get("pending_cantidad") or 1)
        nombre = (session.get("pending_sku_nombre") or "tu pedido") + \
                 (f" x{cantidad}" if cantidad > 1 else "")

    order = await get_order_service(settings.redis_url).create(
        phone=phone, sku_id=sku_id,
        sku_nombre=nombre, cantidad=cantidad, total=total,
        mp_payment_id="", tipo_entrega=tipo_entrega,
        direccion_envio=direccion, pago="cuenta_corriente", extra=extra_pedido,
    )
    logger.info(f"Pedido con cuenta corriente: {order['order_id']} phone={phone} "
                f"total=${total:,.2f}")

    # Embudo/tablero: mismo evento que un pago, con el medio identificado.
    try:
        from app.services.db import get_db as _gdb
        from app.services.metrics_store import get_metrics_store as _gmet
        await _gmet(_gdb(settings.database_url)).evento(
            "pago_aprobado", phone=phone, dato="cuenta_corriente",
            monto=total, ref=order["order_id"],
            extra={"pasarela": "cuenta_corriente", "socio": True},
        )
    except Exception as e:
        logger.debug(f"evento pago_aprobado (CC): {e}")

    await session_svc.set_entrega(phone, tipo_entrega, direccion)
    await session_svc.set_estado(phone, "pedido_confirmado")
    # El medio elegido es POR PEDIDO: si mañana compra otra cosa, se le vuelve
    # a mandar link salvo que pida cuenta corriente de nuevo.
    _s_fin = await session_svc.get(phone)
    _s_fin["_ultimo_pedido"] = order["order_id"]
    if _s_fin.pop("pago_metodo", None):
        await session_svc.save(phone, _s_fin)

    code = order.get("pickup_code", "")
    envio_line = f" (incluye ${costo_envio:,.0f} de envío)" if costo_envio > 0 else ""
    # Si ya le habíamos mandado un link de pago, que no lo use: pagaría dos veces.
    link_line = ("\n\nNo hace falta que uses el link de pago que te mandé antes."
                 if link_previo else "")
    if tipo_entrega == "envio":
        dir_txt = f" a *{direccion}*" if direccion else " a tu domicilio"
        return (
            f"✅ *¡Listo! Quedó cargado a tu cuenta corriente* 🙌\n\n"
            f"*{nombre}* — ${total:,.2f}{envio_line}\n"
            f"🚚 Te lo enviamos{dir_txt}. Nos comunicamos para coordinar la entrega.\n"
            f"📋 Código de pedido: *{code}*\n\n"
            f"¡Muchas gracias! 💊{link_line}"
        )
    return (
        f"✅ *¡Listo! Quedó cargado a tu cuenta corriente* 🙌\n\n"
        f"*{nombre}* — ${total:,.2f}\n"
        f"🔑 *Tu código de retiro es: {code}*\n\n"
        f"Presentalo al retirar. ¡Muchas gracias! 💊{link_line}"
    )


def precio_sin_descuento(items: list[dict], pct: float, sku_svc=None) -> Optional[float]:
    """
    Total de los ítems a precio de lista: el de catálogo para los que se
    cobran con el descuento aplicado, el cobrado para los que no lo tienen
    (receta, precio fijado por el operador, ítems libres). None si no se
    puede leer el catálogo.
    """
    if pct <= 0 or not items:
        return None
    try:
        if sku_svc is None:
            from app.config import get_settings as _gs
            from app.services.sku_service import get_sku_service
            sku_svc = get_sku_service(_gs().sku_csv_path)
    except Exception as e:
        logger.warning(f"Sin catálogo para el precio de lista: {e}")
        return None
    total = 0.0
    for it in items:
        precio = float(it.get("precio") or 0)
        cant = int(it.get("cantidad", 1) or 1)
        lista = None
        try:
            sku = sku_svc.get_by_id(str(it.get("sku_id"))) if it.get("sku_id") else None
            lista = float(sku.precio_venta or 0) if sku else None
        except Exception:
            lista = None
        con_desc = (lista and abs(precio - round(lista * (1 - pct / 100), 2))
                    <= max(0.02, lista * 0.002))
        total += (lista if con_desc else precio) * cant
    return round(total, 2)


async def crear_link_y_responder(
    payment_svc,
    session_svc,
    phone: str,
    session: dict,
    tipo_entrega: str = "retiro",
    direccion: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """
    Genera el link de pago para el producto pendiente, guarda el modo de
    entrega en la sesión y deja la sesión en estado 'esperando_pago'.

    Devuelve (respuesta_para_el_cliente, link_o_None).
    """
    # Carrito: si hay más de un producto, el link sale por el total de todos.
    items = session.get("pending_items") or []
    if len(items) > 1:
        cantidad = 1
        precio_unitario = sum(i["precio"] * i.get("cantidad", 1) for i in items)
        nombre_link = f"{len(items)} productos"
        sku_link = "MULTI"
    else:
        cantidad = session.get("pending_cantidad", 1)
        precio_unitario = session["pending_precio"]
        nombre_link = session["pending_sku_nombre"]
        sku_link = session["pending_sku_id"]
    total = precio_unitario * cantidad

    # Config (best-effort): descuento de socio para la línea informativa y
    # costo de envío para sumarlo al total.
    _cfg: dict = {}
    try:
        from app.config import get_settings as _gs
        from app.services.config_service import get_config_service as _gcs
        _settings = _gs()
        _cfg = await _gcs(_settings.redis_url).get_all()
    except Exception as e:
        logger.warning(f"No se pudo leer la config para el link de {phone}: {e}")

    # Costo de envío: se suma al total y el link sale como ítem único con el
    # envío incluido. El cliente ya lo vio al elegir la entrega
    # (pregunta_entrega) y el mensaje lo desglosa igual.
    _costo_envio = costo_envio_de(_cfg) if tipo_entrega == "envio" else 0.0
    total_productos = total          # sin envío: base del desglose de socio
    if _costo_envio > 0:
        total = round(total + _costo_envio, 2)
        nombre_link = f"{nombre_link} + envío"
        precio_unitario, cantidad = total, 1

    # Descuento de socio: acá NO se recalcula nada. El precio pendiente ya
    # viene con el descuento aplicado desde la búsqueda (aplicar_descuento_socio),
    # que es lo que el bot le dijo al cliente. Volver a aplicarlo cobraría dos
    # veces el beneficio, por debajo del precio ofrecido. Sólo se agrega la
    # línea que explica el beneficio en el mensaje del link.
    descuento_line = ""
    try:
        pct, _tipo = descuento_para(phone, _cfg)
        # La línea sale SOLO si el precio cobrado tiene el descuento: los
        # productos con receta no se bonifican en el catálogo, y antes el link
        # decía "te aplicamos un 20% (precio de lista $44.523)" cobrando el
        # precio de lista de $35.619 (caso real Femiden 2/10).
        antes = precio_sin_descuento(
            items or [{"sku_id": session.get("pending_sku_id"),
                       "precio": session.get("pending_precio") or 0,
                       "cantidad": session.get("pending_cantidad", 1)}],
            pct) if pct > 0 else None
        if antes is not None and antes - total_productos > 0.01:
            if _tipo == "empleado":
                plantilla = (_cfg.get("empleado_discount_message")
                             or "🎉 Como empleado te aplicamos un {pct}% de descuento "
                                "(precio de lista: ${antes}).")
            else:
                plantilla = _cfg.get("socio_discount_message") or ""
            descuento_line = plantilla.replace("{pct}", f"{pct:g}").replace("{antes}", f"{antes:,.2f}")
    except Exception as e:
        logger.warning(f"No se pudo evaluar descuento de socio para {phone}: {e}")

    # Chequeo de stock EN VIVO contra el ERP de la sucursal (si el agente está
    # conectado): mejor frenar acá que cobrar algo que ya no está. Todo el
    # bloque es best-effort — sin agente, timeout o error se cobra con el dato
    # cacheado (frenar la venta porque el agente se cayó es peor que vender
    # con stock de 15 minutos). El PRECIO nunca se recotiza acá: se cobra el
    # que el bot dijo; si el ERP devuelve otro, se loguea.
    _precio_erp = None
    try:
        import time as _t
        _recien = ((_t.time() - float(session.get("_stock_ok_at") or 0)) < 600
                   and session.get("_stock_ok_para") == _clave_stock(session))
        _msg_freno, _precio_erp = (None, None) if _recien else \
            await _chequear_stock_vivo(session, phone, session_svc, _cfg)
        if _msg_freno:
            return _msg_freno, None
    except Exception as e:
        logger.warning(f"Chequeo de stock en vivo falló para {phone}: {e} — se sigue")

    # Cuenta corriente (minuta 79): mismo checkout que el link, pero sin pago
    # online — el pedido entra directo al backoffice y la farmacia registra el
    # saldo en su sistema contable (estado cc_cargado, aparte).
    if session.get("pago_metodo") == "efectivo":
        # Efectivo con envío solo si la farmacia lo habilitó: que un cadete
        # cobre en la puerta es otra operatoria.
        if tipo_entrega == "envio" and not _flag(_cfg, "efectivo_con_envio", False):
            _s_ef = await session_svc.get(phone)
            if _s_ef.get("_efectivo_envio_avisado"):
                # Ya se le avisó y eligió envío igual: sigue con link de pago.
                _s_ef.pop("pago_metodo", None)
                _s_ef.pop("_efectivo_envio_avisado", None)
                await session_svc.save(phone, _s_ef)
            else:
                _s_ef["_efectivo_envio_avisado"] = True
                await session_svc.save(phone, _s_ef)
                await session_svc.set_estado(phone, "esperando_entrega")
                return (_cfg.get("efectivo_solo_retiro_message") or (
                    "El pago en efectivo es solo retirando en la sucursal. ¿Lo pasás a "
                    "retirar, o preferís *envío* pagando con tarjeta?")), None
        else:
            respuesta_ef = await _cerrar_venta_efectivo(
                session_svc, phone, session, tipo_entrega, direccion,
                total=total, costo_envio=_costo_envio, cfg=_cfg,
                link_previo=session.get("estado") == "esperando_pago")
            return await nota_envio_fuera_horario(respuesta_ef, tipo_entrega), None

    if session.get("pago_metodo") == "cuenta_corriente":
        respuesta_cc = await _cerrar_venta_cc(
            session_svc, phone, session, tipo_entrega, direccion,
            total=total, costo_envio=_costo_envio,
            link_previo=session.get("estado") == "esperando_pago")
        return await nota_envio_fuera_horario(respuesta_cc, tipo_entrega), None

    link, err = await payment_svc.crear_link(
        sku_id=sku_link,
        nombre=nombre_link,
        precio=precio_unitario,
        phone=phone,
        cantidad=cantidad,
    )

    if not link:
        logger.error(f"MP error para {phone}: {err}")
        await session_svc.clear_pending(phone)
        return "Tuve un problema generando el link de pago. Te paso con alguien del equipo.", None

    # Guardar entrega y pasar a esperando_pago
    await session_svc.set_entrega(phone, tipo_entrega, direccion)
    await session_svc.set_estado(phone, "esperando_pago")

    # Métrica de embudo: punto único por el que pasan bot, simulador y backoffice.
    try:
        from app.config import get_settings as _gs2
        from app.services.db import get_db as _gdb
        from app.services.metrics_store import get_metrics_store as _gms
        _extra_evt = {"producto": nombre_link, "cantidad": cantidad}
        if _precio_erp is not None:
            _extra_evt["precio_erp"] = _precio_erp   # difiere del cotizado (D6)
        await _gms(_gdb(_gs2().database_url)).evento(
            "link_enviado", phone=phone, monto=total,
            ref=link.rsplit("/", 1)[-1][:64],
            extra=_extra_evt,
        )
    except Exception as e:
        logger.debug(f"evento link_enviado: {e}")

    if len(items) > 1:
        nombre_con_cant = " + ".join(
            i["nombre"] + (f" x{i.get('cantidad', 1)}" if i.get("cantidad", 1) > 1 else "")
            for i in items
        )
    else:
        nombre_con_cant = session["pending_sku_nombre"] + (f" x{cantidad}" if cantidad > 1 else "")
    entrega_line = texto_entrega(tipo_entrega, direccion, _costo_envio)
    descuento_bloque = f"{descuento_line}\n" if descuento_line else ""
    respuesta = (
        f"Perfecto! Acá te mando el link de pago para "
        f"{nombre_con_cant} (${total:,.2f}):\n\n{link}\n\n"
        f"{descuento_bloque}{entrega_line}\n"
        "El link tiene vigencia de 24hs. ¡Cualquier cosa me avisás!"
    )
    return await nota_envio_fuera_horario(respuesta, tipo_entrega), link


def _clave_stock(session: dict) -> str:
    """Qué se verificó: el producto pendiente o los ítems del carrito."""
    items = session.get("pending_items") or []
    if items:
        return ",".join(sorted(f"{i.get('sku_id')}x{i.get('cantidad', 1)}" for i in items))
    return f"{session.get('pending_sku_id')}x{session.get('pending_cantidad', 1)}"


async def confirmar_pedido(
    sku_svc, payment_svc, session_svc, socio_svc, cfg: dict, phone: str, session: dict,
    entrega: Optional[str] = None, nombre: str = "",
) -> tuple[str, str]:
    """
    Maneja la confirmación positiva de un pedido pendiente.
    Decide entre: derivar por receta / resolver entrega / preguntar entrega / link.

    `entrega` opcional: si el cliente ya indicó "retiro" o "envio" al confirmar
    (ej. "sí, con envío"), se resuelve directo sin volver a preguntar.
    Devuelve (respuesta, intencion).
    """
    modo = cfg.get("receta_mode", "conservador")
    envio_enabled = str(cfg.get("envio_enabled", "true")).lower() == "true"
    sku_id = session.get("pending_sku_id")

    # 1. Requiere receta → derivar a una persona (no se genera link).
    #    Excepción: receta_validada — el OPERADOR ya vio la receta y cotizó
    #    este producto; el "sí" del cliente sigue derecho a entrega y link.
    if necesita_receta(sku_svc, sku_id, modo) and not session.get("receta_validada"):
        from app.services.receta_marcas import recordar_producto_por_receta
        await recordar_producto_por_receta(session_svc, sku_svc, phone, sku_id)
        await session_svc.clear_pending(phone)
        await session_svc.set_estado(phone, "operador", motivo="receta")
        inicio = f"{nombre}, ese" if nombre else "Ese"
        return (
            f"{inicio} medicamento requiere receta 🩺. Te paso con alguien del equipo "
            "para gestionarlo con vos. ¡En un momento te contactamos!",
            "derivado_receta",
        )

    # 1b. Stock EN VIVO antes de preguntar retiro/envío (caso real 24/9: se le
    #     pidió la dirección y RECIÉN después se le dijo que no había stock).
    #     El chequeo del final (al generar el link) queda como red de seguridad
    #     y se saltea si este ya dio OK hace menos de 10 minutos.
    try:
        _freno, _ = await _chequear_stock_vivo(session, phone, session_svc, cfg)
    except Exception as e:
        logger.warning(f"Chequeo de stock al confirmar falló para {phone}: {e} — se sigue")
        _freno = None
    if _freno:
        return _freno, "sin_stock_vivo"
    try:
        import time as _t
        _s_ok = await session_svc.get(phone)
        _s_ok["_stock_ok_at"] = _t.time()
        _s_ok["_stock_ok_para"] = _clave_stock(_s_ok)
        await session_svc.save(phone, _s_ok)
    except Exception:
        pass

    # 2. Envío habilitado
    if envio_enabled:
        # 2a. Ya indicó la preferencia al confirmar → resolver directo (sin re-preguntar)
        if entrega in ("retiro", "envio"):
            return await resolver_entrega(
                payment_svc, session_svc, socio_svc, phone, session,
                es_retiro=(entrega == "retiro"), es_envio=(entrega == "envio"),
            )
        # 2b. No indicó → preguntar retiro o envío
        await session_svc.set_estado(phone, "esperando_entrega")
        return (pregunta_entrega(cfg, phone=phone, socio_svc=socio_svc), "esperando_entrega")

    # 3. Sin envío → link directo con retiro
    respuesta, _ = await crear_link_y_responder(payment_svc, session_svc, phone, session, "retiro", None)
    return respuesta, "pedido_confirmado"


async def resolver_entrega(
    payment_svc, session_svc, socio_svc, phone: str, session: dict,
    es_retiro: bool, es_envio: bool,
) -> tuple[str, str]:
    """
    Maneja la elección de modo de entrega (estado esperando_entrega).
    Devuelve (respuesta, intencion).
    """
    if es_retiro and not es_envio:
        respuesta, _ = await crear_link_y_responder(payment_svc, session_svc, phone, session, "retiro", None)
        return respuesta, "pedido_confirmado"

    if es_envio and not es_retiro:
        # La dirección ya se mostró en la pregunta ("envío a San Javier 837 —
        # si es a otra, decímela"): elegir envío la confirma.
        dom = domicilio_de(phone, socio_svc)
        if dom:
            respuesta, _ = await crear_link_y_responder(
                payment_svc, session_svc, phone, session, "envio", dom
            )
            return respuesta, "pedido_confirmado"
        await session_svc.set_estado(phone, "esperando_direccion")
        return (
            "Dale! Pasame la dirección completa (calle, número y localidad) y te lo enviamos 🚚",
            "esperando_direccion",
        )

    # Ambiguo o mencionó ambas → volver a preguntar (con el costo a la vista)
    _cfg_e: dict = {}
    try:
        from app.config import get_settings as _gs
        from app.services.config_service import get_config_service as _gcs
        _cfg_e = await _gcs(_gs().redis_url).get_all()
    except Exception:
        pass
    return (pregunta_entrega(_cfg_e, saludo=False, phone=phone, socio_svc=socio_svc),
            "esperando_entrega")


async def capturar_direccion(
    payment_svc, session_svc, phone: str, session: dict, texto: str,
) -> tuple[str, str]:
    """
    Captura la dirección de envío (estado esperando_direccion) y genera el link.
    Devuelve (respuesta, intencion).
    """
    # Solo la parte que es dirección ("por favor me lo envías san javier 837"
    # → "san javier 837"); si no se reconoce, el texto tal cual (el llamador ya
    # validó que hay una dirección).
    direccion = extraer_direccion_de(texto) or texto.strip()
    respuesta, _ = await crear_link_y_responder(
        payment_svc, session_svc, phone, session, "envio", direccion
    )
    return respuesta, "pedido_confirmado"


# ══════════════════════════════════════════════════════════════════════════════
# Feedback piloto 16/9 (filas 47, 48, 54, 59, 61): cancelación, obras sociales,
# bonos, farmacéutico por síntoma, confirmación con producto distinto.
# ══════════════════════════════════════════════════════════════════════════════

# ── 54: "anular el pedido" en cualquier estado ────────────────────────────────
# Antes "cancelar" sólo se entendía dentro del flujo de entrega; fuera de él
# el bot lo tomaba como consulta nueva y volvía a buscar el producto.
_CANCELAR_VERBO = r"\b(anul\w*|cancel\w*|dar de baja|d[ée] de baja|dejalo|dej[aá] sin efecto)\b"
_CANCELAR_OBJ = r"\b(pedido|compra|orden|encargo|todo|eso|esto|lo)\b"
_NO_CANCELA = [r"\bno\s+(lo\s+)?(anul|cancel)", r"\bcancelaci[oó]n de (la )?tarjeta\b"]


def pide_cancelar_pedido(t: str) -> bool:
    """True si el cliente pide anular/cancelar el pedido (en cualquier estado)."""
    s = (t or "").strip().lower()
    if not s or any(re.search(p, s) for p in _NO_CANCELA):
        return False
    if not re.search(_CANCELAR_VERBO, s):
        return False
    # Verbo + objeto en la misma frase, o un mensaje corto que es sólo el pedido.
    return bool(re.search(_CANCELAR_OBJ, s)) or len(s.split()) <= 3


def parsear_lista(valor: str) -> list[str]:
    """Lista configurable en el backoffice: separada por coma, ; o salto de línea."""
    return [x.strip() for x in re.split(r"[,;\n]+", valor or "") if x.strip()]


def _norm(s: str) -> str:
    s = (s or "").lower()
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ü", "u")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9ñ ]+", " ", s).strip()


def _en_lista(nombre: str, lista: list[str]) -> Optional[str]:
    """Devuelve el ítem de la lista que corresponde a `nombre` (tolerante), o None."""
    n = _norm(nombre)
    if not n:
        return None
    for item in lista:
        i = _norm(item)
        if not i:
            continue
        if n == i or n in i or i in n:
            return item
        # "swiss" vs "swiss medical": primera palabra de 4+ letras coincide
        p, q = n.split()[0], i.split()[0]
        if len(p) >= 4 and p == q:
            return item
    return None


# ── 59: obras sociales — el modelo contestaba de memoria (dijo sí a OSDE) ──────
_OS_CONOCIDAS = [
    "osde", "pami", "ioma", "swiss medical", "galeno", "medife", "medifé", "omint",
    "sancor", "sancor salud", "federada", "jerarquicos", "jerárquicos", "osecac", "ospe",
    "osprera", "ospecon", "osuthgra", "ospim", "osdepym", "accord", "prevencion salud",
    "prevención salud", "amur", "apross", "iapos", "osep", "ipross", "iosfa", "obsba",
    "osseg", "ospat", "osplad", "union personal", "unión personal", "medicus",
    "hospital italiano", "hominis", "luis pasteur", "avalian", "aca salud", "osfatun",
    "ospia", "osmata", "osde binario", "scis", "prensa", "andar", "boreal",
]
_OS_GENERICO = r"\b(obras?\s*sociales?|prepagas?|mutual(es)?|convenios?|cobertura)\b"
_OS_VERBO = r"\b(trabaj\w*|acept\w*|atiend\w*|atend\w*|tom\w*|recib\w*|tienen|tenes|tenés|hay|est[aá]n|convenio|cobertura|descuento)\b"


def pregunta_obra_social(t: str, lista_cfg: list[str] | None = None) -> Optional[str]:
    """
    Detecta una pregunta del tipo "¿trabajan con OSDE?" / "¿aceptan mi obra
    social?". Devuelve el nombre de la obra social mencionada, "" si la
    pregunta es genérica ("¿con qué obras sociales trabajan?"), o None si el
    mensaje no es sobre eso.
    """
    s = (t or "").strip()
    if not s:
        return None
    n = _norm(s)
    conocidas = list(_OS_CONOCIDAS) + [x for x in (lista_cfg or []) if x]
    mencionada = None
    for os_ in sorted(conocidas, key=len, reverse=True):
        if re.search(rf"\b{re.escape(_norm(os_))}\b", n):
            mencionada = os_
            break
    generico = bool(re.search(_OS_GENERICO, n))
    if not mencionada and not generico:
        return None
    if not re.search(_OS_VERBO, n) and "?" not in s:
        return None
    # Datos propios del socio ("mi obra social es X, ¿me cubre?") también
    # cuentan como consulta de convenio.
    if mencionada:
        return mencionada
    # Genérico: intentar extraer un nombre propio después de "con"
    m = re.search(r"\bcon\s+(la\s+|el\s+)?([A-ZÁÉÍÓÚ][\wÁÉÍÓÚáéíóú\.]{1,25}(?:\s+[A-ZÁÉÍÓÚ][\wáéíóú]{1,25})?)", s)
    if m and _norm(m.group(2)) not in ("obra", "obras", "prepaga", "mutual"):
        return m.group(2)
    return ""


def responder_obra_social(mencionada: str, cfg: dict) -> tuple[str, bool]:
    """
    Respuesta determinista sobre convenios: nunca la redacta el modelo.
    Devuelve (texto, ofrece_derivacion). Con la lista vacía en config, no se
    afirma nada: se deriva la consulta al equipo.
    """
    lista = parsear_lista(cfg.get("obras_sociales", ""))
    if not lista:
        return (cfg.get("obras_sociales_sin_lista_message")
                or "Eso lo confirma el equipo. ¿Querés que te pase con alguien para que lo vea con vos?"), True
    if not mencionada:
        return (cfg.get("obras_sociales_lista_message")
                or "Trabajamos con: {lista}. ¿Con cuál sería?").replace("{lista}", ", ".join(lista)), False
    hit = _en_lista(mencionada, lista)
    if hit:
        return (cfg.get("obras_sociales_si_message")
                or "Sí, trabajamos con {obra_social} 🙂 ¿Qué necesitás?").replace("{obra_social}", hit), False
    # No listada ≠ sin convenio (la lista puede estar incompleta: "no tenemos
    # convenio con PAMI" a una clienta de PAMI, 28/9). Un texto viejo que
    # niega el convenio se reemplaza por el que deriva.
    no_msg = cfg.get("obras_sociales_no_message") or ""
    if not no_msg or re.search(r"\bno\s+(tenemos|trabajamos|hay)\b", no_msg, re.IGNORECASE):
        no_msg = ("{obra_social} no la tengo en mi lista, lo confirma el equipo. ¿Querés que "
                  "te pase con alguien para que lo vea con vos?")
    return no_msg.replace("{obra_social}", mencionada.strip()), True


# ── 61: bonos de laboratorio — la foto se cotizaba renglón por renglón ─────────
_BONO_RE = r"\bbono(s)?\b"


def pregunta_bono(t: str) -> Optional[str]:
    """
    "¿trabajan el bono de Cassará?" → "Cassará"; "¿trabajan bonos?" → "";
    None si el mensaje no habla de bonos.
    """
    s = (t or "").strip()
    if not re.search(_BONO_RE, s, re.IGNORECASE):
        return None
    m = re.search(r"\bbonos?\s+(de\s+|del\s+)?(?:laboratorio\s+)?([A-Za-zÁÉÍÓÚáéíóúñ][\wÁÉÍÓÚáéíóúñ\-]{2,30})",
                  s, re.IGNORECASE)
    if m:
        lab = m.group(2)
        if _norm(lab) not in ("que", "para", "con", "los", "las", "una", "uno", "este", "esta",
                              "mi", "tu", "descuento", "laboratorio", "medicamento", "medicamentos"):
            return lab
    return ""


def laboratorio_trabajado(lab: str, cfg: dict) -> Optional[str]:
    """El laboratorio de la lista configurada que corresponde a `lab`, o None."""
    return _en_lista(lab or "", parsear_lista(cfg.get("bonos_laboratorios", "")))


def responder_bono(lab: str, cfg: dict, nombre: str = "", por_foto: bool = False) -> tuple[str, bool]:
    """
    Respuesta sobre bonos: (texto, trabajado). Se deriva SIEMPRE (lo gestiona
    una persona); lo que cambia es si afirmamos que lo trabajamos.
    """
    hit = laboratorio_trabajado(lab, cfg)
    if hit:
        if por_foto:
            txt = (cfg.get("bono_recibido_message")
                   or get_perfil().textos["bono_recibido_message"])
        else:
            txt = (cfg.get("bono_consulta_si_message")
                   or get_perfil().textos["bono_consulta_si_message"])
        return personalizar_nombre(txt.replace("{laboratorio}", hit), nombre), True
    if por_foto:
        txt = (cfg.get("bono_no_reconocido_message")
               or get_perfil().textos["bono_no_reconocido_message"])
    else:
        txt = cfg.get("bono_consulta_no_message") or (
            "Eso lo confirma el equipo: te paso con alguien para que lo vea con vos 🙂")
    return personalizar_nombre(txt, nombre), False


# ── 48b: pedido por síntoma → dejar a mano el farmacéutico ─────────────────────
def agregar_oferta_farmaceutico(respuesta: str, cfg: dict) -> str:
    # Vacío en la config → el del perfil (petshop lo trae vacío: no agrega nada).
    extra = (cfg.get("sintoma_farmaceutico_message")
             or get_perfil().textos["sintoma_farmaceutico_message"])
    if not extra.strip() or "farmac" in (respuesta or "").lower():
        return respuesta
    return f"{(respuesta or '').rstrip()}\n\n{extra}"


def acepta_farmaceutico(t: str) -> bool:
    return bool(re.search(r"\bfarmac[eé]utic[oa]s?\b", t or "", re.IGNORECASE))


# ── 47: confirmar con un producto distinto en el mensaje no confirma ──────────
def entidad_contradice_pendiente(entidad: Optional[str], pending_nombre: Optional[str]) -> bool:
    """
    "quiero el curflex x 30" con Curflex Plus pendiente: el cliente nombra un
    producto y no coincide con el pendiente → NO es una confirmación, es otro
    pedido (feedback 47: saltaba a "¿cómo lo querés recibir?").
    """
    if not entidad or not pending_nombre:
        return False
    from app.services.sku_service import nombre_coincide, numeros_de
    if not nombre_coincide(entidad, pending_nombre):
        return True
    # Mismo nombre pero distinta presentación numérica ("x 30" vs "x 60").
    n_ent, n_pend = set(numeros_de(entidad)), set(numeros_de(pending_nombre))
    return bool(n_ent and n_pend and not (n_ent & n_pend))


# ── 5: lo que no se entiende, se deriva ────────────────────────────────────────
def debe_derivar_desconocido(intencion: str, entidad: Optional[str], tuvo_kb: bool, cfg: dict) -> bool:
    """
    Con intención "desconocido", sin producto y sin nada en la base de
    conocimiento, el bot no tiene con qué responder: mejor una persona que un
    "¿en qué te puedo ayudar?" en el aire (pedido de la farmacia, 16/9).
    """
    modo = (cfg.get("desconocido_mode") or "derivar").strip().lower()
    return modo == "derivar" and intencion == "desconocido" and not (entidad or "").strip() and not tuvo_kb


# ══════════════════════════════════════════════════════════════════════════════
# Pago en EFECTIVO (19/9): mismo esquema que cuenta corriente — el pedido entra
# al backoffice sin link de pago — pero el cobro queda PENDIENTE hasta que la
# farmacia lo marca como cobrado. Apagado por default (efectivo_enabled).
# ══════════════════════════════════════════════════════════════════════════════
_EFECTIVO = [
    r"\befectivo\b", r"\bcash\b", r"\ben mano\b", r"\bcontado\b",
    r"\bpag\w+\b.{0,30}\b(sucursal|local|farmacia|caja|mostrador|retir\w+|recib\w+|ah[ií]|all[aá])\b",
    r"\b(retir\w+|sucursal|recib\w+)\b.{0,30}\bpag\w+",
    r"\bcontra\s*(entrega|reembolso)\b",
]
_NO_EFECTIVO = [r"\bno\s+(tengo|uso|manejo|quiero)\s+efectivo\b", r"\bsin\s+efectivo\b",
                # "¿o será más efectivo la laca?": eficaz, no medio de pago (1/10)
                r"\b(mas|menos|tan|muy|bastante|igual\s+de|sera|seria|es|sea|son|resulta\w*)"
                r"\s+efectiv[oa]s?\b"]


def _flag(cfg: dict, clave: str, default: bool) -> bool:
    v = cfg.get(clave)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() == "true"


def pide_efectivo(t: str) -> bool:
    """True si el cliente pide pagar en efectivo / al retirar / al recibir."""
    s = _sin_tildes(t).lower()
    if any(re.search(p, s) for p in _NO_EFECTIVO):
        return False
    return any(re.search(p, s) for p in _EFECTIVO)


def habilitado_efectivo(phone: str, cfg: dict, socio_svc, monto: float = 0.0) -> bool:
    """
    ¿Se le puede tomar el pedido en efectivo? False si la función está apagada
    (default), si es solo para socios y no lo es, o si supera el tope
    (efectivo_tope_monto, 0 = sin tope). Cuando da False, el pedido cae al
    flujo de pago manual de siempre (derivar / solo tarjeta).
    """
    if not _flag(cfg, "efectivo_enabled", False):
        return False
    if _flag(cfg, "efectivo_solo_socios", False):
        try:
            if not (socio_svc and socio_svc.find_by_phone(phone)):
                return False
        except Exception:
            return False
    try:
        tope = float(cfg.get("efectivo_tope_monto") or 0)
    except (TypeError, ValueError):
        tope = 0.0
    return not (tope > 0 and monto > tope)


async def _cerrar_venta_efectivo(session_svc, phone: str, session: dict,
                                 tipo_entrega: str, direccion: Optional[str],
                                 total: float, costo_envio: float = 0.0,
                                 cfg: Optional[dict] = None, link_previo: bool = False,
                                 extra_pedido: Optional[dict] = None) -> str:
    """
    Cierra una venta a pagar en EFECTIVO: crea el pedido (pago="efectivo", cobro
    pendiente) y devuelve la confirmación. A diferencia de cuenta corriente NO
    se registra como pago aprobado: la plata todavía no entró.
    """
    from app.config import get_settings as _gs
    from app.services.order_service import get_order_service
    settings = _gs()
    cfg = cfg or {}

    items = session.get("pending_items") or []
    if len(items) > 1:
        sku_id = "MULTI"
        nombre = " + ".join(
            i["nombre"] + (f" x{i.get('cantidad', 1)}" if i.get("cantidad", 1) > 1 else "")
            for i in items)
        cantidad = 1
    else:
        sku_id = session.get("pending_sku_id") or ""
        cantidad = int(session.get("pending_cantidad") or 1)
        nombre = (session.get("pending_sku_nombre") or "tu pedido") + \
                 (f" x{cantidad}" if cantidad > 1 else "")

    order = await get_order_service(settings.redis_url).create(
        phone=phone, sku_id=sku_id, sku_nombre=nombre, cantidad=cantidad, total=total,
        mp_payment_id="", tipo_entrega=tipo_entrega, direccion_envio=direccion,
        pago="efectivo", extra=extra_pedido,
    )
    logger.info(f"Pedido en efectivo: {order['order_id']} phone={phone} total=${total:,.2f}")

    try:
        from app.services.db import get_db as _gdb
        from app.services.metrics_store import get_metrics_store as _gmet
        await _gmet(_gdb(settings.database_url)).evento(
            "pedido_efectivo", phone=phone, dato=tipo_entrega, monto=total, ref=order["order_id"])
    except Exception as e:
        logger.debug(f"evento pedido_efectivo: {e}")

    await session_svc.set_entrega(phone, tipo_entrega, direccion)
    await session_svc.set_estado(phone, "pedido_confirmado")
    _s_fin = await session_svc.get(phone)
    _s_fin["_ultimo_pedido"] = order["order_id"]
    _s_fin.pop("_efectivo_envio_avisado", None)
    if _s_fin.pop("pago_metodo", None) is not None or True:
        await session_svc.save(phone, _s_fin)

    try:
        horas = int(float(cfg.get("efectivo_horas_reserva") or 0))
    except (TypeError, ValueError):
        horas = 0
    plazo = f" Tenés {horas} hs para pasar a buscarlo." if horas > 0 and tipo_entrega != "envio" else ""
    envio_line = f" (incluye ${costo_envio:,.0f} de envío)" if costo_envio > 0 else ""
    if tipo_entrega == "envio":
        plantilla = (cfg.get("efectivo_envio_message")
                     or get_perfil().textos["efectivo_envio_message"])
    else:
        plantilla = (cfg.get("efectivo_retiro_message")
                     or get_perfil().textos["efectivo_retiro_message"])
    msg = (plantilla.replace("{producto}", nombre).replace("{total}", f"{total:,.2f}")
           .replace("{envio}", envio_line).replace("{plazo}", plazo)
           .replace("{direccion}", direccion or "tu domicilio")
           .replace("{codigo}", str(order.get("pickup_code", ""))))
    if link_previo:
        msg += "\n\nNo hace falta que uses el link de pago que te mandé antes."
    return msg


# ══════════════════════════════════════════════════════════════════════════════
# Interruptor global del bot (19/9): apagado, no responde NADA automático.
# ══════════════════════════════════════════════════════════════════════════════
def bot_encendido(cfg: dict) -> bool:
    return _flag(cfg, "bot_enabled", True)


# ══════════════════════════════════════════════════════════════════════════════
# Alternativas sin precio y "no me figura" duplicado (caso real 19/9, "tenés
# actron 500": el modelo nombró el Actron 600 sin precio, cerró con "¿te
# gustaría más información?" y el sistema le pegó debajo OTRO "No me figura
# disponible" con otra pregunta).
# ══════════════════════════════════════════════════════════════════════════════
# La pregunta vaga se saca con TODA su oración, aunque arranque a mitad
# ("En cuanto a la avena…, ¿te gustaría que te ofrezca otras opciones?", 23/9).
_CIERRE_VAGO = re.compile(
    r"(?:^|(?<=[.!?…\n]))(?P<previo>[^.!?…\n¿]*)¿[^?¿]*\b("
    r"m[aá]s\s+informaci[oó]n|"
    r"te\s+gustar[ií]a\s+(consider\w*|saber|conocer|ver|que\s+te\s+(ofrezca|cuente|muestre|detalle))|"
    r"quer[eé]s\s+(saber|conocer|que\s+te\s+(cuente|detalle|ofrezca|muestre))|"
    r"te\s+interesa(r[ií]a)?\s+(alguna|alguno|conocer|saber)|"
    r"alguna\s+de\s+estas\s+opciones"
    r")\b[^?¿]*\?",
    re.IGNORECASE,
)


def quitar_cierres_vagos(texto: str) -> str:
    """Saca preguntas de cierre que no llevan a nada ("¿Te gustaría más
    información sobre alguna de estas opciones?"). Si el resultado queda
    vacío, devuelve el original."""
    # Los decimales de un precio no son fin de oración ("$4.770, ¿te…?").
    protegido = re.sub(r"(?<=\d)\.(?=\d)", "\x00", texto or "")
    def _recorte(m):
        # Si lo que precede a la pregunta trae un precio, es la oferta: se
        # conserva y se quita solo la pregunta.
        previo = m.group("previo") or ""
        if "$" in previo or re.search(r"\d", previo):
            return previo.rstrip(" ,;:") + "."
        return " "
    limpio = _CIERRE_VAGO.sub(_recorte, protegido).replace("\x00", ".")
    limpio = re.sub(r"[ \t]{2,}", " ", limpio)
    limpio = re.sub(r"\s*\n\s*\n\s*\n+", "\n\n", limpio).strip()
    return limpio if limpio else (texto or "")


_NO_DISPONIBLE = re.compile(
    r"\bno\s+(me\s+)?(figura|aparece)\b|\bno\s+(lo\s+|la\s+)?ten(go|emos)\b|"
    r"\bsin\s+stock\b|\bno\s+(est[aá]|hay)\s+disponible\b|\bno\s+contamos\b",
    re.IGNORECASE,
)


def ya_dice_no_disponible(texto: str) -> bool:
    return bool(_NO_DISPONIBLE.search(texto or ""))


def solo_la_pregunta(oferta: str) -> str:
    """De "No me figura disponible 🙏 ¿Querés que lo consulte...?" deja solo la
    pregunta, para no repetir lo que el modelo ya dijo."""
    i = (oferta or "").find("¿")
    return (oferta[i:] if i >= 0 else (oferta or "")).strip()


def alternativas_con_precio(resultados: list[dict], maximo: int = 3) -> list[dict]:
    """Resultados que se pueden vender (con stock y precio), para listarlos
    cuando el modelo los nombró sin precio."""
    out = []
    for r in resultados or []:
        try:
            precio = float(r.get("precio") or 0)
        except (TypeError, ValueError):
            precio = 0.0
        if r.get("vendible", True) and precio > 0 and r.get("estado", "disponible") == "disponible":
            out.append(r)
        if len(out) >= maximo:
            break
    return out


def texto_alternativas(alternativas: list[dict]) -> str:
    _rec_on = get_perfil().recetas      # sin recetas, nunca "(requiere receta)"
    lineas = []
    for a in alternativas:
        receta = " (requiere receta)" if _rec_on and a.get("requiere_receta") == "si" else ""
        lineas.append(f"• {a['nombre']} — ${float(a['precio']):,.2f}{receta}")
    cierre = "¿Te sirve?" if len(alternativas) == 1 else "¿Te sirve alguno? Decime el nombre o el número."
    return "Lo que tengo disponible:\n" + "\n".join(lineas) + "\n\n" + cierre


# ══════════════════════════════════════════════════════════════════════════════
# Caso real 23/9: receta en PDF ignorada + "necesito eso" confirmó un
# medicamento que había quedado pendiente; y una derivación prometida por el
# modelo que nunca se hizo.
# ══════════════════════════════════════════════════════════════════════════════
def referencia_ambigua_bloquea(texto: str, session: dict, pendiente_con_receta: bool) -> bool:
    """
    True si el mensaje solo señala algo ("necesito eso", "quiero esto") y NO
    puede tomarse como confirmación del producto pendiente: porque después del
    pendiente llegó un adjunto (el "eso" es el adjunto) o porque el pendiente
    lleva receta y no lo cotizó el operador (nunca se confirma sin nombrarlo).
    """
    if not texto_deictico(texto):
        return False
    if pendiente_con_receta and not session.get("receta_validada"):
        return True
    adj = session.get("_adjunto_at") or 0
    pend = session.get("pending_at") or 0
    return bool(adj) and float(adj) >= float(pend)


_DERIV_PROMETIDA = re.compile(
    r"\b(te\s+(paso|derivo|comunico|conecto)\s+con|ya\s+te\s+(paso|derivo)|"
    r"te\s+va\s+a\s+(atender|contactar|escribir)\s+(alguien|una\s+persona|el\s+equipo|una\s+de\s+las\s+chicas))",
    re.IGNORECASE,
)
_DERIV_OFERTA = re.compile(
    r"\b(si\s+quer[eé]s|quer[eé]s\s+que|puedo|pod[eé]s|prefer[ií]s|te\s+gustar[ií]a)\b|¿",
    re.IGNORECASE)


def derivacion_prometida(texto: str) -> bool:
    """
    True si el texto AFIRMA que pasa la charla a una persona ("te paso con
    alguien del equipo, aguardá"). No cuenta si solo lo OFRECE ("¿querés que
    te pase con alguien?", "si querés te paso").
    """
    for oracion in re.split(r"(?<=[.!?…\n])\s*", texto or ""):
        if _DERIV_PROMETIDA.search(oracion) and not _DERIV_OFERTA.search(oracion):
            return True
    return False


async def cumplir_derivacion_prometida(session_svc, phone: str, respuesta: str) -> bool:
    """Si la respuesta promete una persona, la conversación pasa a la cola del
    operador y se suelta el producto pendiente. Devuelve True si derivó."""
    if not derivacion_prometida(respuesta):
        return False
    s = await session_svc.get(phone)
    if s.get("estado") == "operador":
        return True
    await session_svc.clear_pending(phone)
    await session_svc.set_estado(phone, "operador", motivo="derivacion_prometida")
    logger.info(f"Derivación prometida por el modelo → cumplida para {phone}")
    return True


# ══════════════════════════════════════════════════════════════════════════════
# Presentación distinta a la pedida (caso 24/9: "Atenolol 50 x50" → ofreció el
# de 30 sin decir que el de 50 no está).
# ══════════════════════════════════════════════════════════════════════════════
_CANTIDAD_RE = re.compile(r"\bx\s*(\d{1,4})\b", re.IGNORECASE)
_DOSIS_RE = re.compile(r"(?<![x\d])(\d+(?:[.,]\d+)?)\s*(mg|mcg|g|ml|%|ui)?\b", re.IGNORECASE)


def _presentacion(texto: str) -> tuple[set, set]:
    t = (texto or "").lower()
    cantidades = set(_CANTIDAD_RE.findall(t))
    sin_cant = _CANTIDAD_RE.sub(" ", t)
    dosis = {m.group(1).replace(",", ".") for m in _DOSIS_RE.finditer(sin_cant)}
    return dosis, cantidades


def presentacion_distinta(pedido: str, ofrecido: str) -> bool:
    """True si lo pedido especifica una dosis o cantidad que el producto
    ofrecido no tiene (ambos la informan y no coinciden)."""
    d_p, c_p = _presentacion(pedido)
    d_o, c_o = _presentacion(ofrecido)
    if c_p and c_o and not (c_p & c_o):
        return True
    if d_p and d_o and not (d_p & d_o):
        return True
    return False


def aviso_presentacion(pedido: str, respuesta: str) -> str:
    """Antepone que la presentación pedida no está, salvo que el texto ya lo diga."""
    if ya_dice_no_disponible(respuesta):
        return respuesta
    return f"No tengo {pedido.strip()} en esa presentación. {respuesta}".strip()



# ── Fuera de horario (27/9) ─────────────────────────────────────────────────────
_PROMESA_INMEDIATA = re.compile(
    r"[^.!?\n]*\b(en un (momento|ratito|rato)|en breve|enseguida|en unos minutos|"
    r"ya te (contacta|escrib|respond|atiend)\w*|aguard\w*)\b[^.!?\n]*[.!?]?[ \t]*[🙌🙏😊🙂]*",
    re.IGNORECASE)


def aviso_fuera_horario(texto: str, cuando: str) -> str:
    """Mensaje de derivación mandado con la farmacia cerrada: sin promesas de
    atención inmediata ("en un momento te contactamos") y con cuándo abrimos."""
    limpio = _PROMESA_INMEDIATA.sub("", texto or "").strip()
    limpio = re.sub(r"[ \t]+\n", "\n", limpio).strip() or "Eso lo ve alguien del equipo."
    aviso = ("Ahora estamos fuera de horario: te respondemos apenas abramos"
             + (f" ({cuando})" if cuando else "") + " 🙏")
    return f"{limpio}\n\n{aviso}"


_SOY_SOCIO = re.compile(
    r"(?<!no )\b(soy|somos) (soci[oa]s?|de la mutual|afiliad[oa]s?)\b"
    r"|(?<!no )\bestoy (asociad[oa]|afiliad[oa])\b"
    r"|(?<!no )\bsoy (un |una )?soci[oa]\b"
    r"|\b(mi|el) (n[úu]mero|nro|n°) de soci[oa]\b",
    re.IGNORECASE)


def dice_ser_socio(texto: str) -> bool:
    """El cliente dice ser socio ("soy socia", "estoy afiliado", "mi número de
    socio es…"). Si no está en el padrón, se deriva para validar el DNI y
    dar de alta la línea (plan a producción, 24/9)."""
    return bool(_SOY_SOCIO.search(texto or ""))


async def nota_envio_fuera_horario(respuesta: str, tipo_entrega: str) -> str:
    """Pedido con envío cerrado el local: se avisa que sale cuando abrimos."""
    if tipo_entrega != "envio":
        return respuesta
    try:
        from app.config import get_settings as _gs
        from app.services.config_service import get_config_service as _gcs
        cfg_svc = _gcs(_gs().redis_url)
        hours = await cfg_svc.get_hours()
        if cfg_svc.is_open_now(hours):
            return respuesta
        cuando = cfg_svc.proxima_apertura(hours)
    except Exception:
        return respuesta
    nota = "🛵 Estamos fuera de horario: el envío sale apenas abramos" + (
        f" ({cuando})." if cuando else ".")
    return f"{respuesta}\n\n{nota}"


# ── "¿Qué horario tienen?" (29/9) ───────────────────────────────────────────────
# 5/10: "¿A qué hora ABRE la farmacia?" (singular) no matcheaba y caía a
# "no te entendí"; tampoco "hasta q hora", "¿están atendiendo?", "¿abre el
# sábado?". "No me abre el link" NO es una pregunta de horario.
_VERBO_HORARIO = r"(abr(?:e|en|[ií]s)|cierr(?:a|an)|cerr[aá]s|atiend(?:e|en)|atend[eé]s|trabaj(?:a|an|[aá]s))"
_DIA_HORARIO = (r"(hoy|ma[nñ]ana|esta\s+(tarde|noche)|(el|los)\s+(s[aá]bados?|domingos?|feriados?|"
                r"lunes|martes|mi[eé]rcoles|jueves|viernes|fin\s+de\s+semana))")
_PREGUNTA_HORARIO = re.compile(
    r"\bhorarios?\b|\bhora\s+de\s+atenci[oó]n\b"
    r"|\b(a\s+)?(qu[eé]|q)\s+hora\s+" + _VERBO_HORARIO +
    r"|\bhasta\s+(qu[eé]|q)\s+hora\b|\bdesde\s+(qu[eé]|q)\s+hora\b"
    r"|\best[aá]n?\s+(abiert[oa]s?|atendiendo|trabajando)\b"
    r"|\b" + _VERBO_HORARIO + r"\s+" + _DIA_HORARIO + r"\b"
    r"|\bcu[aá]ndo\s+" + _VERBO_HORARIO + r"\b"
    r"|\bqu[eé]\s+d[ií]as\s+" + _VERBO_HORARIO + r"\b",
    re.IGNORECASE)


def pregunta_horario(texto: str) -> bool:
    """El cliente pregunta por el horario de atención. Se contesta con el
    horario cargado en el backoffice, nunca redacta el modelo (lo inventaba
    o decía que no sabía — pedido de la farmacia 29/9)."""
    return bool(_PREGUNTA_HORARIO.search(texto or ""))


def responder_horario(cfg_svc, hours: dict) -> str:
    """Respuesta fija con el horario y si ahora está abierto. Vacío si no hay
    horario cargado (que conteste el modelo como siempre)."""
    texto = cfg_svc.texto_horario(hours)
    if not texto:
        return ""
    r = f"Atendemos {texto} 🕐"
    if hours.get("enabled"):
        if cfg_svc.is_open_now(hours):
            r += " Ahora estamos abiertos 🙂"
        else:
            cuando = cfg_svc.proxima_apertura(hours)
            r += " Ahora estamos cerrados" + (f": abrimos {cuando}." if cuando else ".")
    return r + " ¿Te ayudo con algo más?"


# ══════════════════════════════════════════════════════════════════════════════
# Auditoría de chats 2/10: precios inventados. Ante consultas genéricas
# ("perfumes importados", "algo para várices", "protectores solares") el modelo
# respondía marcas y precios que no existen ("Chanel N°5 $7.200", "Venotonic
# $3.200"). El prompt ya lo prohíbe; esto lo verifica antes de enviar.
# ══════════════════════════════════════════════════════════════════════════════
_PRECIO_SIGNO_RE = re.compile(r"\$\s?(\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)")


def precios_con_signo(texto: str) -> list[float]:
    """Precios escritos con "$" (los números sueltos son dosis o tamaños:
    "Ibuprofeno 600", "x 100")."""
    out = []
    for crudo in _PRECIO_SIGNO_RE.findall(texto or ""):
        s = crudo.strip()
        if len(s) > 3 and s[-3] in ".,":
            s = f"{re.sub(r'[.,]', '', s[:-3])}.{s[-2:]}"
        elif len(s) > 2 and s[-2] in "," and s.count(",") == 1 and "." not in s:
            s = f"{s[:-2]}.{s[-1]}"
        else:
            s = re.sub(r"[.,]", "", s)
        try:
            out.append(round(float(s), 2))
        except ValueError:
            continue
    return out


def precios_inventados(respuesta: str, referencias, extras=()) -> list[float]:
    """
    Precios de la respuesta que no salen de ningún dato real. `referencias`
    son precios unitarios (se aceptan múltiplos por cantidad y sumas de a dos);
    `extras` se aceptan tal cual (totales, envío). Tolera el redondeo a pesos.
    """
    refs = sorted({round(float(v), 2) for v in referencias if v and float(v) > 0})
    validos = {round(v * q, 2) for v in refs for q in range(1, 21)}
    validos |= {round(a + b, 2) for i, a in enumerate(refs) for b in refs[i + 1:]}
    validos |= {round(float(e), 2) for e in extras if e and float(e) > 0}
    malos = []
    for p in precios_con_signo(respuesta):
        if p <= 0:
            continue
        if any(abs(p - v) <= max(1.0, v * 0.001) for v in validos):
            continue
        malos.append(p)
    return malos


def referencias_de_precio(resultados, session: dict, cfg: dict,
                          respuesta: str = "") -> tuple[list[float], list[float]]:
    """(unitarios, totales) válidos para citar en una respuesta: resultados de
    la búsqueda, pedido en curso, opciones mostradas, envío y lo que ya se dijo
    en la conversación. El precio de lista de un producto bonificado vale solo
    si la respuesta habla del precio "de lista": decir "$36.221 ya con tu 20%"
    cuando con el descuento sale $28.977 es dar un precio falso (Nivea, 1/10)."""
    claves = (("precio", "precio_lista") if re.search(r"\blista\b", respuesta or "", re.I)
              else ("precio",))
    unit: list[float] = []
    for r in list(resultados or []) + list(session.get("pending_opciones") or []):
        if r.get("precio_dudoso"):
            continue
        for k in claves:
            try:
                if r.get(k):
                    unit.append(float(r[k]))
            except (TypeError, ValueError):
                pass
    items = session.get("pending_items") or []
    for i in items:
        unit.append(float(i.get("precio") or 0))
    if session.get("pending_precio"):
        unit.append(float(session["pending_precio"]))
    envio = costo_envio_de(cfg)
    total = sum(float(i.get("precio") or 0) * int(i.get("cantidad", 1) or 1) for i in items)
    if not total and session.get("pending_precio"):
        total = float(session["pending_precio"]) * int(session.get("pending_cantidad", 1) or 1)
    totales = [envio, total, total + envio if total else 0]
    # Lo que ya se dijo (el operador cotizó una receta, el cliente citó un
    # precio): repetirlo no es inventar.
    for m in (session.get("history") or [])[-8:]:
        totales.extend(precios_con_signo(str(m.get("content") or "")))
    return unit, totales


# Auditoría 2/10: el modelo decía "ese requiere receta" de productos que el
# catálogo marca de venta libre (Hipoglós, curitas, Ultraflex) y después
# igual salía el link. Manda la marca del catálogo: la frase se saca.
_FRASE_RE = re.compile(r"[^.!?\n]*[.!?]?\s*")
_RECETA_AFIRMA = re.compile(
    r"(requier\w*|necesit\w*|lleva\w*|va|es|son|piden?|bajo|con)\s+(una\s+|la\s+)?receta",
    re.IGNORECASE)


def quitar_receta_inventada(respuesta: str) -> str:
    """Saca las frases que afirman que algo lleva receta."""
    if not respuesta or not _RECETA_AFIRMA.search(respuesta):
        return respuesta
    partes = [p for p in _FRASE_RE.findall(respuesta) if p]
    limpio = "".join(p for p in partes if not _RECETA_AFIRMA.search(p)).strip()
    return limpio or respuesta


# ── Cierre por inactividad según quién habló último (auditoría 2/10) ──────────
_DESPEDIDA_RE = re.compile(
    r"\b(gracias|grax|chau|chao|adi[oó]s|nos vemos|hasta (luego|ma[nñ]ana|pronto|la pr[oó]xima)|"
    r"(lo |los |la |las )?paso a buscar|(despu[eé]s|luego|ma[nñ]ana|m[aá]s tarde) paso|"
    r"paso (despu[eé]s|luego|ma[nñ]ana|m[aá]s tarde)|lo pienso|lo voy a pensar|"
    r"buen (d[ií]a|fin de semana)|saludos|abrazo|besos?)\b",
    re.IGNORECASE)


def es_despedida(t: str) -> bool:
    return bool(_DESPEDIDA_RE.search(t or ""))


def cierre_por_inactividad(history: list) -> str:
    """
    Qué hacer con una conversación inactiva, según el último mensaje:
      "derivar"  → habló el cliente y nadie le contestó: que lo vea una
                   persona (antes recibía "como no tuvimos respuesta").
      "silencio" → se despidió el cliente, o el último fue el operador: se
                   cierra sin aviso.
      "avisar"   → el bot le preguntó algo y no contestó: aviso de cierre.
    """
    if not history:
        return "silencio"
    ultimo = history[-1]
    rol = ultimo.get("role")
    if rol == "user":
        return "silencio" if es_despedida(ultimo.get("content") or "") else "derivar"
    if rol != "assistant":
        return "silencio"
    ult_cliente = next((m for m in reversed(history) if m.get("role") == "user"), None)
    if ult_cliente and es_despedida(ult_cliente.get("content") or ""):
        return "silencio"
    return "avisar"



# ── Saludo repetido en medio de la charla (auditoría 2/10) ────────────────────
# "¡Hola Claudia! Qué bueno verte de nuevo 😊" aparecía en cualquier turno,
# a veces con el nombre equivocado ("Muff", "Luna", "Gasperi").
_SALUDO_INICIO_RE = re.compile(
    r"^\s*¡?\s*(hola|buen[oa]s(\s+(d[ií]as|tardes|noches))?|buen\s+d[ií]a)\b(\s+[^\s!.,?]+){0,2}?\s*[!.,]\s*"
    r"([\U0001F300-\U0001FAFF\u2600-\u27BF]\s*)*"
    r"(¡?\s*qu[eé]\s+(bueno|lindo|gusto)\s+(verte|leerte|saludarte)(\s+de\s+nuevo|\s+otra\s+vez)?\s*[!.]?\s*"
    r"([\U0001F300-\U0001FAFF\u2600-\u27BF]\s*)*)?",
    re.IGNORECASE)


def quitar_saludo_repetido(respuesta: str, history: list) -> str:
    """Saca el saludo del principio si la conversación ya está en curso."""
    if not respuesta or not any(m.get("role") in ("assistant", "operator") for m in history or []):
        return respuesta
    m = _SALUDO_INICIO_RE.match(respuesta)
    if not m or not m.group(0).strip():
        return respuesta
    resto = respuesta[m.end():].lstrip(" ,.!")
    if len(resto) < 3:
        return respuesta
    return resto[0].upper() + resto[1:]



# ── "Sí, encargalo" (auditoría 2/10) ──────────────────────────────────────────
# El bot decía "no lo tengo, ¿lo encargamos?" y en la misma respuesta ofrecía
# un sustituto con precio: el "encargalo" confirmaba el SUSTITUTO y pedía
# retiro/envío del Bagovit corporal, de la tintura o del jabón equivocado.
_ENCARGO_RE = re.compile(
    r"\b(encarg\w*|ped[ií](lo|la|los|las|melo|mela|melos)|p[ií]dan(lo|la|melo)?|"
    r"que (lo|la|los|las|me lo|me la) traigan|traigan(lo|la|melo)|"
    r"consegu[ií](lo|la|melo|mela)|consigan(lo|la|melo|mela)?)\b",
    re.IGNORECASE)
_NO_ENCARGO_RE = re.compile(r"\bno\b.{0,12}\b(encarg|ped[ií]|traig|consig)", re.IGNORECASE)
_FALTA_STOCK_RE = re.compile(
    r"(no (me )?(figura|tengo|tenemos|hay|queda|quedan|encontr)|sin stock|agotad|encarg)",
    re.IGNORECASE)


def pide_encargo(texto: str, history: list) -> bool:
    """True si pide encargar algo que el bot acaba de decir que no tiene."""
    if not texto or not _ENCARGO_RE.search(texto) or _NO_ENCARGO_RE.search(texto):
        return False
    ult_bot = next((m for m in reversed(history or []) if m.get("role") == "assistant"), None)
    return bool(ult_bot and _FALTA_STOCK_RE.search(ult_bot.get("content") or ""))



# ── Precios absurdos del ERP (auditoría 2/10) ─────────────────────────────────
def marcar_precio_dudoso(resultados: list[dict], cfg: dict, sku_svc=None) -> list[dict]:
    """
    Productos con precio por debajo de `precio_minimo_venta` (precio viejo del
    ERP: shampoo Dove $56,90, Head & Shoulders $49,66): el bot no los cotiza
    ni los cobra — quedan no vendibles y el modelo ofrece consultarlos.
    Devuelve copias.
    """
    try:
        minimo = float(cfg.get("precio_minimo_venta") or 0)
    except (TypeError, ValueError):
        minimo = 0.0
    if not resultados:
        return resultados
    out = []
    for r in resultados:
        precio = float(r.get("precio_lista") or r.get("precio") or 0)
        # El mismo producto cargado dos veces en el ERP con un precio viejo
        # (C-3996: "ELVIVE COLOR VIVE SHA X 400" a $1.006 y "(NUEVO)" a
        # $9.591; se vendió a $804 con descuento): si otro con el mismo
        # nombre vale más del triple, el barato no se cotiza.
        ref = sku_svc.precio_referencia(r.get("nombre") or "") if sku_svc else 0.0
        if precio > 0 and ((minimo > 0 and precio < minimo) or (ref and precio * 3 < ref)):
            r = dict(r, vendible=False, precio_dudoso=True)
        out.append(r)
    return out



# C-4148 (5/10): el modelo nombró "Tratamiento Alisante Sin Formol de la marca
# Liss" — sin precio, así que el control de precios no lo vio — y después
# dijo que no lo tenía. Un nombre entre comillas o "de la marca X" que no
# está en ningún resultado es un producto inventado.
_NOMBRE_CITADO_RE = re.compile(r'[“"«]([^”"»\n]{4,70})[”"»]|\bde\s+la\s+marca\s+[“"«]?([A-ZÁÉÍÓÚÑ][\w\-]{2,30})')


def nombres_inventados(respuesta: str, resultados) -> list[str]:
    """Nombres de producto citados en la respuesta que no salen de ningún resultado."""
    nombres = " ".join((r.get("nombre") or "").lower() for r in (resultados or []))
    malos = []
    for a, b in _NOMBRE_CITADO_RE.findall(respuesta or ""):
        cita = (a or b).strip()
        toks = [t for t in re.findall(r"[a-záéíóúñ0-9]{4,}", cita.lower())]
        if not toks:
            continue
        presentes = sum(1 for t in toks if t in nombres)
        if presentes * 2 < len(toks):
            malos.append(cita)
    return malos



# C-4051 (5/10): "protector solar drenarlos" → … → "Dermaglos" a secas.
def es_refinamiento_de_marca(nueva: str, previa: str) -> bool:
    """La nueva búsqueda es solo una marca (1-2 palabras, sin tipo de producto)
    que refina la búsqueda anterior, que sí decía qué tipo de producto era."""
    from app.services.catalogo_enriquecido import tipos_mencionados
    toks = re.findall(r"[a-záéíóúñ0-9]+", (nueva or "").lower())
    prev = re.findall(r"[a-záéíóúñ0-9]+", (previa or "").lower())
    if not toks or len(toks) > 2 or len(prev) < 2:
        return False
    if tipos_mencionados(nueva) or set(toks) <= set(prev):
        return False
    return True


def marca_en_resultado(marca: str, nombre: str) -> bool:
    toks = [t for t in re.findall(r"[a-záéíóúñ0-9]{4,}", (marca or "").lower())]
    n = (nombre or "").lower()
    return bool(toks) and all(t in n for t in toks)
