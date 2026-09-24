"""Atajos de test que escriben directo en la base como radar_app."""

import uuid
from datetime import datetime
from typing import Optional

import asyncpg

from app.radar.auth import COOKIE, armar_cookie, emitir_sesion
from app.radar.consentimiento import hash_texto
from app.radar.constantes import TENANT_KIS
from app.radar.db import RadarDB

# Dominio con el que hay que setear una cookie A MANO en el cliente httpx con
# base_url="http://testserver". http.cookiejar trata un host sin punto como
# "testserver.local": una cookie con domain="testserver" NUNCA se manda, y una
# sin domain convive con la que el servidor setea (Set-Cookie queda bajo
# "testserver.local") y cliente.cookies.get() lanza CookieConflict. Con el host
# efectivo, la del servidor reemplaza a la manual. Verificado con httpx 0.27/0.28.
DOMINIO_COOKIE = "testserver.local"


async def entrar(cliente, ctx, tenant_id: uuid.UUID, user_id: uuid.UUID, rol: str) -> None:
    """Deja al cliente httpx con una cookie de sesión válida (sin pasar por el email)."""
    async with ctx.db.tenant_tx(tenant_id) as con:
        token = await emitir_sesion(con, tenant_id=tenant_id, user_id=user_id, rol=rol, ip="127.0.0.1")
    cliente.cookies.clear()
    cliente.cookies.set(COOKIE, armar_cookie(ctx.settings.cookie_secret, tenant_id, token),
                        domain=DOMINIO_COOKIE, path="/radar")


async def crear_tenant_directo(db: RadarDB, nombre: str = "Farmacia Test", rubro: str = "farmacia",
                               perfil: str = "estandar") -> uuid.UUID:
    tid = uuid.uuid4()
    async with db.tenant_tx(tid) as con:
        await con.fetchval(
            "SELECT radar_admin_crear_tenant($1, $2, $3, $4, 12, FALSE, TRUE, 'lotes')",
            tid, nombre, rubro, perfil,
        )
    return tid


async def crear_usuario(db: RadarDB, tenant_id: uuid.UUID, email: str, rol: str,
                        lineas_permitidas: list[uuid.UUID] | None = None) -> uuid.UUID:
    async with db.tenant_tx(tenant_id) as con:
        uid = await con.fetchval(
            "INSERT INTO users (email, nombre) VALUES ($1, $2) RETURNING id", email, email.split("@")[0])
        await con.execute(
            "INSERT INTO memberships (user_id, rol, lineas_permitidas) VALUES ($1, $2, $3)",
            uid, rol, lineas_permitidas)
    return uid


async def crear_linea_directa(db: RadarDB, tenant_id: uuid.UUID, nombre: str = "Línea 1",
                              estado: str = "sin_vinculo") -> uuid.UUID:
    async with db.tenant_tx(tenant_id) as con:
        lid = await con.fetchval("INSERT INTO lines (nombre) VALUES ($1) RETURNING id", nombre)
        if estado != "sin_vinculo":
            await con.execute("UPDATE lines SET estado = $2 WHERE id = $1", lid, estado)
    return lid


class MailerQueFalla:
    """Mailer que lanza para los mails cuyo asunto contiene `falla_si` (todos si
    es ""), y guarda el resto como MemoryMailer."""

    def __init__(self, falla_si: str = ""):
        self.falla_si = falla_si
        self.enviados = []

    async def enviar(self, mail) -> None:
        if self.falla_si in mail.asunto:
            raise RuntimeError("proveedor de mail caído")
        self.enviados.append(mail)


async def como_superusuario(radar_urls: dict, sql: str, *args) -> None:
    """Para tests: escribe columnas que radar_app no puede actualizar (created_at,
    max_intentos). No sirve el migrator: con FORCE RLS no ve filas."""
    con = await asyncpg.connect(radar_urls["super"])
    try:
        await con.execute(sql, *args)
    finally:
        await con.close()


async def crear_worker_directo(db: RadarDB, nombre: str = "w1", engine: str = "NOWEB",
                               max_sesiones: int = 50, disco_max_gb: int = 10) -> uuid.UUID:
    async with db.tenant_tx(TENANT_KIS) as con:
        return await con.fetchval(
            "INSERT INTO waha_workers (nombre, base_url, engine, max_sesiones, disco_max_gb) "
            "VALUES ($1, 'http://waha.interno', $2, $3, $4) RETURNING id", nombre, engine, max_sesiones, disco_max_gb)


# Clave HMAC del webhook en tests (32+ caracteres, como exige validar_settings).
HMAC_TEST = "clave-hmac-de-test-de-32-caracteres!!"


async def crear_consentimiento_directo(db: RadarDB, tenant_id: uuid.UUID, line_id: uuid.UUID,
                                       user_id: uuid.UUID) -> uuid.UUID:
    async with db.tenant_tx(tenant_id) as con:
        return await con.fetchval(
            "INSERT INTO consents (line_id, user_id, version_texto, hash_texto, opciones) "
            "VALUES ($1, $2, 'v1', $3, '{}'::jsonb) RETURNING id", line_id, user_id, hash_texto("v1"))


async def crear_link_directo(db: RadarDB, tenant_id: uuid.UUID, line_id: uuid.UUID, worker_id: uuid.UUID,
                             consent_id: uuid.UUID, estado: str = "esperando_qr",
                             caido_desde: Optional[datetime] = None,
                             restriccion_hasta: Optional[datetime] = None) -> uuid.UUID:
    link_id = uuid.uuid4()
    async with db.tenant_tx(tenant_id) as con:
        await con.execute(
            "INSERT INTO links (id, line_id, worker_id, consent_id, session_name, engine, estado, caido_desde, "
            "restriccion_hasta) VALUES ($1, $2, $3, $4, $5, 'NOWEB', $6, $7, $8)",
            link_id, line_id, worker_id, consent_id, "v_" + link_id.hex[:12], estado, caido_desde,
            restriccion_hasta)
    return link_id
