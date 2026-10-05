"""
Email transaccional de Radar. Los mails llevan solo conteos y un link
autenticado (§3 P5, §7): nunca datos de conversación.

Backends (RADAR_MAILER):
- memoria: MemoryMailer guarda los mails en memoria (los tests leen el link de ahí).
- log: LogMailer, dev/staging. Loguea SOLO `*@dominio`, asunto y la huella del
  token (sha256 truncada); jamás el cuerpo ni el token. No manda nada.
- smtp: SmtpMailer, el que manda de verdad. SMTP genérico con smtplib (Google
  Workspace, Resend, SES, Brevo...): sin proveedor atado ni dependencias nuevas.
construir_mailer() rechaza cualquier otro nombre.
"""

import asyncio
import hashlib
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Protocol

from pydantic import SecretStr

from app.radar.settings import RadarSettings

logger = logging.getLogger("app.radar.mailer")

SEGURIDADES_SMTP = ("starttls", "ssl", "ninguna")


@dataclass(frozen=True)
class Email:
    para: str
    asunto: str
    texto: str
    huella: str   # huella_token() del token que viaja en el cuerpo; sirve para correlacionar logs


class Mailer(Protocol):
    async def enviar(self, mail: Email) -> None: ...


class MemoryMailer:
    def __init__(self):
        self.enviados: list[Email] = []

    async def enviar(self, mail: Email) -> None:
        self.enviados.append(mail)


def _loguear_envio(mail: Email) -> None:
    logger.info("email enviado a *@%s asunto=%r huella=%s", dominio_de(mail.para), mail.asunto, mail.huella)


class LogMailer:
    async def enviar(self, mail: Email) -> None:
        _loguear_envio(mail)


class SmtpMailer:
    """Un mail por conexión (el volumen son links de acceso y avisos, no campañas).
    smtplib es bloqueante: el envío corre en un hilo para no frenar el loop.

    La contraseña se guarda como SecretStr y se abre recién al autenticar: ni un
    repr ni un vars() del mailer la muestran.

    Si el envío falla, la excepción sale tal cual y acá no se loguea nada: quien
    llama (enviar_link_seguro y los demás) registra solo type(e).__name__, porque
    el texto de un error de smtplib puede traer el destinatario completo o la
    respuesta del servidor. Tampoco se usa set_debuglevel (imprime el AUTH)."""

    def __init__(self, *, host: str, puerto: int, usuario: str, password: SecretStr, seguridad: str,
                 timeout_s: float, remitente: str):
        if seguridad not in SEGURIDADES_SMTP:
            # Una seguridad desconocida no puede degradarse en silencio a "sin cifrar".
            raise ValueError("RADAR_SMTP_SEGURIDAD desconocida (" + "|".join(SEGURIDADES_SMTP) + ")")
        self._host, self._puerto = host, puerto
        self._usuario, self._password = usuario, password
        self._seguridad, self._timeout_s = seguridad, timeout_s
        self._remitente = remitente

    async def enviar(self, mail: Email) -> None:
        await asyncio.to_thread(self._enviar_bloqueante, mail)
        _loguear_envio(mail)

    def _armar(self, mail: Email) -> EmailMessage:
        # Con la política por defecto, un salto de línea en un valor de encabezado lanza
        # ValueError: ni el asunto (nombre del cliente) ni el destinatario pueden inyectar encabezados.
        mensaje = EmailMessage()
        mensaje["From"] = self._remitente
        mensaje["To"] = mail.para
        mensaje["Subject"] = mail.asunto
        mensaje["Date"] = formatdate(usegmt=True)
        # RADAR_REMITENTE puede ser "Nombre <dir@dominio>": el dominio sale de la dirección sola.
        mensaje["Message-ID"] = make_msgid(domain=parseaddr(self._remitente)[1].rpartition("@")[2] or None)
        # quoted-printable deja el mensaje en 7 bits: no depende de que el servidor anuncie 8BITMIME.
        mensaje.set_content(mail.texto, charset="utf-8", cte="quoted-printable")
        return mensaje

    def _enviar_bloqueante(self, mail: Email) -> None:
        mensaje = self._armar(mail)   # antes de conectar: un mensaje inválido no abre ninguna conexión
        # smtplib sin contexto propio usa uno que NO verifica el certificado: se le pasa uno que sí.
        if self._seguridad == "ssl":
            conexion = smtplib.SMTP_SSL(self._host, self._puerto, timeout=self._timeout_s,
                                        context=ssl.create_default_context())
        else:
            conexion = smtplib.SMTP(self._host, self._puerto, timeout=self._timeout_s)
        with conexion:
            if self._seguridad == "starttls":
                conexion.starttls(context=ssl.create_default_context())   # antes del login, siempre
            if self._usuario:
                conexion.login(self._usuario, self._password.get_secret_value())
            conexion.send_message(mensaje)


def huella_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:8]


def dominio_de(email: str) -> str:
    return email.rsplit("@", 1)[-1] if "@" in email else "?"


def construir_mailer(nombre: str, rs: RadarSettings | None = None) -> Mailer:
    if nombre == "memoria":
        return MemoryMailer()
    if nombre == "log":
        return LogMailer()
    if nombre == "smtp":
        if rs is None:
            raise ValueError("RADAR_MAILER=smtp necesita los settings (RADAR_SMTP_*)")
        return SmtpMailer(host=rs.smtp_host, puerto=rs.smtp_port, usuario=rs.smtp_usuario, password=rs.smtp_password,
                          seguridad=rs.smtp_seguridad, timeout_s=rs.smtp_timeout_s, remitente=rs.remitente)
    raise ValueError(f"RADAR_MAILER desconocido: {nombre!r} (log|memoria|smtp)")
