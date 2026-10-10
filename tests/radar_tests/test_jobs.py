"""
Cola en Postgres (§6.2): sin contenido, reclamo entre tenants con SKIP LOCKED
por una función SECURITY DEFINER, reintentos con backoff y un solo job vivo
por (link, tipo).
"""
import uuid

from app.radar import jobs as cola

from .helpers import (como_superusuario, crear_consentimiento_directo, crear_link_directo, crear_linea_directa,
                      crear_tenant_directo, crear_usuario, crear_worker_directo)


async def _link(db, worker_id, estado="vinculado", nombre="A"):
    t = await crear_tenant_directo(db, nombre)
    u = await crear_usuario(db, t, "dueno@cliente.com", "dueno")
    li = await crear_linea_directa(db, t)
    c = await crear_consentimiento_directo(db, t, li, u)
    return t, await crear_link_directo(db, t, li, worker_id, c, estado=estado)


async def _job(db, t, job_id):
    async with db.tenant_tx(t) as con:
        return await con.fetchrow("SELECT *, ejecutar_desde - now() AS falta FROM jobs WHERE id = $1", job_id)


async def test_encolar_es_idempotente_por_link_y_tipo(radar_db):
    t, k = await _link(radar_db, await crear_worker_directo(radar_db))
    async with radar_db.tenant_tx(t) as con:
        primero = await cola.encolar(con, tipo="fin_vinculo", link_id=k, causa="pedido_kis")
        segundo = await cola.encolar(con, tipo="fin_vinculo", link_id=k, causa="pedido_kis")
    assert primero is not None and segundo is None


async def test_reclamar_y_completar(radar_db):
    t, k = await _link(radar_db, await crear_worker_directo(radar_db))
    async with radar_db.tenant_tx(t) as con:
        jid = await cola.encolar(con, tipo="fin_vinculo", link_id=k, causa="pedido_kis")
    [job] = await cola.reclamar(radar_db)
    assert (job.id, job.tenant_id, job.tipo, job.link_id, job.causa, job.intentos) == \
           (jid, t, "fin_vinculo", k, "pedido_kis", 1)
    await cola.completar(radar_db, job)
    assert (await _job(radar_db, t, jid))["estado"] == "hecho"
    assert await cola.reclamar(radar_db) == []


async def test_no_se_reclama_dos_veces_mientras_corre(radar_db):
    t, k = await _link(radar_db, await crear_worker_directo(radar_db))
    async with radar_db.tenant_tx(t) as con:
        await cola.encolar(con, tipo="fin_vinculo", link_id=k)
    assert len(await cola.reclamar(radar_db)) == 1
    assert await cola.reclamar(radar_db) == []


async def test_lease_vencido_se_vuelve_a_reclamar(radar_db):
    t, k = await _link(radar_db, await crear_worker_directo(radar_db))
    async with radar_db.tenant_tx(t) as con:
        jid = await cola.encolar(con, tipo="fin_vinculo", link_id=k)
    await cola.reclamar(radar_db, lease_s=0)
    [otra_vez] = await cola.reclamar(radar_db)
    assert otra_vez.id == jid and otra_vez.intentos == 2


async def test_lease_vencido_sin_intentos_disponibles_pasa_a_fallido(radar_db, radar_urls):
    """Nota de revisión de Task 2: si un job tumba al worker, no debe
    reintentarse para siempre. Cuando el lease vence y ya no quedan intentos
    (intentos >= max_intentos), radar_jobs_reclamar lo pasa a 'fallido' en vez
    de reclamarlo de nuevo."""
    t, k = await _link(radar_db, await crear_worker_directo(radar_db))
    async with radar_db.tenant_tx(t) as con:
        jid = await cola.encolar(con, tipo="fin_vinculo", link_id=k)
    await como_superusuario(radar_urls, "UPDATE jobs SET max_intentos = 1 WHERE id = $1", jid)
    [job] = await cola.reclamar(radar_db, lease_s=0)
    assert job.intentos == 1
    # El lease ya venció (lease_s=0) y no quedan intentos: no se reclama de nuevo.
    assert await cola.reclamar(radar_db) == []
    fila = await _job(radar_db, t, jid)
    assert fila["estado"] == "fallido"


async def test_fallar_con_backoff_y_fallido_al_tope(radar_db, radar_urls):
    t, k = await _link(radar_db, await crear_worker_directo(radar_db))
    async with radar_db.tenant_tx(t) as con:
        jid = await cola.encolar(con, tipo="fin_vinculo", link_id=k)
    [job] = await cola.reclamar(radar_db)
    assert await cola.fallar(radar_db, job, RuntimeError("x")) == "pendiente"
    assert (await _job(radar_db, t, jid))["falta"].total_seconds() > 25
    # max_intentos no es actualizable por radar_app (GRANT por columna de r0003).
    await como_superusuario(radar_urls, "UPDATE jobs SET ejecutar_desde = now(), max_intentos = 2 WHERE id = $1",
                            jid)
    [job] = await cola.reclamar(radar_db)
    assert await cola.fallar(radar_db, job, RuntimeError("x")) == "fallido"


async def test_fallar_guarda_solo_el_tipo_de_error(radar_db):
    t, k = await _link(radar_db, await crear_worker_directo(radar_db))
    async with radar_db.tenant_tx(t) as con:
        jid = await cola.encolar(con, tipo="fin_vinculo", link_id=k)
    [job] = await cola.reclamar(radar_db)
    await cola.fallar(radar_db, job, ValueError("el 5493411234567 no respondió"))
    assert (await _job(radar_db, t, jid))["ultimo_error"] == "ValueError"
    async with radar_db.tenant_tx(t) as con:
        await con.execute("UPDATE jobs SET ejecutar_desde = now() WHERE id = $1", jid)
    [job] = await cola.reclamar(radar_db)
    await cola.fallar(radar_db, job, type("Error404x", (Exception,), {})())
    assert (await _job(radar_db, t, jid))["ultimo_error"] == "Errorx"


async def test_reprogramar_vuelve_a_pendiente_con_intentos_en_cero(radar_db):
    t, k = await _link(radar_db, await crear_worker_directo(radar_db))
    async with radar_db.tenant_tx(t) as con:
        jid = await cola.encolar(con, tipo="chequeo_salud", link_id=k)
    [job] = await cola.reclamar(radar_db)
    await cola.reprogramar(radar_db, job, 300)
    fila = await _job(radar_db, t, jid)
    assert (fila["estado"], fila["intentos"]) == ("pendiente", 0) and fila["falta"].total_seconds() > 250


async def test_programar_salud_solo_vinculos_vivos(radar_db):
    w = await crear_worker_directo(radar_db)
    t = await crear_tenant_directo(radar_db)
    u = await crear_usuario(radar_db, t, "dueno@cliente.com", "dueno")
    # 'creando' entra: un vínculo huérfano en 'creando' lo destraba el chequeo de salud.
    for i, estado in enumerate(("creando", "esperando_qr", "vinculado", "caido", "cerrado")):
        li = await crear_linea_directa(radar_db, t, f"Línea {i}")
        c = await crear_consentimiento_directo(radar_db, t, li, u)
        await crear_link_directo(radar_db, t, li, w, c, estado=estado)
    assert await cola.programar_salud(radar_db) == 4
    assert await cola.programar_salud(radar_db) == 0


async def test_reclamo_concurrente_no_duplica(radar_db):
    w = await crear_worker_directo(radar_db)
    t1, k1 = await _link(radar_db, w, nombre="A")
    t2, k2 = await _link(radar_db, w, nombre="B")
    for t, k in ((t1, k1), (t2, k2)):
        async with radar_db.tenant_tx(t) as con:
            await cola.encolar(con, tipo="fin_vinculo", link_id=k)
    async with radar_db.sin_tenant() as a:
        primero = await a.fetch("SELECT * FROM radar_jobs_reclamar(1, 300)")
        async with radar_db.sin_tenant() as b:          # otra conexión: la fila de `a` está bloqueada
            segundo = await b.fetch("SELECT * FROM radar_jobs_reclamar(10, 300)")
    assert len(primero) == 1 and len(segundo) == 1
    assert primero[0]["id"] != segundo[0]["id"]
    assert {primero[0]["tenant_id"], segundo[0]["tenant_id"]} == {t1, t2}


async def test_fin_agotado_en_cerrando_se_reencola_al_programar_salud(radar_db, radar_urls):
    """Final B2: un fin_vinculo 'fallido' deja el vínculo en 'cerrando'; la
    próxima programación de salud lo vuelve a encolar (sin duplicar uno vivo)."""
    t, k = await _link(radar_db, await crear_worker_directo(radar_db), estado="cerrando")
    async with radar_db.tenant_tx(t) as con:
        await con.execute("UPDATE links SET fin_causa = 'pedido_kis' WHERE id = $1", k)
        jid = await cola.encolar(con, tipo="fin_vinculo", link_id=k, causa="pedido_kis")
    await como_superusuario(radar_urls, "UPDATE jobs SET estado = 'fallido' WHERE id = $1", jid)
    assert await cola.programar_salud(radar_db) == 1
    assert await cola.programar_salud(radar_db) == 0
    async with radar_db.tenant_tx(t) as con:
        vivo = await con.fetchrow("SELECT tipo, causa FROM jobs WHERE link_id = $1 AND estado = 'pendiente'", k)
    assert (vivo["tipo"], vivo["causa"]) == ("fin_vinculo", "pedido_kis")


async def test_worker_con_lease_vencido_no_pisa_el_estado_de_otro(radar_db):
    """Final B3: A reclama y se le vence el lease; B lo reclama de nuevo. Lo
    que A haga después (completar, reprogramar, fallar) no toca el job de B."""
    t, k = await _link(radar_db, await crear_worker_directo(radar_db))
    async with radar_db.tenant_tx(t) as con:
        jid = await cola.encolar(con, tipo="fin_vinculo", link_id=k)
    [de_a] = await cola.reclamar(radar_db, lease_s=0)
    [de_b] = await cola.reclamar(radar_db)
    assert (de_a.intentos, de_b.intentos) == (1, 2)
    await cola.completar(radar_db, de_a)
    await cola.reprogramar(radar_db, de_a, 300)
    assert await cola.fallar(radar_db, de_a, RuntimeError("x")) is None
    fila = await _job(radar_db, t, jid)
    assert (fila["estado"], fila["intentos"], fila["ultimo_error"]) == ("corriendo", 2, None)
    await cola.completar(radar_db, de_b)
    assert (await _job(radar_db, t, jid))["estado"] == "hecho"


def test_lease_cubre_el_peor_caso_del_fin():
    assert cola.LEASE_S >= 600
