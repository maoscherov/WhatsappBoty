"""
Email transaccional de Radar. Los mails llevan solo conteos y un link
autenticado (§3 P5, §7): nunca datos de conversación.

Backends:
- MemoryMailer: guarda los mails en memoria (los tests leen el link de ahí).
- LogMailer: dev/staging. Loguea SOLO `*@dominio`, asunto y la huella del
  token (sha256 truncada); jamás el cuerpo ni el token.
El proveedor de producción es una decisión pendiente del dueño (ver el plan,
Self-Review): construir_mailer() rechaza cualquier otro nombre.
"""

import hashlib
import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger("app.radar.mailer")


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


class LogMailer:
    async def enviar(self, mail: Email) -> None:
        logger.info("email enviado a *@%s asunto=%r huella=%s", dominio_de(mail.para), mail.asunto, mail.huella)


def huella_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:8]


def dominio_de(email: str) -> str:
    return email.rsplit("@", 1)[-1] if "@" in email else "?"


def construir_mailer(nombre: str) -> Mailer:
    if nombre == "memoria":
        return MemoryMailer()
    if nombre == "log":
        return LogMailer()
    raise ValueError(f"RADAR_MAILER desconocido: {nombre!r} (log|memoria)")
