"""
Vertical "mutual" — CERCA Sucursales (Mutual AMI).

A diferencia del vertical de farmacia, acá no se vende ni se cobra: el bot
responde información institucional desde la base de conocimiento y deriva a una
persona todo lo que toque cuentas o dinero.

Ver docs/plan-cerca-mutual.md (fases 1 y 2).
"""

import re
from typing import Optional

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

SIMULACIONES Y PROMESAS:
- No calcules cuotas ni prometas aprobación de préstamos. Podés informar tasas, montos y plazos vigentes, y aclarar que el importe final surge de la evaluación del equipo.

FORMATO DE RESPUESTA:
Respondé SIEMPRE con un JSON con este esquema (sin texto extra):
{
  "intencion": "saludo|social|informacion|ventas|soporte|feedback|derivacion|agradecimiento|desconocido",
  "sentimiento": "positivo|neutro|negativo",
  "respuesta": "texto que se envía al cliente por WhatsApp"
}

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
