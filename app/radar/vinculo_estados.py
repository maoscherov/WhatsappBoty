"""
Máquina de estados del vínculo (§3 P3 "Estados", "Estados especiales", §6.3
punto 6). Pura: sin base ni red. La usan el receptor de webhooks, el polling
de respaldo y el chequeo de salud, así las tres fuentes dan el mismo
resultado.

Estados de links.estado: creando -> esperando_qr -> vinculado; caido (estuvo
vivo y se perdió); cerrando -> cerrado (job de fin); abortado (falló la
creación). "AUSENTE" es un pseudo-estado de WAHA: la sesión ya no existe (404).
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal, Optional

ACTIVOS = ("creando", "esperando_qr", "vinculado")
VIVOS = ACTIVOS + ("caido",)
IGNORADOS = ("cerrando", "cerrado", "abortado")
MAX_REINICIOS_QR = 3
CAIDA_MAXIMA = timedelta(hours=72)
QR_ABANDONADO = timedelta(minutes=30)
SIN_EVENTOS_POLLING = timedelta(seconds=20)
UMBRAL_WORKER_LLENO = 0.8
PATRON_STATUS = re.compile(r"^[A-Z_]{1,40}$")


@dataclass(frozen=True)
class Transicion:
    estado: str
    efectos: frozenset = field(default_factory=frozenset)


def _t(estado: str, *efectos: str) -> Transicion:
    return Transicion(estado, frozenset(efectos))


def transicion(estado: str, waha_status: str) -> Transicion:
    if estado in IGNORADOS:
        # §6.3 punto 6.1: un vínculo que cierra no dispara banner, email ni "Reconectar".
        return _t(estado, "ignorar")
    if estado not in VIVOS:
        raise ValueError(f"estado de vínculo desconocido: {estado}")
    en_qr = estado in ("creando", "esperando_qr")
    if waha_status == "WORKING":
        return _t("vinculado") if estado == "vinculado" else _t("vinculado", "conectado", "reemplazar_anteriores")
    if waha_status == "STARTING":
        return _t("esperando_qr" if estado == "creando" else estado)
    if waha_status == "SCAN_QR_CODE":
        return _t("esperando_qr") if en_qr else _t("caido", "caida")
    if waha_status in ("FAILED", "STOPPED"):
        return _t("esperando_qr", "qr_vencido") if en_qr else _t("caido", "caida")
    if waha_status == "AUSENTE":
        return _t("caido", "caida", "abandonar") if en_qr else _t("caido", "caida")
    if waha_status == "PASSKEY_REQUIRED":
        return _t(estado, "passkey")
    return _t(estado, "desconocido")


def sufijo_de(me_id: Any) -> Optional[str]:
    """Últimos 4 dígitos del número de la línea ("…1234"). El resto no se guarda ni se loguea."""
    if not isinstance(me_id, str):
        return None
    usuario, _, dominio = me_id.partition("@")
    usuario = usuario.split(":")[0]
    if dominio not in ("c.us", "s.whatsapp.net") or not usuario.isdigit() or len(usuario) < 8:
        return None
    return usuario[-4:]


def restriccion_activa(hasta: Optional[datetime], sin_fecha: Optional[bool], ahora: datetime) -> bool:
    return bool(sin_fecha) or (hasta is not None and hasta > ahora)


def semaforo(fila: dict, ahora: datetime) -> Literal["verde", "amarillo", "rojo", "gris"]:
    """C1 (§3.1). Tráfico, silencio y cobertura se suman en el tramo 3."""
    estado = fila.get("link_estado")
    if estado is None or estado in ("cerrado", "abortado"):
        return "gris"
    if restriccion_activa(fila.get("restriccion_hasta"), fila.get("restriccion_sin_fecha"), ahora):
        return "rojo"
    if estado == "caido":
        return "rojo"
    maximo = fila.get("worker_max_sesiones")
    if maximo and (fila.get("worker_sesiones") or 0) >= maximo * UMBRAL_WORKER_LLENO:
        return "rojo"
    if estado == "vinculado":
        if fila.get("waha_status") == "WORKING":
            return "verde"
        return "rojo" if fila.get("waha_status") == "FAILED" else "amarillo"
    return "amarillo"
