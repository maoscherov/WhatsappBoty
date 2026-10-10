"""
access_audit_log y product_events desde la app: lista blanca de claves,
valores sin texto libre y solo inserción.
"""
from decimal import Decimal

import pytest

from app.radar import auditoria, eventos_producto
from app.radar.auditoria import DetalleProhibido
from app.radar.eventos_producto import ValoresProhibidos

from .helpers import crear_tenant_directo, crear_usuario


async def test_registrar_auditoria_y_leer(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    u = await crear_usuario(radar_db, a, "dueno@cliente.com", "dueno")
    async with radar_db.tenant_tx(a) as con:
        id_ = await auditoria.registrar(
            con, tenant_id=a, actor_user_id=u, actor_rol="dueno", accion="parametro_cambiado",
            tipo_objeto="line", objeto_id=u, ip="10.0.0.1",
            detalle={"parametro": "tope_ia_mensual_usd", "valor_anterior": None,
                     "valor_nuevo": Decimal("12.50"), "ambito": "linea"},
        )
        fila = await con.fetchrow("SELECT * FROM access_audit_log WHERE id = $1", id_)
    assert fila["actor_user_id"] == u and fila["actor_rol"] == "dueno"
    assert fila["accion"] == "parametro_cambiado" and fila["tipo_objeto"] == "line"
    assert fila["ip"] == "10.0.0.1"
    import json
    assert json.loads(fila["detalle"]) == {"parametro": "tope_ia_mensual_usd", "valor_anterior": None,
                                           "valor_nuevo": 12.5, "ambito": "linea"}


@pytest.mark.parametrize("detalle", [
    {"nombre": "Juan"},                                   # clave fuera de la lista blanca
    {"telefono": "+5493411234567"},
    {"contact_hmac": "zz"},                               # no es hex de 64
    {"parametro": "no_existe"},
    {"valor_nuevo": "hola"},                              # string que no es enum de parámetro
    {"rol_nuevo": "root"},
    {"ambito": "global"},
    {"longitud_termino": "12"},                           # entero como string
    {"cantidad": True},                                   # bool no es entero
])
def test_validar_detalle_rechaza(detalle):
    with pytest.raises(DetalleProhibido):
        auditoria.validar_detalle(detalle)


def test_validar_detalle_acepta_lo_permitido():
    assert auditoria.validar_detalle(None) == {}
    d = auditoria.validar_detalle({"contact_hmac": "0f" * 32, "valor_nuevo": "sensible",
                                   "rol_anterior": "gestor", "rol_nuevo": "lector", "horas": 48,
                                   "longitud_termino": 5, "resultados": 0, "proposito": "invitacion"})
    assert d["valor_nuevo"] == "sensible" and d["horas"] == 48


async def test_registrar_rechaza_accion_o_tipo_desconocidos(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    async with radar_db.tenant_tx(a) as con:
        with pytest.raises(ValueError):
            await auditoria.registrar(con, tenant_id=a, actor_user_id=None, actor_rol="sistema",
                                      accion="borrar_todo", tipo_objeto="line")
        with pytest.raises(ValueError):
            await auditoria.registrar(con, tenant_id=a, actor_user_id=None, actor_rol="sistema",
                                      accion="login_canjeado", tipo_objeto="telefono")
        with pytest.raises(ValueError):
            await auditoria.registrar(con, tenant_id=a, actor_user_id=None, actor_rol="root",
                                      accion="login_canjeado", tipo_objeto="login_token")


async def test_registrar_evento_y_leer(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    async with radar_db.tenant_tx(a) as con:
        id_ = await eventos_producto.registrar_evento(
            con, tenant_id=a, evento="login_canjeado", valores={"intentos": 1, "demora_ms": 120.5})
        fila = await con.fetchrow("SELECT evento, valores FROM product_events WHERE id = $1", id_)
    import json
    assert fila["evento"] == "login_canjeado"
    assert json.loads(fila["valores"]) == {"intentos": 1, "demora_ms": 120.5}


@pytest.mark.parametrize("valores", [
    {"nombre": "x"}, {"n": None}, {"n": [1]}, {"n": {"m": 1}}, {"Clave Rara": 1},
])
def test_validar_valores_rechaza(valores):
    with pytest.raises(ValoresProhibidos):
        eventos_producto.validar_valores(valores)


async def test_registrar_evento_rechaza_evento_desconocido(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    async with radar_db.tenant_tx(a) as con:
        with pytest.raises(ValueError):
            await eventos_producto.registrar_evento(con, tenant_id=a, evento="cualquier_cosa")
