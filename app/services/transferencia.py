"""
Pago por TRANSFERENCIA (minuta 14/10, liberar el flujo).

1. El cliente elige transferencia con un pedido armado: el pedido se crea como
   "pendiente de comprobante" (la farmacia lo ve) y el bot pasa el alias
   (configurable en Comportamiento) con el importe exacto.
2. Llega el comprobante: se lee (importe, fecha, destinatario, N° de
   operación) y se aprueba solo si el importe coincide, el total es MENOR al
   monto máximo configurado (transferencia_auto_max, 0 = nunca), la fecha es
   de hoy o de ayer, el destinatario es nuestra cuenta y la operación no se
   usó antes. Si algo no cierra, el pedido queda "a verificar" y pasa a una
   persona con lo que se leyó.
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


def validar(info: Optional[dict], total: float, cfg: dict, hoy: Optional[date] = None) -> tuple[bool, str]:
    """(aprobado, motivo). Motivo legible para el operador cuando no aprueba."""
    if not info:
        return False, "no se pudo leer el comprobante"
    try:
        maximo = float(cfg.get("transferencia_auto_max") or 0)
    except (TypeError, ValueError):
        maximo = 0.0
    importe = importe_de(info.get("importe"))
    if importe is None:
        return False, "no se lee el importe"
    if abs(importe - float(total)) > 1.0:
        return False, f"el importe (${importe:,.2f}) no coincide con el pedido (${float(total):,.2f})"
    if maximo <= 0:
        return False, "la aprobación automática está desactivada"
    if float(total) >= maximo:
        return False, f"el pedido (${float(total):,.2f}) supera el máximo automático (${maximo:,.2f})"
    f = fecha_de(str(info.get("fecha") or ""))
    hoy = hoy or (datetime.utcnow() - timedelta(hours=3)).date()
    if f is None:
        return False, "no se lee la fecha"
    if f not in (hoy, hoy - timedelta(days=1)):
        return False, f"la fecha ({f:%d/%m/%Y}) no es de hoy ni de ayer"
    if not es_nuestra_cuenta(info, cfg):
        return False, "el destinatario no coincide con nuestra cuenta"
    if not str(info.get("operacion") or "").strip():
        return False, "no se lee el número de operación"
    return True, "ok"


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
    """Crea el pedido pendiente de comprobante y devuelve el mensaje con el alias."""
    from app.config import get_settings
    from app.services.order_service import get_order_service
    settings = get_settings()
    cfg = cfg or {}
    sku_id, nombre, cantidad = _nombre_pedido(session)
    order = await get_order_service(settings.redis_url).create(
        phone=phone, sku_id=sku_id, sku_nombre=nombre, cantidad=cantidad, total=total,
        mp_payment_id="", tipo_entrega=tipo_entrega, direccion_envio=direccion,
        pago="transferencia", extra={"transferencia": PENDIENTE})
    logger.info(f"Pedido por transferencia: {order['order_id']} phone={phone} total=${total:,.2f}")
    try:
        from app.services.db import get_db
        from app.services.metrics_store import get_metrics_store
        await get_metrics_store(get_db(settings.database_url)).evento(
            "pedido_transferencia", phone=phone, dato=tipo_entrega, monto=total, ref=order["order_id"])
    except Exception as e:
        logger.debug(f"evento pedido_transferencia: {e}")
    await session_svc.set_entrega(phone, tipo_entrega, direccion)
    await session_svc.set_estado(phone, "esperando_comprobante")
    s = await session_svc.get(phone)
    s["_transferencia"] = {"order_id": order["order_id"], "total": round(float(total), 2),
                           "ts": time.time()}
    s["_ultimo_pedido"] = order["order_id"]
    s.pop("pago_metodo", None)
    await session_svc.save(phone, s)
    envio = f" (incluye ${costo_envio:,.0f} de envío)" if costo_envio > 0 else ""
    return (f"✅ *¡Listo! Tomamos tu pedido* 🙌\n\n*{nombre}* — ${total:,.2f}{envio}\n\n"
            + mensaje_datos(cfg, total))


async def pedido_pendiente(session: dict, phone: str) -> Optional[dict]:
    """El pedido por transferencia que espera comprobante: el de la sesión, o
    (si la sesión se cerró) el último pendiente de ese teléfono en 48 h."""
    from app.config import get_settings
    from app.services.order_service import get_order_service
    svc = get_order_service(get_settings().redis_url)
    t = session.get("_transferencia") or {}
    if t.get("order_id"):
        o = await svc.get(t["order_id"])
        if o and o.get("transferencia") == PENDIENTE:
            return o
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
                return o
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
    session = await deps["session"].get(phone)
    order = await pedido_pendiente(session, phone)
    if not order:
        return None
    cfg = await deps["config"].get_all()
    info = None
    try:
        info = await deps["image"].leer_comprobante(image_bytes, mime)
    except Exception as e:
        logger.warning(f"OCR de comprobante falló para {phone}: {e}")
    ok, motivo = validar(info, order["total"], cfg)
    if ok:
        # Un mismo comprobante no paga dos pedidos.
        op = re.sub(r"\s+", "", str(info.get("operacion")))
        if not await deps["session"].adquirir_unico(f"trf_op:{op}", 120 * 24 * 3600):
            ok, motivo = False, f"la operación {op} ya se usó en otro pedido"
    svc = get_order_service(get_settings().redis_url)
    datos = {k: (info or {}).get(k) for k in ("importe", "fecha", "destinatario", "cuenta_destino",
                                               "operacion", "origen")}
    if ok:
        await svc.actualizar(order["order_id"], transferencia=APROBADA, comprobante=datos,
                             mp_payment_id=f"TRF-{datos.get('operacion')}")
        try:
            from app.services.db import get_db
            from app.services.metrics_store import get_metrics_store
            await get_metrics_store(get_db(get_settings().database_url)).evento(
                "pago_aprobado", phone=phone, dato="transferencia", monto=order["total"],
                ref=order["order_id"], extra={"pasarela": "transferencia", "operacion": datos.get("operacion")})
        except Exception as e:
            logger.debug(f"evento pago_aprobado (transferencia): {e}")
        await deps["session"].set_estado(phone, "pedido_confirmado")
        s = await deps["session"].get(phone)
        s.pop("_transferencia", None)
        await deps["session"].save(phone, s)
        if order.get("tipo_entrega") == "envio":
            entrega = f"🚚 Te lo enviamos a *{order.get('direccion_envio') or 'tu domicilio'}*. " \
                      "Nos comunicamos para coordinar la entrega."
        else:
            entrega = f"🔑 *Tu código de retiro es: {order.get('pickup_code')}*\n\nPresentalo al retirar."
        logger.info(f"Transferencia aprobada: {order['order_id']} {phone} op={datos.get('operacion')}")
        return (f"✅ *¡Recibimos tu transferencia!* 🙌\n\n*{order.get('sku_nombre')}* — "
                f"${float(order['total']):,.2f}\n{entrega}\n\n¡Muchas gracias! 💊"), "transferencia_aprobada"

    await svc.actualizar(order["order_id"], transferencia=A_VERIFICAR, comprobante=datos,
                         transferencia_motivo=motivo)
    await deps["session"].set_estado(phone, "operador", motivo="comprobante_a_verificar")
    s = await deps["session"].get(phone)
    s["comprobante_info"] = {**datos, "motivo": motivo, "order_id": order["order_id"]}
    await deps["session"].save(phone, s)
    logger.info(f"Transferencia a verificar: {order['order_id']} {phone} — {motivo}")
    from app.services.checkout_helper import personalizar_nombre
    resp = cfg.get("comprobante_recibido_message") or (
        "¡Listo {nombre}! Recibimos tu comprobante 🙌 Lo verificamos y te confirmamos en un rato.")
    return personalizar_nombre(resp, nombre), "transferencia_a_verificar"
