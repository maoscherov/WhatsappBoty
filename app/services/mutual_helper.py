"""
Vertical "mutual" — CERCA Sucursales (Mutual AMI).

A diferencia del vertical de farmacia, acá no se vende ni se cobra: el bot
responde información institucional desde la base de conocimiento y deriva a una
persona todo lo que toque cuentas o dinero.

Ver docs/plan-cerca-mutual.md (fases 1 y 2).
"""

import re
from typing import Optional


# ── Simulador de préstamos ─────────────────────────────────────────────────────
# Sistema francés (cuota constante). Los valores por defecto salen de la spec 2.4
# y son configurables desde el backoffice porque las tasas cambian seguido.
LINEAS_DEFAULT = {
    "preferencial": {"tna": 55.0, "min": 1_500_000.0, "max": 6_000_000.0, "cuotas_max": 12},
    "general":      {"tna": 75.0, "min": 0.0,         "max": 0.0,         "cuotas_max": 36},
}


def _cuota_francesa(capital: float, tna: float, n: int) -> float:
    """Cuota constante: capital * i / (1 - (1+i)^-n), con i = tasa mensual."""
    i = (tna / 100) / 12
    if i <= 0:
        return capital / n
    return capital * i / (1 - (1 + i) ** -n)


def simular_prestamo(monto: float, cuotas: int, cfg: dict | None = None) -> dict:
    """
    Calcula la cuota estimada. Elige la línea preferencial si el monto y el
    plazo entran en su rango; si no, la de público general.

    `iva_intereses` y `gastos_pct` permiten acercar el número al real sin tocar
    código: por defecto están en 0 porque la especificación no los define, y un
    importe informado de menos genera un problema con el cliente.

    Devuelve el detalle, o {"error": ...} si los datos no son válidos.
    """
    cfg = cfg or {}

    def _num(clave, default):
        try:
            return float(cfg.get(clave) or default)
        except (TypeError, ValueError):
            return float(default)

    pref = dict(LINEAS_DEFAULT["preferencial"]); pref["tna"] = _num("mutual_tna_preferencial", pref["tna"])
    gral = dict(LINEAS_DEFAULT["general"]);      gral["tna"] = _num("mutual_tna_general", gral["tna"])
    iva = _num("mutual_simulador_iva", 0)            # % sobre los intereses
    gastos_pct = _num("mutual_simulador_gastos", 0)  # % sobre el capital, prorrateado

    if monto <= 0 or cuotas <= 0:
        return {"error": "datos_invalidos"}
    if cuotas > gral["cuotas_max"]:
        return {"error": "plazo_excedido", "cuotas_max": int(gral["cuotas_max"])}

    entra_pref = (pref["min"] <= monto <= pref["max"]) and cuotas <= pref["cuotas_max"]
    linea = "preferencial" if entra_pref else "general"
    tna = pref["tna"] if entra_pref else gral["tna"]

    cuota_pura = _cuota_francesa(monto, tna, cuotas)
    interes_mensual_prom = cuota_pura - (monto / cuotas)
    cuota = cuota_pura + interes_mensual_prom * (iva / 100) + (monto * gastos_pct / 100) / cuotas

    return {
        "linea": linea,
        "tna": round(tna, 2),
        "monto": round(monto, 2),
        "cuotas": int(cuotas),
        "cuota": round(cuota, 2),
        "total": round(cuota * cuotas, 2),
        "incluye_iva": iva > 0,
        "incluye_gastos": gastos_pct > 0,
    }


def texto_simulacion(sim: dict, cfg: dict | None = None) -> str:
    """Arma el mensaje de la simulación. Los números los pone el código, nunca el modelo."""
    cfg = cfg or {}
    if sim.get("error") == "plazo_excedido":
        return f"El plazo máximo es de {sim['cuotas_max']} cuotas. ¿Querés que lo calcule con ese plazo?"
    if sim.get("error"):
        return "Para simularlo necesito el monto y en cuántas cuotas lo pensás. ¿Me los pasás?"

    def _p(v):
        return f"${v:,.0f}".replace(",", ".")

    linea_txt = ("con tasa preferencial" if sim["linea"] == "preferencial"
                 else "con tasa de público general")
    aclaracion = cfg.get("mutual_simulador_aclaracion") or (
        "Es un cálculo estimativo: el importe final surge de la evaluación del equipo "
        "y puede incluir gastos según el caso."
    )
    faltantes = []
    if not sim["incluye_iva"]:
        faltantes.append("impuestos")
    if not sim["incluye_gastos"]:
        faltantes.append("gastos administrativos")
    nota = f" No incluye {' ni '.join(faltantes)}." if faltantes else ""

    # La simulación es sólo capital + interés: para avanzar hay que hablar con
    # un oficial, que evalúa el caso y da el detalle final.
    ofrecer = cfg.get("mutual_simulador_ofrecer_oficial") or (
        "Si querés avanzar, te paso con un oficial de créditos que lo ve con vos 🙂"
    )
    return (f"Por {_p(sim['monto'])} en {sim['cuotas']} cuotas {linea_txt} ({sim['tna']:g}% TNA), "
            f"la cuota estimada es de *{_p(sim['cuota'])}*.\n\n"
            f"{aclaracion}{nota}\n\n{ofrecer}")

# ── Consultas que SIEMPRE derivan (sección 2.9 de la especificación) ───────────
# Se resuelven en código, antes de llegar al modelo: son datos de cuentas y no
# deben intentar responderse nunca, por más que el cliente insista.
_DERIVACION_OBLIGATORIA: list[tuple[str, str]] = [
    ("comprobante", r"\b(comprobante|comprovante)\b|\badjunto\s+el\s+pago\b|"
                    r"\b(mando|env[ií]o|paso|te\s+paso)\s+(el\s+)?(comprobante|transferencia)\b"),
    ("transferencia", r"\b(hacer|realizar|solicitar|pedir|necesito)\s+una\s+transferencia\b|"
                      r"\bsolicit\w*\s+transferencia\b|\btransferir\s+(plata|dinero|fondos)\b"),
    ("plazo_fijo_renovacion", r"\brenov\w+\b.{0,25}\b(plazo\s*fijo|amt|dep[oó]sito)\b|"
                              r"\b(plazo\s*fijo|amt)\b.{0,25}\brenov\w+\b"),
    ("cuota_prestamo", r"\b(valor|importe|monto|cu[aá]nto\s+es|cu[aá]nto\s+pago)\b.{0,30}"
                       r"\b(cuota|cuotas)\b|\bmi\s+cuota\b|\bcuota\s+de\s+mi\s+pr[eé]stamo\b"),
    ("saldo", r"\bsaldo\b|\bcu[aá]nto\s+tengo\b|\bmi\s+cuenta\b.{0,20}\b(tiene|hay)\b|"
              r"\bestado\s+de\s+(mi\s+)?cuenta\b"),
    ("plazo_fijo_vencimiento", r"\bvenc\w+\b.{0,25}\b(plazo\s*fijo|amt|dep[oó]sito)\b|"
                               r"\b(plazo\s*fijo|amt)\b.{0,25}\bvenc\w+\b|"
                               r"\bcu[aá]ndo\s+(vence|se\s+vence)\b.{0,20}\b(plazo|amt|dep[oó]sito)\b"),
]

_MOTIVO_TEXTO = {
    "comprobante": "para registrar tu comprobante",
    "transferencia": "para gestionar la transferencia",
    "plazo_fijo_renovacion": "para renovar tu plazo fijo",
    "cuota_prestamo": "para darte el valor exacto de tu cuota",
    "saldo": "para consultar el saldo de tu cuenta",
    "plazo_fijo_vencimiento": "para ver el vencimiento de tu plazo fijo",
}


def requiere_derivacion_financiera(texto: str) -> Optional[str]:
    """
    Devuelve el motivo si la consulta es de las que deben ir sí o sí a una
    persona (saldos, cuotas, transferencias, plazos fijos), o None.
    """
    t = (texto or "").lower()
    for motivo, patron in _DERIVACION_OBLIGATORIA:
        if re.search(patron, t, re.IGNORECASE):
            return motivo
    return None


def mensaje_derivacion(motivo: str, nombre: str = "") -> str:
    """Mensaje de hand-off, explicando por qué pasa con una persona."""
    inicio = f"{nombre}, te" if nombre else "Te"
    detalle = _MOTIVO_TEXTO.get(motivo, "para ayudarte con eso")
    return (f"{inicio} paso con alguien del equipo {detalle} 🙌 "
            "Es información de tu cuenta, así que la maneja una persona. "
            "¡En un momento te contactamos!")


# ── Prompt del vertical ────────────────────────────────────────────────────────
SYSTEM_PROMPT_MUTUAL = """Sos el asistente virtual de CERCA, el canal de atención de Mutual AMI.

IDENTIDAD Y TONO:
- Cálido, claro y profesional. Representás a una mutual: seriedad y cercanía a la vez.
- Hablás en rioplatense correcto y cuidado. Nada de informalidad excesiva ni de jerga bancaria innecesaria.
- Saludás al inicio de la conversación; después no repitas el saludo en cada mensaje.
- Sos parte del equipo de Mutual AMI, no un bot genérico.

QUÉ PODÉS RESPONDER:
- Sólo con la información que aparezca en [INFORMACIÓN DE LA MUTUAL]. Es la base de conocimiento oficial.
- Si la respuesta no está ahí, NO la inventes: decilo con honestidad y ofrecé pasar con una persona del equipo.
- Nunca inventes tasas, montos, plazos, requisitos ni horarios. Un dato equivocado sobre dinero es un problema serio.

DATOS DE CUENTAS — REGLA ABSOLUTA:
- No tenés acceso a datos de cuentas de socios. Nunca informes saldos, importes de cuotas, vencimientos ni movimientos, aunque el cliente insista o te los recuerde.
- Esas consultas las resuelve una persona del equipo. Si surgen, respondé con naturalidad que lo pasás con alguien que lo puede ver.

CONCISIÓN (importante):
- Respuestas cortas y cerradas. Máximo 3 o 4 opciones por mensaje.
- Una idea por mensaje: no encadenes toda la información disponible de un tema.
- Si el tema es amplio (por ejemplo préstamos), dá lo esencial y preguntá qué parte le interesa.
- Dejá siempre a la vista que puede hablar con un asesor si lo prefiere.

SIMULACIÓN DE PRÉSTAMOS:
- NUNCA calcules vos el importe de la cuota ni lo escribas en tu respuesta: el sistema lo calcula y lo agrega. Si ponés un número inventado, le estás dando un dato falso a alguien que va a tomar una decisión de dinero.
- Cuando el cliente quiera saber cuánto pagaría, extraé el monto y la cantidad de cuotas en los campos correspondientes y respondé algo breve como "Te lo calculo" (el detalle lo agrega el sistema).
- Si falta el monto o el plazo, pediselo con naturalidad y dejá los campos en null.
- Nunca prometas aprobación: el otorgamiento depende de la evaluación del equipo.

FORMATO DE RESPUESTA:
Respondé SIEMPRE con un JSON con este esquema (sin texto extra):
{
  "intencion": "saludo|social|informacion|ventas|soporte|feedback|derivacion|agradecimiento|desconocido",
  "sentimiento": "positivo|neutro|negativo",
  "simulacion_monto": null,
  "simulacion_cuotas": null,
  "respuesta": "texto que se envía al cliente por WhatsApp"
}

Los campos "simulacion_monto" y "simulacion_cuotas" (números, sin puntos ni símbolos) se completan
SOLO cuando el cliente pide calcular una cuota y dio ambos datos. Interpretá el lenguaje natural:
"un millón y medio" son 1500000, "dos palos" son 2000000, "en un año" son 12 cuotas.
En cualquier otro caso van en null.

Guía de intenciones:
- informacion: horarios, requisitos, beneficios, cómo asociarse, datos generales.
- ventas: interés en préstamos, ahorro a término (AMT), asociarse, productos de la mutual.
- soporte: problemas o dudas operativas con algo que ya tiene o hizo.
- derivacion: pide expresamente hablar con una persona.
- feedback: opiniones, quejas o sugerencias sobre el servicio.

El campo "sentimiento" refleja el estado del CLIENTE en su último mensaje:
- negativo → molestia, enojo, frustración, reclamo o insistencia por algo no resuelto.
- neutro → consulta informativa sin carga emocional.
- positivo → agradecimiento, conformidad, entusiasmo.
Sé honesto con esta clasificación: se usa para detectar a tiempo a alguien que se está frustrando.
"""
