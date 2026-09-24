"""
Esquema del tramo 2: RLS forzada en las tablas nuevas, un vínculo activo por
línea, consentimiento obligatorio, nada de identificadores de WhatsApp, y las
funciones SECURITY DEFINER que cruzan tenants.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from app.radar.constantes import TENANT_KIS

from .helpers import (crear_consentimiento_directo, crear_link_directo, crear_linea_directa, crear_tenant_directo,
                      crear_usuario, crear_worker_directo, como_superusuario)

NUEVAS = ("waha_workers", "links", "link_status_events", "jobs")


async def _base(db, nombre="A"):
    t = await crear_tenant_directo(db, nombre)
    u = await crear_usuario(db, t, f"dueno-{uuid.uuid4().hex[:6]}@cliente.com", "dueno")
    li = await crear_linea_directa(db, t, "Local")
    c = await crear_consentimiento_directo(db, t, li, u)
    return t, u, li, c


async def test_tablas_nuevas_con_tenant_y_rls_forzada(radar_urls):
    con = await asyncpg.connect(radar_urls["migrator"])
    try:
        for t in NUEVAS:
            col = await con.fetchrow("SELECT is_nullable, column_default FROM information_schema.columns "
                                     "WHERE table_name = $1 AND column_name = 'tenant_id'", t)
            assert col["is_nullable"] == "NO" and "radar_tenant_actual()" in col["column_default"], t
            r = await con.fetchrow("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = $1", t)
            assert r["relrowsecurity"] and r["relforcerowsecurity"], t
    finally:
        await con.close()


async def test_workers_solo_en_el_tenant_kis(radar_db):
    t, *_ = await _base(radar_db)
    with pytest.raises(asyncpg.CheckViolationError):
        async with radar_db.tenant_tx(t) as con:
            await con.execute("INSERT INTO waha_workers (nombre, base_url, engine, max_sesiones, disco_max_gb) "
                              "VALUES ('x', 'http://w', 'NOWEB', 5, 1)")
    await crear_worker_directo(radar_db)


async def test_link_exige_consentimiento(radar_db):
    t, _, li, _ = await _base(radar_db)
    w = await crear_worker_directo(radar_db)
    with pytest.raises(asyncpg.NotNullViolationError):
        async with radar_db.tenant_tx(t) as con:
            await con.execute("INSERT INTO links (line_id, worker_id, session_name, engine) "
                              "VALUES ($1, $2, 'v_0123456789ab', 'NOWEB')", li, w)


async def test_un_solo_vinculo_activo_por_linea(radar_db):
    t, _, li, c = await _base(radar_db)
    w = await crear_worker_directo(radar_db)
    await crear_link_directo(radar_db, t, li, w, c, estado="caido")
    await crear_link_directo(radar_db, t, li, w, c, estado="vinculado")
    with pytest.raises(asyncpg.UniqueViolationError):
        await crear_link_directo(radar_db, t, li, w, c, estado="esperando_qr")


async def test_nombre_de_sesion_y_sufijo(radar_db):
    t, _, li, c = await _base(radar_db)
    w = await crear_worker_directo(radar_db)
    with pytest.raises(asyncpg.CheckViolationError):
        async with radar_db.tenant_tx(t) as con:
            await con.execute("INSERT INTO links (line_id, worker_id, consent_id, session_name, engine) "
                              "VALUES ($1, $2, $3, 'MaroSession', 'NOWEB')", li, w, c)
    k = await crear_link_directo(radar_db, t, li, w, c)
    with pytest.raises(asyncpg.CheckViolationError):
        async with radar_db.tenant_tx(t) as con:
            await con.execute("UPDATE links SET numero_sufijo = '5493411234567' WHERE id = $1", k)


async def test_consentimiento_asistido_completo(radar_db):
    t, u, li, _ = await _base(radar_db)
    admin = await crear_usuario(radar_db, TENANT_KIS, "admin@keepitsimple.com.ar", "admin")
    with pytest.raises(asyncpg.CheckViolationError):
        async with radar_db.tenant_tx(t) as con:
            await con.execute("INSERT INTO consents (line_id, user_id, version_texto, hash_texto, opciones, modo) "
                              "VALUES ($1, $2, 'v1', repeat('a', 64), '{}'::jsonb, 'asistido')", li, u)
    async with radar_db.tenant_tx(t) as con:
        await con.execute(
            "INSERT INTO consents (line_id, user_id, version_texto, hash_texto, opciones, modo, cargado_por, "
            "modo_asistencia, aceptado_por_nombre) VALUES ($1, $2, 'v1', repeat('a', 64), '{}'::jsonb, "
            "'asistido', $3, 'videollamada', 'Ana')", li, u, admin)


async def test_fin_resultado_sin_identificadores(radar_db):
    t, _, li, c = await _base(radar_db)
    w = await crear_worker_directo(radar_db)
    k = await crear_link_directo(radar_db, t, li, w, c)
    async with radar_db.tenant_tx(t) as con:
        await con.execute("UPDATE links SET fin_resultado = $2::jsonb WHERE id = $1", k,
                          json.dumps({"ok": True, "error_claves": "WahaHttpError", "claves_borradas": 2}))
    with pytest.raises(asyncpg.CheckViolationError):
        async with radar_db.tenant_tx(t) as con:
            await con.execute("UPDATE links SET fin_resultado = $2::jsonb WHERE id = $1", k,
                              json.dumps({"me": "5493411234567@c.us"}))


async def test_un_job_vivo_por_link_y_tipo(radar_db):
    t, _, li, c = await _base(radar_db)
    w = await crear_worker_directo(radar_db)
    k = await crear_link_directo(radar_db, t, li, w, c)
    async with radar_db.tenant_tx(t) as con:
        await con.execute("INSERT INTO jobs (tipo, link_id) VALUES ('fin_vinculo', $1)", k)
        await con.execute("INSERT INTO jobs (tipo, link_id) VALUES ('chequeo_salud', $1)", k)
    with pytest.raises(asyncpg.UniqueViolationError):
        async with radar_db.tenant_tx(t) as con:
            await con.execute("INSERT INTO jobs (tipo, link_id) VALUES ('fin_vinculo', $1)", k)


async def test_ocupacion_de_workers_cuenta_todos_los_tenants(radar_db):
    w = await crear_worker_directo(radar_db, max_sesiones=5)
    for nombre in ("A", "B"):
        t, _, li, c = await _base(radar_db, nombre)
        await crear_link_directo(radar_db, t, li, w, c, estado="vinculado")
    async with radar_db.sin_tenant() as con:
        assert await con.fetchval("SELECT count(*) FROM links") == 0          # RLS: sin tenant, nada
        filas = await con.fetch("SELECT * FROM radar_admin_ocupacion_workers()")
    assert [(f["nombre"], f["sesiones"], f["max_sesiones"]) for f in filas] == [("w1", 2, 5)]


async def test_radar_app_no_borra_vinculos_ni_jobs(radar_db):
    t, *_ = await _base(radar_db)
    for sql in ("DELETE FROM links", "DELETE FROM jobs", "DELETE FROM link_status_events"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with radar_db.tenant_tx(t) as con:
                await con.execute(sql)


async def test_consola_devuelve_el_ultimo_vinculo_de_cada_linea(radar_db, radar_urls):
    t, _, li, c = await _base(radar_db, "Farmacia A")
    w = await crear_worker_directo(radar_db)
    viejo = await crear_link_directo(radar_db, t, li, w, c, estado="caido")
    # created_at no es actualizable por radar_app (y el migrator, con FORCE RLS,
    # no ve filas): se envejece con el superusuario.
    await como_superusuario(radar_urls, "UPDATE links SET estado = 'cerrado', "
                                        "created_at = now() - interval '1 day' WHERE id = $1", viejo)
    nuevo = await crear_link_directo(radar_db, t, li, w, c, estado="vinculado",
                                     restriccion_hasta=datetime.now(timezone.utc) + timedelta(days=2))
    async with radar_db.sin_tenant() as con:
        filas = await con.fetch("SELECT * FROM radar_admin_consola_lineas()")
    assert len(filas) == 1
    f = filas[0]
    assert (f["tenant_nombre"], f["line_id"], f["link_id"], f["link_estado"]) == ("Farmacia A", li, nuevo, "vinculado")
    assert f["worker_nombre"] == "w1" and f["worker_sesiones"] == 1 and f["restriccion_hasta"] is not None


async def test_grant_update_no_alcanza_columnas_de_identidad(radar_db):
    t, _, li, c = await _base(radar_db)
    w = await crear_worker_directo(radar_db)
    k = await crear_link_directo(radar_db, t, li, w, c)
    async with radar_db.tenant_tx(t) as con:          # lo que sí se actualiza
        await con.execute("INSERT INTO jobs (tipo, link_id) VALUES ('fin_vinculo', $1)", k)
        await con.execute("UPDATE jobs SET estado = 'corriendo', intentos = 1 WHERE link_id = $1", k)
        await con.execute("UPDATE links SET estado = 'vinculado', numero_sufijo = '1234' WHERE id = $1", k)
        await con.execute("UPDATE lines SET borrado_solicitado_at = now() WHERE id = $1", li)
    async with radar_db.tenant_tx(TENANT_KIS) as con:
        await con.execute("UPDATE waha_workers SET disco_usado_gb = 1 WHERE id = $1", w)
    prohibidos = [(t, "UPDATE links SET line_id = line_id"), (t, "UPDATE links SET consent_id = consent_id"),
                  (t, "UPDATE links SET session_name = session_name"), (t, "UPDATE jobs SET link_id = link_id"),
                  (t, "UPDATE jobs SET tipo = tipo"), (TENANT_KIS, "UPDATE waha_workers SET engine = engine")]
    for tenant, sql in prohibidos:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with radar_db.tenant_tx(tenant) as con:
                await con.execute(sql)
