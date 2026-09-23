"""
Regla de §2.1: los parámetros los fija un admin de KIS; el cliente solo
endurece; endurecer se aplica directo y queda auditado; aflojar sobre una
línea viva exige una fila nueva en consents que acepte lo que KIS propuso.
"""
import hashlib
import uuid

from app.radar.consentimiento import VERSIONES
from app.radar.constantes import TENANT_KIS

from .helpers import crear_linea_directa, crear_tenant_directo, crear_usuario, entrar

CONSENT = {"version_texto": "v1", "acepta": True, "titular": True}


async def _base(ctx, estado="sin_vinculo"):
    a = await crear_tenant_directo(ctx.db, "A")
    d = await crear_usuario(ctx.db, a, "dueno@cliente.com", "dueno")
    l = await crear_linea_directa(ctx.db, a, "Centro", estado=estado)
    adm = await crear_usuario(ctx.db, TENANT_KIS, "admin@keepitsimple.com.ar", "admin")
    return a, d, l, adm


async def _auditados(ctx, tenant_id, accion="parametro_cambiado"):
    async with ctx.db.tenant_tx(tenant_id) as con:
        return [f["detalle"] for f in await con.fetch(
            "SELECT detalle FROM access_audit_log WHERE accion = $1 ORDER BY id", accion)]


async def test_get_parametros_de_linea_y_tenant(cliente, radar_ctx):
    a, d, l, _ = await _base(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    r = await cliente.get(f"/radar/api/lineas/{l}/parametros")
    assert r.status_code == 200
    assert r.json() == {
        "linea": {"duracion_vinculo_dias": 0, "retencion_fuente_dias": 0,
                  "retencion_tras_desvinculo_dias": 0, "tope_ia_mensual_usd": None},
        "estado": "sin_vinculo", "almacen_fuente": "permanente",
        "tenant": {"retencion_fichas_meses": 12, "perfil_de_datos": "estandar", "retener_fragmentos": False,
                   "ia_habilitada": True, "via_llm": "lotes"},
        "propuesta": {"linea": {}, "tenant": {}},
    }
    g = await crear_usuario(radar_ctx.db, a, "g@cliente.com", "gestor", lineas_permitidas=[uuid.uuid4()])
    await entrar(cliente, radar_ctx, a, g, "gestor")
    assert (await cliente.get(f"/radar/api/lineas/{l}/parametros")).status_code == 404


async def test_gestor_y_lector_no_cambian_parametros(cliente, radar_ctx):
    """requiere_rol("dueno") en los dos PUT del cliente: el rol se valida en el
    servidor en cada consulta (§4.4)."""
    a, d, l, _ = await _base(radar_ctx)
    for rol in ("gestor", "lector"):
        u = await crear_usuario(radar_ctx.db, a, f"{rol}@cliente.com", rol)
        await entrar(cliente, radar_ctx, a, u, rol)
        assert (await cliente.put(f"/radar/api/lineas/{l}/parametros", json={"duracion_vinculo_dias": 1})).status_code == 403
        assert (await cliente.put("/radar/api/cuenta/parametros", json={"ia_habilitada": False})).status_code == 403
        assert (await cliente.get(f"/radar/api/lineas/{l}/consentimientos")).status_code == 403
    async with radar_ctx.db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT duracion_vinculo_dias FROM lines WHERE id = $1", l) == 0
        assert await con.fetchval("SELECT ia_habilitada FROM tenants") is True


async def test_dueno_endurece_directo_y_queda_auditado(cliente, radar_ctx):
    a, d, l, _ = await _base(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    r = await cliente.put(f"/radar/api/lineas/{l}/parametros",
                          json={"duracion_vinculo_dias": 30, "tope_ia_mensual_usd": "20.00"})
    assert r.status_code == 200
    assert r.json()["duracion_vinculo_dias"] == 30 and r.json()["tope_ia_mensual_usd"] == "20.00"
    detalles = await _auditados(radar_ctx, a)
    assert len(detalles) == 2
    assert '"parametro": "duracion_vinculo_dias"' in detalles[0] and '"valor_anterior": 0' in detalles[0] \
        and '"valor_nuevo": 30' in detalles[0] and '"ambito": "linea"' in detalles[0]


async def test_dueno_no_puede_aflojar(cliente, radar_ctx):
    a, d, l, _ = await _base(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    await cliente.put(f"/radar/api/lineas/{l}/parametros", json={"duracion_vinculo_dias": 30})
    r = await cliente.put(f"/radar/api/lineas/{l}/parametros", json={"duracion_vinculo_dias": 0})
    assert r.status_code == 403 and r.json()["detail"] == {"error": "solo_endurecer", "parametros": ["duracion_vinculo_dias"]}
    r = await cliente.put(f"/radar/api/lineas/{l}/parametros", json={"duracion_vinculo_dias": 60})
    assert r.status_code == 403
    r = await cliente.put(f"/radar/api/lineas/{l}/parametros", json={"duracion_vinculo_dias": 7})
    assert r.status_code == 200 and r.json()["duracion_vinculo_dias"] == 7


async def test_valores_invalidos_y_almacen(cliente, radar_ctx):
    a, d, l, adm = await _base(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    r = await cliente.put(f"/radar/api/lineas/{l}/parametros", json={"duracion_vinculo_dias": -3})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "valor_invalido"
    r = await cliente.put(f"/radar/api/lineas/{l}/parametros", json={"inventado": 1})
    assert r.status_code == 422
    r = await cliente.put(f"/radar/api/lineas/{l}/parametros", json={"retencion_fuente_dias": 7})   # más estricto, pero sin almacén
    assert r.status_code == 422 and r.json()["detail"]["error"] == "almacen_no_disponible"
    assert (await cliente.put(f"/radar/api/lineas/{uuid.uuid4()}/parametros", json={"duracion_vinculo_dias": 1})).status_code == 404


async def test_admin_afloja_directo_sobre_linea_sin_vinculo(cliente, radar_ctx):
    a, d, l, adm = await _base(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    await cliente.put(f"/radar/api/lineas/{l}/parametros", json={"duracion_vinculo_dias": 30})
    await entrar(cliente, radar_ctx, TENANT_KIS, adm, "admin")
    r = await cliente.put(f"/radar/admin/tenants/{a}/lineas/{l}/parametros", json={"duracion_vinculo_dias": 0})
    assert r.status_code == 200 and r.json()["duracion_vinculo_dias"] == 0
    detalles = await _auditados(radar_ctx, a)
    assert '"valor_anterior": 30' in detalles[-1] and '"valor_nuevo": 0' in detalles[-1]


async def test_aflojar_linea_viva_exige_consentimiento(cliente, radar_ctx):
    a, d, l, adm = await _base(radar_ctx, estado="vinculada")
    await entrar(cliente, radar_ctx, a, d, "dueno")
    assert (await cliente.put(f"/radar/api/lineas/{l}/parametros", json={"duracion_vinculo_dias": 30})).status_code == 200

    await entrar(cliente, radar_ctx, TENANT_KIS, adm, "admin")
    r = await cliente.put(f"/radar/admin/tenants/{a}/lineas/{l}/parametros", json={"duracion_vinculo_dias": 0})
    assert r.status_code == 409 and r.json()["detail"] == {"error": "requiere_consentimiento", "parametros": ["duracion_vinculo_dias"]}
    async with radar_ctx.db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT duracion_vinculo_dias FROM lines WHERE id = $1", l) == 30
        # el 409 dejó la propuesta guardada y auditada
        assert await con.fetchval("SELECT parametros_propuestos FROM lines WHERE id = $1", l) == '{"duracion_vinculo_dias": 0}'
        prop = await con.fetchrow("SELECT actor_user_id, detalle FROM access_audit_log WHERE accion = 'parametro_propuesto'")
        assert prop["actor_user_id"] == adm and '"valor_nuevo": 0' in prop["detalle"]

    await entrar(cliente, radar_ctx, a, d, "dueno")
    r = await cliente.get(f"/radar/api/lineas/{l}/parametros")
    assert r.json()["propuesta"] == {"linea": {"duracion_vinculo_dias": 0}, "tenant": {}}
    # otro valor que el propuesto: no hay consentimiento sin propuesta
    r = await cliente.post(f"/radar/api/lineas/{l}/consentimientos",
                           json=dict(CONSENT, parametros_linea={"duracion_vinculo_dias": 7}))
    assert r.status_code == 422 and r.json()["detail"] == {"error": "sin_propuesta", "parametros": ["duracion_vinculo_dias"]}

    r = await cliente.post(f"/radar/api/lineas/{l}/consentimientos",
                           json=dict(CONSENT, parametros_linea={"duracion_vinculo_dias": 0}))
    assert r.status_code == 201, r.text
    cuerpo = r.json()
    assert cuerpo["hash_texto"] == hashlib.sha256(VERSIONES["v1"].encode()).hexdigest()
    assert cuerpo["tenant_aplicado"] is True and cuerpo["lineas_pendientes"] == []
    async with radar_ctx.db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT duracion_vinculo_dias FROM lines WHERE id = $1", l) == 0
        assert await con.fetchval("SELECT parametros_propuestos FROM lines WHERE id = $1", l) is None   # consumida
        c = await con.fetchrow("SELECT version_texto, hash_texto, user_id, ip, opciones FROM consents WHERE id = $1",
                               uuid.UUID(cuerpo["consent_id"]))
        assert c["version_texto"] == "v1" and c["hash_texto"] == cuerpo["hash_texto"] and c["user_id"] == d
        assert c["ip"] is not None
        assert '"duracion_vinculo_dias": 0' in c["opciones"] and '"parametros_tenant"' in c["opciones"]
        assert await con.fetchval("SELECT count(*) FROM access_audit_log WHERE accion = 'consentimiento_registrado'") == 1
        assert await con.fetchval("SELECT count(*) FROM product_events WHERE evento = 'consentimiento_registrado'") == 1
        assert await con.fetchval("SELECT count(*) FROM consents") == 1        # el 422 no dejó fila

    r = await cliente.get(f"/radar/api/lineas/{l}/consentimientos")
    assert r.status_code == 200 and len(r.json()) == 1 and r.json()[0]["version_texto"] == "v1"


async def test_consentimiento_solo_acepta_lo_propuesto_por_kis(cliente, radar_ctx):
    """§2.1: los parámetros los fija un admin de KIS y el cliente solo endurece.
    Sin propuesta previa, el consentimiento no afloja nada: ni de línea, ni de
    tenant, ni el camino sensible → estandar (§7: solo un admin de KIS)."""
    a = await crear_tenant_directo(radar_ctx.db, "A", perfil="sensible")
    d = await crear_usuario(radar_ctx.db, a, "dueno@cliente.com", "dueno")
    l = await crear_linea_directa(radar_ctx.db, a, "Centro", estado="vinculada")
    await entrar(cliente, radar_ctx, a, d, "dueno")
    assert (await cliente.put(f"/radar/api/lineas/{l}/parametros", json={"duracion_vinculo_dias": 30})).status_code == 200
    assert (await cliente.put("/radar/api/cuenta/parametros", json={"ia_habilitada": False})).status_code == 200
    for malo, parametro in (({"parametros_tenant": {"perfil_de_datos": "estandar"}}, "perfil_de_datos"),
                            ({"parametros_tenant": {"ia_habilitada": True}}, "ia_habilitada"),
                            ({"parametros_linea": {"duracion_vinculo_dias": 0}}, "duracion_vinculo_dias"),
                            ({"parametros_linea": {"duracion_vinculo_dias": 30}}, "duracion_vinculo_dias")):   # ni el valor actual
        r = await cliente.post(f"/radar/api/lineas/{l}/consentimientos", json=dict(CONSENT, **malo))
        assert r.status_code == 422 and r.json()["detail"] == {"error": "sin_propuesta", "parametros": [parametro]}, malo
    async with radar_ctx.db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT perfil_de_datos FROM tenants") == "sensible"
        assert await con.fetchval("SELECT ia_habilitada FROM tenants") is False
        assert await con.fetchval("SELECT duracion_vinculo_dias FROM lines WHERE id = $1", l) == 30
        assert await con.fetchval("SELECT count(*) FROM consents") == 0


async def test_consentimiento_invalido(cliente, radar_ctx):
    a, d, l, _ = await _base(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    for malo in (dict(CONSENT, acepta=False), dict(CONSENT, titular=False), dict(CONSENT, version_texto="v99"),
                 dict(CONSENT, parametros_linea={"ia_habilitada": False}),
                 dict(CONSENT, parametros_tenant={"duracion_vinculo_dias": 1}),
                 dict(CONSENT, parametros_linea={"duracion_vinculo_dias": 0})):    # sin propuesta
        r = await cliente.post(f"/radar/api/lineas/{l}/consentimientos", json=malo)
        assert r.status_code == 422, malo
    le = await crear_usuario(radar_ctx.db, a, "lector@cliente.com", "lector")
    await entrar(cliente, radar_ctx, a, le, "lector")
    assert (await cliente.post(f"/radar/api/lineas/{l}/consentimientos", json=CONSENT)).status_code == 403


async def test_parametros_de_tenant_del_dueno(cliente, radar_ctx):
    a, d, l, _ = await _base(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    r = await cliente.put("/radar/api/cuenta/parametros", json={"ia_habilitada": False, "perfil_de_datos": "sensible"})
    assert r.status_code == 200 and r.json()["ia_habilitada"] is False and r.json()["perfil_de_datos"] == "sensible"
    detalles = await _auditados(radar_ctx, a)
    assert any('"ambito": "tenant"' in x and '"parametro": "perfil_de_datos"' in x for x in detalles)
    r = await cliente.put("/radar/api/cuenta/parametros", json={"perfil_de_datos": "estandar"})
    assert r.status_code == 403        # el camino inverso solo lo hace un admin de KIS


async def test_tenant_mas_laxo_con_lineas_vivas(cliente, radar_ctx):
    a, d, l1, adm = await _base(radar_ctx, estado="vinculada")
    l2 = await crear_linea_directa(radar_ctx.db, a, "Norte", estado="vinculada")
    await entrar(cliente, radar_ctx, a, d, "dueno")
    assert (await cliente.put("/radar/api/cuenta/parametros", json={"ia_habilitada": False})).status_code == 200

    await entrar(cliente, radar_ctx, TENANT_KIS, adm, "admin")
    r = await cliente.put(f"/radar/admin/tenants/{a}/parametros", json={"ia_habilitada": True})
    assert r.status_code == 409
    assert set(r.json()["detail"]["lineas"]) == {str(l1), str(l2)}
    async with radar_ctx.db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT parametros_propuestos FROM tenants") == '{"ia_habilitada": true}'

    await entrar(cliente, radar_ctx, a, d, "dueno")
    assert (await cliente.get(f"/radar/api/lineas/{l2}/parametros")).json()["propuesta"] == {"linea": {}, "tenant": {"ia_habilitada": True}}
    r = await cliente.post(f"/radar/api/lineas/{l1}/consentimientos", json=dict(CONSENT, parametros_tenant={"ia_habilitada": True}))
    assert r.status_code == 201 and r.json()["tenant_aplicado"] is False and r.json()["lineas_pendientes"] == [str(l2)]
    async with radar_ctx.db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT ia_habilitada FROM tenants") is False
        assert await con.fetchval("SELECT parametros_propuestos FROM tenants") is not None   # sigue pendiente
    r = await cliente.post(f"/radar/api/lineas/{l2}/consentimientos", json=dict(CONSENT, parametros_tenant={"ia_habilitada": True}))
    assert r.status_code == 201 and r.json()["tenant_aplicado"] is True
    async with radar_ctx.db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT ia_habilitada FROM tenants") is True
        assert await con.fetchval("SELECT parametros_propuestos FROM tenants") is None       # consumida

    await entrar(cliente, radar_ctx, TENANT_KIS, adm, "admin")
    assert (await cliente.put(f"/radar/admin/tenants/{a}/parametros", json={"retencion_fichas_meses": 0})).status_code == 409


async def test_admin_tenant_sin_lineas_vivas_aplica_directo(cliente, radar_ctx):
    a, d, l, adm = await _base(radar_ctx)
    await entrar(cliente, radar_ctx, TENANT_KIS, adm, "admin")
    r = await cliente.put(f"/radar/admin/tenants/{a}/parametros", json={"retencion_fichas_meses": 0, "via_llm": "lotes"})
    assert r.status_code == 200 and r.json()["retencion_fichas_meses"] == 0
    assert len(await _auditados(radar_ctx, a)) == 1           # via_llm no cambió: no se audita


async def test_cambiar_parametros_tenant_inexistente_da_404(radar_ctx):
    """Igual que cambiar_parametros_linea: la fila ausente (o invisible por RLS)
    es un 404, no un TypeError."""
    import pytest
    from app.radar.parametros_service import CambioRechazado, cambiar_parametros_tenant
    otro = uuid.uuid4()
    async with radar_ctx.db.tenant_tx(otro) as con:
        with pytest.raises(CambioRechazado) as e:
            await cambiar_parametros_tenant(con, tenant_id=otro, nuevos={"ia_habilitada": False}, actor_user_id=None,
                                            actor_rol="admin", ip=None, solo_endurecer=False)
    assert e.value.status == 404 and e.value.detalle == {"error": "tenant_inexistente"}
