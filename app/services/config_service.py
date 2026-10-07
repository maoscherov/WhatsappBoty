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

from app.services.perfil import get_perfil

logger = logging.getLogger(__name__)

CONFIG_KEY   = "bot:config"
HOURS_KEY    = "bot:hours"
TZ_ARG       = ZoneInfo("America/Argentina/Buenos_Aires")
DAY_MAP      = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}
DIAS_ES = {"mon": "lunes", "tue": "martes", "wed": "miércoles", "thu": "jueves",
           "fri": "viernes", "sat": "sábado", "sun": "domingo"}

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
    # Pago con cuenta corriente (minuta 79): habilitado por default para todo
    # socio del padrón, salvo la lista de excepciones de la farmacia
    # (/bo/cc/excepciones). Tope 0 = sin tope.
    "cc_enabled": "true",
    "cc_tope_monto": "0",
    # Pago en EFECTIVO (19/9): el pedido entra al backoffice sin link y el
    # cobro queda pendiente ("Marcar cobrado"). Apagado por default: hasta
    # encenderlo, "efectivo" sigue el flujo de pago manual (derivar/solo tarjeta).
    "efectivo_enabled": "false",
    "efectivo_solo_socios": "false",     # "true" = solo socios del padrón
    # Fuera de horario (27/9): el bot vende igual lo que no lleva receta; lo
    # que necesita una persona queda derivado con aviso de cuándo abrimos.
    # "false" = comportamiento anterior (solo el mensaje de cerrado).
    "vender_fuera_horario": "true",
    "efectivo_con_envio": "false",       # "true" = también con envío (paga al recibir)
    "efectivo_tope_monto": "0",          # tope por pedido, 0 = sin tope
    "efectivo_horas_reserva": "24",      # plazo que se informa para retirarlo (0 = no se informa)
    # Placeholders: {producto} {total} {codigo} {plazo} / {direccion} {envio}
    "efectivo_retiro_message": (
        "✅ *¡Listo! Tomamos tu pedido* 🙌\n\n"
        "*{producto}* — ${total}\n"
        "💵 Lo pagás en efectivo al retirar.{plazo}\n"
        "🔑 *Tu código de retiro es: {codigo}*\n\n¡Muchas gracias! 💊"
    ),
    "efectivo_envio_message": (
        "✅ *¡Listo! Tomamos tu pedido* 🙌\n\n"
        "*{producto}* — ${total}{envio}\n"
        "🚚 Te lo enviamos a *{direccion}* y lo pagás en efectivo al recibirlo.\n"
        "📋 Código de pedido: *{codigo}*\n\n¡Muchas gracias! 💊"
    ),
    "efectivo_solo_retiro_message": (
        "El pago en efectivo es solo retirando en la sucursal. ¿Lo pasás a "
        "retirar, o preferís *envío* pagando con tarjeta?"
    ),
    # Interruptor global del bot: "false" = no responde nada automático; los
    # mensajes entran a la cola de derivadas (motivo bot_apagado) para que los
    # conteste una persona. Se cambia con POST /bo/bot (registra quién y cuándo).
    "bot_enabled": "true",
    "bot_cambiado_por": "",
    "bot_cambiado_at": "",
    # Fuente del catálogo (11/9): "erp" = Postgres sincronizado por el agente
    # de la sucursal (gana si hay datos); "csv" = forzar el CSV viejo.
    "catalogo_fuente": "erp",
    # Umbral del fallback semántico de productos (0-1). Por debajo, el
    # vecino no se ofrece: mejor "no lo encontramos" que Dove para dipirona.
    "rag_sku_min_score": "0.30",
    # Aviso de "pedido preparado" según tipo de entrega (minuta 79, acción 1).
    # Placeholders: {producto} {total} {codigo} {direccion} {horario}.
    "pedido_listo_retiro_message": (
        "🎉 *¡Tu pedido está listo para retirar!*\n\n"
        "*{producto}* — ${total}\n"
        "🔑 *Código de retiro: {codigo}*{horario}\n\n"
        "Presentá este código y te lo entregamos. ¡Te esperamos! 💊"
    ),
    "pedido_listo_envio_message": (
        "🎉 *¡Tu pedido está listo!*\n\n"
        "*{producto}* — ${total}\n"
        "🚚 Sale para *{direccion}*. Te avisamos cuando esté en camino. 💊"
    ),
    # Imagen de un comprobante de pago (transferencia/billetera): se acusa
    # recibo y se deriva a una persona que lo verifique. {nombre} = socio.
    "comprobante_recibido_message": (
        "¡Listo {nombre}! Recibimos tu comprobante 🙌 Lo verificamos y "
        "te confirmamos en un rato."
    ),
    # Imagen que el clasificador no reconoce: derivar en vez de trabarse
    # (minuta 79, acción 8).
    "imagen_no_reconocida_message": (
        "¡Hola {nombre}! Recibí tu imagen 🙌 Te paso con alguien del equipo "
        "que la mira y te ayuda."
    ),
    # Venta frenada por el chequeo de stock EN VIVO contra el ERP (justo antes
    # de generar el link de pago). {producto} se reemplaza por el nombre.
    "live_sin_stock_message": (
        "Justo me fijé y no nos queda stock de {producto}. "
        "¿Querés que lo consultemos con el equipo?"
    ),
    # Feedback piloto 16/9 (fila 59): convenios con obras sociales. La lista la
    # carga la farmacia (coma o renglón por obra social); la respuesta es fija,
    # nunca la redacta el modelo (dijo "sí" a OSDE, "no" a AMUR, al revés).
    "obras_sociales": "",
    "obras_sociales_si_message": "Sí, trabajamos con {obra_social} 🙂 ¿Qué necesitás?",
    # No listada ≠ sin convenio: la lista puede estar incompleta (le dijo "no
    # tenemos convenio con PAMI" a una clienta de PAMI, 28/9). Nunca se niega.
    "obras_sociales_no_message": (
        "{obra_social} no la tengo en mi lista, lo confirma el equipo. ¿Querés que "
        "te pase con alguien para que lo vea con vos?"
    ),
    "obras_sociales_lista_message": "Trabajamos con: {lista}. ¿Con cuál sería?",
    "obras_sociales_sin_lista_message": (
        "Eso lo confirma el equipo. ¿Querés que te pase con alguien para que lo vea con vos?"
    ),
    # Fila 61: bonos de laboratorio (Cassará, Cepage...). Lista de
    # laboratorios cuyos bonos se aceptan; la foto de un bono no se cotiza,
    # se deriva. {laboratorio} {nombre}.
    "bonos_laboratorios": "",
    "bono_recibido_message": (
        "¡Hola {nombre}! Sí, trabajamos los bonos de {laboratorio} 🙌 Te paso con "
        "alguien del equipo que lo gestiona con vos."
    ),
    "bono_no_reconocido_message": (
        "¡Hola {nombre}! Recibí tu bono 🙌 Te paso con alguien del equipo para "
        "confirmar si lo trabajamos."
    ),
    "bono_consulta_si_message": (
        "Sí, trabajamos los bonos de {laboratorio} 🙂 Mandame la foto del bono y te "
        "paso con alguien del equipo que lo gestiona."
    ),
    "bono_consulta_no_message": (
        "Eso lo confirma el equipo: te paso con alguien para que lo vea con vos 🙂"
    ),
    # Fila 48: pedido por síntoma ("algo para la gripe") → tras ofrecer venta
    # libre, dejar a mano al farmacéutico. Un espacio lo apaga; vacío vuelve
    # al texto del perfil de rubro (en la farmacia, este mismo).
    "sintoma_farmaceutico_message": (
        "Si preferís, decime \"farmacéutico\" y te paso con el nuestro para que te oriente."
    ),
    # Lo que el bot no entiende se deriva ("derivar") en vez de improvisar
    # ("responder" = comportamiento anterior).
    "desconocido_mode": "derivar",
    "no_entendi_derivar_message": (
        "No estoy seguro de haberte entendido 🙏 Te paso con alguien del equipo "
        "que sigue con vos desde acá."
    ),
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
    # "mercado pago" en el mensaje cuenta como pago manual (deriva o "solo
    # tarjeta"). "false" = no: para un comercio que cobra con MP (spec 4.4).
    "pago_mp_manual": "true",
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
    # Sucursal de retiro (spec 5): nombre corto que se muestra en "*retiro en
    # {sucursal}*". Vacío = "*retiro en sucursal*" como siempre. La respuesta a
    # "¿dónde queda la sucursal?" solo sale con la sucursal cargada; ningún
    # default trae una dirección (el bot nunca la inventa).
    "retiro_sucursal": "",
    "retiro_info_message": "Lo retirás en *{sucursal}* 🏪",
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
    # {nombre} se reemplaza por el nombre del socio si el teléfono está en el
    # padrón; si no, el saludo que lo contiene se elimina completo.
    "receta_recibida_message": (
        "¡Hola {nombre}! Recibimos tu receta 🙌 Validamos la información y "
        "volvemos con vos dentro de los próximos 10 minutos."
    ),
    # OCR de recetas: al derivar una receta por foto, leerla (visión) y dejar
    # en el backoffice paciente, medicamento, candidato del catálogo y cruce
    # con el padrón. Apagado hasta que la farmacia lo pruebe.
    "receta_ocr_enabled": "false",
    # Precio mínimo para que el bot cotice y cobre (auditoría 2/10): el ERP
    # tiene precios viejos absurdos (shampoo Dove $56,90, algodón $84). Debajo
    # de este valor el precio lo confirma el equipo. 0 = sin control.
    "precio_minimo_venta": "300",
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
    # Descuento de EMPLEADO (24/9): NO acumulable con el de socio — si el
    # teléfono está en el listado de empleados, se aplica este descuento en
    # vez del de socio (aunque también sea socio). "0" = apagado.
    "empleado_discount_pct": "20",
    "empleado_discount_message": "",
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


def valores_base() -> dict:
    """
    DEFAULTS + los textos del perfil de rubro (spec 3.4). Lo guardado en
    Redis/Postgres se mezcla ENCIMA y siempre gana. En farmacia y mutual los
    textos del perfil se arman desde DEFAULTS: da DEFAULTS tal cual. Pública
    para que los tests armen su config falsa con los textos del perfil.
    """
    return {**DEFAULTS, **get_perfil().textos}


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
                    return {**valores_base(), **data}
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
            return {**valores_base(), **durable}

        return {**valores_base(), **self._cache}

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
        return config.get(key, valores_base().get(key, ""))

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
    # Cada día: {"active", "open", "close"} y, opcional, "ranges": [{"open",
    # "close"}, ...] para el horario cortado (29/9: la farmacia atiende de 7:30
    # a 13 y de 16 a 19:30). Sin "ranges" vale open/close como una sola franja.
    # Vive en Redis y, desde 29/9, también en Postgres (config "hours"): antes
    # un reinicio de Redis lo borraba.

    async def get_hours(self) -> dict:
        if await self._usable():
            try:
                raw = await self._redis.get(HOURS_KEY)
                if raw:
                    return json.loads(raw)
            except Exception:
                pass
        durable = (await self._leer_postgres()).get("hours")
        if durable:
            try:
                hours = json.loads(durable)
                if await self._usable():
                    await self._redis.set(HOURS_KEY, durable)
                return hours
            except Exception:
                pass
        return dict(DEFAULT_HOURS)

    async def set_hours(self, hours: dict):
        raw = json.dumps(hours)
        if not await self._guardar_postgres({"hours": raw}):
            logger.error("config: el horario NO se persistió en Postgres (vive solo en Redis)")
        if await self._usable():
            try:
                await self._redis.set(HOURS_KEY, raw)
            except Exception:
                pass

    @staticmethod
    def franjas(cfg: dict) -> list[tuple[str, str]]:
        """[(open, close), ...] de un día, ordenadas. Vacío si no atiende."""
        if not cfg or not cfg.get("active"):
            return []
        rangos = cfg.get("ranges") or [{"open": cfg.get("open"), "close": cfg.get("close")}]
        out = [((r.get("open") or "")[:5], (r.get("close") or "")[:5]) for r in rangos]
        return sorted((a, c) for a, c in out if a and c)

    def is_open_now(self, hours: dict) -> bool:
        """True si el horario está activo y el momento actual cae en alguna franja."""
        if not hours.get("enabled"):
            return True  # sin control de horario → siempre abierto
        now = datetime.now(TZ_ARG)
        cfg = hours.get("schedule", {}).get(DAY_MAP[now.weekday()], {})
        actual = now.strftime("%H:%M")
        return any(a <= actual <= c for a, c in self.franjas(cfg))

    @staticmethod
    def _hora(t: str) -> str:
        h = t[:5].lstrip("0") or "0:00"
        return "0" + h if h.startswith(":") else h

    def proxima_apertura(self, hours: dict) -> str:
        """Cuándo abre la farmacia: "hoy a las 16:00", "mañana a las 7:30",
        "el lunes a las 7:30". Vacío si no hay horario cargado."""
        schedule = hours.get("schedule", {})
        now = datetime.now(TZ_ARG)
        actual = now.strftime("%H:%M")
        for offset in range(8):
            day = DAY_MAP[(now.weekday() + offset) % 7]
            for a, _c in self.franjas(schedule.get(day, {})):
                if offset == 0 and actual >= a:
                    continue
                cuando = "hoy" if offset == 0 else "mañana" if offset == 1 else f"el {DIAS_ES[day]}"
                return f"{cuando} a las {self._hora(a)}"
        return ""

    def texto_horario(self, hours: dict) -> str:
        """"de lunes a viernes de 7:30 a 13 y de 16 a 19:30, y los sábados de
        8:30 a 12:30". Agrupa días seguidos con el mismo horario."""
        schedule = hours.get("schedule", {})
        orden = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        grupos: list[tuple[list[str], list]] = []
        for d in orden:
            f = self.franjas(schedule.get(d, {}))
            if not f:
                continue
            if grupos and grupos[-1][1] == f and orden.index(grupos[-1][0][-1]) + 1 == orden.index(d):
                grupos[-1][0].append(d)
            else:
                grupos.append(([d], f))
        partes = []
        for dias, f in grupos:
            if len(dias) == 1:
                cuando = {"sat": "los sábados", "sun": "los domingos"}.get(dias[0], f"los {DIAS_ES[dias[0]]}")
            else:
                cuando = f"de {DIAS_ES[dias[0]]} a {DIAS_ES[dias[-1]]}"
            rangos = " y ".join(f"de {self._hora(a)} a {self._hora(c)}" for a, c in f)
            partes.append(f"{cuando} {rangos}")
        if not partes:
            return ""
        return partes[0] if len(partes) == 1 else ", ".join(partes[:-1]) + " y " + partes[-1]

    def get_pickup_text(self, hours: dict, pickup_minutes: int = 30) -> str:
        """
        Texto de horario de retiro para los mensajes al cliente (siempre, con o
        sin control de horario activo). Ejemplos:
          "Podés retirarlo hoy de 16:00 a 19:30 hs 🕐"
          "Retiros mañana de 7:30 a 13:00 y de 16:00 a 19:30 hs 🕐"
        """
        schedule = hours.get("schedule", {})
        mins_txt = f"⏱ Tiempo estimado: *{pickup_minutes} min*" if pickup_minutes else ""
        prefix = f"{mins_txt} · " if mins_txt else ""
        now = datetime.now(TZ_ARG)
        actual = now.strftime("%H:%M")
        for offset in range(7):
            day = DAY_MAP[(now.weekday() + offset) % 7]
            f = self.franjas(schedule.get(day, {}))
            if offset == 0:
                f = [(a, c) for a, c in f if c > actual]      # lo que queda de hoy
            if not f:
                continue
            rangos = " y ".join(f"de {self._hora(a)} a {self._hora(c)}" for a, c in f)
            if offset == 0:
                return f"{prefix}Podés retirarlo hoy {rangos} hs 🕐"
            if offset == 1:
                return f"{prefix}Retiros mañana {rangos} hs 🕐"
            return f"{prefix}Próximos retiros el {DIAS_ES[day]} {rangos} hs 🕐"
        return mins_txt


_instance: Optional[ConfigService] = None


def get_config_service(redis_url: str) -> ConfigService:
    global _instance
    if _instance is None:
        _instance = ConfigService(redis_url)
    return _instance
