"""
Roles (§4.4): el dueño ve todo e invita; el gestor puede limitarse a líneas
determinadas; el lector solo lee. El rol se valida en el servidor en cada
consulta.
"""
import re
import uuid

import pytest

from .helpers import DOMINIO_COOKIE, crear_linea_directa, crear_tenant_directo, crear_usuario, entrar

LINK = re.compile(r"/radar/login/canjear\?t=([0-9a-f-]+)#k=([A-Za-z0-9_-]+)")


async def _tenant_con_dueno(ctx):
    a = await crear_tenant_directo(ctx.db, "A")
    d = await crear_usuario(ctx.db, a, "dueno@cliente.com", "dueno")
    l1 = await crear_linea_directa(ctx.db, a, "Centro")
    l2 = await crear_linea_directa(ctx.db, a, "Norte")
    return a, d, l1, l2


async def test_lineas_segun_rol(cliente, radar_ctx):
    a, d, l1, l2 = await _tenant_con_dueno(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    r = await cliente.get("/radar/api/lineas")
    assert r.status_code == 200
    assert [x["nombre"] for x in r.json()] == ["Centro", "Norte"]
    assert r.json()[0]["parametros"] == {"duracion_vinculo_dias": 0, "retencion_fuente_dias": 0,
                                         "retencion_tras_desvinculo_dias": 0, "tope_ia_mensual_usd": None}

    g = await crear_usuario(radar_ctx.db, a, "gestor@cliente.com", "gestor", lineas_permitidas=[l2])
    await entrar(cliente, radar_ctx, a, g, "gestor")
    assert [x["nombre"] for x in (await cliente.get("/radar/api/lineas")).json()] == ["Norte"]

    le = await crear_usuario(radar_ctx.db, a, "lector@cliente.com", "lector")
    await entrar(cliente, radar_ctx, a, le, "lector")
    assert len((await cliente.get("/radar/api/lineas")).json()) == 2


async def test_lineas_no_cruzan_tenants(cliente, radar_ctx):
    a, d, l1, l2 = await _tenant_con_dueno(radar_ctx)
    b = await crear_tenant_directo(radar_ctx.db, "B")
    await crear_linea_directa(radar_ctx.db, b, "De B")
    db_ = await crear_usuario(radar_ctx.db, b, "dueno@otro.com", "dueno")
    await entrar(cliente, radar_ctx, b, db_, "dueno")
    assert [x["nombre"] for x in (await cliente.get("/radar/api/lineas")).json()] == ["De B"]


async def test_dueno_invita_gestor_y_este_entra_con_su_rol(cliente, radar_ctx):
    a, d, l1, l2 = await _tenant_con_dueno(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    r = await cliente.post("/radar/api/usuarios", json={"email": "Gestor@Cliente.com", "nombre": "Gus",
                                                        "rol": "gestor", "lineas_permitidas": [str(l1)]})
    assert r.status_code == 201 and r.json()["invitacion_enviada"] is True
    uid = uuid.UUID(r.json()["user_id"])
    async with radar_ctx.db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT count(*) FROM access_audit_log WHERE accion = 'usuario_invitado' "
                                  "AND objeto_id = $1 AND actor_user_id = $2", uid, d) == 1
    t, k = LINK.search(radar_ctx.mailer.enviados[-1].texto).groups()
    cliente.cookies.clear()
    assert (await cliente.post("/radar/login/canjear", data={"t": t, "k": k})).status_code == 303
    yo = (await cliente.get("/radar/api/yo")).json()
    assert yo["rol"] == "gestor" and yo["lineas_permitidas"] == [str(l1)]
    assert [x["id"] for x in (await cliente.get("/radar/api/lineas")).json()] == [str(l1)]

    # repetido → 409; rol dueño por invitación → 422
    await entrar(cliente, radar_ctx, a, d, "dueno")
    assert (await cliente.post("/radar/api/usuarios", json={"email": "gestor@cliente.com", "rol": "lector"})).status_code == 409
    assert (await cliente.post("/radar/api/usuarios", json={"email": "otro@cliente.com", "rol": "dueno"})).status_code == 422


async def test_listar_usuarios(cliente, radar_ctx):
    a, d, l1, l2 = await _tenant_con_dueno(radar_ctx)
    await crear_usuario(radar_ctx.db, a, "lector@cliente.com", "lector")
    await entrar(cliente, radar_ctx, a, d, "dueno")
    r = await cliente.get("/radar/api/usuarios")
    assert r.status_code == 200
    assert sorted((u["email"], u["rol"]) for u in r.json()) == [("dueno@cliente.com", "dueno"), ("lector@cliente.com", "lector")]


async def test_cambio_de_rol_revoca_sesiones_y_audita(cliente, radar_ctx):
    a, d, l1, l2 = await _tenant_con_dueno(radar_ctx)
    g = await crear_usuario(radar_ctx.db, a, "gestor@cliente.com", "gestor")
    await entrar(cliente, radar_ctx, a, g, "gestor")
    cookie_gestor = cliente.cookies.get("radar_sesion")

    await entrar(cliente, radar_ctx, a, d, "dueno")
    r = await cliente.put(f"/radar/api/usuarios/{g}/rol", json={"rol": "lector"})
    assert r.status_code == 200 and r.json() == {"user_id": str(g), "rol": "lector", "lineas_permitidas": None}
    async with radar_ctx.db.tenant_tx(a) as con:
        fila = await con.fetchrow("SELECT detalle FROM access_audit_log WHERE accion = 'rol_cambiado'")
        assert '"rol_anterior": "gestor"' in fila["detalle"] and '"rol_nuevo": "lector"' in fila["detalle"]

    cliente.cookies.clear()
    cliente.cookies.set("radar_sesion", cookie_gestor, domain=DOMINIO_COOKIE, path="/radar")
    assert (await cliente.get("/radar/api/yo")).status_code == 401      # la sesión vieja murió

    await entrar(cliente, radar_ctx, a, d, "dueno")
    assert (await cliente.put(f"/radar/api/usuarios/{d}/rol", json={"rol": "lector"})).status_code == 400
    assert (await cliente.put(f"/radar/api/usuarios/{uuid.uuid4()}/rol", json={"rol": "lector"})).status_code == 404


async def test_solo_el_dueno_administra_usuarios(cliente, radar_ctx):
    a, d, l1, l2 = await _tenant_con_dueno(radar_ctx)
    for rol in ("gestor", "lector"):
        u = await crear_usuario(radar_ctx.db, a, f"{rol}@cliente.com", rol)
        await entrar(cliente, radar_ctx, a, u, rol)
        assert (await cliente.get("/radar/api/usuarios")).status_code == 403
        assert (await cliente.post("/radar/api/usuarios", json={"email": "x@cliente.com", "rol": "lector"})).status_code == 403
        assert (await cliente.put(f"/radar/api/usuarios/{d}/rol", json={"rol": "lector"})).status_code == 403


@pytest.mark.parametrize("caracter", ["\n", "\r", "\x0b", "\x85", " ", " ", "\t", "\x00"], ids=ascii)
async def test_el_nombre_de_un_invitado_no_admite_saltos_de_linea_ni_controles(cliente, radar_ctx, caracter):
    """Como el del dueño en el alta (test_admin.py): un nombre de persona va en un renglón."""
    a, d, _, _ = await _tenant_con_dueno(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    r = await cliente.post("/radar/api/usuarios", json={"email": "gestor@cliente.com", "nombre": f"Gus{caracter}Bo",
                                                        "rol": "gestor"})
    assert r.status_code == 422
    async with radar_ctx.db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT count(*) FROM users") == 1          # solo el dueño
    assert radar_ctx.mailer.enviados == []


async def test_invitacion_con_mailer_que_falla(cliente, radar_ctx, monkeypatch):
    """Revisión final: el usuario ya quedó creado; un fallo del mail da 201 con
    invitacion_enviada false, no un 500."""
    from .helpers import MailerQueFalla
    a, d, l1, _ = await _tenant_con_dueno(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    monkeypatch.setattr(radar_ctx, "mailer", MailerQueFalla())
    r = await cliente.post("/radar/api/usuarios", json={"email": "gestor@cliente.com", "nombre": "Gus", "rol": "gestor"})
    assert r.status_code == 201 and r.json()["invitacion_enviada"] is False
    async with radar_ctx.db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT count(*) FROM users WHERE email = 'gestor@cliente.com'") == 1
