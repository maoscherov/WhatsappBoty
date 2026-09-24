"""
Gestor del ciclo de vida de una sesión WAHA (§3 P3, §6.3 punto 6).

- crear_sesion_verificada: POST, relee y compara; si no coincide, borra la
  sesión y lanza ConfigNoCoincide (nunca se muestra un QR de una sesión mal
  configurada).
- terminar_sesion: un único DELETE /api/sessions/{name}. Nunca logout: sobre
  una sesión en marcha, WAHA hace logout y la vuelve a arrancar con QR nuevo y
  los mismos webhooks. Si la sesión estaba STOPPED/FAILED y se permite, intenta
  start y espera WORKING antes del DELETE, porque el desvínculo del lado de
  WhatsApp solo ocurre con la sesión WORKING. Después borra las claves de ESA
  sesión (WAHA no las borra solo) y verifica 404. Un paso fallido no impide los
  siguientes.
"""

import asyncio
import time
from typing import Any, Optional

from app.radar.waha.cliente import WahaCliente, WahaError, verificar_nombre_sesion
from app.radar.waha.sesion import ACCIONES_CLAVE_LECTURA, verificar_config


class ConfigNoCoincide(RuntimeError):
    def __init__(self, problemas: list[str]) -> None:
        super().__init__("la sesión no quedó como se pidió: " + ", ".join(problemas))
        self.problemas = problemas


async def crear_sesion_verificada(cli: WahaCliente, cuerpo: dict[str, Any]) -> dict[str, Any]:
    nombre = verificar_nombre_sesion(cuerpo["name"])
    await cli.crear_sesion(cuerpo)
    try:
        sesion = await cli.leer_sesion(nombre) or {}
    except WahaError:
        try:
            await cli.borrar_sesion(nombre)
        except WahaError:
            pass
        raise
    problemas = verificar_config(sesion, cuerpo)
    if problemas:
        await cli.borrar_sesion(nombre)
        raise ConfigNoCoincide(problemas)
    return sesion


async def crear_clave_lectura(cli: WahaCliente, nombre: str) -> tuple[str, str]:
    return await cli.crear_clave(nombre, actions=ACCIONES_CLAVE_LECTURA)


async def esperar_estado(cli: WahaCliente, nombre: str, *, objetivos: tuple[str, ...], timeout_s: float,
                         intervalo_s: float) -> Optional[str]:
    inicio = time.monotonic()
    while True:
        sesion = await cli.leer_sesion(nombre)
        status = sesion["status"] if sesion else None
        if status in objetivos or time.monotonic() - inicio >= timeout_s:
            return status
        await asyncio.sleep(intervalo_s)


async def terminar_sesion(cli: WahaCliente, nombre: str, *, intentar_start: bool, espera_start_s: float = 180,
                          intervalo_s: float = 5) -> dict[str, Any]:
    verificar_nombre_sesion(nombre)
    status_antes: Optional[str] = None
    error_lectura: Optional[str] = None
    try:
        antes = await cli.leer_sesion(nombre)
        status_antes = antes["status"] if antes else None
    except WahaError as e:
        error_lectura = type(e).__name__
    status_al_borrar = status_antes
    start_intentado = False
    if intentar_start and status_antes in ("STOPPED", "FAILED"):
        start_intentado = True
        try:
            await cli.iniciar_sesion(nombre)
            status_al_borrar = await esperar_estado(cli, nombre, objetivos=("WORKING",), timeout_s=espera_start_s,
                                                    intervalo_s=intervalo_s)
        except WahaError:
            status_al_borrar = status_antes

    delete_status: Optional[int] = None
    error_borrado: Optional[str] = None
    try:
        delete_status = await cli.borrar_sesion(nombre)
    except WahaError as e:
        error_borrado = type(e).__name__

    borradas = 0
    error_claves: Optional[str] = None
    try:
        for clave in [k for k in await cli.listar_claves() if k["session"] == nombre]:
            await cli.borrar_clave(clave["id"])
            borradas += 1
    except WahaError as e:
        error_claves = type(e).__name__

    try:
        borrada = await cli.leer_sesion(nombre) is None
    except WahaError:
        borrada = False
    restantes: Optional[int] = None
    if error_claves is None:
        try:
            restantes = len([k for k in await cli.listar_claves() if k["session"] == nombre])
        except WahaError as e:
            error_claves = type(e).__name__
    return {
        "status_antes": status_antes,
        "start_intentado": start_intentado,
        "desvinculo_confirmado": status_al_borrar == "WORKING",
        "delete_status": delete_status,
        "sesion_borrada": borrada,
        "claves_borradas": borradas,
        "claves_restantes": restantes,
        "error_claves": error_claves,
        "error_lectura": error_lectura,
        "error_borrado": error_borrado,
        "ok": borrada and restantes == 0 and error_lectura is None and error_borrado is None,
    }
