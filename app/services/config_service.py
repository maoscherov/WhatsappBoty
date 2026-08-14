"""
Configuración dinámica del bot guardada en Redis.
Permite cambiar comportamientos sin redeploy.

Clave: bot:config (hash Redis)

Campos actuales:
  send_images   → "always" | "on_request"   (default: "always")
"""

import json
import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

CONFIG_KEY   = "bot:config"
HOURS_KEY    = "bot:hours"
TZ_ARG       = ZoneInfo("America/Argentina/Buenos_Aires")
DAY_MAP      = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}

DEFAULTS: dict[str, str] = {
    "send_images":    "always",
    "pickup_minutes": "30",    # tiempo estimado de preparación/retiro
    "receta_mode":    "conservador",  # "conservador" (ambiguo deriva) | "estricto" (solo "si")
    "envio_enabled":  "true",  # ofrecer envío a domicilio además de retiro
    # ── Vertical "mutual" (CERCA Sucursales) ──────────────────────────────
    # Escalada por señal conversacional (spec 4.2). 0 desactiva cada regla.
    "mutual_negativos_para_escalar": "2",     # mensajes negativos seguidos
    "mutual_max_turnos": "30",                # corte por conversación larga
    "mutual_max_minutos": "90",
    "mutual_turno_ofrecer_asesor": "10",      # desde qué turno se recuerda el asesor
    # Corte de relevancia de la base de conocimiento. Bajo a propósito: las
    # preguntas cortas puntúan poco contra documentos largos y se perdía el dato.
    "mutual_kb_min_score": "0.05",
    "mutual_escalada_message": (
        "Perdón por las vueltas 🙏 Te paso con alguien del equipo para que te "
        "ayude personalmente."
    ),
    "mutual_corte_message": (
        "Para no hacerte perder más tiempo, te paso con alguien del equipo que "
        "sigue con vos desde acá 🙌"
    ),
    # Simulador de préstamos. Las tasas cambian seguido: se editan acá, sin deploy.
    "mutual_simulador_activo": "true",
    "mutual_tna_preferencial": "55",
    "mutual_tna_general": "75",
    # Ajustes para acercar la cuota al importe real. En 0 hasta que la mutual
    # confirme qué incluye: informar de menos genera un problema con el cliente.
    "mutual_simulador_iva": "0",      # % de IVA sobre los intereses
    "mutual_simulador_gastos": "0",   # % de gastos sobre el capital
    "mutual_simulador_aclaracion": (
        "Es un cálculo estimativo: el importe final surge de la evaluación del "
        "equipo y puede incluir gastos según el caso."
    ),
    # La simulación es capital + interés; para avanzar se pasa con un oficial.
    "mutual_simulador_ofrecer_oficial": (
        "Si querés avanzar, te paso con un oficial de créditos que lo ve con vos 🙂"
    ),
    "mutual_derivar_oficial_message": (
        "Dale, te paso con un oficial de créditos 🙌 En un momento te contactan."
    ),
    # Pasarela con la que se cobra: "payway" | "mercadopago". Vacío = usa la
    # variable de entorno del deploy. Se cambia desde el backoffice sin deploy.
    "payment_provider": "",
    # Qué hacer cuando el cliente pide un producto que no tenemos (no está en
    # el catálogo o está sin stock):
    #   "preguntar" → el bot ofrece consultarlo y deriva si el cliente acepta.
    #   "derivar"   → deriva directo a una persona.
    #   "nunca"     → sólo avisa que no está (comportamiento anterior).
    "sin_stock_mode": "preguntar",
    "sin_stock_ofrecer_message": (
        "No me figura disponible en este momento 🙏 ¿Querés que lo consulte "
        "con el equipo para conseguírtelo o encargarlo?"
    ),
    "sin_stock_derivar_message": (
        "Te paso con alguien del equipo para ver si podemos conseguirlo o "
        "encargarlo 🙌 ¡En un momento te contactamos!"
    ),
    # Qué hacer si el cliente pide transferencia/efectivo/CBU/alias:
    #   "derivar"      → lo atiende una persona (mensaje pago_manual_message).
    #   "solo_tarjeta" → el bot responde que solo se acepta tarjeta (mensaje
    #                    pago_solo_tarjeta_message) y sigue la venta normal.
    "pago_manual_mode": "derivar",
    "pago_manual_message": (
        "Dale! Para pagar por ese medio te paso con alguien del equipo, "
        "que lo coordina con vos 🙌. ¡En un momento te contactamos!"
    ),
    "pago_solo_tarjeta_message": (
        "Por este canal aceptamos pago con tarjeta (débito o crédito) 💳. "
        "Si querés, seguimos con tu pedido y te mando el link de pago seguro."
    ),
    # Cierre por inactividad (minuta 2026-07-31). El texto es provisorio hasta
    # que la farmacia mande el definitivo — se cambia desde el backoffice.
    "inactivity_minutes": "15",
    "inactivity_close_message": (
        "Como no tuvimos respuesta, damos por cerrada esta conversación 🙏 "
        "Cuando quieras retomarla, escribinos de nuevo y te ayudamos. ¡Gracias!"
    ),
    # Cierre distinto para conversaciones con LINK DE PAGO enviado: el link
    # vale 24hs, así que el cierre acompaña esa vigencia y el mensaje lo aclara.
    "inactivity_minutes_pago": "1440",
    "inactivity_close_message_pago": (
        "Cerramos esta conversación por inactividad 🙏 Tu link de pago sigue "
        "vigente — podés pagar cuando quieras dentro de las 24hs. "
        "¡Cualquier duda escribinos!"
    ),
    # Si nadie toma una conversación derivada en N minutos, vuelve al bot.
    # 0 = nunca (queda esperando a una persona, comportamiento por defecto).
    "auto_liberar_minutos": "0",
    "auto_liberar_message": (
        "Sigo yo mientras tanto 🙂 Contame en qué te puedo ayudar y, si hace "
        "falta, te paso con alguien del equipo."
    ),
    # Aviso al cliente si la atención humana demora tras una derivación.
    # 0 = desactivado. Texto provisorio — editable desde el backoffice.
    "handoff_reminder_minutes": "15",
    "handoff_reminder_message": (
        "Seguimos con tu consulta 🙌 El equipo está con mucha demanda en este "
        "momento, pero en breve te respondemos. ¡Gracias por la paciencia!"
    ),
    # Descuento automático de socio (compras sin receta). 0 = desactivado —
    # activar cuando la farmacia valide el cruce del padrón. {pct} y {antes}
    # se reemplazan por el porcentaje y el precio sin descuento.
    "socio_discount_pct": "0",
    "socio_discount_message": (
        "🎉 Por ser socio de la Mutual te aplicamos un {pct}% de descuento "
        "(precio de lista: ${antes})."
    ),
    # Cada cuántos segundos el backoffice pollea /bo/derivadas para la alerta
    # sonora. Lo lee el frontend (Lovable) desde /bo/config.
    "derivadas_poll_seconds": "15",
}

DEFAULT_HOURS = {
    "enabled": False,
    "closed_message": "Estamos fuera del horario de atención. Te respondemos en cuanto abramos 🙏",
    "schedule": {
        "mon": {"open": "09:00", "close": "18:00", "active": True},
        "tue": {"open": "09:00", "close": "18:00", "active": True},
        "wed": {"open": "09:00", "close": "18:00", "active": True},
        "thu": {"open": "09:00", "close": "18:00", "active": True},
        "fri": {"open": "09:00", "close": "18:00", "active": True},
        "sat": {"open": "09:00", "close": "13:00", "active": True},
        "sun": {"open": "09:00", "close": "13:00", "active": False},
    },
}


class ConfigService:
    def __init__(self, redis_url: str):
        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._ok: Optional[bool] = None
        self._cache: dict[str, str] = {}   # fallback in-memory

    async def _usable(self) -> bool:
        if self._ok is None:
            try:
                self._ok = await self._redis.ping()
            except Exception:
                self._ok = False
        return bool(self._ok)

    async def get_all(self) -> dict[str, str]:
        if await self._usable():
            try:
                data = await self._redis.hgetall(CONFIG_KEY)
                return {**DEFAULTS, **data}
            except Exception:
                pass
        return {**DEFAULTS, **self._cache}

    async def get(self, key: str) -> str:
        config = await self.get_all()
        return config.get(key, DEFAULTS.get(key, ""))

    async def set(self, key: str, value: str):
        self._cache[key] = value
        if await self._usable():
            try:
                await self._redis.hset(CONFIG_KEY, key, value)
                return
            except Exception:
                pass

    async def set_many(self, updates: dict[str, str]):
        self._cache.update(updates)
        if await self._usable():
            try:
                await self._redis.hset(CONFIG_KEY, mapping=updates)
            except Exception:
                pass

    # ── Horarios ──────────────────────────────────────────────────────────────

    async def get_hours(self) -> dict:
        if await self._usable():
            try:
                raw = await self._redis.get(HOURS_KEY)
                if raw:
                    return json.loads(raw)
            except Exception:
                pass
        return dict(DEFAULT_HOURS)

    async def set_hours(self, hours: dict):
        if await self._usable():
            try:
                await self._redis.set(HOURS_KEY, json.dumps(hours))
                return
            except Exception:
                pass

    def is_open_now(self, hours: dict) -> bool:
        """True si el horario está activo y el momento actual cae dentro del rango."""
        if not hours.get("enabled"):
            return True  # sin control de horario → siempre abierto
        now = datetime.now(TZ_ARG)
        day = DAY_MAP[now.weekday()]
        cfg = hours.get("schedule", {}).get(day, {})
        if not cfg.get("active"):
            return False
        open_t  = cfg.get("open",  "00:00")
        close_t = cfg.get("close", "23:59")
        current = now.strftime("%H:%M")
        return open_t <= current <= close_t

    def get_pickup_text(self, hours: dict, pickup_minutes: int = 30) -> str:
        """
        Devuelve un texto de horario de retiro para incluir en mensajes al cliente.
        Funciona siempre, independientemente de si enabled=True/False.
        Ejemplos:
          "Podés retirarlo hoy de 9:00 a 18:00 hs 🕐"
          "Retiros mañana de 9:00 a 13:00 hs (hoy estamos cerrados) 🕐"
          "Consultá nuestro horario de atención 🕐"
        """
        schedule = hours.get("schedule", {})
        mins_txt = f"⏱ Tiempo estimado: *{pickup_minutes} min*" if pickup_minutes else ""

        DAY_ES = {
            "mon": "lunes", "tue": "martes", "wed": "miércoles",
            "thu": "jueves", "fri": "viernes", "sat": "sábado", "sun": "domingo",
        }

        now = datetime.now(TZ_ARG)

        # Buscar el próximo día activo (hoy y los 6 siguientes)
        for offset in range(7):
            idx = (now.weekday() + offset) % 7
            day = DAY_MAP[idx]
            cfg = schedule.get(day, {})
            if not cfg.get("active"):
                continue

            open_t  = cfg.get("open", "")
            close_t = cfg.get("close", "")
            if not open_t or not close_t:
                continue

            # Formatear horas sin segundos
            def fmt(t: str) -> str:
                return t[:5].lstrip("0") or "0:00"

            prefix = f"{mins_txt} · " if mins_txt else ""

            if offset == 0:
                if now.strftime("%H:%M") >= close_t:
                    continue
                return f"{prefix}Podés retirarlo hoy de {fmt(open_t)} a {fmt(close_t)} hs 🕐"
            elif offset == 1:
                return f"{prefix}Retiros mañana de {fmt(open_t)} a {fmt(close_t)} hs 🕐"
            else:
                return f"{prefix}Próximos retiros el {DAY_ES[day]} de {fmt(open_t)} a {fmt(close_t)} hs 🕐"

        # Sin schedule: mostrar solo el tiempo estimado si existe
        return mins_txt


_instance: Optional[ConfigService] = None


def get_config_service(redis_url: str) -> ConfigService:
    global _instance
    if _instance is None:
        _instance = ConfigService(redis_url)
    return _instance
