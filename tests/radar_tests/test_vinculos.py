"""
Servicio de vínculos: una sola lógica para la Consola KIS y para el dueño.
Consentimiento obligatorio, admisión, verificación, estados, QR, código,
reinicios, fin pedido y restricción de cuenta.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.radar.constantes import TENANT_KIS
from app.radar.vinculos import (VinculoRechazado, aplicar_status, estado_de_linea, iniciar_vinculo,
                                levantar_restriccion, marcar_restriccion, pedir_codigo, pedir_fin, qr_png,
                                reiniciar_qr)
from app.radar.waha.sesion import nombre_sesion
from app.radar.workers import listar_workers, nombre_clave_lectura

from .helpers import crear_link_directo, crear_linea_directa, escenario_vinculable, vincular_de_prueba
from .waha_falso import PNG


async def _iniciar(ctx, esc, **kw):
    return await iniciar_vinculo(ctx, tenant_id=esc["tenant_id"], line_id=esc["line_id"],
                                 actor_user_id=esc["dueno_id"], actor_rol="dueno", ip="10.0.0.1", **kw)


async def _fila(ctx, esc, sql, *args):
    async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
        return await con.fetchrow(sql, *args)


async def _rechazo(coro) -> VinculoRechazado:
    with pytest.raises(VinculoRechazado) as e:
        await coro
    return e.value


def _actor(esc):
    return dict(tenant_id=esc["tenant_id"], line_id=esc["line_id"], actor_user_id=esc["dueno_id"],
                actor_rol="dueno", ip=None)


async def test_iniciar_crea_la_sesion_de_p3_y_una_clave_de_lectura(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    estado = await _iniciar(ctx_waha, esc)
    link_id = uuid.UUID(estado["link_id"])
    assert estado["estado"] == "esperando_qr" and estado["qr_disponible"] is True
    [cuerpo] = waha.cuerpos
    assert cuerpo["name"] == nombre_sesion(link_id)
    assert cuerpo["config"]["metadata"] == {"tenant_id": str(esc["tenant_id"]), "line_id": str(esc["line_id"]),
                                            "link_id": str(link_id)}
    assert ctx_waha.secretos.get(nombre_clave_lectura(link_id)) == b"valor-secreto-1"
    fila = await _fila(ctx_waha, esc, "SELECT key_id, consent_id, engine FROM links WHERE id = $1", link_id)
    assert (fila["key_id"], fila["consent_id"], fila["engine"]) == ("k1", esc["consent_id"], "NOWEB")
    auditado = await _fila(ctx_waha, esc, "SELECT count(*) AS n FROM access_audit_log "
                                          "WHERE accion = 'vinculo_iniciado' AND objeto_id = $1", link_id)
    evento = await _fila(ctx_waha, esc, "SELECT count(*) AS n FROM product_events WHERE evento = 'vinculo_iniciado'")
    assert auditado["n"] == 1 and evento["n"] == 1


async def test_sin_consentimiento_no_se_crea_ninguna_sesion(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    otra = await crear_linea_directa(ctx_waha.db, esc["tenant_id"], "Sin consentimiento")
    e = await _rechazo(iniciar_vinculo(ctx_waha, tenant_id=esc["tenant_id"], line_id=otra,
                                       actor_user_id=esc["dueno_id"], actor_rol="dueno", ip=None))
    assert (e.status, e.codigo) == (409, "sin_consentimiento")
    assert waha.llamadas == []


async def test_un_vinculo_activo_por_linea(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    await _iniciar(ctx_waha, esc)
    e = await _rechazo(_iniciar(ctx_waha, esc))
    assert (e.status, e.codigo) == (409, "vinculo_activo")


async def test_config_distinta_aborta_sin_qr(ctx_waha, waha):
    waha.mutar_eco = lambda c: c["noweb"].__setitem__("markOnline", True)
    esc = await escenario_vinculable(ctx_waha)
    e = await _rechazo(_iniciar(ctx_waha, esc))
    assert (e.status, e.codigo) == (502, "config_no_coincide")
    assert waha.sesiones == {}
    fila = await _fila(ctx_waha, esc, "SELECT estado FROM links WHERE line_id = $1", esc["line_id"])
    audit = await _fila(ctx_waha, esc, "SELECT detalle::text AS d FROM access_audit_log WHERE accion = 'vinculo_abortado'")
    assert fila["estado"] == "abortado" and "config_no_coincide" in audit["d"]


async def test_sin_capacidad_no_crea_sesion_ni_muestra_qr(ctx_waha, waha):
    async with ctx_waha.db.tenant_tx(TENANT_KIS) as con:
        await con.execute("UPDATE waha_workers SET max_sesiones = 1")
    esc = await escenario_vinculable(ctx_waha)
    e = await _rechazo(_iniciar(ctx_waha, esc))
    assert (e.status, e.codigo) == (503, "sin_capacidad")
    assert waha.llamadas == []
    audit = await _fila(ctx_waha, esc, "SELECT count(*) AS n FROM access_audit_log WHERE accion = 'admision_rechazada'")
    assert audit["n"] == 1


async def test_restriccion_activa_bloquea_volver_a_vincular(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    [w] = await listar_workers(ctx_waha)
    await crear_link_directo(ctx_waha.db, esc["tenant_id"], esc["line_id"], w.id, esc["consent_id"], estado="caido",
                             restriccion_hasta=datetime.now(timezone.utc) + timedelta(days=2))
    e = await _rechazo(_iniciar(ctx_waha, esc))
    assert (e.status, e.codigo) == (409, "restriccion_activa")
    assert waha.llamadas == []


async def test_sin_webhook_configurado_no_vincula(ctx_waha, waha):
    ctx_waha.settings = ctx_waha.settings.model_copy(update={"waha_webhook_url": ""})
    esc = await escenario_vinculable(ctx_waha)
    e = await _rechazo(_iniciar(ctx_waha, esc))
    assert (e.status, e.codigo) == (503, "waha_sin_configurar")


async def test_working_vincula_la_linea_y_guarda_solo_el_sufijo(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    fila = await _fila(ctx_waha, esc, "SELECT estado, numero_sufijo, conectado_at FROM links WHERE id = $1",
                       v["link_id"])
    assert (fila["estado"], fila["numero_sufijo"]) == ("vinculado", "4567") and fila["conectado_at"] is not None
    assert (await _fila(ctx_waha, esc, "SELECT estado FROM lines WHERE id = $1", esc["line_id"]))["estado"] == "vinculada"
    ev = await _fila(ctx_waha, esc, "SELECT waha_status, origen FROM link_status_events WHERE link_id = $1 "
                                    "ORDER BY id DESC LIMIT 1", v["link_id"])
    assert (ev["waha_status"], ev["origen"]) == ("WORKING", "webhook")
    todo = await _fila(ctx_waha, esc, "SELECT row_to_json(l)::text AS t FROM links l WHERE id = $1", v["link_id"])
    assert "5493411234567" not in todo["t"]
    estado = await estado_de_linea(ctx_waha, tenant_id=esc["tenant_id"], line_id=esc["line_id"])
    assert estado["numero"] == "…4567" and estado["estado"] == "vinculado"


async def test_reconectar_reemplaza_al_vinculo_caido(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v1 = await vincular_de_prueba(ctx_waha, waha, esc)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        r = await aplicar_status(con, tenant_id=esc["tenant_id"], link_id=v1["link_id"], waha_status="FAILED",
                                 origen="webhook")
    assert r["estado"] == "caido"
    await vincular_de_prueba(ctx_waha, waha, esc)
    job = await _fila(ctx_waha, esc, "SELECT tipo, causa FROM jobs WHERE link_id = $1 AND tipo = 'fin_vinculo'", v1["link_id"])
    assert (job["tipo"], job["causa"]) == ("fin_vinculo", "reemplazado")


async def test_vinculo_caido_que_revive_con_otro_activo_se_cierra(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v1 = await vincular_de_prueba(ctx_waha, waha, esc)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await aplicar_status(con, tenant_id=esc["tenant_id"], link_id=v1["link_id"], waha_status="FAILED",
                             origen="webhook")
    await vincular_de_prueba(ctx_waha, waha, esc, working=False)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        r = await aplicar_status(con, tenant_id=esc["tenant_id"], link_id=v1["link_id"], waha_status="WORKING",
                                 origen="webhook", me_id=waha.me_id)
    assert r["estado"] == "caido"
    job = await _fila(ctx_waha, esc, "SELECT causa FROM jobs WHERE link_id = $1 AND tipo = 'fin_vinculo'", v1["link_id"])
    assert job["causa"] == "reemplazado"


async def test_estado_hace_polling_de_respaldo_sin_eventos(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    estado = await _iniciar(ctx_waha, esc)
    waha.estados[nombre_sesion(uuid.UUID(estado["link_id"]))] = ["SCAN_QR_CODE"]
    estado = await estado_de_linea(ctx_waha, tenant_id=esc["tenant_id"], line_id=esc["line_id"])
    assert estado["waha_status"] == "SCAN_QR_CODE"
    ev = await _fila(ctx_waha, esc, "SELECT origen FROM link_status_events ORDER BY id DESC LIMIT 1")
    assert ev["origen"] == "polling"


async def test_qr_y_hasta_tres_reinicios(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    await _iniciar(ctx_waha, esc)
    assert await qr_png(ctx_waha, tenant_id=esc["tenant_id"], line_id=esc["line_id"]) == PNG
    for i in range(3):
        estado = await reiniciar_qr(ctx_waha, **_actor(esc))
        assert estado["reinicios_restantes"] == 2 - i
    e = await _rechazo(reiniciar_qr(ctx_waha, **_actor(esc)))
    assert (e.status, e.codigo) == (409, "sin_reinicios")


async def test_codigo_de_vinculacion_no_se_guarda(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    await _iniciar(ctx_waha, esc)
    assert await pedir_codigo(ctx_waha, telefono="+5493411234567", **_actor(esc)) == "ABCD-EFGH"
    todo = await _fila(ctx_waha, esc, "SELECT string_agg(detalle::text, '') AS t FROM access_audit_log")
    assert "ABCD" not in todo["t"] and "3411234567" not in todo["t"]
    e = await _rechazo(pedir_codigo(ctx_waha, telefono="+1 555 123 4567", **_actor(esc)))
    assert (e.status, e.codigo) == (422, "telefono_invalido")
    waha.falla_codigo = True
    e = await _rechazo(pedir_codigo(ctx_waha, telefono="+5493411234567", **_actor(esc)))
    assert (e.status, e.codigo) == (502, "codigo_no_disponible")


async def test_pedir_fin_encola_el_job_y_marca_el_borrado(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    r = await pedir_fin(ctx_waha, causa="pedido_dueno", borrar=True, **_actor(esc))
    assert r == {"vinculos": [str(v["link_id"])], "borrado_solicitado": True}
    job = await _fila(ctx_waha, esc, "SELECT tipo, causa FROM jobs WHERE link_id = $1", v["link_id"])
    assert (job["tipo"], job["causa"]) == ("fin_vinculo", "pedido_dueno")
    linea = await _fila(ctx_waha, esc, "SELECT borrado_solicitado_at FROM lines WHERE id = $1", esc["line_id"])
    assert linea["borrado_solicitado_at"] is not None
    otro = await escenario_vinculable(ctx_waha, "Farmacia B")
    e = await _rechazo(pedir_fin(ctx_waha, causa="pedido_dueno", borrar=False, **_actor(otro)))
    assert (e.status, e.codigo) == (409, "sin_vinculo")


async def test_marcar_y_levantar_restriccion(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    await vincular_de_prueba(ctx_waha, waha, esc)
    estado = await marcar_restriccion(ctx_waha, hasta=None, **_actor(esc))
    assert estado["restriccion_activa"] is True
    estado = await levantar_restriccion(ctx_waha, **_actor(esc))
    assert estado["restriccion_activa"] is False
    audit = await _fila(ctx_waha, esc, "SELECT count(*) AS n FROM access_audit_log "
                                       "WHERE accion IN ('restriccion_marcada', 'restriccion_levantada')")
    assert audit["n"] == 2
    otro = await escenario_vinculable(ctx_waha, "Farmacia B")
    e = await _rechazo(marcar_restriccion(ctx_waha, hasta=None, **_actor(otro)))
    assert (e.status, e.codigo) == (409, "sin_vinculo")


async def test_con_restriccion_activa_no_se_reinicia_el_qr(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    await _iniciar(ctx_waha, esc)
    await marcar_restriccion(ctx_waha, hasta=datetime.now(timezone.utc) + timedelta(days=1), **_actor(esc))
    waha.llamadas.clear()
    e = await _rechazo(reiniciar_qr(ctx_waha, **_actor(esc)))
    assert (e.status, e.codigo) == (409, "restriccion_activa")
    assert not any(x.endswith("/restart") for x in waha.llamadas)
    fila = await _fila(ctx_waha, esc, "SELECT qr_reinicios FROM links WHERE line_id = $1", esc["line_id"])
    assert fila["qr_reinicios"] == 0


async def test_caida_de_un_vinculado_encola_el_aviso_y_la_de_uno_que_cierra_no(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        r = await aplicar_status(con, tenant_id=esc["tenant_id"], link_id=v["link_id"], waha_status="FAILED",
                                 origen="webhook")
        await aplicar_status(con, tenant_id=esc["tenant_id"], link_id=v["link_id"], waha_status="FAILED",
                             origen="webhook")                       # caído → caído: no repite el aviso
    assert (r["estado_anterior"], r["estado"]) == ("vinculado", "caido")
    avisos = await _fila(ctx_waha, esc, "SELECT count(*) AS n FROM jobs WHERE tipo = 'aviso_caida' "
                                        "AND link_id = $1", v["link_id"])
    assert avisos["n"] == 1
    otro = await escenario_vinculable(ctx_waha, "Farmacia B")
    w = await vincular_de_prueba(ctx_waha, waha, otro)
    async with ctx_waha.db.tenant_tx(otro["tenant_id"]) as con:
        await con.execute("UPDATE links SET estado = 'cerrando' WHERE id = $1", w["link_id"])
        await aplicar_status(con, tenant_id=otro["tenant_id"], link_id=w["link_id"], waha_status="FAILED",
                             origen="webhook")
        n = await con.fetchval("SELECT count(*) FROM jobs WHERE tipo = 'aviso_caida' AND link_id = $1", w["link_id"])
    assert n == 0
