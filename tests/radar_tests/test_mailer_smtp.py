"""
Email por SMTP (Decisión 6): SmtpMailer con smtplib de la stdlib. Ningún test abre
un socket: smtplib.SMTP y smtplib.SMTP_SSL se reemplazan por una clase falsa que
anota cada llamada. La contraseña SMTP es un secreto: no puede estar en un log,
en un repr ni en un mensaje de error.
"""
import email
import email.policy
import email.utils
import inspect
import logging
import pathlib
import re
import smtplib
import ssl
import threading
from datetime import datetime, timezone

import pytest
from pydantic import SecretStr

from app.radar.app import crear_app_radar, validar_settings
from app.radar.contexto import RadarContexto
from app.radar.links import enviar_link_seguro
from app.radar.mailer import Email, LogMailer, MemoryMailer, SmtpMailer, construir_mailer, huella_token
from app.radar.settings import RadarSettings

from .helpers import crear_tenant_directo, crear_usuario

HOST = "smtp.ejemplo.test"
USUARIO = "usuario-smtp@ejemplo.test"
PASSWORD = "clave-smtp-que-no-debe-salir"
REMITENTE = "radar@keepitsimple.com.ar"
DESTINO = "dueno@cliente.com"
TOKEN = "token-del-link-que-no-debe-salir-0123456789"
ASUNTO = "Tu acceso a Radar de Farmacia Ñandú"
TEXTO = ("Entrá a Radar de Farmacia Ñandú con este link. Vence en 15 minutos y sirve una sola vez.\n\n"
         f"https://radar.keepitsimple.com.ar/radar/login/canjear?t=1#k={TOKEN}\n\n"
         "Si no lo pediste, ignorá este mail. ¡Gracias! ü € 日本\n")
ID_DE_MENSAJE = re.compile(r"<[^<>@\s]+@keepitsimple\.com\.ar>")


# ── smtplib falso ──────────────────────────────────────────────────────────────

_SIN_TIMEOUT = object()   # como socket._GLOBAL_DEFAULT_TIMEOUT: si el mailer no pasa timeout, el test lo ve


class EstadoSmtp:
    """Lo que le pasó a smtplib en un test: las llamadas en orden, las conexiones que se abrieron, los
    hilos desde los que se llamó y las fallas a inyectar (nombre de la llamada -> excepción)."""

    def __init__(self):
        self.llamadas: list[tuple] = []
        self.conexiones: list = []
        self.hilos: set[int] = set()
        self.fallas: dict[str, BaseException] = {}

    def anotar(self, metodo: str, *args) -> None:
        self.hilos.add(threading.get_ident())
        self.llamadas.append((metodo, *args))
        if metodo in self.fallas:
            raise self.fallas[metodo]

    def metodos(self) -> list[str]:
        return [ll[0] for ll in self.llamadas]

    @property
    def mensajes(self) -> list:
        """Los EmailMessage que llegaron a send_message, aunque la llamada haya fallado."""
        return [ll[1] for ll in self.llamadas if ll[0] == "send_message"]


class _ConexionFalsa:
    """Lo que Radar usa de smtplib.SMTP / SMTP_SSL, con las firmas de Python 3.12 (un argumento de más en el
    mailer falla acá) y sin socket. Nunca se usa set_debuglevel: imprimiría el AUTH completo."""
    estado: EstadoSmtp

    def _abrir(self, tipo, host, port, timeout, context):
        self.estado.anotar(tipo, host, port)    # una falla de conexión sale acá: no hay nada que cerrar
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.cerrada = False
        self.estado.conexiones.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cerrada = True
        self.estado.anotar("cerrar")

    def starttls(self, *, context=None):
        self.context = context
        self.estado.anotar("starttls")

    def login(self, user, password, *, initial_response_ok=True):
        self.estado.anotar("login", user, password)

    def send_message(self, msg, from_addr=None, to_addrs=None, mail_options=(), rcpt_options=()):
        self.estado.anotar("send_message", msg)

    def set_debuglevel(self, debuglevel):
        self.estado.anotar("set_debuglevel", debuglevel)


class _SmtpFalso(_ConexionFalsa):
    def __init__(self, host="", port=0, local_hostname=None, timeout=_SIN_TIMEOUT, source_address=None):
        self._abrir("SMTP", host, port, timeout, None)


class _SmtpSslFalso(_ConexionFalsa):
    def __init__(self, host="", port=0, local_hostname=None, *, timeout=_SIN_TIMEOUT, source_address=None,
                 context=None):
        self._abrir("SMTP_SSL", host, port, timeout, context)


@pytest.fixture(autouse=True)
def _entorno_sin_variables_de_mail(monkeypatch):
    """RadarSettings lee el entorno: lo que el desarrollador tenga exportado (RADAR_SMTP_*, RADAR_MAILER...)
    no puede cambiar los defaults que estos tests comprueban."""
    for nombre in ("MAILER", "REMITENTE", "SMTP_HOST", "SMTP_PORT", "SMTP_USUARIO", "SMTP_PASSWORD",
                   "SMTP_SEGURIDAD", "SMTP_TIMEOUT_S"):
        monkeypatch.delenv(f"RADAR_{nombre}", raising=False)


@pytest.fixture
def smtp(monkeypatch):
    """smtplib.SMTP y smtplib.SMTP_SSL falsos. Devuelve el EstadoSmtp del test."""
    estado = EstadoSmtp()
    monkeypatch.setattr(smtplib, "SMTP", type("SMTP", (_SmtpFalso,), {"estado": estado}))
    monkeypatch.setattr(smtplib, "SMTP_SSL", type("SMTP_SSL", (_SmtpSslFalso,), {"estado": estado}))
    return estado


# ── armado ─────────────────────────────────────────────────────────────────────

def _settings(**cambios) -> RadarSettings:
    base = dict(_env_file=None, mailer="smtp", remitente=REMITENTE, smtp_host=HOST, smtp_usuario=USUARIO,
                smtp_password=PASSWORD)
    return RadarSettings(**{**base, **cambios})


def _mailer(**cambios) -> SmtpMailer:
    return construir_mailer("smtp", _settings(**cambios))


def _mail(**cambios) -> Email:
    base = dict(para=DESTINO, asunto=ASUNTO, texto=TEXTO, huella=huella_token(TOKEN))
    return Email(**{**base, **cambios})


# ── envío ──────────────────────────────────────────────────────────────────────

async def test_starttls_conecta_autentica_y_manda_el_mensaje(smtp):
    mail = _mail()
    await _mailer().enviar(mail)
    # starttls ANTES de login: la contraseña nunca viaja sin cifrar. Y ningún set_debuglevel.
    assert smtp.metodos() == ["SMTP", "starttls", "login", "send_message", "cerrar"]
    assert smtp.llamadas[0] == ("SMTP", HOST, 587)
    assert smtp.llamadas[2] == ("login", USUARIO, PASSWORD)
    m = smtp.mensajes[0]
    assert (m["From"], m["To"], str(m["Subject"])) == (REMITENTE, DESTINO, ASUNTO)
    assert m.get_content_type() == "text/plain" and m.get_content_charset() == "utf-8"
    assert m.get_content() == TEXTO


async def test_el_mensaje_viaja_en_ascii_y_se_lee_igual(smtp):
    """Lo que ve el servidor: 7 bits (no depende de que anuncie 8BITMIME), con el asunto y el cuerpo
    codificados; quien lo recibe lee las mismas tildes."""
    await _mailer().enviar(_mail())
    crudo = smtp.mensajes[0].as_bytes()
    crudo.decode("ascii")                      # no lanza: ni una tilde cruda
    recibido = email.message_from_bytes(crudo, policy=email.policy.default)
    assert str(recibido["Subject"]) == ASUNTO
    assert recibido.get_content() == TEXTO


async def test_lleva_date_y_message_id_con_el_dominio_del_remitente(smtp):
    mailer = _mailer()
    await mailer.enviar(_mail())
    await mailer.enviar(_mail())
    primero, segundo = smtp.mensajes
    fecha = email.utils.parsedate_to_datetime(str(primero["Date"]))
    assert fecha.tzinfo is not None             # con zona explícita (GMT), no "-0000"
    assert abs((datetime.now(timezone.utc) - fecha).total_seconds()) < 60
    assert ID_DE_MENSAJE.fullmatch(str(primero["Message-ID"])) and not primero["Message-ID"].defects
    assert primero["Message-ID"] != segundo["Message-ID"]


async def test_un_remitente_con_nombre_se_respeta_y_el_message_id_usa_solo_el_dominio(smtp):
    await _mailer(remitente="Radar KIS <radar@keepitsimple.com.ar>").enviar(_mail())
    m = smtp.mensajes[0]
    assert m["From"] == "Radar KIS <radar@keepitsimple.com.ar>"
    # La política de email arregla en silencio un ">" de más en el Message-ID, pero lo deja como defecto.
    assert ID_DE_MENSAJE.fullmatch(str(m["Message-ID"])) and not m["Message-ID"].defects


async def test_ssl_usa_smtp_ssl_y_no_hace_starttls(smtp):
    await _mailer(smtp_seguridad="ssl", smtp_port=465).enviar(_mail())
    assert smtp.metodos() == ["SMTP_SSL", "login", "send_message", "cerrar"]
    assert smtp.llamadas[0] == ("SMTP_SSL", HOST, 465)
    assert smtp.llamadas[1] == ("login", USUARIO, PASSWORD)


async def test_ninguna_no_usa_ni_starttls_ni_ssl(smtp):
    """Relay sin autenticar de una red privada: la conexión y el mensaje, nada más."""
    await _mailer(smtp_seguridad="ninguna", smtp_port=25, smtp_usuario="", smtp_password="").enviar(_mail())
    assert smtp.metodos() == ["SMTP", "send_message", "cerrar"]
    assert smtp.llamadas[0] == ("SMTP", HOST, 25)
    assert smtp.conexiones[0].context is None


async def test_sin_usuario_no_hace_login(smtp):
    await _mailer(smtp_usuario="", smtp_password="").enviar(_mail())
    assert smtp.metodos() == ["SMTP", "starttls", "send_message", "cerrar"]


@pytest.mark.parametrize("seguridad", ["starttls", "ssl"])
async def test_tls_verifica_el_certificado_y_el_nombre_del_servidor(smtp, seguridad):
    """smtplib sin un contexto propio usa uno que NO verifica nada (ssl._create_stdlib_context)."""
    await _mailer(smtp_seguridad=seguridad).enviar(_mail())
    contexto = smtp.conexiones[0].context
    assert isinstance(contexto, ssl.SSLContext)
    assert contexto.verify_mode == ssl.CERT_REQUIRED and contexto.check_hostname is True


@pytest.mark.parametrize("seguridad", ["starttls", "ssl", "ninguna"])
async def test_el_timeout_va_al_constructor(smtp, seguridad):
    await _mailer(smtp_seguridad=seguridad, smtp_usuario="", smtp_password="", smtp_timeout_s=7.5).enviar(_mail())
    assert smtp.conexiones[0].timeout == 7.5


async def test_sin_configurar_el_timeout_es_de_20_segundos(smtp):
    await _mailer().enviar(_mail())
    assert smtp.conexiones[0].timeout == 20.0


async def test_el_envio_corre_en_otro_hilo(smtp):
    """smtplib es bloqueante: en el loop, un servidor lento frenaría a todo Radar."""
    mailer = _mailer()
    assert inspect.iscoroutinefunction(mailer.enviar)
    await mailer.enviar(_mail())
    assert smtp.hilos and threading.get_ident() not in smtp.hilos


@pytest.mark.parametrize("campo, valor", [
    ("asunto", "Tu acceso\r\nBcc: espia@otro.com"),
    ("para", "dueno@cliente.com\r\nBcc: espia@otro.com"),
])
async def test_un_salto_de_linea_en_un_encabezado_no_inyecta_encabezados(smtp, campo, valor):
    with pytest.raises(ValueError):
        await _mailer().enviar(_mail(**{campo: valor}))
    assert smtp.llamadas == []                  # el mensaje se arma antes de conectar


# ── fallas y logs ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("etapa, error", [
    pytest.param("SMTP", ConnectionRefusedError("conexion rechazada"), id="conexion"),
    pytest.param("starttls", smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server."),
                 id="starttls"),
    pytest.param("login", smtplib.SMTPAuthenticationError(535, b"5.7.8 usuario o clave incorrectos"), id="login"),
    pytest.param("send_message", smtplib.SMTPRecipientsRefused({DESTINO: (550, b"5.1.1 no existe")}), id="envio"),
])
async def test_un_error_de_smtp_se_propaga_tal_cual_y_no_filtra_nada(smtp, caplog, etapa, error):
    caplog.set_level(logging.DEBUG)
    smtp.fallas[etapa] = error
    with pytest.raises(type(error)) as exc:
        await _mailer().enviar(_mail())
    # Sin envolver: los llamadores loguean type(e).__name__ y de ahí se diagnostica.
    assert exc.value is error
    # str(error) de SMTPRecipientsRefused trae el destinatario completo: el mailer no loguea el texto del error.
    for secreto in (PASSWORD, TOKEN, DESTINO, "ignorá este mail"):
        assert secreto not in caplog.text
    assert "email enviado" not in caplog.text
    assert all(c.cerrada for c in smtp.conexiones)          # la conexión que se abrió, se cierra
    if etapa == "starttls":
        assert "login" not in smtp.metodos()                # sin TLS no se manda la contraseña


async def test_en_exito_loguea_lo_mismo_que_el_log_mailer(smtp, caplog):
    caplog.set_level(logging.DEBUG)
    mail = _mail()
    await LogMailer().enviar(mail)
    esperado = [r.getMessage() for r in caplog.records if r.name == "app.radar.mailer"]
    caplog.clear()
    await _mailer().enviar(mail)
    obtenido = [r.getMessage() for r in caplog.records if r.name == "app.radar.mailer"]
    assert obtenido == esperado and len(esperado) == 1
    assert "*@cliente.com" in obtenido[0] and mail.huella in obtenido[0] and ASUNTO in obtenido[0]
    for secreto in (PASSWORD, TOKEN, DESTINO, "ignorá este mail"):
        assert secreto not in caplog.text


@pytest.mark.parametrize("seguridad", ["tls", "SSL", "", "starttls "])
def test_una_seguridad_desconocida_no_cae_en_texto_plano(seguridad):
    with pytest.raises(ValueError, match="RADAR_SMTP_SEGURIDAD") as exc:
        _mailer(smtp_seguridad=seguridad)
    for valor in (HOST, USUARIO, PASSWORD):
        assert valor not in str(exc.value)


# ── settings, validación y fábrica ─────────────────────────────────────────────

BASE = dict(_env_file=None, database_url="x", migrator_database_url="x", fuente_database_url="x",
            cookie_secret="secreto-de-test-de-32-caracteres!")


def _validar(**cambios) -> None:
    base = dict(BASE, mailer="smtp", smtp_host=HOST, smtp_usuario=USUARIO, smtp_password=PASSWORD)
    validar_settings(RadarSettings(**{**base, **cambios}))


def test_los_settings_de_smtp_y_sus_defaults():
    rs = RadarSettings(_env_file=None)
    assert (rs.smtp_host, rs.smtp_port, rs.smtp_usuario, rs.smtp_seguridad, rs.smtp_timeout_s) == \
           ("", 587, "", "starttls", 20.0)
    assert isinstance(rs.smtp_password, SecretStr) and rs.smtp_password.get_secret_value() == ""


def test_los_settings_de_smtp_se_leen_de_las_variables_radar_smtp(monkeypatch):
    for nombre, valor in (("HOST", HOST), ("PORT", "2525"), ("USUARIO", USUARIO), ("PASSWORD", PASSWORD),
                          ("SEGURIDAD", "ssl"), ("TIMEOUT_S", "7.5")):
        monkeypatch.setenv(f"RADAR_SMTP_{nombre}", valor)
    rs = RadarSettings(_env_file=None)
    assert (rs.smtp_host, rs.smtp_port, rs.smtp_usuario, rs.smtp_seguridad, rs.smtp_timeout_s) == \
           (HOST, 2525, USUARIO, "ssl", 7.5)
    assert rs.smtp_password.get_secret_value() == PASSWORD


@pytest.mark.parametrize("cambios, patron", [
    (dict(smtp_host=""), "RADAR_SMTP_HOST"),
    (dict(smtp_password=""), "RADAR_SMTP_PASSWORD"),                        # usuario sin contraseña
    (dict(smtp_seguridad="tls"), "RADAR_SMTP_SEGURIDAD"),
    (dict(smtp_seguridad="SSL"), "RADAR_SMTP_SEGURIDAD"),
    (dict(smtp_seguridad=""), "RADAR_SMTP_SEGURIDAD"),
    (dict(smtp_seguridad="ninguna"), "sin cifrar"),                         # con usuario: la contraseña iría en claro
    (dict(smtp_usuario="usuário@ejemplo.test"), "ASCII"),                   # smtplib solo autentica en ASCII
    (dict(smtp_password="contraseña-con-ñ"), "ASCII"),
])
def test_validar_settings_rechaza_smtp_mal_configurado(cambios, patron):
    with pytest.raises(RuntimeError, match=patron) as exc:
        _validar(**cambios)
    # El mensaje nombra variables, nunca valores (ni los de este caso ni los no ASCII).
    for valor in (HOST, USUARIO, PASSWORD, "usuário@ejemplo.test", "contraseña-con-ñ"):
        assert valor not in str(exc.value)


@pytest.mark.parametrize("cambios", [
    {},                                                                      # starttls con usuario
    dict(smtp_seguridad="ssl", smtp_port=465),
    dict(smtp_usuario="", smtp_password=""),                                 # relay sin autenticar, por starttls
    dict(smtp_seguridad="ninguna", smtp_usuario="", smtp_password=""),       # relay de una red privada
])
def test_validar_settings_acepta_smtp_bien_configurado(cambios):
    _validar(**cambios)


@pytest.mark.parametrize("mailer", ["log", "memoria"])
def test_validar_settings_no_mira_smtp_si_el_mailer_no_es_smtp(mailer):
    validar_settings(RadarSettings(**BASE, mailer=mailer, smtp_seguridad="basura", smtp_usuario="u"))


def test_construir_mailer_con_los_settings(smtp):
    assert isinstance(construir_mailer("smtp", _settings()), SmtpMailer)
    assert smtp.llamadas == []                                               # construir no se conecta a nada
    # El nombre manda: pasar settings no cambia a los otros backends.
    assert isinstance(construir_mailer("log", _settings()), LogMailer)
    assert isinstance(construir_mailer("memoria", _settings()), MemoryMailer)


def test_construir_mailer_smtp_sin_settings_es_un_error():
    with pytest.raises(ValueError, match="RADAR_SMTP"):
        construir_mailer("smtp")


@pytest.mark.parametrize("args", [("sendgrid",), ("sendgrid", None), ("SMTP",), ("",)])
def test_construir_mailer_sigue_rechazando_nombres_desconocidos(args):
    with pytest.raises(ValueError, match=r"log\|memoria\|smtp"):
        construir_mailer(*args)


def test_la_contrasena_no_aparece_en_ningun_repr():
    rs = _settings()
    mailer = construir_mailer("smtp", rs)
    ctx = RadarContexto(settings=rs, db=None, fuente=None, secretos=None, mailer=mailer)
    for texto in (repr(rs), str(rs), repr(rs.model_dump()), rs.model_dump_json(), repr(mailer), repr(vars(mailer)),
                  repr(ctx)):
        assert PASSWORD not in texto
    assert rs.smtp_password.get_secret_value() == PASSWORD                  # el valor sigue ahí para quien lo necesita


# ── con los llamadores reales ──────────────────────────────────────────────────

async def test_enviar_link_seguro_manda_el_link_por_smtp(radar_ctx, smtp, caplog):
    caplog.set_level(logging.DEBUG)
    radar_ctx.mailer = _mailer()
    t = await crear_tenant_directo(radar_ctx.db, "Farmacia Ñandú")
    u = await crear_usuario(radar_ctx.db, t, DESTINO, "dueno")
    assert await enviar_link_seguro(radar_ctx, tenant_id=t, user_id=u, email=DESTINO, proposito="login",
                                    ip=None) is True
    m = smtp.mensajes[0]
    assert (m["To"], str(m["Subject"])) == (DESTINO, "Tu acceso a Radar de Farmacia Ñandú")
    cuerpo = m.get_content()
    assert f"http://testserver/radar/login/canjear?t={t}#k=" in cuerpo
    token = re.search(r"#k=(\S+)", cuerpo).group(1)
    assert "*@cliente.com" in caplog.text
    assert token not in caplog.text and DESTINO not in caplog.text and PASSWORD not in caplog.text


async def test_enviar_link_seguro_con_el_servidor_rechazando_devuelve_false_sin_filtrar(radar_ctx, smtp, caplog):
    """Quien llama loguea solo type(e).__name__: el texto de SMTPRecipientsRefused trae el email completo."""
    caplog.set_level(logging.DEBUG)
    radar_ctx.mailer = _mailer()
    t = await crear_tenant_directo(radar_ctx.db, "Farmacia Ñandú")
    u = await crear_usuario(radar_ctx.db, t, DESTINO, "dueno")
    smtp.fallas["send_message"] = smtplib.SMTPRecipientsRefused({DESTINO: (550, b"5.1.1 buzon inexistente")})
    assert await enviar_link_seguro(radar_ctx, tenant_id=t, user_id=u, email=DESTINO, proposito="login",
                                    ip=None) is False
    token = re.search(r"#k=(\S+)", smtp.mensajes[0].get_content()).group(1)
    assert "SMTPRecipientsRefused" in caplog.text
    for secreto in (PASSWORD, token, DESTINO, "buzon inexistente"):
        assert secreto not in caplog.text


async def test_el_script_crear_admin_sigue_imprimiendo_el_link_con_smtp_configurado(
        radar_db, radar_urls, tmp_path, monkeypatch, capsys, smtp):
    """scripts/radar_admin.py es para el caso sin SMTP: con RADAR_MAILER=smtp en el entorno sigue poniendo un
    MemoryMailer, imprime el link y no abre ninguna conexión."""
    import scripts.radar_admin as script
    from app.radar.settings import get_radar_settings
    for nombre, valor in (("RADAR_DATABASE_URL", radar_urls["app"]),
                          ("RADAR_MIGRATOR_DATABASE_URL", radar_urls["migrator"]),
                          ("RADAR_FUENTE_DATABASE_URL", radar_urls["fuente"]),
                          ("RADAR_COOKIE_SECRET", "secreto-de-test-de-32-caracteres!"),
                          ("RADAR_SECRETS_DIR", str(tmp_path / "s")),
                          ("RADAR_PUBLIC_BASE_URL", "https://radar.test"),
                          ("RADAR_MAILER", "smtp"), ("RADAR_SMTP_HOST", HOST),
                          ("RADAR_SMTP_USUARIO", USUARIO), ("RADAR_SMTP_PASSWORD", PASSWORD)):
        monkeypatch.setenv(nombre, valor)
    get_radar_settings.cache_clear()
    try:
        script.main(["crear-admin", "--email", "admin-smtp@keepitsimple.com.ar", "--nombre", "Admin"])
    finally:
        get_radar_settings.cache_clear()
    salida = capsys.readouterr().out
    assert "https://radar.test/radar/login/canjear?t=00000000-0000-0000-0000-000000000001#k=" in salida
    assert PASSWORD not in salida and smtp.llamadas == []


def _settings_de_arranque(radar_urls, tmp_path, **cambios) -> RadarSettings:
    return RadarSettings(
        _env_file=None, database_url=radar_urls["app"], migrator_database_url=radar_urls["migrator"],
        fuente_database_url=radar_urls["fuente"], cookie_secret="secreto-de-test-de-32-caracteres!",
        secrets_dir=str(tmp_path / "secretos"), worker_embebido=False, **cambios)


async def test_el_arranque_con_smtp_arma_el_mailer_sin_conectarse(radar_urls, tmp_path, smtp):
    rs = _settings_de_arranque(radar_urls, tmp_path, mailer="smtp", smtp_host=HOST, smtp_usuario=USUARIO,
                               smtp_password=PASSWORD)
    app = crear_app_radar(rs)
    async with app.router.lifespan_context(app):
        assert isinstance(app.state.radar.mailer, SmtpMailer)
    assert smtp.llamadas == []                                               # arrancar no prueba el servidor SMTP


async def test_el_arranque_con_smtp_incompleto_no_arranca(radar_urls, tmp_path, smtp):
    app = crear_app_radar(_settings_de_arranque(radar_urls, tmp_path, mailer="smtp"))
    with pytest.raises(RuntimeError, match="RADAR_SMTP_HOST"):
        async with app.router.lifespan_context(app):
            pass


def test_la_tabla_de_despliegue_documenta_las_variables_de_smtp():
    doc = (pathlib.Path(__file__).resolve().parents[2] / "docs" / "radar-despliegue.md").read_text(encoding="utf-8")
    for variable in ("RADAR_SMTP_HOST", "RADAR_SMTP_PORT", "RADAR_SMTP_USUARIO", "RADAR_SMTP_PASSWORD",
                     "RADAR_SMTP_SEGURIDAD", "RADAR_SMTP_TIMEOUT_S"):
        assert f"| `{variable}` |" in doc, variable
    fila_mailer = next(f for f in doc.splitlines() if f.startswith("| `RADAR_MAILER` |"))
    assert "smtp" in fila_mailer and "por definir" not in fila_mailer
    corrido = " ".join(doc.split())             # la prosa de la doc está cortada en líneas
    assert "smtp.gmail.com" in corrido and "contraseña de aplicación" in corrido
