"""
API de la Consola KIS (§3.1): C1 lista con semáforo, C2 consentimiento
asistido + vínculo con QR, C4 acciones con doble confirmación. Solo el rol
admin del tenant KIS; todo auditado.
"""
import json
import uuid

from app.radar.constantes import TENANT_KIS
from app.radar.parametros_service import lineas_vivas_sin_consentir
from app.radar.vinculos import aplicar_status

from .helpers import (MailerQueFalla, como_superusuario, crear_linea_directa, crear_tenant_directo, crear_usuario,
                      entrar, escenario_vinculable, vincular_de_prueba)
from .waha_falso import PNG


def _url(esc, sufijo="", line_id=None):
    return f"/radar/admin/tenants/{esc['tenant_id']}/lineas/{line_id or esc['line_id']}{sufijo}"


async def _admin(cliente, ctx):
    uid = await crear_usuario(ctx.db, TENANT_KIS, "admin@keepitsimple.com.ar", "admin")
    await entrar(cliente, ctx, TENANT_KIS, uid, "admin")
    return uid


async def _sql(ctx, esc, sql, *args):
    async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
        return await con.fetchrow(sql, *args)


async def test_solo_admins_de_kis(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    await entrar(cliente, ctx_waha, esc["tenant_id"], esc["dueno_id"], "dueno")
    assert (await cliente.get("/radar/admin/consola/lineas")).status_code == 403
    assert (await cliente.post(_url(esc, "/vinculo"), json={})).status_code == 403
    cliente.cookies.clear()
    assert (await cliente.get("/radar/admin/consola/lineas")).status_code == 401
    assert waha.llamadas == []


async def test_lista_todas_las_lineas_con_semaforo_y_pendientes(cliente, ctx_waha, waha):
    a = await escenario_vinculable(ctx_waha, "Farmacia A")
    await escenario_vinculable(ctx_waha, "Farmacia B")
    await vincular_de_prueba(ctx_waha, waha, a)
    await _admin(cliente, ctx_waha)
    r = await cliente.get("/radar/admin/consola/lineas")
    assert r.status_code == 200
    por = {f["tenant_nombre"]: f for f in r.json()}
    assert set(por) == {"Farmacia A", "Farmacia B"}
    assert (por["Farmacia A"]["semaforo"], por["Farmacia A"]["numero"]) == ("verde", "…4567")
    assert (por["Farmacia B"]["semaforo"], por["Farmacia B"]["link_estado"]) == ("gris", None)
    assert por["Farmacia A"]["pendiente"] == {"ultimo_mensaje": "tramo 3", "sincronizacion": "tramo 3",
                                              "huecos": "tramo 3", "gasto_ia_mes": "tramo 5"}
    assert por["Farmacia A"]["ultimo_mensaje"] is None and por["Farmacia A"]["worker"] == "w1 (1/50)"
    assert "5493411234567" not in r.text


async def test_filtros_por_estado_y_cliente(cliente, ctx_waha, waha):
    a = await escenario_vinculable(ctx_waha, "Farmacia A")
    b = await escenario_vinculable(ctx_waha, "Farmacia B")
    await vincular_de_prueba(ctx_waha, waha, a)
    await _admin(cliente, ctx_waha)
    vinculadas = (await cliente.get("/radar/admin/consola/lineas?estado=vinculada")).json()
    assert [f["tenant_nombre"] for f in vinculadas] == ["Farmacia A"]
    de_b = (await cliente.get(f"/radar/admin/consola/lineas?tenant_id={b['tenant_id']}")).json()
    assert [f["tenant_nombre"] for f in de_b] == ["Farmacia B"]
    assert (await cliente.get("/radar/admin/consola/lineas?estado=otro")).status_code == 422


async def test_texto_de_consentimiento_con_los_parametros_de_la_linea(cliente, ctx_waha):
    esc = await escenario_vinculable(ctx_waha)
    await _admin(cliente, ctx_waha)
    t = (await cliente.get(_url(esc, "/consentimiento-asistido/texto"))).json()
    assert t["version"] == "v1" and "Qué hacemos" in t["texto"] and len(t["hash"]) == 64
    assert t["parametros_linea"]["duracion_vinculo_dias"] == 0 and t["linea_nombre"] == "Local centro"


async def test_consentimiento_asistido_registra_quien_y_como_y_manda_copia(cliente, ctx_waha):
    esc = await escenario_vinculable(ctx_waha)
    nueva = await crear_linea_directa(ctx_waha.db, esc["tenant_id"], "Sucursal")
    admin = await _admin(cliente, ctx_waha)
    r = await cliente.post(_url(esc, "/consentimiento-asistido", line_id=nueva),
                           json={"version_texto": "v1", "titular_leyo_y_acepto": True, "modo": "videollamada",
                                 "nombre": "Ana Pérez"})
    assert r.status_code == 201 and r.json()["copia_enviada"] is True
    c = await _sql(ctx_waha, esc, "SELECT user_id, modo, cargado_por, modo_asistencia, aceptado_por_nombre, opciones "
                                  "FROM consents WHERE line_id = $1", nueva)
    assert (c["user_id"], c["modo"], c["cargado_por"], c["modo_asistencia"], c["aceptado_por_nombre"]) == \
           (esc["dueno_id"], "asistido", admin, "videollamada", "Ana Pérez")
    assert json.loads(c["opciones"])["parametros_linea"]["duracion_vinculo_dias"] == 0
    mail = ctx_waha.mailer.enviados[-1]
    assert mail.para == esc["dueno_email"] and "versión v1" in mail.texto and "Qué hacemos" in mail.texto
    audit = await _sql(ctx_waha, esc, "SELECT actor_rol, detalle::text AS d FROM access_audit_log "
                                      "WHERE accion = 'consentimiento_asistido'")
    assert audit["actor_rol"] == "admin" and "videollamada" in audit["d"]


async def test_consentimiento_asistido_exige_la_aceptacion_y_un_dueno(cliente, ctx_waha):
    esc = await escenario_vinculable(ctx_waha)
    await _admin(cliente, ctx_waha)
    cuerpo = {"version_texto": "v1", "titular_leyo_y_acepto": False, "modo": "presencial", "nombre": "Ana"}
    r = await cliente.post(_url(esc, "/consentimiento-asistido"), json=cuerpo)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "consentimiento_no_aceptado"
    sin_dueno = await crear_tenant_directo(ctx_waha.db, "Sin dueño")
    li = await crear_linea_directa(ctx_waha.db, sin_dueno)
    r = await cliente.post(f"/radar/admin/tenants/{sin_dueno}/lineas/{li}/consentimiento-asistido",
                           json={**cuerpo, "titular_leyo_y_acepto": True})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "sin_dueno"


async def test_vincular_muestra_qr_solo_con_consentimiento(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    sin = await crear_linea_directa(ctx_waha.db, esc["tenant_id"], "Sin consentimiento")
    await _admin(cliente, ctx_waha)
    r = await cliente.post(_url(esc, "/vinculo", line_id=sin), json={})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "sin_consentimiento"
    r = await cliente.post(_url(esc, "/vinculo"), json={"full_sync": False})
    assert r.status_code == 201 and r.json()["estado"] == "esperando_qr"
    assert (await cliente.get(_url(esc, "/vinculo"))).json()["qr_disponible"] is True
    qr = await cliente.get(_url(esc, "/vinculo/qr"))
    assert qr.status_code == 200 and qr.content == PNG
    assert qr.headers["content-type"] == "image/png" and qr.headers["cache-control"] == "no-store"


async def test_codigo_y_reinicio_del_qr(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    await _admin(cliente, ctx_waha)
    await cliente.post(_url(esc, "/vinculo"), json={})
    r = await cliente.post(_url(esc, "/vinculo/codigo"), json={"telefono": "+5493411234567"})
    assert r.json() == {"codigo": "ABCD-EFGH"} and r.headers["cache-control"] == "no-store"
    r = await cliente.post(_url(esc, "/vinculo/reiniciar-qr"))
    assert r.status_code == 200 and r.json()["reinicios_restantes"] == 2


async def test_desconectar_exige_confirmar(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    await _admin(cliente, ctx_waha)
    r = await cliente.post(_url(esc, "/vinculo/desconectar"), json={})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "falta_confirmacion"
    assert (await cliente.post(_url(esc, "/vinculo/desconectar"), json={"confirmar": True})).status_code == 202
    job = await _sql(ctx_waha, esc, "SELECT causa FROM jobs WHERE link_id = $1", v["link_id"])
    assert job["causa"] == "pedido_kis"


async def test_borrar_todo_exige_el_nombre_exacto_de_la_linea(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    await vincular_de_prueba(ctx_waha, waha, esc)
    await _admin(cliente, ctx_waha)
    r = await cliente.post(_url(esc, "/vinculo/desconectar-y-borrar"), json={"confirmar": True, "nombre_linea": "Otra"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "confirmacion_incorrecta"
    r = await cliente.post(_url(esc, "/vinculo/desconectar-y-borrar"),
                           json={"confirmar": True, "nombre_linea": "Local centro"})
    assert r.status_code == 202 and r.json()["borrado_solicitado"] is True
    linea = await _sql(ctx_waha, esc, "SELECT borrado_solicitado_at FROM lines WHERE id = $1", esc["line_id"])
    assert linea["borrado_solicitado_at"] is not None


async def test_restriccion_bloquea_reconectar_pero_no_desconectar(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await aplicar_status(con, tenant_id=esc["tenant_id"], link_id=v["link_id"], waha_status="FAILED",
                             origen="webhook")
    await _admin(cliente, ctx_waha)
    r = await cliente.put(_url(esc, "/vinculo/restriccion"), json={"hasta": None})
    assert r.status_code == 200 and r.json()["restriccion_activa"] is True
    r = await cliente.post(_url(esc, "/vinculo"), json={})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "restriccion_activa"
    assert (await cliente.post(_url(esc, "/vinculo/desconectar"), json={"confirmar": True})).status_code == 202
    assert (await cliente.delete(_url(esc, "/vinculo/restriccion"))).json()["restriccion_activa"] is False


async def test_las_acciones_quedan_auditadas_con_el_admin(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    admin = await _admin(cliente, ctx_waha)
    await cliente.post(_url(esc, "/vinculo"), json={})
    await cliente.post(_url(esc, "/vinculo/codigo"), json={"telefono": "+5493411234567"})
    await cliente.post(_url(esc, "/vinculo/desconectar"), json={"confirmar": True})
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        filas = await con.fetch("SELECT accion, actor_rol, actor_user_id FROM access_audit_log ORDER BY id")
    assert [f["accion"] for f in filas] == ["vinculo_iniciado", "codigo_solicitado", "desconexion_pedida"]
    assert all(f["actor_rol"] == "admin" and f["actor_user_id"] == admin for f in filas)


async def test_rutas_de_linea_solo_sobre_un_tenant_cliente(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    await _admin(cliente, ctx_waha)
    kis = f"/radar/admin/tenants/{TENANT_KIS}/lineas/{esc['line_id']}"
    otro = f"/radar/admin/tenants/{uuid.uuid4()}/lineas/{esc['line_id']}"
    for r in (await cliente.get(kis + "/consentimiento-asistido/texto"),
              await cliente.post(kis + "/vinculo", json={}),
              await cliente.post(otro + "/vinculo/desconectar", json={"confirmar": True}),
              await cliente.put(otro + "/vinculo/restriccion", json={"hasta": None})):
        assert r.status_code == 404 and r.json()["detail"]["error"] == "tenant_inexistente"
    assert waha.llamadas == []


async def test_copia_que_no_sale_no_rompe_el_consentimiento(cliente, ctx_waha):
    esc = await escenario_vinculable(ctx_waha)
    nueva = await crear_linea_directa(ctx_waha.db, esc["tenant_id"], "Sucursal")
    await _admin(cliente, ctx_waha)
    ctx_waha.mailer = MailerQueFalla()
    r = await cliente.post(_url(esc, "/consentimiento-asistido", line_id=nueva),
                           json={"version_texto": "v1", "titular_leyo_y_acepto": True, "modo": "presencial",
                                 "nombre": "Ana"})
    assert r.status_code == 201 and r.json()["copia_enviada"] is False
    assert (await _sql(ctx_waha, esc, "SELECT count(*) AS n FROM consents WHERE line_id = $1", nueva))["n"] == 1


async def test_asistido_arrastra_la_propuesta_de_tenant_ya_aceptada(cliente, ctx_waha, radar_urls):
    esc = await escenario_vinculable(ctx_waha)
    t, li = esc["tenant_id"], esc["line_id"]
    await como_superusuario(radar_urls, "UPDATE tenants SET parametros_propuestos = '{\"ia_habilitada\": true}'::jsonb, "
                                        "parametros_propuestos_at = now() - interval '1 hour' WHERE id = $1", t)
    async with ctx_waha.db.tenant_tx(t) as con:        # el dueño ya había aceptado la propuesta
        await con.execute("UPDATE lines SET estado = 'vinculada' WHERE id = $1", li)
        await con.execute("INSERT INTO consents (line_id, user_id, version_texto, hash_texto, opciones) "
                          "VALUES ($1, $2, 'v1', repeat('a', 64), $3::jsonb)", li, esc["dueno_id"],
                          json.dumps({"parametros_linea": {}, "parametros_tenant": {"ia_habilitada": True}}))
        assert await lineas_vivas_sin_consentir(con, {"ia_habilitada": True}) == []
    await _admin(cliente, ctx_waha)
    r = await cliente.post(_url(esc, "/consentimiento-asistido"),
                           json={"version_texto": "v1", "titular_leyo_y_acepto": True, "modo": "presencial",
                                 "nombre": "Ana"})
    assert r.status_code == 201
    c = await _sql(ctx_waha, esc, "SELECT opciones FROM consents WHERE modo = 'asistido'")
    assert json.loads(c["opciones"])["parametros_tenant"]["ia_habilitada"] is True
    async with ctx_waha.db.tenant_tx(t) as con:        # la línea no vuelve a quedar pendiente
        assert await lineas_vivas_sin_consentir(con, {"ia_habilitada": True}) == []


async def test_consentimiento_asistido_solo_acepta_la_version_vigente(cliente, ctx_waha, monkeypatch):
    """Final: una versión vieja de VERSIONES no sirve para un consentimiento nuevo."""
    from app.radar.consentimiento import VERSIONES
    monkeypatch.setitem(VERSIONES, "v0", "texto viejo")
    esc = await escenario_vinculable(ctx_waha)
    await _admin(cliente, ctx_waha)
    nueva = await crear_linea_directa(ctx_waha.db, esc["tenant_id"], "Nueva")
    r = await cliente.post(_url(esc, "/consentimiento-asistido", line_id=nueva),
                           json={"version_texto": "v0", "titular_leyo_y_acepto": True, "modo": "presencial",
                                 "nombre": "Ana"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "version_desconocida"
    fila = await _sql(ctx_waha, esc, "SELECT count(*) AS n FROM consents WHERE line_id = $1", nueva)
    assert fila["n"] == 0
