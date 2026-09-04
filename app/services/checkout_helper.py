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
    r"te\s+esperamos\b[^.!?…$]{0,40}\bretirar\w*)"
    r"[^.!?…$]*[.!?…]?",
    re.IGNORECASE,
)


def quitar_confirmaciones_fantasma(texto: str) -> str:
    """
    Recorta oraciones donde el modelo anuncia una compra o confirmación que el
    sistema no hizo. Las oraciones con importes nunca se tocan (el precio que
    el bot dice es el que cobra), y si el resultado queda vacío se devuelve el
    original — mejor un anuncio de más que un bot mudo.
    """
    protegido = re.sub(r"(?<=\d)\.(?=\d)", "\x00", texto or "")
    limpio = _CONFIRMACION_FANTASMA.sub(" ", protegido)
    limpio = re.sub(r"\s{2,}", " ", limpio).strip().replace("\x00", ".")
    return limpio if limpio else (texto or "")


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
        await session_svc.clear_pending(phone)
        await session_svc.set_estado(phone, "operador", motivo="receta")
        inicio = f"{nombre}, ese" if nombre else "Ese"
        return (
            f"{inicio} producto requiere receta 🩺. Te paso con alguien del equipo "
            "para gestionarlo con vos. ¡En un momento te contactamos!"
        )
    return None


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
        await _gms(_gdb(_gs2().database_url)).evento(
            "link_enviado", phone=phone, monto=total,
            ref=link.rsplit("/", 1)[-1][:64],
            extra={"producto": nombre_link, "cantidad": cantidad},
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
