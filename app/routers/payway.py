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
function msg(t,c){const m=document.getElementById('msg');m.textContent=t;m.style.color=c||'#94a3b8';}
async function pagar(){
  const btn=document.getElementById('btn'); btn.disabled=true; msg('Procesando…');
  const num=document.getElementById('num').value.replace(/\\s/g,'');
  let [mm,aa]=(document.getElementById('exp').value||'').split(/[\\/\\-\\s]+/).map(s=>s.trim());
  mm=(mm||'').padStart(2,'0').slice(0,2);        // mes 2 dígitos
  aa=(aa||'').replace(/\\D/g,'').slice(-2);        // año a 2 dígitos (28, no 2028)
  if(!mm||mm==='00'||aa.length!==2){msg('Revisá el vencimiento (MM/AA).','#ef4444');btn.disabled=false;return;}
  try{
    const tk=await fetch(TOKENS_URL,{method:'POST',headers:{'Content-Type':'application/json','apikey':PUBLIC_KEY},
      body:JSON.stringify({card_number:num,card_expiration_month:mm,card_expiration_year:aa,
        security_code:document.getElementById('cvv').value,card_holder_name:document.getElementById('name').value,
        device_unique_identifier:DEVICE_ID,
        fraud_detection:{device_unique_identifier:DEVICE_ID},
        card_holder_identification:{type:document.getElementById('dtype').value,number:document.getElementById('dnum').value}})});
    const tkd=await tk.json();
    if(!tkd.id){msg('No se pudo validar la tarjeta: '+JSON.stringify(tkd),'#ef4444');btn.disabled=false;return;}
    const ch=await fetch('/payway/charge',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({pid:PID,token:tkd.id,bin:tkd.bin||num.slice(0,6),device_id:DEVICE_ID})});
    const chd=await ch.json();
    if(chd.status==='approved'){msg('✅ ¡Pago aprobado! Ya podés cerrar esta página.','#25d366');}
    else{msg('❌ Pago '+(chd.status||'rechazado')+'. '+(JSON.stringify(chd.detail||'')),'#ef4444');btn.disabled=false;}
  }catch(e){msg('Error: '+e.message,'#ef4444');btn.disabled=false;}
}
</script></body></html>"""
