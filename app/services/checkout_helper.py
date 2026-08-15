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


def producto_respaldado(respuesta: str, resultados: list[dict]) -> Optional[dict]:
    """
    Producto de `resultados` cuyo precio aparece en la respuesta enviada al
    cliente, o None si ninguno. Es la evidencia de que el bot está ofreciendo
    ese producto: si respondió "no lo tengo" (sin precio), no hay nada que
    dejar pendiente y no debe generarse un link de pago.
    """
    if not respuesta or not resultados:
        return None
    precios = precios_mencionados(respuesta)
    if not precios:
        return None
    for r in resultados:
        try:
            if round(float(r.get("precio") or 0), 2) in precios:
                return r
        except (TypeError, ValueError):
            continue
    return None


_PAGO_MANUAL = [
    r"\btransferencia\b", r"\btransferir\b", r"\btransfiero\b", r"\btransferis\b",
    r"\befectivo\b", r"\bcbu\b", r"\balias\b", r"\bmercado\s*pago\b",
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
_FRASE_ESPERA = re.compile(
    r"(?:^|(?<=[.!?…]))\s*[^.!?…]*"
    r"\b(ahora|voy\s+a|dejame|d[eé]jame|en\s+un\s+momento|ya\s+te|luego\s+te|"
    r"despu[eé]s\s+te|un\s+segundito|aguardame|esperame)\b"
    r"[^.!?…]*\b(verific\w+|chequ\w+|consulto|confirmo|busco|averig\w+|reviso|fijo)\w*"
    r"[^.!?…]*[.!?…]?",
    re.IGNORECASE,
)


def quitar_frases_de_espera(texto: str) -> str:
    """
    Elimina oraciones tipo "Ahora verifico X para vos": prometen una
    verificación que nunca llega (no hay segundo mensaje). Si el resultado
    queda vacío, se devuelve el original — mejor una promesa fea que silencio.
    """
    limpio = _FRASE_ESPERA.sub(" ", texto or "").strip()
    limpio = re.sub(r"\s{2,}", " ", limpio)
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


def texto_entrega(tipo: str, direccion: Optional[str]) -> str:
    """Línea que describe la entrega elegida, para el mensaje del link de pago."""
    if tipo == "envio":
        dir_txt = f" a *{direccion}*" if direccion else ""
        return f"🚚 Te lo enviamos a domicilio{dir_txt}."
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

    # Descuento automático de socio (config socio_discount_pct, 0 = apagado).
    # Solo llega acá mercadería sin receta (la receta deriva antes).
    descuento_line = ""
    try:
        from app.config import get_settings as _gs
        from app.services.config_service import get_config_service as _gcs
        from app.services.socio_service import get_socio_service as _gss
        _settings = _gs()
        _cfg = await _gcs(_settings.redis_url).get_all()
        pct = float(_cfg.get("socio_discount_pct") or 0)
        if pct > 0 and _gss(_settings.socios_path).find_by_phone(phone):
            antes = total
            precio_unitario = round(precio_unitario * (1 - pct / 100), 2)
            total = precio_unitario * cantidad
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
    entrega_line = texto_entrega(tipo_entrega, direccion)
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

    # 1. Requiere receta → derivar a una persona (no se genera link)
    if necesita_receta(sku_svc, sku_id, modo):
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
        return (
            f"¡Genial! ¿Cómo preferís recibirlo: *retiro en sucursal* o *envío a domicilio*?{extra}",
            "esperando_entrega",
        )

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

    # Ambiguo o mencionó ambas → volver a preguntar
    return ("¿Preferís *retiro en sucursal* o *envío a domicilio*? 🙂", "esperando_entrega")


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
