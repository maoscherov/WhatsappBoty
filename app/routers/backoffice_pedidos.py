"""
El operador arma un pedido desde la conversación (1/10). Antes el backoffice
solo generaba un link de pago de un producto; ahora: varios productos (del
catálogo o monto libre), retiro o envío, y link, efectivo (cobra el mostrador
o el repartidor, con su nombre para conciliar la caja) o cuenta corriente
(también a quien no está en el padrón: el operador decide, y puede darlo de
alta como socio en el mismo paso).

Reusa el cierre de venta del bot: el pedido queda igual que uno del bot
(código de retiro, pantalla de pedidos, tablero).
"""

import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.routers.backoffice import _auth
from app.services.db import get_db
from app.services.session_service import get_session_service
from app.services.socio_service import get_socio_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bo")

PAGOS = ("link", "efectivo", "cuenta_corriente")


class ItemIn(BaseModel):
    sku_id: Optional[str] = None       # producto del catálogo…
    detalle: Optional[str] = None      # …o concepto libre con su monto
    monto: Optional[float] = None      # precio unitario (pisa el de lista)
    cantidad: int = 1


class AltaSocioIn(BaseModel):
    nombre: str
    apellido: str = ""
    dni: str = ""
    domicilio: str = ""
    nro_socio: str = ""


class PedidoIn(BaseModel):
    phone: str
    items: list[ItemIn]
    entrega: str = "retiro"                  # retiro | envio
    direccion: Optional[str] = None          # envío: si falta, el domicilio del socio
    pago: str = "link"                       # link | efectivo | cuenta_corriente
    cobra: Optional[str] = None              # efectivo: mostrador | repartidor
    repartidor: Optional[str] = None         # nombre, opcional (conciliar caja)
    aplicar_descuento: bool = True           # socio / empleado, como el bot
    mensaje: Optional[str] = None            # texto propio antes de la confirmación
    enviar: bool = True
    devolver_al_bot: bool = True
    agente: Optional[str] = None
    alta_socio: Optional[AltaSocioIn] = None


class AltaSocioReq(AltaSocioIn):
    phone: str


def _db():
    db = get_db(get_settings().database_url)
    if not db.available():
        raise HTTPException(status_code=503, detail="Base de datos no disponible")
    return db


async def _alta(phone: str, datos: AltaSocioIn) -> dict:
    from app.services.socio_service import SocioYaExiste, agregar_socio
    try:
        return await agregar_socio(_db(), get_socio_service(get_settings().socios_path), phone,
                                   datos.nombre, datos.apellido, datos.dni, datos.domicilio,
                                   datos.nro_socio)
    except SocioYaExiste as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/socios/alta")
async def bo_socio_alta(body: AltaSocioReq, _=Depends(_auth)):
    """Alta de un socio que no está en el padrón (el bot lo reconoce en el acto)."""
    socio = await _alta(body.phone, body)
    return {"ok": True, "socio": {k: socio.get(k) for k in
                                  ("nombre", "apellido", "nombre_pila", "nro_socio", "domicilio")}}


@router.post("/pedido")
async def bo_pedido(body: PedidoIn, _=Depends(_auth)):
    from app.routers.webhook import _deps
    from app.services.checkout_helper import (
        _cerrar_venta_cc, _cerrar_venta_efectivo, _clave_stock, aplicar_descuento_socio,
        costo_envio_de, crear_link_y_responder, domicilio_de)
    from app.services.message_store import guardar_historico

    # ── Validaciones ────────────────────────────────────────────────────────
    if not body.items:
        raise HTTPException(status_code=422, detail="Agregá al menos un producto")
    if body.entrega not in ("retiro", "envio"):
        raise HTTPException(status_code=422, detail="entrega: retiro | envio")
    if body.pago not in PAGOS:
        raise HTTPException(status_code=422, detail="pago: link | efectivo | cuenta_corriente")
    cobra = body.cobra or ("repartidor" if body.entrega == "envio" else "mostrador")
    if body.pago == "efectivo" and cobra not in ("mostrador", "repartidor"):
        raise HTTPException(status_code=422, detail="cobra: mostrador | repartidor")

    deps = _deps()
    phone = body.phone
    if body.alta_socio:
        await _alta(phone, body.alta_socio)
    socios = deps["socios"]
    direccion = (body.direccion or "").strip() or None
    if body.entrega == "envio" and not direccion:
        direccion = domicilio_de(phone, socios) or None
        if not direccion:
            raise HTTPException(status_code=422, detail="Falta la dirección de envío")

    # ── Productos ───────────────────────────────────────────────────────────
    cfg = await deps["config"].get_all()
    lineas: list[dict] = []
    for n, it in enumerate(body.items, start=1):
        cant = max(1, int(it.cantidad or 1))
        if it.sku_id:
            sku = deps["sku"].get_by_id(it.sku_id)
            if sku is None:
                raise HTTPException(status_code=404, detail=f"No existe el producto {it.sku_id}")
            precio = it.monto if it.monto and it.monto > 0 else sku.precio_venta
            if not precio or precio <= 0:
                raise HTTPException(status_code=422,
                                    detail=f"{sku.sku_nombre_original} no tiene precio: cargá el monto")
            lineas.append({"sku_id": sku.sku_id, "nombre": sku.sku_nombre_original or sku.sku_nombre,
                           "precio": float(precio), "cantidad": cant,
                           "requiere_receta": sku.requiere_receta,
                           "_precio_fijo": bool(it.monto and it.monto > 0)})
        else:
            if not (it.detalle or "").strip() or not it.monto or it.monto <= 0:
                raise HTTPException(status_code=422,
                                    detail="Un ítem libre necesita detalle y monto")
            lineas.append({"sku_id": f"LIBRE{n}", "nombre": it.detalle.strip(),
                           "precio": float(it.monto), "cantidad": cant,
                           "requiere_receta": "no", "_precio_fijo": True})

    # Descuento de socio / empleado (mismo criterio que el bot) sobre lo que
    # tiene precio de lista; lo que el operador fijó a mano no se toca.
    pct = 0.0
    if body.aplicar_descuento:
        de_lista = [l for l in lineas if not l["_precio_fijo"]]
        # Con receta también: el operador ya la validó, y la cotización de
        # receta del backoffice aplica el mismo descuento (Femiden 2/10).
        con_desc, pct = aplicar_descuento_socio(de_lista, phone, cfg, socios,
                                                incluir_receta=True)
        por_id = {l["sku_id"]: l for l in con_desc}
        lineas = [por_id.get(l["sku_id"], l) if not l["_precio_fijo"] else l for l in lineas]

    # ── Armar el pedido en la sesión (igual que el carrito del bot) ─────────
    ss = deps["session"]
    await ss.clear_pending(phone)
    primero, *resto = lineas
    await ss.set_pending(phone, primero["sku_id"], primero["nombre"], primero["precio"],
                         primero["cantidad"], opciones=[])
    for l in resto:
        await ss.agregar_item(phone, l["sku_id"], l["nombre"], l["precio"], l["cantidad"])
    session = await ss.get(phone)
    # El operador ya validó stock y receta: no se re-chequea en vivo.
    session["_stock_ok_at"] = time.time()
    session["_stock_ok_para"] = _clave_stock(session)
    session.pop("pago_metodo", None)
    await ss.save(phone, session)

    total_productos = round(sum(l["precio"] * l["cantidad"] for l in lineas), 2)
    costo_envio = costo_envio_de(cfg) if body.entrega == "envio" else 0.0
    extra = {"origen": "operador", "armado_por": body.agente or None}
    link = None
    if body.pago == "efectivo":
        extra.update({"cobra": cobra, "repartidor": (body.repartidor or "").strip() or None})
        respuesta = await _cerrar_venta_efectivo(
            ss, phone, session, body.entrega, direccion,
            total=round(total_productos + costo_envio, 2), costo_envio=costo_envio, cfg=cfg,
            extra_pedido=extra)
    elif body.pago == "cuenta_corriente":
        respuesta = await _cerrar_venta_cc(
            ss, phone, session, body.entrega, direccion,
            total=round(total_productos + costo_envio, 2), costo_envio=costo_envio,
            extra_pedido=extra)
    else:
        respuesta, link = await crear_link_y_responder(
            deps["payment"], ss, phone, session, body.entrega, direccion)
        if not link:
            raise HTTPException(status_code=502, detail=respuesta)

    texto = f"{body.mensaje.strip()}\n\n{respuesta}" if (body.mensaje or "").strip() else respuesta
    enviado = False
    if body.enviar:
        enviado = await deps["wa"].send_text(phone, texto)
        if not enviado:
            raise HTTPException(status_code=502, detail="WhatsApp no aceptó el mensaje")
        await ss.add_message(phone, "operator", texto)
        await guardar_historico(phone, "operator", texto, autor=body.agente)

    # ¿Sigue el bot o el operador?
    session = await ss.get(phone)
    if body.devolver_al_bot:
        for k in ("agente", "derivada_at", "derivada_motivo", "_handoff_avisado", "atendida_at"):
            session.pop(k, None)
    else:
        session["estado"] = "operador"
        if body.agente:
            session["agente"] = body.agente
    await ss.save(phone, session)

    try:
        await deps["metrics"].evento("pedido_operador", phone=phone, dato=body.pago,
                                     monto=round(total_productos + costo_envio, 2),
                                     ref=(body.agente or "")[:40] or None)
    except Exception:
        pass
    logger.info(f"Pedido armado por operador {body.agente or '?'} para {phone}: "
                f"{len(lineas)} ítems, {body.pago}, {body.entrega}")
    return {"ok": True, "pago": body.pago, "entrega": body.entrega,
            "total": round(total_productos + costo_envio, 2), "costo_envio": costo_envio,
            "descuento_pct": pct, "link": link, "pedido": session.get("_ultimo_pedido"),
            "mensaje": texto, "enviado": enviado}
