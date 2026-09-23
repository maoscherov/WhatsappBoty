"""
Link mágico de un solo uso con vencimiento, token en el fragmento (nunca
llega al servidor ni a los logs), canjeado por POST, cookie HttpOnly atada
al tenant (§3 P0, §7).
"""
import re
import uuid
from datetime import timedelta

from app.radar.auth import COOKIE, generar_token
from app.radar.mailer import huella_token

from .helpers import DOMINIO_COOKIE, crear_tenant_directo, crear_usuario, entrar

LINK = re.compile(r"/radar/login/canjear\?t=([0-9a-f-]+)#k=([A-Za-z0-9_-]+)")


def _link(mail) -> tuple[str, str]:
    m = LINK.search(mail.texto)
    assert m, "el mail no trae el link"
    return m.group(1), m.group(2)


async def test_flujo_completo_de_login(cliente, radar_ctx):
    a = await crear_tenant_directo(radar_ctx.db, "Farmacia A")
    u = await crear_usuario(radar_ctx.db, a, "dueno@cliente.com", "dueno")

    r = await cliente.post("/radar/login", json={"email": " Dueno@Cliente.com "})
    assert r.status_code == 202 and r.json() == {"ok": True}
    assert len(radar_ctx.mailer.enviados) == 1
    mail = radar_ctx.mailer.enviados[0]
    assert mail.para == "dueno@cliente.com" and "Farmacia A" in mail.asunto
    t, k = _link(mail)
    assert t == str(a) and mail.huella == huella_token(k)

    # la página de canje no recibe el token (va en #k): ni en el HTML ni en el log
    r = await cliente.get(f"/radar/login/canjear?t={t}")
    assert r.status_code == 200 and 'name="k"' in r.text and "<form" in r.text
    assert k not in r.text
    assert ".submit()" not in r.text              # sin auto-envío: un escáner que ejecuta JS no consume el token
    assert r.headers["content-security-policy"].startswith("default-src 'none'; script-src 'sha256-")

    r = await cliente.post("/radar/login/canjear", data={"t": t, "k": k})
    assert r.status_code == 303 and r.headers["location"] == "/radar/api/yo"
    set_cookie = r.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=lax" in set_cookie and "path=/radar" in set_cookie
    assert COOKIE in set_cookie

    r = await cliente.get("/radar/api/yo")
    assert r.status_code == 200
    assert r.json() == {"user_id": str(u), "tenant_id": str(a), "rol": "dueno",
                        "email": "dueno@cliente.com", "es_kis": False, "lineas_permitidas": None}

    async with radar_ctx.db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT count(*) FROM access_audit_log WHERE accion = 'login_canjeado' "
                                  "AND actor_user_id = $1", u) == 1
        assert await con.fetchval("SELECT count(*) FROM product_events WHERE evento = 'login_canjeado'") == 1

    # un solo uso
    r = await cliente.post("/radar/login/canjear", data={"t": t, "k": k})
    assert r.status_code == 400


async def test_email_desconocido_o_invalido_responde_igual(cliente, radar_ctx):
    for email in ("nadie@cliente.com", "no-es-un-email", ""):
        r = await cliente.post("/radar/login", json={"email": email})
        assert r.status_code == 202 and r.json() == {"ok": True}
    assert radar_ctx.mailer.enviados == []


async def test_limite_de_tres_pedidos_por_usuario(cliente, radar_ctx):
    a = await crear_tenant_directo(radar_ctx.db, "A")
    await crear_usuario(radar_ctx.db, a, "dueno@cliente.com", "dueno")
    for _ in range(5):
        r = await cliente.post("/radar/login", json={"email": "dueno@cliente.com"})
        assert r.status_code == 202
    assert len(radar_ctx.mailer.enviados) == 3


async def test_mismo_email_en_dos_tenants_recibe_un_link_por_tenant(cliente, radar_ctx):
    a = await crear_tenant_directo(radar_ctx.db, "A")
    b = await crear_tenant_directo(radar_ctx.db, "B")
    await crear_usuario(radar_ctx.db, a, "dueno@cliente.com", "dueno")
    await crear_usuario(radar_ctx.db, b, "dueno@cliente.com", "lector")
    await cliente.post("/radar/login", json={"email": "dueno@cliente.com"})
    tenants = {_link(m)[0] for m in radar_ctx.mailer.enviados}
    assert tenants == {str(a), str(b)}


async def test_link_vencido_no_canjea(cliente, radar_ctx):
    a = await crear_tenant_directo(radar_ctx.db, "A")
    u = await crear_usuario(radar_ctx.db, a, "dueno@cliente.com", "dueno")
    claro, h = generar_token()
    async with radar_ctx.db.tenant_tx(a) as con:
        await con.execute("INSERT INTO login_tokens (user_id, token_hash, proposito, expires_at) "
                          "VALUES ($1, $2, 'login', now() - interval '1 second')", u, h)
    r = await cliente.post("/radar/login/canjear", data={"t": str(a), "k": claro})
    assert r.status_code == 400


async def test_link_no_canjea_en_otro_tenant(cliente, radar_ctx):
    a = await crear_tenant_directo(radar_ctx.db, "A")
    b = await crear_tenant_directo(radar_ctx.db, "B")
    await crear_usuario(radar_ctx.db, a, "dueno@cliente.com", "dueno")
    await cliente.post("/radar/login", json={"email": "dueno@cliente.com"})
    _, k = _link(radar_ctx.mailer.enviados[0])
    r = await cliente.post("/radar/login/canjear", data={"t": str(b), "k": k})
    assert r.status_code == 400
    r = await cliente.post("/radar/login/canjear", data={"t": str(a), "k": k})
    assert r.status_code == 303      # el token sigue vivo: el intento ajeno no lo consumió


async def test_link_con_token_malformado(cliente, radar_ctx):
    a = await crear_tenant_directo(radar_ctx.db, "A")
    assert (await cliente.post("/radar/login/canjear", data={"t": str(a), "k": "x"})).status_code == 400
    assert (await cliente.post("/radar/login/canjear", data={"t": str(a), "k": "<script>" * 5})).status_code == 400
    assert (await cliente.get("/radar/login/canjear?t=no-uuid")).status_code == 422


async def test_usuario_sin_membresia_no_entra(cliente, radar_ctx):
    a = await crear_tenant_directo(radar_ctx.db, "A")
    async with radar_ctx.db.tenant_tx(a) as con:
        await con.execute("INSERT INTO users (email) VALUES ('sin@cliente.com')")
    await cliente.post("/radar/login", json={"email": "sin@cliente.com"})
    t, k = _link(radar_ctx.mailer.enviados[0])
    r = await cliente.post("/radar/login/canjear", data={"t": t, "k": k})
    assert r.status_code == 403


async def test_yo_sin_cookie_o_con_cookie_adulterada(cliente, radar_ctx):
    assert (await cliente.get("/radar/api/yo")).status_code == 401
    a = await crear_tenant_directo(radar_ctx.db, "A")
    u = await crear_usuario(radar_ctx.db, a, "dueno@cliente.com", "dueno")
    await entrar(cliente, radar_ctx, a, u, "dueno")
    assert (await cliente.get("/radar/api/yo")).status_code == 200   # guarda: la cookie manual SÍ viaja
    valor = cliente.cookies.get(COOKIE)
    cliente.cookies.set(COOKIE, valor[:-1] + ("0" if valor[-1] != "0" else "1"), domain=DOMINIO_COOKIE, path="/radar")
    assert (await cliente.get("/radar/api/yo")).status_code == 401


async def test_logout_revoca_la_sesion(cliente, radar_ctx):
    a = await crear_tenant_directo(radar_ctx.db, "A")
    u = await crear_usuario(radar_ctx.db, a, "dueno@cliente.com", "dueno")
    await entrar(cliente, radar_ctx, a, u, "dueno")
    r = await cliente.post("/radar/logout")
    assert r.status_code == 200
    assert (await cliente.get("/radar/api/yo")).status_code == 401
    async with radar_ctx.db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT count(*) FROM sessions WHERE revoked_at IS NOT NULL") == 1
        assert await con.fetchval("SELECT count(*) FROM access_audit_log WHERE accion = 'sesion_cerrada'") == 1
