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
from app.services.order_service import get_order_service
from app.services.whatsapp_service import get_whatsapp_service

logger = logging.getLogger(__name__)
router = APIRouter()

_PENDING_TTL = 60 * 60 * 24   # 24h


def _redis():
    return aioredis.from_url(get_settings().redis_url, decode_responses=True)


async def crear_pago_pendiente(phone: str, sku_id: str, sku_nombre: str,
                               cantidad: int, total: float) -> str:
    """Guarda un pago pendiente y devuelve la URL de la página de pago."""
    settings = get_settings()
    pid = uuid.uuid4().hex[:16]
    data = {
        "id": pid, "phone": phone, "sku_id": sku_id, "sku_nombre": sku_nombre,
        "cantidad": cantidad, "total": total, "estado": "pendiente",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await _redis().setex(f"payway:pending:{pid}", _PENDING_TTL, json.dumps(data))
    except Exception as e:
        logger.error(f"No se pudo guardar pago pendiente Payway: {e}")
    base = settings.public_base_url.rstrip("/")
    return f"{base}/pay/{pid}"


async def _get_pending(pid: str) -> dict | None:
    try:
        raw = await _redis().get(f"payway:pending:{pid}")
        return json.loads(raw) if raw else None
    except Exception:
        return None


# ── Página de pago ──────────────────────────────────────────────────────────────

@router.get("/pay/{pid}", response_class=HTMLResponse)
async def pay_page(pid: str):
    settings = get_settings()
    pending = await _get_pending(pid)
    if not pending:
        return HTMLResponse("<h3>Link de pago vencido o inexistente.</h3>", status_code=404)
    if pending.get("estado") == "aprobado":
        return HTMLResponse("<h3>✅ Este pago ya fue realizado. ¡Gracias!</h3>")

    pw = get_payway_service(settings.payway_public_key, settings.payway_private_key, settings.payway_sandbox)
    total = float(pending["total"])
    nombre = pending["sku_nombre"]
    return HTMLResponse(_PAY_HTML
                        .replace("{{PID}}", pid)
                        .replace("{{NOMBRE}}", nombre)
                        .replace("{{TOTAL}}", f"{total:,.2f}")
                        .replace("{{TOKENS_URL}}", f"{pw.base_url}/tokens")
                        .replace("{{PUBLIC_KEY}}", pw.public_key))


# ── Cobro ────────────────────────────────────────────────────────────────────────

class ChargeIn(BaseModel):
    pid: str
    token: str
    bin: str
    payment_method_id: int = 1   # 1 = Visa (default sandbox)
    installments: int = 1


@router.post("/payway/charge")
async def payway_charge(body: ChargeIn):
    settings = get_settings()
    pending = await _get_pending(body.pid)
    if not pending:
        raise HTTPException(status_code=404, detail="Pago no encontrado o vencido")
    if pending.get("estado") == "aprobado":
        return {"status": "approved", "ya_pagado": True}

    pw = get_payway_service(settings.payway_public_key, settings.payway_private_key, settings.payway_sandbox)
    data, err = await pw.crear_pago(
        token=body.token,
        amount=float(pending["total"]),
        site_transaction_id=f"{body.pid}-{int(datetime.now().timestamp())}",
        payment_method_id=body.payment_method_id,
        bin=body.bin,
        installments=body.installments,
    )
    if err or not data:
        logger.error(f"Payway charge falló: {err}")
        return {"status": "error", "detail": err or "sin respuesta"}

    estado = data.get("status")   # "approved" | "rejected" | ...
    if estado == "approved":
        pending["estado"] = "aprobado"
        pending["payway_id"] = data.get("id")
        await _redis().setex(f"payway:pending:{body.pid}", _PENDING_TTL, json.dumps(pending))
        # Crear el pedido + avisar por WhatsApp (mismo flujo que MP)
        try:
            order_svc = get_order_service(settings.redis_url)
            order = await order_svc.create(
                phone=pending["phone"], sku_id=pending["sku_id"],
                sku_nombre=pending["sku_nombre"], cantidad=int(pending["cantidad"]),
                total=float(pending["total"]), mp_payment_id=str(data.get("id")),
            )
            wa = get_whatsapp_service(settings.whatsapp_token, settings.whatsapp_phone_number_id)
            await wa.send_text(pending["phone"],
                               f"✅ *¡Pago confirmado!* Recibimos tu pago de *{pending['sku_nombre']}*. 🙌\n"
                               f"🔑 Código de retiro: *{order.get('pickup_code','')}*\n¡Gracias! 💊")
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


@router.get("/payway/link-test/{monto}")
async def payway_link_test(monto: float):
    """Prueba del GenerateLink (checkout HOSTEADO de Payway) — devuelve la URL o el error."""
    settings = get_settings()
    pw = get_payway_service(settings.payway_public_key, settings.payway_private_key,
                            settings.payway_sandbox, settings.payway_site_id, settings.payway_template_id)
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
    return HTMLResponse(f"<h3>{'✅ Pago realizado' if r=='ok' else 'Pago cancelado'}. Podés cerrar esta página.</h3>")


@router.post("/payway/notification")
async def payway_notification(payload: dict):
    """Notificación de Payway (webhook). Por ahora solo loguea para ver el formato."""
    logger.info(f"Payway notification: {payload}")
    return {"status": "ok"}


# ── HTML de la página de pago (tokeniza en el navegador, cobra en el backend) ──────
_PAY_HTML = """<!doctype html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pago Remedia</title>
<style>
  body{font-family:-apple-system,Segoe UI,sans-serif;background:#0f1117;color:#e2e8f0;margin:0;padding:24px;display:flex;justify-content:center}
  .card{background:#1a1d27;border:1px solid #2a2d3e;border-radius:14px;padding:24px;max-width:420px;width:100%}
  h1{font-size:18px;margin:0 0 4px}.sub{color:#64748b;font-size:14px;margin-bottom:18px}
  .total{font-size:26px;font-weight:700;color:#25d366;margin-bottom:18px}
  label{display:block;font-size:12px;color:#94a3b8;margin:10px 0 4px}
  input{width:100%;box-sizing:border-box;background:#0f1117;border:1px solid #2a2d3e;border-radius:8px;padding:11px;color:#e2e8f0;font-size:15px}
  .row{display:flex;gap:10px}.row>div{flex:1}
  button{width:100%;margin-top:18px;background:#25d366;color:#062;border:none;border-radius:8px;padding:13px;font-size:16px;font-weight:700;cursor:pointer}
  button:disabled{opacity:.5}
  #msg{margin-top:14px;font-size:14px;text-align:center}
</style></head><body>
<div class="card">
  <h1>💊 Pago Remedia</h1>
  <div class="sub">{{NOMBRE}}</div>
  <div class="total">$ {{TOTAL}}</div>
  <label>Número de tarjeta</label><input id="num" inputmode="numeric" placeholder="4507 9900 0000 4905">
  <div class="row">
    <div><label>Vencimiento (MM/AA)</label><input id="exp" placeholder="08/28"></div>
    <div><label>CVV</label><input id="cvv" inputmode="numeric" placeholder="123"></div>
  </div>
  <label>Nombre en la tarjeta</label><input id="name" placeholder="Juan Perez">
  <div class="row">
    <div><label>Tipo doc</label><input id="dtype" value="dni"></div>
    <div><label>Nro documento</label><input id="dnum" inputmode="numeric" placeholder="12345678"></div>
  </div>
  <button id="btn" onclick="pagar()">Pagar $ {{TOTAL}}</button>
  <div id="msg"></div>
</div>
<script>
const TOKENS_URL="{{TOKENS_URL}}", PUBLIC_KEY="{{PUBLIC_KEY}}", PID="{{PID}}";
function msg(t,c){const m=document.getElementById('msg');m.textContent=t;m.style.color=c||'#94a3b8';}
async function pagar(){
  const btn=document.getElementById('btn'); btn.disabled=true; msg('Procesando…');
  const num=document.getElementById('num').value.replace(/\\s/g,'');
  const [mm,aa]=(document.getElementById('exp').value||'').split('/').map(s=>s.trim());
  try{
    const tk=await fetch(TOKENS_URL,{method:'POST',headers:{'Content-Type':'application/json','apikey':PUBLIC_KEY},
      body:JSON.stringify({card_number:num,card_expiration_month:mm,card_expiration_year:aa,
        security_code:document.getElementById('cvv').value,card_holder_name:document.getElementById('name').value,
        card_holder_identification:{type:document.getElementById('dtype').value,number:document.getElementById('dnum').value}})});
    const tkd=await tk.json();
    if(!tkd.id){msg('No se pudo validar la tarjeta: '+JSON.stringify(tkd),'#ef4444');btn.disabled=false;return;}
    const ch=await fetch('/payway/charge',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({pid:PID,token:tkd.id,bin:tkd.bin||num.slice(0,6)})});
    const chd=await ch.json();
    if(chd.status==='approved'){msg('✅ ¡Pago aprobado! Ya podés cerrar esta página.','#25d366');}
    else{msg('❌ Pago '+(chd.status||'rechazado')+'. '+(JSON.stringify(chd.detail||'')),'#ef4444');btn.disabled=false;}
  }catch(e){msg('Error: '+e.message,'#ef4444');btn.disabled=false;}
}
</script></body></html>"""
