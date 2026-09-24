"""
Fin de vínculo (§6.3 punto 6): cerrando → start si hace falta y no hay
restricción → un único DELETE → claves de esa sesión → verificación 404 →
clave local borrada → registro → aviso al dueño. Nunca logout.
"""
import json

import pytest

from app.radar import fin_vinculo
from app.radar import jobs as cola
from app.radar.fin_vinculo import FinIncompleto
from app.radar.vinculos import aplicar_status, marcar_restriccion, pedir_fin
from app.radar.workers import nombre_clave_lectura

from .helpers import MailerQueFalla, escenario_vinculable, vincular_de_prueba


async def _job_fin(ctx):
    return next(j for j in await cola.reclamar(ctx.db) if j.tipo == "fin_vinculo")


async def _fila(ctx, esc, sql, *args):
    async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
        return await con.fetchrow(sql, *args)


async def _pedir(ctx, esc, causa="pedido_dueno"):
    await pedir_fin(ctx, tenant_id=esc["tenant_id"], line_id=esc["line_id"], causa=causa, borrar=False,
                    actor_user_id=esc["dueno_id"], actor_rol="dueno", ip=None)


async def test_fin_completo(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    await _pedir(ctx_waha, esc)
    waha.llamadas.clear()
    assert await fin_vinculo.ejecutar(ctx_waha, await _job_fin(ctx_waha)) == "hecho"
    n = v["session_name"]
    assert waha.llamadas.count(f"DELETE /api/sessions/{n}") == 1
    assert not any("logout" in x for x in waha.llamadas)
    assert waha.sesiones == {} and waha.claves == []
    assert ctx_waha.secretos.get(nombre_clave_lectura(v["link_id"])) is None
    link = await _fila(ctx_waha, esc, "SELECT estado, fin_causa, desvinculo_confirmado, fin_resultado, cerrado_at "
                                      "FROM links WHERE id = $1", v["link_id"])
    assert (link["estado"], link["fin_causa"], link["desvinculo_confirmado"]) == ("cerrado", "pedido_dueno", True)
    assert json.loads(link["fin_resultado"])["ok"] is True and link["cerrado_at"] is not None
    linea = await _fila(ctx_waha, esc, "SELECT estado FROM lines WHERE id = $1", esc["line_id"])
    assert linea["estado"] == "sin_vinculo"
    mail = ctx_waha.mailer.enviados[-1]
    assert mail.para == esc["dueno_email"] and "Dispositivos vinculados" in mail.texto
    audit = await _fila(ctx_waha, esc, "SELECT detalle::text AS d FROM access_audit_log WHERE accion = 'vinculo_cerrado'")
    assert "pedido_dueno" in audit["d"]


async def test_sesion_detenida_intenta_start_antes_del_delete(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    waha.sesiones[v["session_name"]]["status"] = "STOPPED"
    await _pedir(ctx_waha, esc)
    await fin_vinculo.ejecutar(ctx_waha, await _job_fin(ctx_waha), espera_start_s=1, intervalo_s=0)
    assert f"POST /api/sessions/{v['session_name']}/start" in waha.llamadas
    link = await _fila(ctx_waha, esc, "SELECT desvinculo_confirmado FROM links WHERE id = $1", v["link_id"])
    assert link["desvinculo_confirmado"] is True


async def test_con_restriccion_no_hay_start_pero_se_borra(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    await marcar_restriccion(ctx_waha, tenant_id=esc["tenant_id"], line_id=esc["line_id"], hasta=None,
                             actor_user_id=None, actor_rol="admin", ip=None)
    waha.sesiones[v["session_name"]]["status"] = "FAILED"
    await _pedir(ctx_waha, esc, causa="pedido_kis")
    await fin_vinculo.ejecutar(ctx_waha, await _job_fin(ctx_waha))
    assert not any(x.endswith("/start") for x in waha.llamadas)
    link = await _fila(ctx_waha, esc, "SELECT estado, desvinculo_confirmado FROM links WHERE id = $1", v["link_id"])
    assert (link["estado"], link["desvinculo_confirmado"]) == ("cerrado", False)


async def test_fallo_en_claves_deja_cerrando_y_el_reintento_completa(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    await _pedir(ctx_waha, esc)
    job = await _job_fin(ctx_waha)
    waha.falla_claves = True
    with pytest.raises(FinIncompleto):
        await fin_vinculo.ejecutar(ctx_waha, job)
    assert waha.llamadas.count(f"DELETE /api/sessions/{v['session_name']}") == 1
    link = await _fila(ctx_waha, esc, "SELECT estado, fin_resultado FROM links WHERE id = $1", v["link_id"])
    assert link["estado"] == "cerrando"
    assert json.loads(link["fin_resultado"])["error_claves"] == "WahaHttpError"
    waha.falla_claves = False
    assert await fin_vinculo.ejecutar(ctx_waha, job) == "hecho"
    link = await _fila(ctx_waha, esc, "SELECT estado FROM links WHERE id = $1", v["link_id"])
    assert link["estado"] == "cerrado" and waha.claves == []


async def test_es_idempotente(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    await vincular_de_prueba(ctx_waha, waha, esc)
    await _pedir(ctx_waha, esc)
    job = await _job_fin(ctx_waha)
    await fin_vinculo.ejecutar(ctx_waha, job)
    waha.llamadas.clear()
    assert await fin_vinculo.ejecutar(ctx_waha, job) == "hecho"
    assert waha.llamadas == []


async def test_reemplazo_no_manda_email(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await cola.encolar(con, tipo="fin_vinculo", link_id=v["link_id"], causa="reemplazado")
    antes = len(ctx_waha.mailer.enviados)
    await fin_vinculo.ejecutar(ctx_waha, await _job_fin(ctx_waha))
    assert len(ctx_waha.mailer.enviados) == antes
    link = await _fila(ctx_waha, esc, "SELECT estado, fin_causa FROM links WHERE id = $1", v["link_id"])
    assert (link["estado"], link["fin_causa"]) == ("cerrado", "reemplazado")


async def test_status_de_un_vinculo_que_cierra_se_ignora(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await con.execute("UPDATE links SET estado = 'cerrando' WHERE id = $1", v["link_id"])
        r = await aplicar_status(con, tenant_id=esc["tenant_id"], link_id=v["link_id"], waha_status="SCAN_QR_CODE",
                                 origen="webhook")
    assert r["aplicado"] is False
    link = await _fila(ctx_waha, esc, "SELECT estado FROM links WHERE id = $1", v["link_id"])
    assert link["estado"] == "cerrando"


async def test_mail_que_falla_no_rompe_el_fin(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    await _pedir(ctx_waha, esc)
    ctx_waha.mailer = MailerQueFalla()
    assert await fin_vinculo.ejecutar(ctx_waha, await _job_fin(ctx_waha)) == "hecho"
    link = await _fila(ctx_waha, esc, "SELECT estado FROM links WHERE id = $1", v["link_id"])
    assert link["estado"] == "cerrado"
    ev = await _fila(ctx_waha, esc, "SELECT valores::text AS v FROM product_events WHERE evento = 'vinculo_cerrado'")
    assert json.loads(ev["v"])["aviso_enviado"] == 0


async def _caer(ctx, esc, v):
    async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
        await aplicar_status(con, tenant_id=esc["tenant_id"], link_id=v["link_id"], waha_status="FAILED",
                             origen="webhook")
        await con.execute("UPDATE links SET caido_desde = '2026-09-20 15:00:00+00' WHERE id = $1", v["link_id"])
    return next(j for j in await cola.reclamar(ctx.db) if j.tipo == "aviso_caida")


async def test_aviso_de_caida_con_la_fecha(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    job = await _caer(ctx_waha, esc, v)
    antes = len(ctx_waha.mailer.enviados)
    assert await fin_vinculo.avisar_caida(ctx_waha, job) == "hecho"
    [mail] = ctx_waha.mailer.enviados[antes:]
    assert mail.para == esc["dueno_email"] and "el 20/09" in mail.texto and "Reconectar" in mail.texto
    assert "4567" not in mail.texto


async def test_aviso_de_caida_no_sale_si_ya_reconecto_ni_rompe_si_falla_el_mail(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    job = await _caer(ctx_waha, esc, v)
    ctx_waha.mailer = MailerQueFalla()
    assert await fin_vinculo.avisar_caida(ctx_waha, job) == "hecho"      # falla el proveedor: no relanza
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await aplicar_status(con, tenant_id=esc["tenant_id"], link_id=v["link_id"], waha_status="WORKING",
                             origen="webhook", me_id=waha.me_id)
    ctx_waha.mailer = MailerQueFalla(falla_si="nunca")
    assert await fin_vinculo.avisar_caida(ctx_waha, job) == "hecho"
    assert ctx_waha.mailer.enviados == []
