"""
Tokens hasheados, cookie firmada atada al tenant, sesiones revocables y un
mailer que jamás loguea el token.
"""
import hashlib
import logging
import uuid
from datetime import timedelta

import pytest
from starlette.requests import Request
from starlette.responses import Response

from app.radar import auth
from app.radar.mailer import Email, LogMailer, MemoryMailer, construir_mailer, huella_token
from app.radar.settings import RadarSettings

from .helpers import crear_tenant_directo, crear_usuario

SECRETO = "secreto-de-test-de-32-caracteres!"


def test_generar_token_devuelve_claro_y_sha256():
    claro, h = auth.generar_token()
    assert h == hashlib.sha256(claro.encode()).hexdigest()
    assert auth.token_valido(claro)
    assert not auth.token_valido("corto")
    assert not auth.token_valido(claro + " ")


def test_cookie_firmada_roundtrip_y_adulteraciones():
    tid = uuid.uuid4()
    claro, _ = auth.generar_token()
    valor = auth.armar_cookie(SECRETO, tid, claro)
    assert auth.abrir_cookie(SECRETO, valor) == (tid, claro)
    t, k, firma = valor.split(".")
    otro, _ = auth.generar_token()
    assert auth.abrir_cookie(SECRETO, f"{t}.{otro}.{firma}") is None          # otro token
    assert auth.abrir_cookie(SECRETO, f"{uuid.uuid4()}.{k}.{firma}") is None  # otro tenant
    assert auth.abrir_cookie("otro-secreto", valor) is None
    assert auth.abrir_cookie(SECRETO, "basura") is None
    assert auth.abrir_cookie(SECRETO, "") is None
    # firma con caracteres no ASCII: hmac.compare_digest sobre str lanzaría TypeError (500 en vez de 401)
    assert auth.abrir_cookie(SECRETO, f"{t}.{k}." + "é" * 64) is None
    assert auth.abrir_cookie(SECRETO, f"{t}.{k}.ñ") is None


def test_set_cookie_sesion_emite_secure_httponly_y_samesite():
    """Los tests de API corren con cookie_secure=False; este guarda el
    atributo Secure de producción."""
    class Ctx:
        settings = RadarSettings(_env_file=None, cookie_secret=SECRETO, cookie_secure=True)
    resp = Response()
    claro, _ = auth.generar_token()
    auth.set_cookie_sesion(resp, Ctx(), uuid.uuid4(), claro)
    sc = resp.headers["set-cookie"].lower()
    assert "secure" in sc and "httponly" in sc and "samesite=lax" in sc and "path=/radar" in sc
    assert f"max-age={int(auth.SESION.total_seconds())}" in sc


async def test_emitir_resolver_y_revocar(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    u = await crear_usuario(radar_db, a, "dueno@cliente.com", "dueno")
    async with radar_db.tenant_tx(a) as con:
        token = await auth.emitir_sesion(con, tenant_id=a, user_id=u, rol="dueno", ip="10.0.0.1")
        s = await auth.resolver_sesion(con, a, token)
        assert s is not None
        assert (s.tenant_id, s.user_id, s.rol, s.email, s.lineas_permitidas, s.es_kis) == \
               (a, u, "dueno", "dueno@cliente.com", None, False)
        assert s.puede_ver_linea(uuid.uuid4())
        assert await auth.resolver_sesion(con, a, token[:-1] + "x") is None
        await auth.revocar_sesion(con, s.id)
        assert await auth.resolver_sesion(con, a, token) is None


async def test_sesion_vencida_no_resuelve(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    u = await crear_usuario(radar_db, a, "dueno@cliente.com", "dueno")
    async with radar_db.tenant_tx(a) as con:
        token = await auth.emitir_sesion(con, tenant_id=a, user_id=u, rol="dueno", ip=None,
                                         duracion=timedelta(seconds=-1))
        assert await auth.resolver_sesion(con, a, token) is None


async def test_sesion_no_cruza_tenants(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    b = await crear_tenant_directo(radar_db, "B")
    u = await crear_usuario(radar_db, a, "dueno@cliente.com", "dueno")
    async with radar_db.tenant_tx(a) as con:
        token = await auth.emitir_sesion(con, tenant_id=a, user_id=u, rol="dueno", ip=None)
    async with radar_db.tenant_tx(b) as con:
        assert await auth.resolver_sesion(con, b, token) is None


async def test_revocar_sesiones_de_usuario(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    u = await crear_usuario(radar_db, a, "g@cliente.com", "gestor", lineas_permitidas=[uuid.uuid4()])
    async with radar_db.tenant_tx(a) as con:
        t1 = await auth.emitir_sesion(con, tenant_id=a, user_id=u, rol="gestor", ip=None)
        t2 = await auth.emitir_sesion(con, tenant_id=a, user_id=u, rol="gestor", ip=None)
        s = await auth.resolver_sesion(con, a, t1)
        assert len(s.lineas_permitidas) == 1 and not s.puede_ver_linea(uuid.uuid4())
        assert await auth.revocar_sesiones_de(con, u) == 2
        assert await auth.resolver_sesion(con, a, t2) is None


async def test_emitir_sesion_rechaza_rol_desconocido():
    with pytest.raises(ValueError):    # falla antes de tocar la conexión
        await auth.emitir_sesion(None, tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), rol="root", ip=None)


def test_ip_de_toma_el_ultimo_salto_de_x_forwarded_for():
    """El primer valor lo escribe el cliente; el proxy de Railway agrega el
    salto real al final. La IP es evidencia (§4.4): no puede ser elegible."""
    scope = {"type": "http", "method": "GET", "path": "/", "headers": [(b"x-forwarded-for", b"1.2.3.4, 5.6.7.8")],
             "client": ("9.9.9.9", 1234), "query_string": b""}
    assert auth.ip_de(Request(scope)) == "5.6.7.8"
    scope["headers"] = [(b"x-forwarded-for", b"1.2.3.4")]
    assert auth.ip_de(Request(scope)) == "1.2.3.4"
    scope["headers"] = []
    assert auth.ip_de(Request(scope)) == "9.9.9.9"


def test_huella_token_es_estable_y_no_revela():
    claro, _ = auth.generar_token()
    h = huella_token(claro)
    assert len(h) == 8 and h == huella_token(claro) and h not in claro


async def test_log_mailer_no_loguea_ni_token_ni_cuerpo(caplog):
    caplog.set_level(logging.INFO, logger="app.radar.mailer")
    claro, _ = auth.generar_token()
    mail = Email(para="dueno@cliente.com", asunto="Tu acceso", texto=f"link: http://x/?k={claro}",
                 huella=huella_token(claro))
    await LogMailer().enviar(mail)
    salida = "\n".join(r.getMessage() for r in caplog.records)
    assert "*@cliente.com" in salida and mail.huella in salida
    assert claro not in salida and "dueno@" not in salida and "link:" not in salida


async def test_memory_mailer_guarda():
    m = MemoryMailer()
    await m.enviar(Email(para="a@b.c", asunto="x", texto="y", huella="00000000"))
    assert m.enviados[0].para == "a@b.c"


def test_construir_mailer():
    assert isinstance(construir_mailer("memoria"), MemoryMailer)
    assert isinstance(construir_mailer("log"), LogMailer)
    with pytest.raises(ValueError):
        construir_mailer("sendgrid")
