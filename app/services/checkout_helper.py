"""
Helpers compartidos del flujo de checkout: derivación por receta y
finalización de compra (link de pago + modo de entrega).

Usados por webhook.py (WhatsApp real) y simulate.py (testing) para no
duplicar la lógica de negocio.
"""

import logging
import re
from typing import Optional

from app.services.sku_service import requiere_derivacion

logger = logging.getLogger(__name__)

# Detección de modo de entrega (compartida por webhook y simulate)
_RETIRO = [r"\bretiro\b", r"\bretirar\b", r"\bsucursal\b", r"\bpaso\b", r"\bbusco\b",
           r"\bvoy\b", r"\bretiro yo\b", r"\ben el local\b", r"\bpasar\b"]
_ENVIO  = [r"\benv[ií]o\b", r"\benviar\b", r"\benv[ií]en\b", r"\bdomicilio\b",
           r"\bmand[aá]\b", r"\bmandame\b", r"\bmanden\b", r"\bcasa\b", r"\bdelivery\b",
           r"\ba domicilio\b"]


def match_retiro(t: str) -> bool:
    return any(re.search(p, t, re.IGNORECASE) for p in _RETIRO)


def match_envio(t: str) -> bool:
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
# Una dirección escrita: nombre de calle + número (ej: "donado 608", "16 de enero 9279").
_DIR_RE = re.compile(r"[a-záéíóúñ]{2,}\.?\s+\d+", re.IGNORECASE)
_CONECTORES = [r"\blo quiero\b", r"\bquiero\b", r"\benv[ií]a?r?\b", r"\bmandar?\b",
               r"\ba\b", r"\ben\b", r"\bla\b", r"\bel\b", r"\bmi\b"]


def quiere_cambiar_direccion(t: str) -> bool:
    return any(re.search(p, t, re.IGNORECASE) for p in _CAMBIO_DIR)


def parece_direccion(t: str) -> bool:
    return bool(_DIR_RE.search(t))


def extraer_direccion_de(t: str) -> Optional[str]:
    """
    Extrae una dirección escrita del mensaje (calle + número), quitando frases
    de cambio y conectores. Devuelve None si el mensaje no contiene una dirección
    reconocible (evita tomar cualquier texto como dirección).
    """
    s = t
    for p in _CAMBIO_DIR + _CONECTORES:
        s = re.sub(p, " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip(" ,.")
    return s if s and parece_direccion(s) else None


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
    r"\b(carg[aá]\w*|anot[aá]\w*|sum[aá]\w*)\b.{0,30}\b(mi\s+)?cuenta\b",
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


_ANOTAR = r"\b(anot[ae](?!d)\w*|apunt[ae](?!d)\w*)\b"   # "anotado" no es un pedido
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
    El socio del padrón si puede pagar con cuenta corriente, o None.
    None si: la función está apagada (cc_enabled), el teléfono no es socio,
    figura en la lista de excepciones de la farmacia, o supera el tope
    (cc_tope_monto, 0 = sin tope).
    """
    if str(cfg.get("cc_enabled", "true")).lower() != "true":
        return None
    try:
        socio = socio_svc.find_by_phone(phone) if socio_svc else None
    except Exception:
        socio = None
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


def contiene_link(t: str) -> bool:
    """
    True si el mensaje trae una URL o referencia a un archivo (receta/bono
    enviado como link en vez de foto) → se deriva a una persona, igual que
    una imagen de receta. Excluye los links de pago propios (pay/...).
    """
    m = _LINK_RE.search(t or "")
    if not m:
        return False
    link = m.group(0).lower()
    return "remedia.ar" not in link and "/pay/" not in link


def necesita_receta(sku_svc, sku_id: str, modo: str) -> bool:
    """True si el producto pendiente requiere derivación por receta."""
    if not sku_id:
        return False
    sku = sku_svc.get_by_id(sku_id)
    if not sku:
        return False
    return requiere_derivacion(sku.requiere_receta, modo)


async def derivar_si_receta(sku_svc, session_svc, cfg: dict, phone: str, sku_id: str,
                            nombre: str = ""):
    """
    Si el producto recién elegido requiere receta, deriva a una persona en el
    acto (sin ofrecer link de pago) y devuelve el mensaje para el cliente.
    Si no, devuelve None y el flujo sigue normal.
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
        await session_svc.clear_pending(phone)
        await session_svc.set_estado(phone, "operador", motivo="receta")
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


def aplicar_descuento_socio(resultados: list[dict], phone: str, cfg: dict,
                            socio_svc=None) -> tuple[list[dict], float]:
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
    try:
        pct = float(cfg.get("socio_discount_pct") or 0)
    except (TypeError, ValueError):
        pct = 0.0
    if pct <= 0:
        return resultados, 0.0

    try:
        if socio_svc is None:
            from app.config import get_settings as _gs
            from app.services.socio_service import get_socio_service as _gss
            socio_svc = _gss(_gs().socios_path)
        if not socio_svc.find_by_phone(phone):
            return resultados, 0.0
    except Exception as e:
        logger.warning(f"No se pudo evaluar el descuento de socio para {phone}: {e}")
        return resultados, 0.0

    modo = cfg.get("receta_mode", "conservador")
    salida = []
    for r in resultados:
        item = dict(r)
        if not requiere_derivacion(item.get("requiere_receta", "no"), modo):
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


def pregunta_entrega(cfg: dict, extra: str = "", saludo: bool = True) -> str:
    """
    La pregunta retiro/envío, con el costo del envío A LA VISTA si existe:
    el cliente lo ve antes de elegir, nunca como sorpresa en el link.
    """
    costo = costo_envio_de(cfg)
    envio_txt = (f"*envío a domicilio* (+${costo:,.0f})" if costo > 0
                 else "*envío a domicilio*")
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
                           link_previo: bool = False) -> str:
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
        direccion_envio=direccion, pago="cuenta_corriente",
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
        from app.services.socio_service import get_socio_service as _gss
        pct = float(_cfg.get("socio_discount_pct") or 0)
        if pct > 0 and _gss(_settings.socios_path).find_by_phone(phone):
            # Precio de lista reconstruido desde el total ya bonificado, sólo
            # para mostrarlo en el mensaje.
            antes = (round(total_productos / (1 - pct / 100), 2)
                     if pct < 100 else total_productos)
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
        _msg_freno, _precio_erp = await _chequear_stock_vivo(session, phone, session_svc, _cfg)
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
            return respuesta_ef, None

    if session.get("pago_metodo") == "cuenta_corriente":
        respuesta_cc = await _cerrar_venta_cc(
            session_svc, phone, session, tipo_entrega, direccion,
            total=total, costo_envio=_costo_envio,
            link_previo=session.get("estado") == "esperando_pago")
        return respuesta_cc, None

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
    return respuesta, link


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
        await session_svc.clear_pending(phone)
        await session_svc.set_estado(phone, "operador", motivo="receta")
        inicio = f"{nombre}, ese" if nombre else "Ese"
        return (
            f"{inicio} medicamento requiere receta 🩺. Te paso con alguien del equipo "
            "para gestionarlo con vos. ¡En un momento te contactamos!",
            "derivado_receta",
        )

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
        socio = socio_svc.find_by_phone(phone) if socio_svc else None
        if socio and socio.get("domicilio"):
            extra = f" Si querés envío, te lo mandamos a *{socio['domicilio']}* (o decime otra dirección)."
        else:
            extra = ""
        return (pregunta_entrega(cfg, extra), "esperando_entrega")

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
        socio = socio_svc.find_by_phone(phone) if socio_svc else None
        if socio and socio.get("domicilio"):
            respuesta, _ = await crear_link_y_responder(
                payment_svc, session_svc, phone, session, "envio", socio["domicilio"]
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
    return (pregunta_entrega(_cfg_e, saludo=False), "esperando_entrega")


async def capturar_direccion(
    payment_svc, session_svc, phone: str, session: dict, texto: str,
) -> tuple[str, str]:
    """
    Captura la dirección de envío (estado esperando_direccion) y genera el link.
    Devuelve (respuesta, intencion).
    """
    direccion = texto.strip()
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
    return (cfg.get("obras_sociales_no_message")
            or "Por ahora no tenemos convenio con {obra_social}. ¿Querés que lo consulte con el "
               "equipo por si hay alguna forma?").replace("{obra_social}", mencionada.strip()), True


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
            txt = cfg.get("bono_recibido_message") or (
                "¡Hola {nombre}! Sí, trabajamos los bonos de {laboratorio} 🙌 Te paso con "
                "alguien del equipo que lo gestiona con vos.")
        else:
            txt = cfg.get("bono_consulta_si_message") or (
                "Sí, trabajamos los bonos de {laboratorio} 🙂 Mandame la foto del bono y te "
                "paso con alguien del equipo que lo gestiona.")
        return personalizar_nombre(txt.replace("{laboratorio}", hit), nombre), True
    if por_foto:
        txt = cfg.get("bono_no_reconocido_message") or (
            "¡Hola {nombre}! Recibí tu bono 🙌 Te paso con alguien del equipo para "
            "confirmar si lo trabajamos.")
    else:
        txt = cfg.get("bono_consulta_no_message") or (
            "Eso lo confirma el equipo: te paso con alguien para que lo vea con vos 🙂")
    return personalizar_nombre(txt, nombre), False


# ── 48b: pedido por síntoma → dejar a mano el farmacéutico ─────────────────────
def agregar_oferta_farmaceutico(respuesta: str, cfg: dict) -> str:
    extra = cfg.get("sintoma_farmaceutico_message") or (
        "Si preferís, decime \"farmacéutico\" y te paso con el nuestro para que te oriente.")
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
_NO_EFECTIVO = [r"\bno\s+(tengo|uso|manejo|quiero)\s+efectivo\b", r"\bsin\s+efectivo\b"]


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
                                 cfg: Optional[dict] = None, link_previo: bool = False) -> str:
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
        pago="efectivo",
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
        plantilla = cfg.get("efectivo_envio_message") or (
            "✅ *¡Listo! Tomamos tu pedido* 🙌\n\n"
            "*{producto}* — ${total}{envio}\n"
            "🚚 Te lo enviamos a *{direccion}* y lo pagás en efectivo al recibirlo.\n"
            "📋 Código de pedido: *{codigo}*\n\n¡Muchas gracias! 💊")
    else:
        plantilla = cfg.get("efectivo_retiro_message") or (
            "✅ *¡Listo! Tomamos tu pedido* 🙌\n\n"
            "*{producto}* — ${total}\n"
            "💵 Lo pagás en efectivo al retirar.{plazo}\n"
            "🔑 *Tu código de retiro es: {codigo}*\n\n¡Muchas gracias! 💊")
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
    lineas = []
    for a in alternativas:
        receta = " (requiere receta)" if a.get("requiere_receta") == "si" else ""
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
