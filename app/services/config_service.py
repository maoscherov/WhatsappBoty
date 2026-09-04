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
        "Perdón por las vueltas. Te paso con alguien del equipo así lo vemos bien."
    ),
    "mutual_corte_message": (
        "Mejor te paso con alguien del equipo, que sigue con vos desde acá."
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
        "Si querés avanzar lo ve un oficial de créditos con vos."
    ),
    "mutual_derivar_oficial_message": (
        "Dale, te paso con un oficial de créditos."
    ),
    # Plazo fijo (AMT): interés simple por días exactos. El sellado queda
    # pendiente de dato, por eso el mensaje aclara que no está incluido.
    "mutual_amt_tna_online": "26",
    "mutual_amt_tna_presencial": "23.5",
    "mutual_amt_monto_minimo": "1000",
    "mutual_amt_dias_min": "29",
    "mutual_amt_dias_max": "60",
    "mutual_amt_ofrecer_asesor": (
        "Si lo querés constituir, lo arma alguien del equipo."
    ),
    # Si preguntan derecho si es un bot: se admite y se ofrece pasar con una
    # persona. Nunca decir que es humano.
    "mutual_bot_identidad_message": (
        "Sí, soy el asistente de Mutual AMI. Si preferís hablar con alguien del "
        "equipo te paso, decime nomás."
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
    # Cierre para conversaciones con LINK DE PAGO enviado: la sesión se
    # cierra a las 24hs (vigencia del link) pero SIN avisar — el aviso de
    # vencimiento caía a cualquier hora y molestaba (pedido de Mariano 20/8).
    # Cargar un texto acá reactiva el aviso.
    "inactivity_minutes_pago": "1440",
    "inactivity_close_message_pago": "",
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
    # Tras esta pausa sin mensajes, la próxima charla arranca de cero (historial
    # y pedido pendiente limpios). El link de pago ya enviado sigue válido.
    # 0 = nunca reiniciar. Evita que la charla de ayer contamine la de hoy
    # mientras la sesión sigue viva por la ventana de 24hs del link.
    "contexto_reinicio_minutos": "120",
    # Costo del envío a domicilio en pesos. "0" = gratis (comportamiento
    # histórico). Con costo, se muestra al preguntar la entrega y se suma al
    # total del link (requerimiento de la farmacia, 4/9: $2000).
    "envio_costo": "0",
    # Cabecera del mensaje de cotización de receta que envía el operador
    # desde el backoffice ({producto} se reemplaza). El desglose de precios y
    # descuentos lo arma el código: los números nunca se redactan a mano.
    "receta_cotizacion_intro": "¡Buenas noticias! Tenemos stock de {producto} 👍",
    # Cierre de la cotización SIN link (modo cotizar): invita a confirmar; el
    # bot manda el link cuando el cliente dice que sí.
    "receta_cotizacion_cierre": (
        "¿Querés que avancemos? Decime *sí* y te mando el link de pago 🙂"
    ),
    # Respuesta del bot al recibir una foto de receta (configurable — pedido
    # 4/9: promete la validación en ~10 min, coherente con el SLA de 15).
    "receta_recibida_message": (
        "Recibimos tu receta 🙌 Validamos la información y volvemos con vos "
        "dentro de los próximos 10 minutos."
    ),
    # OCR de recetas: al derivar una receta por foto, leerla (visión) y dejar
    # en el backoffice paciente, medicamento, candidato del catálogo y cruce
    # con el padrón. Apagado hasta que la farmacia lo pruebe.
    "receta_ocr_enabled": "false",
    "socio_discount_pct": "0",
    # true  = el socio ve el precio ya bonificado desde que se le ofrece el
    #         producto (y el link cobra ese mismo importe).
    # false = vuelve al comportamiento viejo: precio de lista en la charla y
    #         el descuento recién en el link de pago.
    "socio_discount_en_catalogo": "true",
    "socio_discount_message": (
        "🎉 Por ser socio de la Mutual te aplicamos un {pct}% de descuento "
        "(precio de lista: ${antes})."
    ),
    # Respuestas FIJAS cuando preguntan por descuentos (el modelo nunca redacta
    # sobre descuentos: inventó uno con precio inexistente — caso 29, 19/8).
    # info: descuento activo ({pct} se reemplaza). off: descuento apagado.
    "socio_discount_info_message": (
        "¡Sí! Los socios de la Mutual tienen {pct}% de descuento en productos "
        "sin receta — se aplica solo en el link de pago 🙂"
    ),
    "socio_discount_off_message": (
        "Por ahora te puedo ofrecer el precio de lista 🙂 El descuento para "
        "socios lo estamos habilitando — cuando esté activo se aplica "
        "automáticamente."
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
    """
    Config editable desde el backoffice, con tres niveles:

      Postgres  → fuente de verdad (durable)
      Redis     → cache del camino caliente (get_all corre en cada mensaje)
      memoria   → último recurso si los dos fallan

    Antes Redis era la ÚNICA copia: al reiniciarse, los valores se perdían en
    silencio y todo volvía a los defaults del código. Pasó con el descuento de
    socios, que quedaba en 0 sin que nadie se enterara.
    """

    def __init__(self, redis_url: str):
        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._ok: Optional[bool] = None
        self._cache: dict[str, str] = {}   # fallback in-memory
        self._db = None                    # se resuelve perezosamente

    def _get_db(self):
        """
        La db se resuelve tarde y no en el constructor: el pool se crea en el
        lifespan de main.py, después de instanciarse este servicio.
        """
        if self._db is None:
            try:
                from app.config import get_settings
                from app.services.db import get_db
                self._db = get_db(get_settings().database_url)
            except Exception as e:
                logger.warning(f"config: sin acceso a Postgres ({e})")
        return self._db

    async def _usable(self) -> bool:
        if self._ok is None:
            try:
                self._ok = await self._redis.ping()
            except Exception:
                self._ok = False
        return bool(self._ok)

    async def _leer_postgres(self) -> dict[str, str]:
        db = self._get_db()
        if not db or not db.available():
            return {}
        filas = await db.fetch("SELECT clave, valor FROM config")
        return {f["clave"]: f["valor"] for f in filas}

    async def _guardar_postgres(self, updates: dict[str, str]) -> bool:
        db = self._get_db()
        if not db or not db.available():
            return False
        for clave, valor in updates.items():
            await db.execute(
                "INSERT INTO config (clave, valor, updated_at) VALUES ($1, $2, now()) "
                "ON CONFLICT (clave) DO UPDATE SET valor = $2, updated_at = now()",
                clave, str(valor),
            )
        return True

    async def get_all(self) -> dict[str, str]:
        # Camino caliente: Redis. Si trae datos, no se consulta Postgres.
        if await self._usable():
            try:
                data = await self._redis.hgetall(CONFIG_KEY)
                if data:
                    return {**DEFAULTS, **data}
            except Exception:
                pass

        # Redis vacío o caído: la verdad está en Postgres. Si había algo, se
        # repuebla el cache para que la próxima lectura vuelva al camino rápido.
        durable = await self._leer_postgres()
        if durable:
            try:
                if await self._usable():
                    await self._redis.hset(CONFIG_KEY, mapping=durable)
                    logger.info(f"config: cache de Redis repoblado desde Postgres "
                                f"({len(durable)} claves)")
            except Exception:
                pass
            return {**DEFAULTS, **durable}

        return {**DEFAULTS, **self._cache}

    async def sincronizar_durable(self) -> int:
        """
        Copia a Postgres lo que hoy está SOLO en Redis. Se corre al arrancar.

        Al estrenar la persistencia, todo lo ya configurado desde el backoffice
        vive únicamente en el cache; sin esta copia se perdería igual en el
        primer reinicio de Redis. No pisa lo que Postgres ya tenga: la fuente
        de verdad manda.
        """
        db = self._get_db()
        if not db or not db.available():
            return 0
        if await self._leer_postgres():
            return 0            # Postgres ya es la verdad, no se toca
        if not await self._usable():
            return 0
        try:
            data = await self._redis.hgetall(CONFIG_KEY)
        except Exception:
            return 0
        if not data:
            return 0
        await self._guardar_postgres(data)
        logger.info(f"config: {len(data)} claves copiadas de Redis a Postgres "
                    f"(primera persistencia)")
        return len(data)

    async def get(self, key: str) -> str:
        config = await self.get_all()
        return config.get(key, DEFAULTS.get(key, ""))

    async def set(self, key: str, value: str):
        await self.set_many({key: value})

    async def set_many(self, updates: dict[str, str]):
        self._cache.update(updates)
        # Primero lo durable: si Postgres falla, se avisa fuerte — sin eso el
        # cambio se pierde en el próximo reinicio de Redis y nadie se entera.
        persistido = await self._guardar_postgres(updates)
        if not persistido:
            logger.error(
                "config: NO se pudo persistir en Postgres %s — el cambio vive "
                "solo en Redis y se perderá si Redis se reinicia",
                list(updates),
            )
        if await self._usable():
            try:
                await self._redis.hset(CONFIG_KEY, mapping=updates)
            except Exception as e:
                logger.warning(f"config: no se pudo actualizar el cache de Redis: {e}")
        return persistido

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
