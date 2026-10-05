"""
Workers de WAHA y admisión por capacidad (§6.2).

- El motor es del servidor: cada worker tiene su `engine` y el cuerpo de la
  sesión depende de él.
- Admisión: se elige, entre los activos que con la sesión nueva quedan en
  <= 80 % de max_sesiones y con disco <= 70 %, el de menor ocupación. Si no hay
  ninguno se lanza SinCapacidad y NO se crea sesión ni se muestra QR.
- ÚNICO módulo que lee la clave admin de un worker, desde el SecretStore
  (waha_admin:<worker_id>). Nunca va a la base, a un log ni al navegador.
- También es el que la prueba contra el propio WAHA antes de guardarla
  (verificar_clave_worker): una clave que WAHA no acepta no se guarda.
"""

import logging
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from app.radar import auditoria
from app.radar.constantes import TENANT_KIS
from app.radar.contexto import RadarContexto
from app.radar.waha.cliente import WahaCliente, WahaError, WahaHttpError

logger = logging.getLogger("app.radar.workers")

UMBRAL_SESIONES = 0.8
UMBRAL_DISCO = 0.7
MOTORES = ("NOWEB", "GOWS")
# Lo que cabe en la cabecera HTTP que lleva la clave: ASCII visible, sin espacios ni saltos de línea. Otra cosa no
# puede ser la clave de un WAHA, y httpx fallaría con un error cuyo mensaje trae un carácter de la clave.
_CLAVE_ADMIN = re.compile(r"[\x21-\x7e]+")


class SinCapacidad(RuntimeError):
    pass


class ClaveAdminAusente(RuntimeError):
    pass


class VerificacionFallida(RuntimeError):
    """WAHA no aceptó la clave: no se guarda nada. `codigo` es lo que ve el navegador. El mensaje es solo el código:
    nunca la clave ni la URL, porque log_errores loguea el de toda excepción que escapa de una ruta."""
    codigo = "waha_no_responde"

    def __init__(self) -> None:
        super().__init__(self.codigo)


class ClaveIncorrecta(VerificacionFallida):
    codigo = "clave_incorrecta"


class WahaNoResponde(VerificacionFallida):
    codigo = "waha_no_responde"


class MotorDistinto(VerificacionFallida):
    codigo = "motor_distinto"


@dataclass(frozen=True)
class Worker:
    id: uuid.UUID
    nombre: str
    base_url: str
    engine: str
    max_sesiones: int
    disco_max_gb: float
    disco_usado_gb: float
    activo: bool
    sesiones: int

    def ocupacion(self) -> float:
        return max(self.sesiones / self.max_sesiones, self.disco_usado_gb / self.disco_max_gb)


def admite(w: Worker) -> bool:
    return (w.activo
            and (w.sesiones + 1) <= w.max_sesiones * UMBRAL_SESIONES
            and w.disco_usado_gb <= w.disco_max_gb * UMBRAL_DISCO)


def elegir(workers: list[Worker]) -> Optional[Worker]:
    candidatos = [w for w in workers if admite(w)]
    return min(candidatos, key=lambda w: (w.ocupacion(), w.nombre)) if candidatos else None


def nombre_clave_admin(worker_id: uuid.UUID) -> str:
    return f"waha_admin:{worker_id}"


def nombre_clave_lectura(link_id: uuid.UUID) -> str:
    return f"waha_lectura:{link_id}"


async def listar_workers(ctx: RadarContexto) -> list[Worker]:
    async with ctx.db.sin_tenant() as con:
        filas = await con.fetch("SELECT * FROM radar_admin_ocupacion_workers()")
    return [Worker(id=f["worker_id"], nombre=f["nombre"], base_url=f["base_url"], engine=f["engine"],
                   max_sesiones=f["max_sesiones"], disco_max_gb=float(f["disco_max_gb"]),
                   disco_usado_gb=float(f["disco_usado_gb"]), activo=f["activo"], sesiones=f["sesiones"])
            for f in filas]


async def leer_worker(ctx: RadarContexto, worker_id: uuid.UUID) -> Worker:
    for w in await listar_workers(ctx):
        if w.id == worker_id:
            return w
    raise LookupError("worker inexistente")


async def elegir_worker(ctx: RadarContexto) -> Worker:
    elegido = elegir(await listar_workers(ctx))
    if elegido is None:
        logger.warning("ALERTA admisión: ningún worker de WAHA con capacidad (80 %% sesiones / 70 %% disco)")
        raise SinCapacidad("sin worker con capacidad")
    return elegido


def cliente_de(ctx: RadarContexto, w: Worker) -> WahaCliente:
    clave = ctx.secretos.get(nombre_clave_admin(w.id))
    if clave is None:
        raise ClaveAdminAusente(w.nombre)
    return WahaCliente(w.base_url, clave.decode(), transport=ctx.waha_transport, timeout=ctx.settings.waha_timeout_s)


async def registrar_worker(ctx: RadarContexto, *, nombre: str, base_url: str, engine: str, max_sesiones: int,
                           disco_max_gb: float, admin_key: str,
                           actor_user_id: Optional[uuid.UUID] = None) -> uuid.UUID:
    if engine not in MOTORES:
        raise ValueError(f"motor desconocido: {engine}")
    if len(admin_key) < 16:
        raise ValueError("la clave admin de WAHA es demasiado corta")
    async with ctx.db.tenant_tx(TENANT_KIS) as con:
        wid = await con.fetchval(
            "INSERT INTO waha_workers (nombre, base_url, engine, max_sesiones, disco_max_gb) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING id",
            nombre, base_url, engine, max_sesiones, Decimal(str(disco_max_gb)))
        await auditoria.registrar(con, tenant_id=TENANT_KIS, actor_user_id=actor_user_id,
                                  actor_rol="admin" if actor_user_id else "sistema", accion="worker_registrado",
                                  tipo_objeto="waha_worker", objeto_id=wid)
    ctx.secretos.set(nombre_clave_admin(wid), admin_key.encode())
    return wid


async def verificar_clave_worker(ctx: RadarContexto, *, base_url: str, engine: str, admin_key: str) -> dict:
    """Prueba la clave contra el propio WAHA antes de guardarla: GET /api/server/version tiene que contestar con esa
    clave y con el motor del worker. Devuelve {version, engine, tier}. Solo lanza VerificacionFallida."""
    if not _CLAVE_ADMIN.fullmatch(admin_key):
        raise ClaveIncorrecta()
    async with WahaCliente(base_url, admin_key, transport=ctx.waha_transport,
                           timeout=ctx.settings.waha_timeout_s) as cli:
        try:
            info = await cli.version_servidor()
        except WahaHttpError as e:
            raise (ClaveIncorrecta() if e.status in (401, 403) else WahaNoResponde()) from None
        except WahaError:
            raise WahaNoResponde() from None
    if info["engine"] != engine:
        raise MotorDistinto()
    return info


async def reemplazar_clave(ctx: RadarContexto, worker_id: uuid.UUID, admin_key: str, actor_user_id: uuid.UUID) -> dict:
    """Cambia la clave admin de un worker que ya existe (la rotó el dueño de WAHA, o nunca se cargó). Se prueba contra
    el base_url y el motor del propio worker; con una clave que WAHA no acepta no cambia nada. Devuelve lo mismo que
    verificar_clave_worker; LookupError si el worker no existe."""
    w = await leer_worker(ctx, worker_id)
    info = await verificar_clave_worker(ctx, base_url=w.base_url, engine=w.engine, admin_key=admin_key)
    async with ctx.db.tenant_tx(TENANT_KIS) as con:
        await auditoria.registrar(con, tenant_id=TENANT_KIS, actor_user_id=actor_user_id, actor_rol="admin",
                                  accion="worker_clave_reemplazada", tipo_objeto="waha_worker", objeto_id=worker_id)
        # Dentro de la transacción: si el volumen no deja escribir, el rollback no deja una auditoría de un cambio
        # que no ocurrió.
        ctx.secretos.set(nombre_clave_admin(worker_id), admin_key.encode())
    return info


def clave_cargada(ctx: RadarContexto, worker_id: uuid.UUID) -> bool:
    """Si el worker tiene clave, nunca cuál: la Consola muestra solo "clave cargada: sí/no"."""
    return ctx.secretos.get(nombre_clave_admin(worker_id)) is not None


async def actualizar_disco(ctx: RadarContexto, worker_id: uuid.UUID, usado_gb: float,
                           actor_user_id: Optional[uuid.UUID] = None) -> None:
    async with ctx.db.tenant_tx(TENANT_KIS) as con:
        estado = await con.execute(
            "UPDATE waha_workers SET disco_usado_gb = $2, updated_at = now() WHERE id = $1",
            worker_id, Decimal(str(usado_gb)))
        if estado == "UPDATE 0":
            raise LookupError("worker inexistente")
        await auditoria.registrar(con, tenant_id=TENANT_KIS, actor_user_id=actor_user_id,
                                  actor_rol="admin" if actor_user_id else "sistema",
                                  accion="worker_disco_actualizado", tipo_objeto="waha_worker", objeto_id=worker_id)
