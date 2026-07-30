"""
Payway: página de pago hosteada + cobro (flujo estándar Decidir v2).

Flujo:
  1. crear_pago_pendiente() guarda en Redis los datos (monto, phone, sku) y
     devuelve la URL /pay/{id}. El bot manda esa URL por WhatsApp.
  2. GET /pay/{id}  → página con formulario de tarjeta. Tokeniza en el navegador
     contra Decidir (public key) y postea el token a /payway/charge.
  3. POST /payway/charge → cobra con la private key. Si aprueba, crea el pedido.
  4. GET /payway/test/{monto} → crea un pago de prueba y devuelve el link (sandbox).
"""

import json
import logging
import uuid
from datetime import datetime, timezone

import httpx
import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.config import get_settings
from app.services.payway_service import get_payway_service
from app.services.payway_link import crear_pago_pendiente, PENDING_TTL as _PENDING_TTL
from app.services.order_service import get_order_service
from app.services.whatsapp_service import get_whatsapp_service

logger = logging.getLogger(__name__)
router = APIRouter()


def _redis():
    return aioredis.from_url(get_settings().redis_url, decode_responses=True)


def _bo_ok(key: str) -> bool:
    """
    Valida la BO_KEY tolerando el problema clásico de URL: el navegador
    convierte '+' en espacio dentro del query string. Aceptamos la key tal
    cual y también con los espacios revertidos a '+'.
    """
    bo = get_settings().bo_key
    if not bo or not key:
        return False
    return key == bo or key.replace(" ", "+") == bo


async def _get_pending(pid: str) -> dict | None:
    try:
        raw = await _redis().get(f"payway:pending:{pid}")
        return json.loads(raw) if raw else None
    except Exception:
        return None


# ── Página de pago ──────────────────────────────────────────────────────────────

def _status_page(variante: str, titulo: str, sub: str) -> str:
    """Página de estado con la identidad Remedia. variante: ok | warn | err."""
    iconos = {
        "ok": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>',
        "warn": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>',
        "err": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>',
    }
    return (_STATUS_HTML
            .replace("{{VARIANTE}}", variante)
            .replace("{{ICONO}}", iconos.get(variante, iconos["warn"]))
            .replace("{{TITULO}}", titulo)
            .replace("{{SUB}}", sub))


@router.get("/pay/{pid}", response_class=HTMLResponse)
async def pay_page(pid: str):
    settings = get_settings()
    pending = await _get_pending(pid)
    if not pending:
        return HTMLResponse(_status_page("warn", "Link de pago vencido",
                                         "Este link ya no está disponible. Volvé al chat de WhatsApp y pedí uno nuevo."),
                            status_code=404)
    if pending.get("estado") == "aprobado":
        return HTMLResponse(_status_page("ok", "Este pago ya fue realizado",
                                         "Recibimos tu pago correctamente. Revisá WhatsApp: te enviamos el código de retiro. ¡Gracias!"))

    pw = get_payway_service(settings.payway_public_key, settings.payway_private_key,
                            settings.payway_sandbox, settings.payway_site_id,
                            settings.payway_template_id, settings.payway_cybersource)
    total = float(pending["total"])
    nombre = pending["sku_nombre"]
    return HTMLResponse(_PAY_HTML
                        .replace("{{PID}}", pid)
                        .replace("{{NOMBRE}}", nombre)
                        .replace("{{TOTAL}}", f"{total:,.2f}")
                        .replace("{{TOKENS_URL}}", f"{pw.base_url}/tokens")
                        .replace("{{PUBLIC_KEY}}", pw.public_key)
                        .replace("{{CS_ORG_ID}}", settings.payway_cs_org_id)
                        .replace("{{CS_MERCHANT_ID}}", settings.payway_cs_merchant_id))


# ── Cobro ────────────────────────────────────────────────────────────────────────

class ChargeIn(BaseModel):
    pid: str
    token: str
    bin: str
    payment_method_id: int = 1   # 1 = Visa (default sandbox)
    installments: int = 1
    device_id: str = ""          # fingerprint generado al tokenizar (Cybersource)


@router.post("/payway/charge")
async def payway_charge(body: ChargeIn):
    settings = get_settings()
    pending = await _get_pending(body.pid)
    if not pending:
        raise HTTPException(status_code=404, detail="Pago no encontrado o vencido")
    if pending.get("estado") == "aprobado":
        return {"status": "approved", "ya_pagado": True}

    pw = get_payway_service(settings.payway_public_key, settings.payway_private_key,
                            settings.payway_sandbox, settings.payway_site_id,
                            settings.payway_template_id, settings.payway_cybersource)
    data, err = await pw.crear_pago(
        token=body.token,
        amount=float(pending["total"]),
        site_transaction_id=f"{body.pid}-{int(datetime.now().timestamp())}",
        payment_method_id=body.payment_method_id,
        bin=body.bin,
        installments=body.installments,
        device_id=body.device_id,
        producto=pending.get("sku_nombre", "Producto"),
    )
    if err or not data:
        logger.error(f"Payway charge falló: {err}")
        return {"status": "error", "detail": err or "sin respuesta"}

    estado = data.get("status")   # "approved" | "rejected" | ...
    if estado == "approved":
        pending["estado"] = "aprobado"
        pending["payway_id"] = data.get("id")
        await _redis().setex(f"payway:pending:{body.pid}", _PENDING_TTL, json.dumps(pending))
        # Crear el pedido + avisar por WhatsApp + actualizar sesión (mismo flujo que MP)
        try:
            from app.services.session_service import get_session_service
            from app.services.config_service import get_config_service

            phone = pending["phone"]
            nombre_producto = pending["sku_nombre"]

            session_svc = get_session_service(settings.redis_url)
            session = await session_svc.get(phone)
            tipo_entrega = session.get("tipo_entrega") or "retiro"
            direccion_envio = session.get("direccion_envio")

            order_svc = get_order_service(settings.redis_url)
            order = await order_svc.create(
                phone=phone, sku_id=pending["sku_id"],
                sku_nombre=nombre_producto, cantidad=int(pending["cantidad"]),
                total=float(pending["total"]), mp_payment_id=str(data.get("id")),
                tipo_entrega=tipo_entrega, direccion_envio=direccion_envio,
            )
            logger.info(f"Pedido registrado (Payway): {order['order_id']} entrega={tipo_entrega}")

            cfg_svc = get_config_service(settings.redis_url)
            cfg = await cfg_svc.get_all()
            hours = await cfg_svc.get_hours()
            pickup_minutes = int(cfg.get("pickup_minutes") or settings.pickup_minutes)
            pickup_text = cfg_svc.get_pickup_text(hours, pickup_minutes)
            pickup_code = order.get("pickup_code", "")
            pickup_line = f"\n{pickup_text}" if pickup_text else ""

            if tipo_entrega == "envio":
                dir_txt = f" a *{direccion_envio}*" if direccion_envio else ""
                mensaje = (
                    f"✅ *¡Pago confirmado!*\n\n"
                    f"Recibimos tu pago de *{nombre_producto}*. 🙌\n"
                    f"🚚 Te lo enviamos a domicilio{dir_txt}. Nos comunicamos para coordinar la entrega.\n"
                    f"📋 Código de pedido: *{pickup_code}*\n\n"
                    f"¡Muchas gracias! 💊"
                )
            else:
                mensaje = (
                    f"✅ *¡Pago confirmado!*\n\n"
                    f"Recibimos tu pago de *{nombre_producto}*. 🙌\n"
                    f"🔑 *Tu código de retiro es: {pickup_code}*{pickup_line}\n\n"
                    f"Guardalo para presentarlo al retirar. ¡Muchas gracias! 💊"
                )

            wa = get_whatsapp_service(settings.whatsapp_token, settings.whatsapp_phone_number_id)
            await wa.send_text(phone, mensaje)
            await session_svc.set_estado(phone, "pedido_confirmado")
            await session_svc.add_message(phone, "assistant", mensaje)
        except Exception as e:
            logger.error(f"Post-pago Payway: {e}")
        return {"status": "approved"}

    return {"status": estado or "rejected", "detail": data.get("status_details") or data}


# ── Test rápido (sandbox) ────────────────────────────────────────────────────────

@router.get("/payway/test/{monto}")
async def payway_test(monto: float):
    """Formulario propio de prueba (flujo tokenize+charge)."""
    url = await crear_pago_pendiente(phone="549000000000", sku_id="TEST",
                                     sku_nombre="Producto de prueba", cantidad=1, total=monto)
    return {"pay_url": url}


@router.get("/payway/config-check")
async def payway_config_check(key: str = ""):
    """Muestra la config Payway cargada (claves enmascaradas). Requiere ?key=BO_KEY."""
    settings = get_settings()
    if not _bo_ok(key):
        raise HTTPException(status_code=403, detail="key inválida")

    def _mask(s: str) -> dict:
        s = s or ""
        return {"len": len(s), "preview": (s[:4] + "…" + s[-4:]) if len(s) >= 8 else ("(vacío)" if not s else "***"),
                "espacios": s != s.strip()}

    pw = get_payway_service(settings.payway_public_key, settings.payway_private_key,
                            settings.payway_sandbox, settings.payway_site_id,
                            settings.payway_template_id, settings.payway_cybersource)
    return {
        "sandbox": settings.payway_sandbox,
        "base_pagos": pw.base_url,
        "tokens_url": f"{pw.base_url}/tokens",
        "cybersource": settings.payway_cybersource,
        "cs_org_id": settings.payway_cs_org_id or "(vacío)",
        "site_id": settings.payway_site_id or "(vacío)",
        "public_key": _mask(settings.payway_public_key),
        "private_key": _mask(settings.payway_private_key),
        "public_base_url": settings.public_base_url or "(vacío)",
    }


@router.get("/payway/key-test")
async def payway_key_test(key: str = ""):
    """
    Prueba ambas claves contra /tokens con una tarjeta dummy para detectar
    cuál autentica (y si están cruzadas). Requiere ?key=BO_KEY.
    - 'auth_error'  → esa clave NO es válida para tokenizar.
    - 'card_valida' → esa clave SÍ autentica (el error es de datos de tarjeta).
    """
    settings = get_settings()
    if not _bo_ok(key):
        raise HTTPException(status_code=403, detail="key inválida")
    pw = get_payway_service(settings.payway_public_key, settings.payway_private_key,
                            settings.payway_sandbox, settings.payway_site_id,
                            settings.payway_template_id, settings.payway_cybersource)
    tokens_url = f"{pw.base_url}/tokens"
    dummy = {
        "card_number": "4507990000004905", "card_expiration_month": "08",
        "card_expiration_year": "28", "security_code": "123", "card_holder_name": "TEST",
        "card_holder_identification": {"type": "dni", "number": "12345678"},
    }

    async def _probe(apikey: str) -> dict:
        async with httpx.AsyncClient() as client:
            try:
                r = await client.post(tokens_url, headers={"apikey": apikey, "Content-Type": "application/json"},
                                      json=dummy, timeout=15)
                body = r.text[:200]
                low = body.lower()
                if "invalid authentication" in low or r.status_code in (401, 403):
                    verdict = "auth_error (clave NO válida para tokenizar)"
                elif r.status_code in (200, 201) or "id" in low or "param" in low or "card" in low:
                    verdict = "AUTENTICA ✓ (llega a validar la tarjeta)"
                else:
                    verdict = f"otro (status {r.status_code})"
                return {"status": r.status_code, "verdict": verdict, "body": body}
            except Exception as e:
                return {"error": str(e)}

    return {
        "tokens_url": tokens_url,
        "campo_public_key": await _probe(settings.payway_public_key),
        "campo_private_key": await _probe(settings.payway_private_key),
    }


@router.get("/payway/recent")
async def payway_recent(key: str = ""):
    """Lista los pagos Payway recientes (pendientes y aprobados) con su ID. Requiere ?key=BO_KEY."""
    if not _bo_ok(key):
        raise HTTPException(status_code=403, detail="key inválida")
    out = []
    try:
        r = _redis()
        async for k in r.scan_iter("payway:pending:*", count=200):
            raw = await r.get(k)
            if not raw:
                continue
            d = json.loads(raw)
            out.append({
                "pid": d.get("id"), "estado": d.get("estado"),
                "payway_id": d.get("payway_id"), "total": d.get("total"),
                "producto": d.get("sku_nombre"), "phone": d.get("phone"),
                "created_at": d.get("created_at"),
            })
    except Exception as e:
        return {"ok": False, "error": str(e)}
    out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return {"ok": True, "pagos": out[:30]}


@router.get("/payway/refund/{payment_id}")
async def payway_refund(payment_id: str, key: str = "", monto: float = 0):
    """
    Anula/reembolsa un pago por su ID de Payway. Requiere ?key=BO_KEY.
    Sin `monto` → reembolso total (POST sin body). Con `monto` → parcial (body JSON).
    """
    settings = get_settings()
    if not _bo_ok(key):
        raise HTTPException(status_code=403, detail="key inválida")
    pw = get_payway_service(settings.payway_public_key, settings.payway_private_key,
                            settings.payway_sandbox, settings.payway_site_id,
                            settings.payway_template_id, settings.payway_cybersource)
    data, err = await pw.refund(payment_id, amount=monto)
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "refund": data}


@router.get("/payway/status/{payment_id}")
async def payway_status(payment_id: str, key: str = ""):
    """Consulta el estado de un pago. Requiere ?key=BO_KEY."""
    settings = get_settings()
    if not _bo_ok(key):
        raise HTTPException(status_code=403, detail="key inválida")
    pw = get_payway_service(settings.payway_public_key, settings.payway_private_key,
                            settings.payway_sandbox, settings.payway_site_id,
                            settings.payway_template_id, settings.payway_cybersource)
    data = await pw.get_payment(payment_id)
    return {"ok": bool(data), "payment": data}


@router.get("/payway/link-test/{monto}")
async def payway_link_test(monto: float):
    """Prueba del GenerateLink (checkout HOSTEADO de Payway) — devuelve la URL o el error."""
    settings = get_settings()
    pw = get_payway_service(settings.payway_public_key, settings.payway_private_key,
                            settings.payway_sandbox, settings.payway_site_id,
                            settings.payway_template_id, settings.payway_cybersource)
    base = settings.public_base_url.rstrip("/")
    link, err = await pw.crear_link(
        total=monto,
        site_transaction_id=f"test-{uuid.uuid4().hex[:10]}",
        success_url=f"{base}/payway/return?r=ok",
        cancel_url=f"{base}/payway/return?r=cancel",
        notifications_url=f"{base}/payway/notification",
    )
    if err:
        return {"ok": False, "error": err, "site_id": settings.payway_site_id, "template_id": settings.payway_template_id}
    return {"ok": True, "checkout_url": link}


@router.get("/payway/return")
async def payway_return(r: str = ""):
    if r == "ok":
        return HTMLResponse(_status_page("ok", "¡Pago realizado!",
                                         "Ya podés cerrar esta página. Te enviamos la confirmación por WhatsApp."))
    return HTMLResponse(_status_page("err", "Pago cancelado",
                                     "No se realizó ningún cobro. Podés volver a intentar desde el link del chat."))


@router.post("/payway/notification")
async def payway_notification(payload: dict):
    """Notificación de Payway (webhook). Por ahora solo loguea para ver el formato."""
    logger.info(f"Payway notification: {payload}")
    return {"status": "ok"}


# ── Identidad visual Remedia (compartida por la página de pago y las de estado) ──
_BRAND_HEAD = """<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:wght@600;700;800&family=Instrument+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --verde:#0E8F5F; --verde-osc:#0B6E49; --verde-prof:#0A3D2A;
    --crema:#F4F9F5; --tinta:#152B20; --gris:#5E7268;
    --borde:#D9E7DD; --blanco:#FFFFFF; --error:#C93A3A; --warn:#B07C1F;
  }
  *{box-sizing:border-box}
  body{
    font-family:'Instrument Sans',-apple-system,Segoe UI,sans-serif;
    background:var(--crema);
    background-image:radial-gradient(circle at 15% 0%, rgba(14,143,95,.09), transparent 45%),
                     radial-gradient(circle at 100% 90%, rgba(14,143,95,.07), transparent 40%);
    color:var(--tinta); margin:0; min-height:100vh; min-height:100dvh;
    display:flex; flex-direction:column; align-items:center; padding:28px 16px 40px;
  }
  .marca{display:flex;align-items:center;gap:10px;margin-bottom:22px;animation:bajar .5s ease both}
  .logo{width:38px;height:38px;border-radius:11px;background:var(--verde);color:#fff;
    display:flex;align-items:center;justify-content:center;
    font-family:'Bricolage Grotesque',sans-serif;font-weight:800;font-size:21px;
    box-shadow:0 6px 16px rgba(14,143,95,.35)}
  .wordmark{font-family:'Bricolage Grotesque',sans-serif;font-size:22px;font-weight:600;letter-spacing:-.02em}
  .wordmark b{font-weight:800;color:var(--verde)}
  .card{
    background:var(--blanco); border:1px solid var(--borde); border-radius:22px;
    padding:26px 24px 24px; max-width:420px; width:100%;
    box-shadow:0 18px 45px -18px rgba(10,61,42,.25), 0 2px 8px rgba(10,61,42,.06);
    animation:subir .55s ease .08s both;
  }
  .pie{margin-top:18px;display:flex;align-items:center;gap:7px;color:var(--gris);font-size:12.5px;animation:subir .55s ease .16s both}
  .pie svg{width:14px;height:14px;flex:none}
  @keyframes subir{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
  @keyframes bajar{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}}
  @media (prefers-reduced-motion: reduce){*{animation:none!important;transition:none!important}}
</style>"""

_MARCA_HTML = """<div class="marca"><div class="logo">R</div><div class="wordmark">Remedi<b>IA</b></div></div>"""

_PIE_HTML = """<div class="pie">
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="10" width="16" height="11" rx="2.5"/><path d="M8 10V7a4 4 0 118 0v3"/></svg>
Pago seguro procesado por Payway · Farmacia Mutual Independencia</div>"""


# ── HTML de la página de pago (tokeniza en el navegador, cobra en el backend) ──────
_PAY_HTML = """<!doctype html><html lang="es"><head>
<title>Pagar · Remedia</title>
""" + _BRAND_HEAD + """
<style>
  .prod{color:var(--gris);font-size:14px;margin-bottom:2px}
  .total{font-family:'Bricolage Grotesque',sans-serif;font-size:38px;font-weight:800;
    letter-spacing:-.03em;color:var(--verde-prof);margin-bottom:4px}
  .total small{font-size:20px;font-weight:700;color:var(--verde);vertical-align:6px;margin-right:2px}
  .divisor{height:1px;background:var(--borde);margin:16px -24px 6px}
  label{display:block;font-size:12.5px;font-weight:600;color:var(--gris);margin:13px 0 5px;letter-spacing:.01em}
  input{width:100%;background:var(--crema);border:1.5px solid var(--borde);border-radius:12px;
    padding:12px 13px;color:var(--tinta);font-size:16px;font-family:inherit;transition:border-color .15s, box-shadow .15s}
  input::placeholder{color:#A9BCB0}
  input:focus{outline:none;border-color:var(--verde);box-shadow:0 0 0 3px rgba(14,143,95,.18);background:#fff}
  input:focus-visible{border-color:var(--verde)}
  .row{display:flex;gap:12px}.row>div{flex:1}
  .cvvwrap{position:relative}
  .cvvwrap input{padding-right:42px}
  .cvvwrap button{position:absolute;right:5px;top:50%;transform:translateY(-50%);
    background:none;border:none;color:var(--gris);cursor:pointer;width:32px;height:32px;
    margin:0;padding:5px;border-radius:8px;display:flex;align-items:center;justify-content:center}
  .cvvwrap button:hover{background:rgba(14,143,95,.1);color:var(--verde-osc)}
  .cvvwrap button svg{width:19px;height:19px}
  .btn{width:100%;margin-top:20px;background:var(--verde);color:#fff;border:none;border-radius:13px;
    padding:15px;font-size:16.5px;font-weight:700;font-family:inherit;cursor:pointer;
    box-shadow:0 8px 20px -8px rgba(14,143,95,.55);transition:background .15s, transform .1s}
  .btn:hover{background:var(--verde-osc)}
  .btn:active{transform:scale(.985)}
  .btn:disabled{opacity:.55;cursor:default;transform:none}
  .btn:focus-visible{outline:3px solid rgba(14,143,95,.4);outline-offset:2px}
  #msg{margin-top:14px;font-size:14px;text-align:center;line-height:1.45;border-radius:10px;padding:0}
  #msg.err{background:#FBEDED;color:var(--error);padding:10px 12px}
  #msg.info{color:var(--gris)}
  #done{display:none;text-align:center;padding:14px 0 6px}
  #done .tilde{width:64px;height:64px;border-radius:50%;background:var(--verde);color:#fff;
    display:flex;align-items:center;justify-content:center;margin:0 auto 16px;
    box-shadow:0 10px 26px -8px rgba(14,143,95,.6)}
  #done .tilde svg{width:32px;height:32px}
  #done h2{font-family:'Bricolage Grotesque',sans-serif;font-size:23px;font-weight:700;margin:0 0 8px;color:var(--verde-prof)}
  #done p{color:var(--gris);font-size:14.5px;line-height:1.55;margin:0}
</style></head><body>
""" + _MARCA_HTML + """
<div class="card">
  <div id="formwrap">
    <div class="prod">{{NOMBRE}}</div>
    <div class="total"><small>$</small>{{TOTAL}}</div>
    <div class="divisor"></div>
    <label for="num">Número de tarjeta</label>
    <input id="num" inputmode="numeric" placeholder="0000 0000 0000 0000" maxlength="23" autocomplete="cc-number">
    <div class="row">
      <div><label for="exp">Vencimiento (MM/AA)</label>
        <input id="exp" inputmode="numeric" placeholder="08/28" maxlength="5" autocomplete="cc-exp"></div>
      <div><label for="cvv">Código de seguridad</label>
        <div class="cvvwrap">
          <input id="cvv" type="password" inputmode="numeric" placeholder="•••" maxlength="4" autocomplete="cc-csc">
          <button type="button" id="cvveye" onclick="toggleCvv()" aria-label="Mostrar código">
            <svg id="eyeon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg>
            <svg id="eyeoff" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:none"><path d="M17.9 17.9A9.9 9.9 0 0112 19c-6.5 0-10-7-10-7a17.4 17.4 0 014.1-4.9M9.9 4.24A9.1 9.1 0 0112 4c6.5 0 10 7 10 7a17.5 17.5 0 01-2.2 3.2M14.12 14.12a3 3 0 11-4.24-4.24"/><path d="M2 2l20 20"/></svg>
          </button>
        </div>
      </div>
    </div>
    <label for="name">Nombre en la tarjeta</label>
    <input id="name" placeholder="Como figura en el plástico" autocomplete="cc-name">
    <div class="row">
      <div><label for="dtype">Tipo de documento</label><input id="dtype" value="dni"></div>
      <div><label for="dnum">Número de documento</label><input id="dnum" inputmode="numeric" placeholder="12345678"></div>
    </div>
    <button class="btn" id="btn" onclick="pagar()">Pagar $ {{TOTAL}}</button>
    <div id="msg" role="status"></div>
  </div>
  <div id="done">
    <div class="tilde"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg></div>
    <h2>¡Pago aprobado!</h2>
    <p>Recibimos tu pago de <b>{{NOMBRE}}</b>.<br>Te enviamos la confirmación y el código por WhatsApp.<br>Ya podés cerrar esta página.</p>
  </div>
</div>
""" + _PIE_HTML + """
<script>
const TOKENS_URL="{{TOKENS_URL}}", PUBLIC_KEY="{{PUBLIC_KEY}}", PID="{{PID}}",
      CS_ORG_ID="{{CS_ORG_ID}}", CS_MERCHANT_ID="{{CS_MERCHANT_ID}}";
// Fingerprint de dispositivo (Cybersource). Id único por pago.
// El script de online-metrix lleva session_id = <merchant_id><identificador>;
// a la API de Decidir se manda solo el identificador (DEVICE_ID).
const DEVICE_ID = (PID + Math.random().toString(36).slice(2,10)).slice(0,32);
if(CS_ORG_ID){
  const SESSION = CS_MERCHANT_ID + DEVICE_ID;
  const s=document.createElement('script');
  s.src="https://h.online-metrix.net/fp/tags.js?org_id="+CS_ORG_ID+"&session_id="+SESSION;
  s.async=true; document.head.appendChild(s);
  const n=document.createElement('noscript');
  n.innerHTML='<iframe style="width:100px;height:100px;border:0;position:absolute;top:-5000px;" '+
    'src="https://h.online-metrix.net/fp/tags?org_id='+CS_ORG_ID+'&session_id='+SESSION+'"></iframe>';
  document.body.appendChild(n);
}
function msg(t,tipo){const m=document.getElementById('msg');m.textContent=t;m.className=tipo||'info';}
// Número de tarjeta: espacio cada 4 dígitos.
document.getElementById('num').addEventListener('input',function(){
  const d=this.value.replace(/\\D/g,'').slice(0,19);
  this.value=d.replace(/(.{4})/g,'$1 ').trim();
});
// Autoformato del vencimiento: inserta la barra sola (08 → 08/) y solo admite dígitos.
document.getElementById('exp').addEventListener('input',function(e){
  let d=this.value.replace(/\\D/g,'').slice(0,4);
  if(d.length===1 && Number(d)>1) d='0'+d;                 // "9" → "09"
  if(d.length>=3 || (d.length===2 && e.inputType!=='deleteContentBackward'))
    this.value=d.slice(0,2)+'/'+d.slice(2);
  else this.value=d;
});
function toggleCvv(){
  const c=document.getElementById('cvv'), b=document.getElementById('cvveye');
  const ver=c.type==='password';
  c.type=ver?'text':'password';
  document.getElementById('eyeon').style.display=ver?'none':'block';
  document.getElementById('eyeoff').style.display=ver?'block':'none';
  b.setAttribute('aria-label',ver?'Ocultar código':'Mostrar código');
}
async function pagar(){
  const btn=document.getElementById('btn'); btn.disabled=true; msg('Procesando el pago…','info');
  const num=document.getElementById('num').value.replace(/\\s/g,'');
  let [mm,aa]=(document.getElementById('exp').value||'').split(/[\\/\\-\\s]+/).map(s=>s.trim());
  mm=(mm||'').padStart(2,'0').slice(0,2);        // mes 2 dígitos
  aa=(aa||'').replace(/\\D/g,'').slice(-2);        // año a 2 dígitos (28, no 2028)
  if(!mm||mm==='00'||aa.length!==2){msg('Revisá el vencimiento (MM/AA).','err');btn.disabled=false;return;}
  try{
    const tk=await fetch(TOKENS_URL,{method:'POST',headers:{'Content-Type':'application/json','apikey':PUBLIC_KEY},
      body:JSON.stringify({card_number:num,card_expiration_month:mm,card_expiration_year:aa,
        security_code:document.getElementById('cvv').value,card_holder_name:document.getElementById('name').value,
        device_unique_identifier:DEVICE_ID,
        fraud_detection:{device_unique_identifier:DEVICE_ID},
        card_holder_identification:{type:document.getElementById('dtype').value,number:document.getElementById('dnum').value}})});
    const tkd=await tk.json();
    if(!tkd.id){msg('No pudimos validar la tarjeta. Revisá los datos e intentá de nuevo.','err');btn.disabled=false;return;}
    const ch=await fetch('/payway/charge',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({pid:PID,token:tkd.id,bin:tkd.bin||num.slice(0,6),device_id:DEVICE_ID})});
    const chd=await ch.json();
    if(chd.status==='approved'){
      document.getElementById('formwrap').style.display='none';
      document.getElementById('done').style.display='block';
    }
    else{msg('El pago fue rechazado por la tarjeta. Probá con otra tarjeta o contactanos por WhatsApp.','err');btn.disabled=false;}
  }catch(e){msg('Hubo un problema de conexión. Esperá unos segundos y volvé a intentar.','err');btn.disabled=false;}
}
</script></body></html>"""


# ── HTML de las páginas de estado (vencido / ya pagado / retorno) ─────────────────
_STATUS_HTML = """<!doctype html><html lang="es"><head>
<title>Remedia</title>
""" + _BRAND_HEAD + """
<style>
  .card{text-align:center;padding:34px 26px 30px}
  .icono{width:64px;height:64px;border-radius:50%;display:flex;align-items:center;justify-content:center;margin:0 auto 18px}
  .icono svg{width:30px;height:30px}
  .ok .icono{background:var(--verde);color:#fff;box-shadow:0 10px 26px -8px rgba(14,143,95,.6)}
  .warn .icono{background:#FBF3E2;color:var(--warn)}
  .err .icono{background:#FBEDED;color:var(--error)}
  h1{font-family:'Bricolage Grotesque',sans-serif;font-size:23px;font-weight:700;margin:0 0 10px;color:var(--verde-prof)}
  p{color:var(--gris);font-size:15px;line-height:1.6;margin:0}
</style></head><body>
""" + _MARCA_HTML + """
<div class="card {{VARIANTE}}">
  <div class="icono">{{ICONO}}</div>
  <h1>{{TITULO}}</h1>
  <p>{{SUB}}</p>
</div>
""" + _PIE_HTML + """
</body></html>"""

