"""P3 del dueño (§3 P3): el mismo backend y la misma máquina de estados que la Consola."""
import hashlib
import hmac
import json

from app.radar.vinculos import aplicar_status, marcar_restriccion

from .helpers import HMAC_TEST, crear_linea_directa, crear_usuario, entrar, escenario_vinculable, vincular_de_prueba
from .waha_falso import PNG


def _url(line_id, sufijo=""):
    return f"/radar/api/lineas/{line_id}/vinculo{sufijo}"


async def test_el_dueno_consiente_y_vincula_su_linea(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    nueva = await crear_linea_directa(ctx_waha.db, esc["tenant_id"], "Sucursal")
    await entrar(cliente, ctx_waha, esc["tenant_id"], esc["dueno_id"], "dueno")
    r = await cliente.post(_url(nueva), json={})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "sin_consentimiento"
    r = await cliente.post(f"/radar/api/lineas/{nueva}/consentimientos",
                           json={"version_texto": "v1", "acepta": True, "titular": True})
    assert r.status_code == 201
    r = await cliente.post(_url(nueva), json={"full_sync": False})
    assert r.status_code == 201 and r.json()["estado"] == "esperando_qr"
    qr = await cliente.get(_url(nueva, "/qr"))
    assert qr.content == PNG and qr.headers["cache-control"] == "no-store"


async def test_gestor_ve_el_estado_pero_no_opera(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    gestor = await crear_usuario(ctx_waha.db, esc["tenant_id"], "gestor@cliente.com", "gestor")
    await entrar(cliente, ctx_waha, esc["tenant_id"], gestor, "gestor")
    assert (await cliente.get(_url(esc["line_id"]))).status_code == 200
    assert (await cliente.post(_url(esc["line_id"]), json={})).status_code == 403
    assert (await cliente.post(_url(esc["line_id"], "/desconectar"), json={"confirmar": True})).status_code == 403
    assert waha.llamadas == []


async def test_no_cruza_tenants(cliente, ctx_waha, waha):
    a = await escenario_vinculable(ctx_waha, "Farmacia A")
    b = await escenario_vinculable(ctx_waha, "Farmacia B")
    await entrar(cliente, ctx_waha, b["tenant_id"], b["dueno_id"], "dueno")
    assert (await cliente.get(_url(a["line_id"]))).status_code == 404
    assert (await cliente.post(_url(a["line_id"]), json={})).status_code == 404
    assert waha.llamadas == []


async def test_la_misma_maquina_de_estados_por_webhook(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc, working=False)
    sobre = {"event": "session.status", "session": v["session_name"],
             "metadata": {"tenant_id": str(esc["tenant_id"]), "line_id": str(esc["line_id"]),
                          "link_id": str(v["link_id"])},
             "me": {"id": waha.me_id}, "payload": {"status": "WORKING"}}
    crudo = json.dumps(sobre).encode()
    firma = hmac.new(HMAC_TEST.encode(), crudo, hashlib.sha512).hexdigest()
    await cliente.post("/webhook/waha", content=crudo, headers={"content-type": "application/json",
                                                                  "x-webhook-hmac": firma})
    await entrar(cliente, ctx_waha, esc["tenant_id"], esc["dueno_id"], "dueno")
    estado = (await cliente.get(_url(esc["line_id"]))).json()
    assert (estado["estado"], estado["numero"]) == ("vinculado", "…4567")


async def test_desconectar_pedido_por_el_dueno(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    await entrar(cliente, ctx_waha, esc["tenant_id"], esc["dueno_id"], "dueno")
    assert (await cliente.post(_url(esc["line_id"], "/desconectar"), json={"confirmar": True})).status_code == 202
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        causa = await con.fetchval("SELECT causa FROM jobs WHERE link_id = $1", v["link_id"])
    assert causa == "pedido_dueno"


async def test_restriccion_bloquea_reconectar_desde_el_cliente(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await aplicar_status(con, tenant_id=esc["tenant_id"], link_id=v["link_id"], waha_status="FAILED",
                             origen="webhook")
    await marcar_restriccion(ctx_waha, tenant_id=esc["tenant_id"], line_id=esc["line_id"], hasta=None,
                             actor_user_id=None, actor_rol="admin", ip=None)
    await entrar(cliente, ctx_waha, esc["tenant_id"], esc["dueno_id"], "dueno")
    r = await cliente.post(_url(esc["line_id"]), json={})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "restriccion_activa"


async def test_texto_de_consentimiento_solo_para_el_dueno(cliente, ctx_waha):
    esc = await escenario_vinculable(ctx_waha)
    await entrar(cliente, ctx_waha, esc["tenant_id"], esc["dueno_id"], "dueno")
    t = (await cliente.get(_url(esc["line_id"], "/texto-consentimiento"))).json()
    assert t["version"] == "v1" and "Qué no hacemos" in t["texto"]
    lector = await crear_usuario(ctx_waha.db, esc["tenant_id"], "lector@cliente.com", "lector")
    await entrar(cliente, ctx_waha, esc["tenant_id"], lector, "lector")
    assert (await cliente.get(_url(esc["line_id"], "/texto-consentimiento"))).status_code == 403
