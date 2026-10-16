"""
Pago por TRANSFERENCIA (minuta 14/10, liberar el flujo).

1. El cliente elige transferencia con un pedido armado: el bot pasa el alias
   (configurable en Comportamiento) con el importe exacto y guarda el
   carrito en la conversación. El pedido NO se crea todavía (16/10: un
   pedido "pendiente de comprobante" con código de retiro se podía preparar
   sin estar pago).
2. Llega el comprobante: se lee (importe, fecha, destinatario, N° de
   operación) y se clasifica:
   - error (importe distinto, otra cuenta, fecha vieja, operación ya usada):
     NO se crea el pedido y pasa a una persona con lo que se leyó.
   - a verificar (no se lee algún dato, aprobación automática apagada o el
     total no es MENOR al máximo transferencia_auto_max): se crea el pedido
     "a verificar" y lo aprueba una persona.
   - ok: se crea el pedido aprobado y se manda el código de retiro.
   La operación se registra al crear el pedido: no paga dos pedidos.
"""

import logging
import re
import time
import unicodedata
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

_TRANSFER_RE = re.compile(r"\b(transf\w*|alias|cbu|cvu)\b", re.IGNORECASE)
_YA_TRANSFIRIO_RE = re.compile(
    r"\b(ya\s+)?(te\s+|les\s+)?(transfer[ií]|hice\s+la\s+transferencia|pagu[eé]|deposit[eé])\b",
    re.IGNORECASE)

PENDIENTE = "pendiente_comprobante"
APROBADA = "aprobada"
A_VERIFICAR = "a_verificar"


def pide_transferencia(texto: str) -> bool:
    return bool(_TRANSFER_RE.search(texto or ""))


def avisa_que_transfirio(texto: str) -> bool:
    return bool(_YA_TRANSFIRIO_RE.search(texto or ""))


def habilitada(cfg: dict) -> bool:
    return bool((cfg.get("transferencia_alias") or "").strip())


def datos_cuenta(cfg: dict) -> str:
    """ "alias *x* (titular Y, CBU Z)"."""
    alias = (cfg.get("transferencia_alias") or "").strip()
    extra = []
    if (cfg.get("transferencia_titular") or "").strip():
        extra.append(f"titular {cfg['transferencia_titular'].strip()}")
    if (cfg.get("transferencia_cbu") or "").strip():
        extra.append(f"CBU {cfg['transferencia_cbu'].strip()}")
    return f"alias *{alias}*" + (f" ({', '.join(extra)})" if extra else "")


def mensaje_datos(cfg: dict, total: Optional[float] = None) -> str:
    plantilla = (cfg.get("transferencia_message") or "").strip()
    monto = f"*${total:,.2f}* " if total else ""
    if plantilla:
        return (plantilla.replace("{cuenta}", datos_cuenta(cfg))
                .replace("{total}", f"${total:,.2f}" if total else "").strip())
    return (f"Transferí {monto}al {datos_cuenta(cfg)} 🏦\n\n"
            "Cuando lo hagas, mandame la foto o captura del comprobante por acá y te "
            "confirmamos el pedido 🙌")


# ── Lectura del comprobante ─────────────────────────────────────────────────────
def _plano(t: str) -> str:
    t = unicodedata.normalize("NFKD", t or "")
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


def importe_de(valor) -> Optional[float]:
    """"$ 26.863,96" / "26863.96" / 26863.96 → 26863.96."""
    if valor is None or valor == "":
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    s = re.sub(r"[^\d.,]", "", str(valor))
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        ent, _, dec = s.rpartition(",")
        s = f"{ent.replace(',', '')}.{dec}" if len(dec) <= 2 else s.replace(",", "")
    elif s.count(".") > 1 or (s.count(".") == 1 and len(s.rpartition(".")[2]) == 3):
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


_MESES = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8,
          "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12}


def fecha_de(texto: str) -> Optional[date]:
    t = _plano(texto)
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.search(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", t)
        if m:
            d, mo, y = map(int, m.groups())
        else:
            m = re.search(r"(\d{1,2})\s*(?:de\s+)?([a-z]{3})[a-z]*\.?\s*(?:de\s+)?(\d{4})", t)
            if not m or m.group(2) not in _MESES:
                return None
            d, mo, y = int(m.group(1)), _MESES[m.group(2)], int(m.group(3))
    if y < 100:
        y += 2000
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def es_nuestra_cuenta(info: dict, cfg: dict) -> bool:
    """El destinatario del comprobante es la cuenta configurada (alias, CBU o
    titular). Sin dato de destino legible, no se puede afirmar."""
    destino = _plano(" ".join(str(info.get(k) or "") for k in ("destinatario", "cuenta_destino")))
    if not destino.strip():
        return False
    alias = _plano(cfg.get("transferencia_alias") or "").strip()
    if alias and alias in destino:
        return True
    cbu = re.sub(r"\D", "", cfg.get("transferencia_cbu") or "")
    if len(cbu) >= 6 and cbu[-6:] in re.sub(r"\D", "", destino):
        return True
    titular = [t for t in re.findall(r"[a-z]{3,}", _plano(cfg.get("transferencia_titular") or ""))
               if t not in ("de", "del", "la", "los", "srl", "s.a")]
    if titular and sum(1 for t in titular if t in destino) >= min(2, len(titular)):
        return True
    return False


def _destino_legible(info: dict) -> bool:
    return bool(" ".join(str(info.get(k) or "") for k in ("destinatario", "cuenta_destino")).strip())


def clasificar(info: Optional[dict], total: float, cfg: dict,
               hoy: Optional[date] = None) -> tuple[str, str]:
    """
    ("ok" | "verificar" | "error", motivo legible). "error" = el comprobante
    no corresponde al pedido (no se crea); "verificar" = falta un dato o la
    aprobación automática no alcanza (se crea y lo aprueba una persona).
    """
    if not info:
        return "verificar", "no se pudo leer el comprobante"
    importe = importe_de(info.get("importe"))
    if importe is None:
        return "verificar", "no se lee el importe"
    if abs(importe - float(total)) > 1.0:
        return "error", f"el importe (${importe:,.2f}) no coincide con el pedido (${float(total):,.2f})"
    f = fecha_de(str(info.get("fecha") or ""))
    hoy = hoy or (datetime.utcnow() - timedelta(hours=3)).date()
    if f is not None and f not in (hoy, hoy - timedelta(days=1)):
        return "error", f"la fecha ({f:%d/%m/%Y}) no es de hoy ni de ayer"
    if _destino_legible(info) and not es_nuestra_cuenta(info, cfg):
        return "error", "el destinatario no coincide con nuestra cuenta"
    try:
        maximo = float(cfg.get("transferencia_auto_max") or 0)
    except (TypeError, ValueError):
        maximo = 0.0
    if maximo <= 0:
        return "verificar", "la aprobación automática está desactivada"
    if float(total) >= maximo:
        return "verificar", f"el pedido (${float(total):,.2f}) supera el máximo automático (${maximo:,.2f})"
    if f is None:
        return "verificar", "no se lee la fecha"
    if not _destino_legible(info):
        return "verificar", "no se lee el destinatario"
    if not str(info.get("operacion") or "").strip():
        return "verificar", "no se lee el número de operación"
    return "ok", "ok"


def validar(info: Optional[dict], total: float, cfg: dict, hoy: Optional[date] = None) -> tuple[bool, str]:
    """(aprobado, motivo). Motivo legible para el operador cuando no aprueba."""
    estado, motivo = clasificar(info, total, cfg, hoy)
    return estado == "ok", motivo


def operacion_de(info: Optional[dict]) -> str:
    return re.sub(r"\s+", "", str((info or {}).get("operacion") or ""))


async def liberar_operacion(session_svc, op: str) -> None:
    """Transferencia rechazada: la operación queda libre para otro pedido."""
    if op:
        await session_svc.liberar_unico(f"trf_op:{op}")


# ── Pedido ─────────────────────────────────────────────────────────────────────
def _nombre_pedido(session: dict) -> tuple[str, str, int]:
    items = session.get("pending_items") or []
    if len(items) > 1:
        nombre = " + ".join(i["nombre"] + (f" x{i.get('cantidad', 1)}" if i.get("cantidad", 1) > 1 else "")
                            for i in items)
        return "MULTI", nombre, 1
    cant = int(session.get("pending_cantidad") or 1)
    return (session.get("pending_sku_id") or "",
            (session.get("pending_sku_nombre") or "tu pedido") + (f" x{cant}" if cant > 1 else ""), cant)


async def cerrar_venta(session_svc, phone: str, session: dict, tipo_entrega: str,
                       direccion: Optional[str], total: float, costo_envio: float = 0.0,
                       cfg: Optional[dict] = None) -> str:
    """Guarda el pedido a pagar (sin crearlo: se crea con el comprobante) y
    devuelve el mensaje con el alias."""
    from app.config import get_settings
    settings = get_settings()
    cfg = cfg or {}
    sku_id, nombre, cantidad = _nombre_pedido(session)
    items = session.get("pending_items") or []
    pedido = {"sku_id": sku_id, "nombre": nombre, "cantidad": cantidad,
              "total": round(float(total), 2), "tipo_entrega": tipo_entrega,
              "direccion": direccion, "items": items if len(items) > 1 else None}
    logger.info(f"Pedido por transferencia a pagar: phone={phone} total=${total:,.2f}")
    try:
        from app.services.db import get_db
        from app.services.metrics_store import get_metrics_store
        await get_metrics_store(get_db(settings.database_url)).evento(
            "pedido_transferencia", phone=phone, dato=tipo_entrega, monto=total)
    except Exception as e:
        logger.debug(f"evento pedido_transferencia: {e}")
    await session_svc.set_entrega(phone, tipo_entrega, direccion)
    await session_svc.set_estado(phone, "esperando_comprobante")
    s = await session_svc.get(phone)
    s["_transferencia"] = {"pedido": pedido, "total": pedido["total"], "ts": time.time()}
    s.pop("pago_metodo", None)
    await session_svc.save(phone, s)
    envio = f" (incluye ${costo_envio:,.0f} de envío)" if costo_envio > 0 else ""
    return (f"✅ *¡Listo!* Tu pedido 🙌\n\n*{nombre}* — ${total:,.2f}{envio}\n\n"
            + mensaje_datos(cfg, total))


async def _crear_pedido(phone: str, pedido: dict, campos: dict) -> dict:
    from app.config import get_settings
    from app.services.order_service import get_order_service
    return await get_order_service(get_settings().redis_url).create(
        phone=phone, sku_id=pedido["sku_id"], sku_nombre=pedido["nombre"],
        cantidad=pedido["cantidad"], total=pedido["total"], mp_payment_id="",
        tipo_entrega=pedido.get("tipo_entrega") or "retiro", direccion_envio=pedido.get("direccion"),
        pago="transferencia", extra={**campos, "items_stock": pedido.get("items")})


async def pedido_pendiente(session: dict, phone: str) -> Optional[dict]:
    """
    Lo que espera comprobante: {"total", "pedido"} con el carrito guardado en
    la sesión, o {"total", "order"} con un pedido ya creado "pendiente de
    comprobante" (los de antes del 16/10, 48 h).
    """
    from app.config import get_settings
    from app.services.order_service import get_order_service
    t = session.get("_transferencia") or {}
    if t.get("pedido") and time.time() - float(t.get("ts") or 0) < 48 * 3600:
        return {"total": t["pedido"]["total"], "pedido": t["pedido"]}
    svc = get_order_service(get_settings().redis_url)
    if t.get("order_id"):
        o = await svc.get(t["order_id"])
        if o and o.get("transferencia") == PENDIENTE:
            return {"total": o["total"], "order": o}
    try:
        limite = time.time() - 48 * 3600
        for o in await svc.list_all():
            if o.get("phone") != phone or o.get("transferencia") != PENDIENTE:
                continue
            try:
                creado = datetime.fromisoformat(o["created_at"]).timestamp()
            except Exception:
                creado = 0
            if creado >= limite:
                return {"total": o["total"], "order": o}
    except Exception as e:
        logger.debug(f"pedido_pendiente {phone}: {e}")
    return None


async def procesar_comprobante(deps: dict, phone: str, image_bytes: bytes, mime: str,
                               nombre: str = "") -> Optional[tuple[str, str]]:
    """
    Comprobante de un cliente con un pedido por transferencia pendiente.
    Devuelve (respuesta, intencion), o None si no hay pedido pendiente (sigue
    el flujo de siempre: lo verifica una persona).
    """
    from app.config import get_settings
    from app.services.order_service import get_order_service
    from app.services.checkout_helper import personalizar_nombre
    session = await deps["session"].get(phone)
    pend = await pedido_pendiente(session, phone)
    if not pend:
        return None
    cfg = await deps["config"].get_all()
    info = None
    try:
        info = await deps["image"].leer_comprobante(image_bytes, mime)
    except Exception as e:
        logger.warning(f"OCR de comprobante falló para {phone}: {e}")
    estado, motivo = clasificar(info, pend["total"], cfg)
    # Un mismo comprobante no paga dos pedidos: se controla siempre (antes
    # solo si todo lo demás cerraba, y el comprobante de María pasó a
    # verificar en otro pedido sin aviso, 16/10).
    op = operacion_de(info)
    if estado != "error" and op and not await deps["session"].adquirir_unico(
            f"trf_op:{op}", 120 * 24 * 3600):
        estado, motivo = "error", f"la operación {op} ya se usó en otro pedido"
    datos = {k: (info or {}).get(k) for k in ("importe", "fecha", "destinatario", "cuenta_destino",
                                               "operacion", "origen")}

    if estado == "error":
        # No corresponde al pedido: no se crea (si ya existía uno de antes,
        # queda "a verificar") y lo revisa una persona.
        if pend.get("order"):
            await get_order_service(get_settings().redis_url).actualizar(
                pend["order"]["order_id"], transferencia=A_VERIFICAR, comprobante=datos,
                transferencia_motivo=motivo)
        await deps["session"].set_estado(phone, "operador", motivo="comprobante_con_error")
        s = await deps["session"].get(phone)
        s["comprobante_info"] = {**datos, "motivo": motivo, "total_pedido": pend["total"],
                                 "pedido": pend.get("pedido"),
                                 "order_id": (pend.get("order") or {}).get("order_id")}
        await deps["session"].save(phone, s)
        logger.info(f"Comprobante con error, sin pedido: {phone} — {motivo}")
        resp = cfg.get("comprobante_error_message") or (
            "Revisé el comprobante y {motivo} 🙏 Te paso con alguien del equipo para "
            "revisarlo. En un momento te contactamos.")
        return personalizar_nombre(resp.replace("{motivo}", motivo), nombre), "transferencia_con_error"

    campos = {"transferencia": APROBADA if estado == "ok" else A_VERIFICAR, "comprobante": datos}
    if estado == "ok":
        campos["mp_payment_id"] = f"TRF-{datos.get('operacion')}"
    else:
        campos["transferencia_motivo"] = motivo
    svc = get_order_service(get_settings().redis_url)
    if pend.get("order"):
        order = await svc.actualizar(pend["order"]["order_id"], **campos)
    else:
        order = await _crear_pedido(phone, pend["pedido"], campos)
    s = await deps["session"].get(phone)
    s.pop("_transferencia", None)
    s["_ultimo_pedido"] = order["order_id"]
    await deps["session"].save(phone, s)

    if estado == "ok":
        try:
            from app.services.db import get_db
            from app.services.metrics_store import get_metrics_store
            await get_metrics_store(get_db(get_settings().database_url)).evento(
                "pago_aprobado", phone=phone, dato="transferencia", monto=order["total"],
                ref=order["order_id"], extra={"pasarela": "transferencia", "operacion": datos.get("operacion")})
        except Exception as e:
            logger.debug(f"evento pago_aprobado (transferencia): {e}")
        await deps["session"].set_estado(phone, "pedido_confirmado")
        if order.get("tipo_entrega") == "envio":
            entrega = f"🚚 Te lo enviamos a *{order.get('direccion_envio') or 'tu domicilio'}*. " \
                      "Nos comunicamos para coordinar la entrega."
        else:
            entrega = f"🔑 *Tu código de retiro es: {order.get('pickup_code')}*\n\nPresentalo al retirar."
        logger.info(f"Transferencia aprobada: {order['order_id']} {phone} op={datos.get('operacion')}")
        return (f"✅ *¡Recibimos tu transferencia!* 🙌\n\n*{order.get('sku_nombre')}* — "
                f"${float(order['total']):,.2f}\n{entrega}\n\n¡Muchas gracias! 💊"), "transferencia_aprobada"

    await deps["session"].set_estado(phone, "operador", motivo="comprobante_a_verificar")
    s = await deps["session"].get(phone)
    s["comprobante_info"] = {**datos, "motivo": motivo, "order_id": order["order_id"]}
    await deps["session"].save(phone, s)
    logger.info(f"Transferencia a verificar: {order['order_id']} {phone} — {motivo}")
    resp = cfg.get("comprobante_recibido_message") or (
        "¡Listo {nombre}! Recibimos tu comprobante 🙌 Lo verificamos y te confirmamos en un rato.")
    return personalizar_nombre(resp, nombre), "transferencia_a_verificar"
