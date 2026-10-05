"""
P0 (§3): un admin de KIS crea la cuenta, registra la línea con sus
parámetros e invita al dueño. Todo lo cruzado entre tenants pasa por las
funciones SECURITY DEFINER y queda auditado.
"""
import re
import uuid
from pathlib import Path

import pytest

from app.radar.admin_kis import crear_admin_kis
from app.radar.constantes import TENANT_KIS
from app.radar.secrets import obtener_k_tenant

from .helpers import crear_tenant_directo, crear_usuario, entrar

LINK = re.compile(r"/radar/login/canjear\?t=([0-9a-f-]+)#k=([A-Za-z0-9_-]+)")

ALTA = {
    "nombre": "Farmacia del Centro", "rubro": "farmacia",
    "dueno": {"email": "Dueno@Farmacia.com", "nombre": "Ana"},
    "linea": {"nombre": "Local centro", "parametros": {"duracion_vinculo_dias": 30}},
}


async def _admin(cliente, ctx) -> uuid.UUID:
    uid = await crear_usuario(ctx.db, TENANT_KIS, "admin@keepitsimple.com.ar", "admin")
    await entrar(cliente, ctx, TENANT_KIS, uid, "admin")
    return uid


async def test_crear_admin_kis_es_idempotente_y_manda_invitacion(radar_ctx):
    u1 = await crear_admin_kis(radar_ctx, email="Admin@KeepItSimple.com.ar", nombre="Mariano")
    u2 = await crear_admin_kis(radar_ctx, email="admin@keepitsimple.com.ar", nombre="Mariano O.")
    assert u1 == u2
    async with radar_ctx.db.tenant_tx(TENANT_KIS) as con:
        assert await con.fetchval("SELECT rol FROM memberships WHERE user_id = $1", u1) == "admin"
        assert await con.fetchval("SELECT nombre FROM users WHERE id = $1", u1) == "Mariano O."
    assert len(radar_ctx.mailer.enviados) == 2
    assert "Keep IT Simple" in radar_ctx.mailer.enviados[0].asunto


async def test_propuesta_por_rubro(cliente, radar_ctx):
    await _admin(cliente, radar_ctx)
    r = await cliente.get("/radar/admin/propuesta", params={"rubro": "farmacia"})
    assert r.status_code == 200
    assert r.json() == {
        "perfil_de_datos": "sensible",
        "parametros_tenant": {"ia_habilitada": False, "via_llm": "sincronica", "retener_fragmentos": False},
        "parametros_linea": {"retencion_fuente_dias": 7},
        "almacenes_disponibles": ["permanente"],
    }
    r = await cliente.get("/radar/admin/propuesta", params={"rubro": "pet_shop"})
    assert r.json()["perfil_de_datos"] == "estandar" and r.json()["parametros_linea"] == {}


async def test_alta_de_cliente_e_invitacion_del_dueno(cliente, radar_ctx):
    admin = await _admin(cliente, radar_ctx)
    r = await cliente.post("/radar/admin/tenants", json=ALTA)
    assert r.status_code == 201, r.text
    cuerpo = r.json()
    tid, lid, uid = uuid.UUID(cuerpo["tenant_id"]), uuid.UUID(cuerpo["line_id"]), uuid.UUID(cuerpo["user_id"])
    assert cuerpo["invitacion_enviada"] is True

    assert len(obtener_k_tenant(radar_ctx.secretos, tid)) == 32
    async with radar_ctx.db.tenant_tx(tid) as con:
        t = await con.fetchrow("SELECT nombre, rubro, perfil_de_datos, ia_habilitada, via_llm FROM tenants WHERE id = $1", tid)
        # §7: un tenant `sensible` arranca con IA apagada y vía sincrónica; el
        # admin puede pisarlo explícitamente ("propone, no fuerza"), pero no por omisión.
        assert dict(t) == {"nombre": "Farmacia del Centro", "rubro": "farmacia", "perfil_de_datos": "sensible",
                           "ia_habilitada": False, "via_llm": "sincronica"}
        linea = await con.fetchrow("SELECT nombre, estado, almacen_fuente, duracion_vinculo_dias FROM lines WHERE id = $1", lid)
        assert dict(linea) == {"nombre": "Local centro", "estado": "sin_vinculo",
                               "almacen_fuente": "permanente", "duracion_vinculo_dias": 30}
        assert await con.fetchval("SELECT email FROM users WHERE id = $1", uid) == "dueno@farmacia.com"
        assert await con.fetchval("SELECT rol FROM memberships WHERE user_id = $1", uid) == "dueno"
        acciones = {f["accion"]: f for f in await con.fetch("SELECT accion, actor_user_id, objeto_id FROM access_audit_log")}
        assert set(acciones) == {"tenant_creado", "linea_creada", "usuario_invitado"}
        assert acciones["tenant_creado"]["actor_user_id"] == admin and acciones["tenant_creado"]["objeto_id"] == tid
        eventos = {f["evento"] for f in await con.fetch("SELECT evento FROM product_events")}
        assert eventos == {"linea_creada", "invitacion_enviada"}

    mail = radar_ctx.mailer.enviados[-1]
    assert mail.para == "dueno@farmacia.com" and "Farmacia del Centro" in mail.asunto
    t, k = LINK.search(mail.texto).groups()
    cliente.cookies.clear()
    r = await cliente.post("/radar/login/canjear", data={"t": t, "k": k})
    assert r.status_code == 303
    r = await cliente.get("/radar/api/yo")
    assert r.status_code == 200 and r.json()["rol"] == "dueno" and r.json()["tenant_id"] == str(tid)


async def test_alta_con_parametros_explicitos_del_perfil_sensible(cliente, radar_ctx):
    """El perfil propone, el admin decide: un valor explícito pisa la propuesta
    (ia_habilitada=True sobre farmacia queda a cargo de la cláusula contractual
    y el dictamen de §7, fuera del código). Lo no enviado conserva la propuesta."""
    await _admin(cliente, radar_ctx)
    alta = dict(ALTA, parametros={"ia_habilitada": True, "retencion_fichas_meses": 6})
    r = await cliente.post("/radar/admin/tenants", json=alta)
    assert r.status_code == 201
    async with radar_ctx.db.tenant_tx(uuid.UUID(r.json()["tenant_id"])) as con:
        t = await con.fetchrow("SELECT ia_habilitada, via_llm, retencion_fichas_meses, retener_fragmentos FROM tenants")
    assert dict(t) == {"ia_habilitada": True, "via_llm": "sincronica", "retencion_fichas_meses": 6,
                       "retener_fragmentos": False}
    # perfil estandar explícito sobre un rubro sensible: no hereda la propuesta
    alta = dict(ALTA, perfil_de_datos="estandar", dueno={"email": "otro@farmacia.com", "nombre": "Bo"})
    r = await cliente.post("/radar/admin/tenants", json=alta)
    assert r.status_code == 201
    async with radar_ctx.db.tenant_tx(uuid.UUID(r.json()["tenant_id"])) as con:
        t = await con.fetchrow("SELECT perfil_de_datos, ia_habilitada, via_llm FROM tenants")
    assert dict(t) == {"perfil_de_datos": "estandar", "ia_habilitada": True, "via_llm": "lotes"}


async def test_alta_rechaza_almacen_purgable_y_no_deja_secreto_colgado(cliente, radar_ctx):
    await _admin(cliente, radar_ctx)
    alta = dict(ALTA, linea={"nombre": "L", "parametros": {"retencion_fuente_dias": 7}})
    r = await cliente.post("/radar/admin/tenants", json=alta)
    assert r.status_code == 422 and "purgable" in r.text
    async with radar_ctx.db.sin_tenant() as con:
        assert await con.fetch("SELECT * FROM radar_admin_listar_tenants()") == []
    assert list(Path(radar_ctx.settings.secrets_dir).glob("*")) == []      # la k_tenant se destruyó


@pytest.mark.parametrize("malo", [
    dict(ALTA, rubro="Farmacia Con Espacios"),
    dict(ALTA, linea={"nombre": "", "parametros": {}}),
    dict(ALTA, linea={"nombre": "L", "parametros": {"duracion_vinculo_dias": -1}}),
    dict(ALTA, parametros={"via_llm": "streaming"}),
    dict(ALTA, perfil_de_datos="secreto"),
    dict(ALTA, dueno={"email": "sin-arroba", "nombre": ""}),
])
async def test_alta_invalida(cliente, radar_ctx, malo):
    await _admin(cliente, radar_ctx)
    assert (await cliente.post("/radar/admin/tenants", json=malo)).status_code == 422


# Lo que no entra en un renglón: los cortes de str.splitlines() (\n, \r, \x0b, \x0c, \x1c, \x85, U+2028, U+2029) y el
# resto de los caracteres de control. El nombre del cliente va en el asunto de todos sus mails, y con un corte ahí
# EmailMessage lanza: no saldría ningún link, ninguna invitación ni ningún aviso de ese cliente.
FUERA_DE_UN_RENGLON = ["\n", "\r", "\r\n", "\x0b", "\x0c", "\x1c", "\x85", " ", " ", "\t", "\x00", "\x7f"]


@pytest.mark.parametrize("caracter", FUERA_DE_UN_RENGLON, ids=ascii)
async def test_un_nombre_que_no_entra_en_un_renglon_es_422_y_no_crea_nada(cliente, radar_ctx, caracter):
    """El del cliente, el de su primera línea, el del dueño y el de una línea nueva."""
    await _admin(cliente, radar_ctx)
    nombre = f"Farmacia{caracter}Bcc: espia@otro.com"
    for alta in (dict(ALTA, nombre=nombre), dict(ALTA, linea={"nombre": nombre}),
                 dict(ALTA, dueno={"email": "dueno@farmacia.com", "nombre": nombre})):
        r = await cliente.post("/radar/admin/tenants", json=alta)
        assert r.status_code == 422, (alta, r.status_code)
    a = await crear_tenant_directo(radar_ctx.db, "A")
    assert (await cliente.post(f"/radar/admin/tenants/{a}/lineas", json={"nombre": nombre})).status_code == 422
    async with radar_ctx.db.sin_tenant() as con:
        assert [f["nombre"] for f in await con.fetch("SELECT * FROM radar_admin_listar_tenants()")] == ["A"]
    async with radar_ctx.db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT count(*) FROM lines") == 0
    assert radar_ctx.mailer.enviados == []
    assert list(Path(radar_ctx.settings.secrets_dir).glob("*")) == []       # ni una k_tenant colgada


async def test_un_nombre_con_tildes_espacios_y_simbolos_sigue_valiendo(cliente, radar_ctx):
    await _admin(cliente, radar_ctx)
    nombre = "Farmacia Ñandú — Sucursal «Centro» 🐾"            # con un espacio duro: es un espacio, no un corte
    r = await cliente.post("/radar/admin/tenants", json=dict(
        ALTA, nombre=nombre, dueno={"email": "dueno@farmacia.com", "nombre": "José Pérez"},
        linea={"nombre": "Línea 1 — Mostrador"}))
    assert r.status_code == 201, r.text
    assert radar_ctx.mailer.enviados[-1].asunto == f"Invitación a Radar de {nombre}"


async def test_alta_requiere_rol_admin(cliente, radar_ctx):
    assert (await cliente.post("/radar/admin/tenants", json=ALTA)).status_code == 401
    a = await crear_tenant_directo(radar_ctx.db, "A")
    u = await crear_usuario(radar_ctx.db, a, "dueno@cliente.com", "dueno")
    await entrar(cliente, radar_ctx, a, u, "dueno")
    assert (await cliente.post("/radar/admin/tenants", json=ALTA)).status_code == 403
    assert (await cliente.get("/radar/admin/tenants")).status_code == 403


async def test_listar_tenants_auditado(cliente, radar_ctx):
    admin = await _admin(cliente, radar_ctx)
    await crear_tenant_directo(radar_ctx.db, "A")
    await crear_tenant_directo(radar_ctx.db, "B")
    r = await cliente.get("/radar/admin/tenants")
    assert r.status_code == 200
    assert [t["nombre"] for t in r.json()] == ["A", "B"]
    async with radar_ctx.db.tenant_tx(TENANT_KIS) as con:
        fila = await con.fetchrow("SELECT actor_user_id, detalle FROM access_audit_log WHERE accion = 'tenants_listados'")
    assert fila["actor_user_id"] == admin and '"cantidad": 2' in fila["detalle"]


async def test_segunda_linea_para_cliente_existente(cliente, radar_ctx):
    await _admin(cliente, radar_ctx)
    a = await crear_tenant_directo(radar_ctx.db, "A")
    r = await cliente.post(f"/radar/admin/tenants/{a}/lineas",
                           json={"nombre": "Sucursal norte", "parametros": {"tope_ia_mensual_usd": "12.50"}})
    assert r.status_code == 201
    async with radar_ctx.db.tenant_tx(a) as con:
        fila = await con.fetchrow("SELECT nombre, tope_ia_mensual_usd FROM lines WHERE id = $1", uuid.UUID(r.json()["line_id"]))
        assert fila["nombre"] == "Sucursal norte" and str(fila["tope_ia_mensual_usd"]) == "12.50"
        assert await con.fetchval("SELECT count(*) FROM access_audit_log WHERE accion = 'linea_creada'") == 1
    assert (await cliente.post(f"/radar/admin/tenants/{uuid.uuid4()}/lineas",
                               json={"nombre": "x", "parametros": {}})).status_code == 404


async def test_reenviar_invitacion(cliente, radar_ctx):
    await _admin(cliente, radar_ctx)
    a = await crear_tenant_directo(radar_ctx.db, "A")
    await crear_usuario(radar_ctx.db, a, "dueno@cliente.com", "dueno")
    r = await cliente.post(f"/radar/admin/tenants/{a}/invitaciones", json={"email": "dueno@cliente.com"})
    assert r.status_code == 202 and r.json() == {"enviada": True}
    assert radar_ctx.mailer.enviados[-1].para == "dueno@cliente.com"
    r = await cliente.post(f"/radar/admin/tenants/{a}/invitaciones", json={"email": "nadie@cliente.com"})
    assert r.status_code == 404


async def test_script_crear_admin_imprime_el_link(radar_urls, tmp_path, monkeypatch, capsys):
    import scripts.radar_admin as script
    from app.radar.settings import get_radar_settings
    monkeypatch.setenv("RADAR_DATABASE_URL", radar_urls["app"])
    monkeypatch.setenv("RADAR_MIGRATOR_DATABASE_URL", radar_urls["migrator"])
    monkeypatch.setenv("RADAR_FUENTE_DATABASE_URL", radar_urls["fuente"])
    monkeypatch.setenv("RADAR_COOKIE_SECRET", "secreto-de-test-de-32-caracteres!")
    monkeypatch.setenv("RADAR_SECRETS_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("RADAR_PUBLIC_BASE_URL", "https://radar.test")
    get_radar_settings.cache_clear()
    try:
        script.main(["crear-admin", "--email", "primer-admin@keepitsimple.com.ar", "--nombre", "Mariano"])
    finally:
        get_radar_settings.cache_clear()
    salida = capsys.readouterr().out
    assert "https://radar.test/radar/login/canjear?t=00000000-0000-0000-0000-000000000001#k=" in salida


async def test_alta_y_reenvio_con_mailer_que_falla(cliente, radar_ctx, monkeypatch):
    """Revisión final: el alta ya quedó hecha; un fallo del mail no la vuelve un
    500, se informa con invitacion_enviada/enviada = false."""
    from .helpers import MailerQueFalla
    await _admin(cliente, radar_ctx)
    monkeypatch.setattr(radar_ctx, "mailer", MailerQueFalla())
    r = await cliente.post("/radar/admin/tenants", json=ALTA)
    assert r.status_code == 201 and r.json()["invitacion_enviada"] is False
    tid = uuid.UUID(r.json()["tenant_id"])
    async with radar_ctx.db.tenant_tx(tid) as con:
        assert await con.fetchval("SELECT count(*) FROM users") == 1
        assert await con.fetchval("SELECT count(*) FROM product_events WHERE evento = 'invitacion_enviada'") == 0
    r = await cliente.post(f"/radar/admin/tenants/{tid}/invitaciones", json={"email": "dueno@farmacia.com"})
    assert r.status_code == 202 and r.json() == {"enviada": False}


async def test_flujo_de_la_pantalla_de_alta_de_la_consola(cliente, radar_ctx):
    """Lo que hace la sección "Alta" de la Consola, en su orden y con los cuerpos que ella manda (sin parámetros ni
    perfil: los pone el servidor): cliente con su línea, la línea en la tabla, otra línea y el reenvío al dueño."""
    await _admin(cliente, radar_ctx)
    r = await cliente.post("/radar/admin/tenants", json={
        "nombre": "Farmacia del Centro", "rubro": "farmacia",
        "dueno": {"email": "Dueno@Farmacia.com", "nombre": "Ana"}, "linea": {"nombre": "Local centro"}})
    assert r.status_code == 201, r.text
    tid, lid = r.json()["tenant_id"], r.json()["line_id"]
    assert r.json()["invitacion_enviada"] is True
    assert len(radar_ctx.mailer.enviados) == 1

    # los selects de cliente salen de /tenants; "Operar" necesita los cuatro datos de la fila de la tabla
    assert [(c["id"], c["nombre"]) for c in (await cliente.get("/radar/admin/tenants")).json()] == \
           [(tid, "Farmacia del Centro")]
    filas = (await cliente.get("/radar/admin/consola/lineas")).json()
    assert [(f["tenant_id"], f["tenant_nombre"], f["line_id"], f["line_nombre"]) for f in filas] == \
           [(tid, "Farmacia del Centro", lid, "Local centro")]

    r = await cliente.post(f"/radar/admin/tenants/{tid}/lineas", json={"nombre": "Sucursal norte"})
    assert r.status_code == 201
    filas = (await cliente.get("/radar/admin/consola/lineas")).json()
    assert {f["line_nombre"] for f in filas} == {"Local centro", "Sucursal norte"}
    assert {f["line_id"] for f in filas} == {lid, r.json()["line_id"]} and {f["tenant_id"] for f in filas} == {tid}

    # el admin escribe el email como quiere: el servidor lo normaliza y manda otra invitación al mismo dueño
    r = await cliente.post(f"/radar/admin/tenants/{tid}/invitaciones", json={"email": " DUENO@farmacia.com "})
    assert r.status_code == 202 and r.json() == {"enviada": True}
    assert len(radar_ctx.mailer.enviados) == 2 and radar_ctx.mailer.enviados[-1].para == "dueno@farmacia.com"
