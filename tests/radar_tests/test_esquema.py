"""
Guardas de esquema (§6.4, §7, §9): tenant_id NOT NULL y RLS forzada en todas
las tablas; ninguna columna de texto de conversación ni de identificadores de
WhatsApp en la base de resultados; product_events sin texto libre;
access_audit_log sin teléfonos ni emails; coherencia de líneas.
"""
import json
import uuid

import asyncpg
import pytest

from app.radar.constantes import TENANT_KIS

from .helpers import crear_linea_directa, crear_tenant_directo, crear_usuario

TABLAS_TENANT = {"users", "memberships", "lines", "login_tokens", "sessions", "consents",
                 "support_grants", "access_audit_log", "product_events"}


async def _owner(radar_urls):
    return await asyncpg.connect(radar_urls["migrator"])


async def test_todas_las_tablas_tienen_tenant_id_not_null_y_rls_forzada(radar_urls):
    con = await _owner(radar_urls)
    try:
        tablas = {r["table_name"] for r in await con.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")} - {"alembic_version_radar"}
        assert tablas == TABLAS_TENANT | {"tenants"}
        for t in TABLAS_TENANT:
            col = await con.fetchrow(
                "SELECT is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name = $1 AND column_name = 'tenant_id'", t)
            assert col is not None and col["is_nullable"] == "NO", t
            assert "radar_tenant_actual()" in col["column_default"], t
        for r in await con.fetch(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY($1)",
                list(tablas)):
            assert r["relrowsecurity"] and r["relforcerowsecurity"], r["relname"]
    finally:
        await con.close()


async def test_sin_columnas_de_conversacion_ni_identificadores_de_whatsapp(radar_urls):
    con = await _owner(radar_urls)
    try:
        filas = await con.fetch(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name ~ "
            "'(^|_)(phone|telefono|jid|lid|body|contenido|payload|chat_id|mensaje)(_|$)'")
    finally:
        await con.close()
    assert filas == []


async def test_rls_en_tabla_con_datos(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    b = await crear_tenant_directo(radar_db, "B")
    la = await crear_linea_directa(radar_db, a, "Línea de A")
    async with radar_db.tenant_tx(b) as con:
        assert await con.fetch("SELECT id FROM lines") == []
        assert await con.fetchval("SELECT count(*) FROM lines WHERE id = $1", la) == 0
        assert await con.execute("UPDATE lines SET nombre = 'x' WHERE id = $1", la) == "UPDATE 0"
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with radar_db.tenant_tx(b) as con:
            await con.execute("INSERT INTO lines (tenant_id, nombre) VALUES ($1, 'colada')", a)
    async with radar_db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT nombre FROM lines WHERE id = $1", la) == "Línea de A"


async def test_tenant_id_por_defecto_es_el_de_la_transaccion(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    la = await crear_linea_directa(radar_db, a)
    async with radar_db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT tenant_id FROM lines WHERE id = $1", la) == a
        fila = await con.fetchrow("SELECT estado, almacen_fuente, duracion_vinculo_dias, retencion_fuente_dias, "
                                  "retencion_tras_desvinculo_dias, tope_ia_mensual_usd FROM lines WHERE id = $1", la)
    assert dict(fila) == {"estado": "sin_vinculo", "almacen_fuente": "permanente", "duracion_vinculo_dias": 0,
                          "retencion_fuente_dias": 0, "retencion_tras_desvinculo_dias": 0, "tope_ia_mensual_usd": None}


async def test_linea_purgable_coherente_con_retencion(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    with pytest.raises(asyncpg.CheckViolationError):
        async with radar_db.tenant_tx(a) as con:
            await con.execute("INSERT INTO lines (nombre, retencion_fuente_dias) VALUES ('x', 7)")
    with pytest.raises(asyncpg.CheckViolationError):
        async with radar_db.tenant_tx(a) as con:
            await con.execute("INSERT INTO lines (nombre, almacen_fuente) VALUES ('x', 'purgable')")
    with pytest.raises(asyncpg.CheckViolationError):
        async with radar_db.tenant_tx(a) as con:
            await con.execute("INSERT INTO lines (nombre, estado) VALUES ('x', 'de_baja')")


async def test_admin_solo_en_el_tenant_kis(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    with pytest.raises(asyncpg.CheckViolationError):
        await crear_usuario(radar_db, a, "x@cliente.com", "admin")
    await crear_usuario(radar_db, TENANT_KIS, "x@keepitsimple.com.ar", "admin")


async def test_email_normalizado_en_la_base(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    with pytest.raises(asyncpg.CheckViolationError):
        await crear_usuario(radar_db, a, "Mayus@Cliente.com", "dueno")
    with pytest.raises(asyncpg.CheckViolationError):
        await crear_usuario(radar_db, a, "sin-arroba", "dueno")


async def test_product_events_rechaza_texto_libre(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    async with radar_db.tenant_tx(a) as con:
        await con.execute("INSERT INTO product_events (evento, valores) VALUES ('login_canjeado', $1::jsonb)",
                          json.dumps({"n": 3, "ok": True, "ratio": 0.5}))
    for malo in ({"nombre": "Juan"}, {"anidado": {"t": "x"}}, {"lista": ["a"]}, "\"texto\"", "[1]"):
        with pytest.raises(asyncpg.CheckViolationError):
            async with radar_db.tenant_tx(a) as con:
                await con.execute("INSERT INTO product_events (evento, valores) VALUES ('login_canjeado', $1::jsonb)",
                                  malo if isinstance(malo, str) else json.dumps(malo))
    with pytest.raises(asyncpg.CheckViolationError):
        async with radar_db.tenant_tx(a) as con:
            await con.execute("INSERT INTO product_events (evento) VALUES ('Evento Con Espacios')")


async def test_access_audit_log_rechaza_telefonos_y_emails_en_detalle(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    ok = {"contact_hmac": "ab12" * 16, "parametro": "duracion_vinculo_dias", "valor_nuevo": 30}
    async with radar_db.tenant_tx(a) as con:
        await con.execute("INSERT INTO access_audit_log (actor_rol, accion, tipo_objeto, detalle) "
                          "VALUES ('dueno', 'parametro_cambiado', 'line', $1::jsonb)", json.dumps(ok))
    for malo in ({"t": "+5493411234567"}, {"t": "5493411234567@c.us"}, {"e": "a@b.c"}, {"n": "123456"}):
        with pytest.raises(asyncpg.CheckViolationError):
            async with radar_db.tenant_tx(a) as con:
                await con.execute("INSERT INTO access_audit_log (actor_rol, accion, tipo_objeto, detalle) "
                                  "VALUES ('dueno', 'parametro_cambiado', 'line', $1::jsonb)", json.dumps(malo))


async def test_auditoria_y_eventos_son_solo_de_insercion_para_radar_app(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    async with radar_db.tenant_tx(a) as con:
        await con.execute("INSERT INTO access_audit_log (actor_rol, accion, tipo_objeto) VALUES ('sistema', 'x', 'y')")
        await con.execute("INSERT INTO product_events (evento) VALUES ('x')")
    for sql in ("DELETE FROM access_audit_log", "UPDATE access_audit_log SET accion = 'z'",
                "DELETE FROM product_events", "DELETE FROM consents", "DELETE FROM login_tokens"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with radar_db.tenant_tx(a) as con:
                await con.execute(sql)


async def test_usuarios_por_email_cruza_tenants_y_normaliza(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    b = await crear_tenant_directo(radar_db, "B")
    ua = await crear_usuario(radar_db, a, "dueno@cliente.com", "dueno")
    ub = await crear_usuario(radar_db, b, "dueno@cliente.com", "gestor")
    async with radar_db.sin_tenant() as con:
        filas = await con.fetch("SELECT user_id, tenant_id FROM radar_auth_usuarios_por_email($1)",
                                "  Dueno@Cliente.com ")
        assert await con.fetchval("SELECT count(*) FROM users") == 0   # sin tenant, RLS oculta
    assert {(r["user_id"], r["tenant_id"]) for r in filas} == {(ua, a), (ub, b)}


async def test_support_grants_maximo_72_horas(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    u = await crear_usuario(radar_db, a, "dueno@cliente.com", "dueno")
    async with radar_db.tenant_tx(a) as con:
        await con.execute("INSERT INTO support_grants (otorgado_por, expires_at) VALUES ($1, now() + interval '48 hours')", u)
    with pytest.raises(asyncpg.CheckViolationError):
        async with radar_db.tenant_tx(a) as con:
            await con.execute("INSERT INTO support_grants (otorgado_por, expires_at) VALUES ($1, now() + interval '80 hours')", u)


async def test_grant_update_no_alcanza_columnas_de_identidad(radar_db):
    """El GRANT UPDATE por columna (no RLS) protege id, email, user_id y rol
    de sesión: radar_app no tiene privilegio de columna, no importa el
    WHERE. RLS solo filtra filas; esto filtra columnas."""
    a = await crear_tenant_directo(radar_db, "A")
    u = await crear_usuario(radar_db, a, "dueno@cliente.com", "dueno")
    la = await crear_linea_directa(radar_db, a)
    async with radar_db.tenant_tx(a) as con:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await con.execute("UPDATE lines SET id = gen_random_uuid() WHERE id = $1", la)
    async with radar_db.tenant_tx(a) as con:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await con.execute("UPDATE users SET email = 'otro@cliente.com' WHERE id = $1", u)
    async with radar_db.tenant_tx(a) as con:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await con.execute("UPDATE memberships SET user_id = $1 WHERE user_id = $1", u)
    async with radar_db.tenant_tx(a) as con:
        await con.execute(
            "INSERT INTO sessions (user_id, rol, token_hash, expires_at) "
            "VALUES ($1, 'dueno', $2, now() + interval '1 hour')", u, "cd34" * 16)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await con.execute("UPDATE sessions SET rol = 'soporte' WHERE user_id = $1", u)


async def test_membership_admin_solo_en_tenant_kis_tambien_al_actualizar(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    u = await crear_usuario(radar_db, a, "dueno@cliente.com", "dueno")
    with pytest.raises(asyncpg.CheckViolationError):
        async with radar_db.tenant_tx(a) as con:
            await con.execute("UPDATE memberships SET rol = 'admin' WHERE user_id = $1", u)
