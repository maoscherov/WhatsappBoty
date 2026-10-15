"""
Estados por los que pasó cada conversación (15/10): bot atendiendo →
derivada (motivo) → tomada por X → devuelta al bot → link → venta → cerrada,
y un estado final para la lista del historial.

Sale de la tabla `eventos` (derivación, toma, devolución, link, pago, cierre)
y de los mensajes del operador. Cada evento se asigna a la conversación en
cuya ventana cae: desde su primer mensaje hasta el primero de la siguiente.
"""

from datetime import datetime, timedelta
from typing import Optional

_TIPOS = ("derivacion", "derivacion_atendida", "conversacion_tomada", "conversacion_devuelta",
          "link_enviado", "pedido_transferencia", "pago_aprobado", "pedido_operador", "pedido_efectivo",
          "fuera_horario", "conversacion_cerrada")

MOTIVOS = {
    "receta": "receta", "receta_foto": "receta (foto)", "receta_link": "receta (link)",
    "sin_stock": "sin stock / consulta", "encargo": "encargo", "pidio_humano": "pidió una persona",
    "no_entendido": "no entendió", "transferencia_efectivo": "pago por transferencia",
    "consulta_cuenta_corriente": "consulta de cuenta", "cuota_social": "cuota social",
    "tomada_por_operador": "la tomó un operador", "cambio_entrega": "cambio de entrega",
    "pide_foto": "pidió foto", "cliente_sin_respuesta": "el cliente esperaba respuesta",
    "error_bot": "error del bot", "cuenta_de_tercero": "pedido para otra cuenta",
    "cuenta_corriente_no_habilitada": "cuenta corriente no habilitada",
    "seguimiento_receta": "pregunta por su receta", "seguimiento_pedido": "pregunta por su pedido",
    "cambio_postventa": "cambio / devolución", "envio_especial": "envío especial",
    "derivacion_prometida": "el bot prometió una persona", "adjunto_no_reconocido": "archivo",
    "comprobante_a_verificar": "comprobante a verificar",
    "tintura_o_similar": "tintura / lo atiende una persona",
    "bono_foto": "bono", "credencial": "credencial", "comprobante": "comprobante",
    "operador_escribio": "el operador escribió", "conversacion_larga": "conversación larga",
}

FINALES = {
    "derivada_sin_atender": "Derivada sin atender",
    "venta": "Venta",
    "con_operador": "Con operador",
    "link_sin_pagar": "Pago pendiente (link o transferencia)",
    "atendida_devuelta": "Atendida y devuelta al bot",
    "cerrada_sin_respuesta": "Cerrada sin respuesta del cliente",
    "resuelta_bot": "Resuelta por el bot",
}

_MEDIOS = {"cuenta_corriente": "a cuenta corriente", "efectivo": "en efectivo",
           "transferencia": "por transferencia",
           "mercadopago": "pagada", "payway": "pagada"}


def _motivo(m: Optional[str]) -> str:
    return MOTIVOS.get(m or "", (m or "sin motivo").replace("_", " "))


def _iso(t) -> str:
    return t.isoformat() if hasattr(t, "isoformat") else str(t)


def armar(eventos: list[dict], operador_ts: list, desde, hasta=None) -> dict:
    """
    Línea de tiempo y estado final de UNA conversación.
    `eventos`: [{tipo, dato, ref, created_at}] ya filtrados a su ventana.
    `operador_ts`: horarios de los mensajes del operador en la ventana.
    """
    estados = [{"ts": _iso(desde), "estado": "bot", "etiqueta": "Bot atendiendo",
                "detalle": None, "agente": None}]
    fuera_avisado = False
    for e in sorted(eventos, key=lambda x: x["created_at"]):
        t, dato, ref = e["tipo"], e.get("dato"), e.get("ref")
        item = {"ts": _iso(e["created_at"]), "detalle": None, "agente": None}
        if t == "derivacion":
            item.update(estado="derivada", etiqueta="Derivada a una persona", detalle=_motivo(dato))
        elif t in ("conversacion_tomada", "derivacion_atendida"):
            # Las dos llegan juntas al tomar: una sola línea.
            if estados[-1]["estado"] == "tomada" and estados[-1]["agente"] == ref:
                continue
            item.update(estado="tomada", etiqueta=f"Tomada por {ref or 'un operador'}", agente=ref)
        elif t == "conversacion_devuelta":
            auto = (ref or "").lower().startswith("autom")
            item.update(estado="devuelta",
                        etiqueta="Devuelta al bot" + ("" if auto else f" por {ref or 'un operador'}"),
                        detalle="automática (nadie la tomó)" if auto else None,
                        agente=None if auto else ref)
        elif t == "link_enviado":
            item.update(estado="link", etiqueta="Link de pago enviado")
        elif t == "pedido_transferencia":
            item.update(estado="link", etiqueta="Pedido por transferencia",
                        detalle="esperando el comprobante")
        elif t in ("pago_aprobado", "pedido_efectivo", "pedido_operador"):
            if t == "pedido_efectivo":
                det = "en efectivo"
            elif t == "pedido_operador":
                det = f"armada por {ref or 'un operador'}" + (
                    f" ({_MEDIOS.get(dato, dato)})" if dato else "")
            else:
                det = _MEDIOS.get(dato or "", "pagada")
            # pago_aprobado (cuenta corriente) y pedido_operador llegan juntos.
            if estados[-1]["estado"] == "venta" and t == "pedido_operador":
                estados[-1]["detalle"] = det
                estados[-1]["agente"] = ref
                continue
            item.update(estado="venta", etiqueta="Venta", detalle=det,
                        agente=ref if t == "pedido_operador" else None)
        elif t == "fuera_horario":
            if fuera_avisado:
                continue
            fuera_avisado = True
            item.update(estado="fuera_horario", etiqueta="Fuera de horario")
        elif t == "conversacion_cerrada":
            det = {"silencio": "en silencio", "avisar": "con aviso al cliente",
                   "derivar": "el cliente esperaba: se derivó"}.get(dato or "", dato)
            item.update(estado="cerrada", etiqueta="Cerrada por inactividad", detalle=det)
        else:
            continue
        estados.append(item)
    return {"estados": estados, **final(estados, operador_ts)}


def final(estados: list[dict], operador_ts: list) -> dict:
    cods = [e["estado"] for e in estados]
    op_ts = sorted(_iso(t) for t in operador_ts or [])
    # Última derivación: ¿alguien la atendió (tomó o escribió) después?
    i_der = max((i for i, c in enumerate(cods) if c == "derivada"), default=None)
    if i_der is not None:
        ts_der = estados[i_der]["ts"]
        despues = cods[i_der + 1:]
        atendida = "tomada" in despues or any(t >= ts_der for t in op_ts)
        if not atendida and "venta" not in despues and "devuelta" not in despues:
            return _f("derivada_sin_atender")
    if "venta" in cods:
        return _f("venta")
    i_tom = max((i for i, c in enumerate(cods) if c == "tomada"), default=None)
    if i_tom is not None and "devuelta" not in cods[i_tom + 1:]:
        return _f("con_operador")
    if "link" in cods:
        return _f("link_sin_pagar")
    if "devuelta" in cods or "tomada" in cods or op_ts:
        return _f("atendida_devuelta")
    cierre = next((e for e in reversed(estados) if e["estado"] == "cerrada"), None)
    if cierre and cierre.get("detalle") == "con aviso al cliente":
        return _f("cerrada_sin_respuesta")
    return _f("resuelta_bot")


def _f(codigo: str) -> dict:
    return {"estado_final": codigo, "estado_final_etiqueta": FINALES[codigo]}


async def por_conversaciones(db, phones: list[str], convs: list[dict]) -> dict:
    """
    {conversacion_id: {estados, estado_final, estado_final_etiqueta}} para las
    conversaciones dadas ([{id, phone, desde, hasta}]). Dos queries en total.
    La ventana de cada una va hasta el inicio de la siguiente del mismo
    teléfono (o 6 h después de su último mensaje si es la última).
    """
    if not convs or db is None or not db.available():
        return {}
    inicio = min(c["desde"] for c in convs) - timedelta(seconds=10)
    eventos = await db.fetch(
        "SELECT phone, tipo, dato, ref, created_at FROM eventos "
        "WHERE phone = ANY($1::text[]) AND tipo = ANY($2::text[]) AND created_at >= $3 "
        "ORDER BY created_at", list(set(phones)), list(_TIPOS), inicio) or []
    ops = await db.fetch(
        "SELECT phone, created_at FROM messages WHERE phone = ANY($1::text[]) "
        "AND role = 'operator' AND created_at >= $2", list(set(phones)), inicio) or []
    por_phone: dict[str, list[dict]] = {}
    for c in convs:
        por_phone.setdefault(c["phone"], []).append(c)
    salida = {}
    for phone, lista in por_phone.items():
        lista.sort(key=lambda c: c["desde"])
        evs = [dict(e) for e in eventos if e["phone"] == phone]
        op = [o["created_at"] for o in ops if o["phone"] == phone]
        for i, c in enumerate(lista):
            ini = c["desde"] - timedelta(seconds=10)
            fin = lista[i + 1]["desde"] - timedelta(seconds=10) if i + 1 < len(lista) \
                else (c.get("hasta") or c["desde"]) + timedelta(hours=6)
            salida[c["id"]] = armar([e for e in evs if ini <= e["created_at"] < fin],
                                    [t for t in op if ini <= t < fin], c["desde"])
    return salida


async def ultima_conversacion_por_phone(db, phones: list[str]) -> list[dict]:
    """Última conversación de cada teléfono: [{id, phone, desde, hasta}]."""
    if not phones or db is None or not db.available():
        return []
    filas = await db.fetch(
        """SELECT m.phone, m.conversacion_id AS id, MIN(m.created_at) AS desde,
                  MAX(m.created_at) AS hasta
             FROM messages m
             JOIN (SELECT phone, MAX(conversacion_id) AS cid FROM messages
                    WHERE phone = ANY($1::text[]) AND conversacion_id IS NOT NULL
                    GROUP BY phone) u ON u.phone = m.phone AND u.cid = m.conversacion_id
            GROUP BY m.phone, m.conversacion_id""", list(set(phones))) or []
    return [dict(f) for f in filas]


async def conversaciones_de_mensajes(db, phone: str, mensajes: list[dict]) -> list[dict]:
    """Para el historial de un cliente: las conversaciones de los mensajes
    mostrados, cada una con su línea de tiempo y estado final."""
    rangos: dict[int, dict] = {}
    for m in mensajes:
        cid = m.get("conversacion_id")
        if cid is None:
            continue
        ts = datetime.fromisoformat(m["ts"]) if isinstance(m["ts"], str) else m["ts"]
        r = rangos.setdefault(cid, {"id": cid, "phone": phone, "desde": ts, "hasta": ts})
        r["desde"], r["hasta"] = min(r["desde"], ts), max(r["hasta"], ts)
    if not rangos:
        return []
    est = await por_conversaciones(db, [phone], list(rangos.values()))
    return [{"conversacion": f"C-{cid}", "conversacion_id": cid,
             "desde": _iso(r["desde"]), "hasta": _iso(r["hasta"]), **est.get(cid, {})}
            for cid, r in sorted(rangos.items())]
