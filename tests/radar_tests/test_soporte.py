"""
Soporte KIS (§4.4): sin acceso por defecto; el dueño lo otorga con
vencimiento de 24–72 h, el cliente lo ve, y cada acceso queda auditado.
"""
import uuid

from app.radar.constantes import TENANT_KIS

from .helpers import DOMINIO_COOKIE, crear_linea_directa, crear_tenant_directo, crear_usuario, entrar


async def _base(ctx):
    a = await crear_tenant_directo(ctx.db, "A")
    d = await crear_usuario(ctx.db, a, "dueno@cliente.com", "dueno")
    await crear_linea_directa(ctx.db, a, "Centro")
    adm = await crear_usuario(ctx.db, TENANT_KIS, "soporte@keepitsimple.com.ar", "admin")
    return a, d, adm


async def test_dueno_otorga_y_el_cliente_lo_ve(cliente, radar_ctx):
    a, d, adm = await _base(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    for horas in (12, 73, 0):
        assert (await cliente.post("/radar/api/soporte", json={"horas": horas})).status_code == 422
    r = await cliente.post("/radar/api/soporte", json={"horas": 48})
    assert r.status_code == 201 and r.json()["otorgado_por"] == str(d)
    gid = r.json()["id"]
    async with radar_ctx.db.tenant_tx(a) as con:
        fila = await con.fetchrow("SELECT detalle FROM access_audit_log WHERE accion = 'soporte_otorgado'")
        assert '"horas": 48' in fila["detalle"]
    le = await crear_usuario(radar_ctx.db, a, "lector@cliente.com", "lector")
    await entrar(cliente, radar_ctx, a, le, "lector")
    r = await cliente.get("/radar/api/soporte")
    assert r.status_code == 200 and [g["id"] for g in r.json()] == [gid]
    assert (await cliente.post("/radar/api/soporte", json={"horas": 24})).status_code == 403
    assert (await cliente.delete(f"/radar/api/soporte/{gid}")).status_code == 403


async def test_admin_sin_grant_no_entra(cliente, radar_ctx):
    a, d, adm = await _base(radar_ctx)
    await entrar(cliente, radar_ctx, TENANT_KIS, adm, "admin")
    r = await cliente.post(f"/radar/admin/tenants/{a}/sesion-soporte")
    assert r.status_code == 403 and r.json()["detail"] == {"error": "sin_grant"}
    async with radar_ctx.db.tenant_tx(a) as con:     # vencido tampoco
        await con.execute("INSERT INTO support_grants (otorgado_por, created_at, expires_at) "
                          "VALUES ($1, now() - interval '3 days', now() - interval '1 day')", d)
    assert (await cliente.post(f"/radar/admin/tenants/{a}/sesion-soporte")).status_code == 403


async def test_acceso_de_soporte_con_grant(cliente, radar_ctx):
    a, d, adm = await _base(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    gid = (await cliente.post("/radar/api/soporte", json={"horas": 24})).json()["id"]

    await entrar(cliente, radar_ctx, TENANT_KIS, adm, "admin")
    r = await cliente.post(f"/radar/admin/tenants/{a}/sesion-soporte")
    assert r.status_code == 200, r.text
    assert r.json()["rol"] == "soporte" and r.json()["tenant_id"] == str(a)
    assert "radar_sesion" in r.headers["set-cookie"]

    yo = (await cliente.get("/radar/api/yo")).json()
    assert yo == {"user_id": str(adm), "tenant_id": str(a), "rol": "soporte", "email": None,
                  "es_kis": False, "lineas_permitidas": None}
    assert [x["nombre"] for x in (await cliente.get("/radar/api/lineas")).json()] == ["Centro"]
    assert (await cliente.post("/radar/api/usuarios", json={"email": "x@cliente.com", "rol": "lector"})).status_code == 403
    assert (await cliente.get("/radar/admin/tenants")).status_code == 403     # ya no es una sesión de admin

    async with radar_ctx.db.tenant_tx(a) as con:
        fila = await con.fetchrow("SELECT actor_user_id, objeto_id FROM access_audit_log WHERE accion = 'acceso_soporte'")
        assert fila["actor_user_id"] == adm and fila["objeto_id"] == uuid.UUID(gid)
        vence = await con.fetchrow("SELECT s.expires_at = g.expires_at AS igual FROM sessions s, support_grants g "
                                   "WHERE s.rol = 'soporte' AND g.id = $1", uuid.UUID(gid))
        assert vence["igual"] is True


async def test_revocar_grant_cierra_las_sesiones_de_soporte(cliente, radar_ctx):
    a, d, adm = await _base(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    gid = (await cliente.post("/radar/api/soporte", json={"horas": 24})).json()["id"]
    await entrar(cliente, radar_ctx, TENANT_KIS, adm, "admin")
    await cliente.post(f"/radar/admin/tenants/{a}/sesion-soporte")
    # el Set-Cookie del servidor reemplazó a la cookie manual (mismo dominio efectivo): un solo valor
    cookie_soporte = cliente.cookies.get("radar_sesion")

    await entrar(cliente, radar_ctx, a, d, "dueno")
    r = await cliente.delete(f"/radar/api/soporte/{gid}")
    assert r.status_code == 200
    assert (await cliente.get("/radar/api/soporte")).json() == []
    assert (await cliente.delete(f"/radar/api/soporte/{gid}")).status_code == 404

    cliente.cookies.clear()
    cliente.cookies.set("radar_sesion", cookie_soporte, domain=DOMINIO_COOKIE, path="/radar")
    assert (await cliente.get("/radar/api/yo")).status_code == 401
    async with radar_ctx.db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT count(*) FROM access_audit_log WHERE accion = 'soporte_revocado'") == 1


async def test_grant_de_otro_tenant_no_sirve(cliente, radar_ctx):
    a, d, adm = await _base(radar_ctx)
    b = await crear_tenant_directo(radar_ctx.db, "B")
    await entrar(cliente, radar_ctx, a, d, "dueno")
    await cliente.post("/radar/api/soporte", json={"horas": 24})
    await entrar(cliente, radar_ctx, TENANT_KIS, adm, "admin")
    assert (await cliente.post(f"/radar/admin/tenants/{b}/sesion-soporte")).status_code == 403
