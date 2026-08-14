# Plan — CERCA Sucursales (Mutual AMI)

> Estado: **propuesta, sin implementar**. Redactado el 2026-08-13 a partir de
> `Modelo_Conversacional_CERCA_Especificacion_Developer.docx`.
> Entorno: despliegue propio (lo levanta Mariano), línea de WhatsApp nueva por Kapso.

## Qué es

Bot conversacional para las **sucursales de Mutual AMI**: información institucional
y de productos financieros (préstamos, ahorro a término, cuota social, beneficios)
con derivación obligatoria a persona en todo lo que toque cuentas o dinero.

**No vende ni cobra**: no hay catálogo, carrito, checkout ni pasarela. Eso lo
diferencia del bot de la farmacia, que comparte el mismo código.

## Decisión de arquitectura

Mismo repositorio y mismo motor, con un **vertical configurable**
(`vertical = farmacia | mutual`) que define qué módulos se activan:

| Módulo | farmacia | mutual |
|---|---|---|
| Catálogo de productos y búsqueda | ✅ | ❌ |
| Carrito, checkout y pasarela de pago | ✅ | ❌ |
| Derivación por receta | ✅ | ❌ |
| Base de conocimiento (RAG) | opcional | ✅ **núcleo** |
| Simulador de préstamos | ❌ | ✅ |
| Derivación por consulta de cuenta | ❌ | ✅ |
| Escalada por emoción / turnos / duración | ✅ (mejora) | ✅ |

Las mejoras transversales (emoción, escalada por turnos, FCR) benefician a los dos
verticales y se implementan una sola vez.

---

## Fase 1 — Base de conocimiento y derivaciones (el corazón)

**1.1 Cargar la base de conocimiento** (secciones 2.1 a 2.6 del doc)
El buscador semántico (`kb_documents` + `kb_search`) ya existe y está en producción.
Falta cargar el contenido y hacerlo la fuente principal de respuesta:
- Horarios (caja, administración, farmacia)
- Datos generales: alias AMICORREA, cuota social $4.000, vencimiento día 10,
  documentación de alta
- Requisitos de préstamos personales (titular y garantes)
- Condiciones: tasa preferencial 55% TNA ($1,5M–$6M, hasta 12 cuotas) y
  público general 75% TNA (hasta 36 cuotas), con la nota de la primera cuota
- AMT: 23,5% presencial / 26% online, 29 a 60 días, sellado reducido a 29 días
- Beneficios para socios (incluye 15% en la Farmacia Mutual)

Carga desde el backoffice (ya hay pantalla de KB). Cada respuesta cita solo lo que
está cargado; **no inventar** — si no está, ofrecer derivar.

**1.2 Derivación obligatoria por tipo de consulta** (sección 2.9)
Matchers específicos, con el mismo mecanismo que hoy usa "transferencia/efectivo"
en farmacia. Derivan **siempre**, sin intentar responder:
- comprobante de transferencia / envío de comprobante
- solicitud de transferencia
- renovación de plazo fijo
- valor de cuota de préstamo
- saldo de caja de ahorro
- vencimiento de plazo fijo

Motivo de derivación propio para cada uno (ya existe `derivada_motivo`), para poder
medir la sección 4.5.

**1.3 Prompt del vertical**
Tono institucional de mutual, respuestas concisas, **máximo 3-4 opciones por turno**
(sección 4.4) y opción de hablar con un asesor siempre visible.

---

## Fase 2 — Escalada inteligente (sección 4.2)

Reglas nuevas, todas configurables desde el backoffice:

**2.1 Emoción negativa persistente**
El clasificador de intención ya corre en cada mensaje: se le agrega un campo
`sentimiento` (positivo/neutro/negativo) en la misma llamada — costo cero adicional.
Con 2 mensajes negativos consecutivos → ofrecer pasar con una persona.

**2.2 Corte por turnos y duración**
- Prioridad alta sin resolver a los 8-10 turnos → escalar proactivamente.
- Más de 30 interacciones o 90 minutos sin cierre → cortar el flujo automático y
  derivar (evita el efecto bucle).
Contadores en la sesión; umbrales configurables.

**2.3 Salida siempre a la vista**
A partir del turno 10, cada respuesta cierra ofreciendo hablar con un asesor.

---

## Fase 3 — Simulador de préstamos (sección 4.1)

⚠️ **Requiere definición de producto antes de construir.**

Cálculo de cuota por sistema francés a partir de monto, plazo y línea (preferencial
o general), usando la tabla 2.4.

Recaudos no negociables:
- Siempre acompañado de "importe estimado, sujeto a evaluación crediticia".
- No promete aprobación ni califica al solicitante.
- No pide ni almacena datos sensibles (recibos, DNI) — para eso deriva.
- Contempla la nota operativa: liquidación a partir del día 18 mueve la primera
  cuota al mes subsiguiente.

**Decisión pendiente**: ¿el bot calcula cuotas, o solo informa tasas y condiciones y
deriva a un asesor para la simulación?

---

## Fase 4 — Personalización por recurrencia (sección 4.3)

- **Primera vez**: onboarding breve — qué es Mutual AMI, cómo asociarse, beneficios
  destacados.
- **Recurrente**: reconocer historial (ya tenemos el padrón por teléfono y el
  historial en Postgres), no repreguntar lo ya provisto, saludo adaptado.

La clasificación primera vez / ocasional / frecuente sale de `messages`.

---

## Fase 5 — KPIs del doc (sección 4.5)

Sobre la base de métricas ya construida (`interacciones`, `eventos`, dashboard):

| KPI | Estado |
|---|---|
| Duración media y mediana de conversación | **nuevo** — hoy medimos tiempo de respuesta, no de conversación |
| Interacciones por conversación | **nuevo** (dato disponible, falta agregación) |
| **FCR** (resuelta en el primer contacto) | **nuevo** — definir qué cuenta como "resuelta" |
| % de emoción negativa y tasa de escalada desde ese disparador | depende de Fase 2 |
| Derivaciones por causal (regla de negocio vs. señal conversacional) | ya existe `derivada_motivo`, falta la vista |

**Definición pendiente**: qué es una conversación "resuelta" para el FCR. Propuesta:
cerrada sin derivación a humano y sin reapertura del mismo número en 24 hs.

---

## Advertencias

**Los KPIs de la sección 3 son de datos simulados.** El propio documento lo aclara
("base de datos simulada representativa, ~1.000 conversaciones"). Los 59,41 minutos
y las 20,8 interacciones **no describen la operación real de la mutual**. Sirven como
criterio de diseño (concisión, salidas claras), pero no como línea base: al reportar
resultados hay que comparar contra datos reales medidos desde el día uno, no contra
estos valores.

**Datos de cuentas fuera del bot.** Saldos, cuotas, plazos fijos y transferencias
derivan siempre. Es la regla que más hay que blindar: no alcanza con que el prompt lo
pida, tiene que estar en código antes de llegar al modelo.

**Canal.** El doc analiza chat, voz, email y redes; este desarrollo cubre **WhatsApp**.
Si se esperan los otros canales, es alcance aparte.

## Orden sugerido

Fase 1 → Fase 2 → Fase 4 → Fase 5 → Fase 3 (el simulador último: es el que más
definición de negocio necesita y el que más riesgo de expectativa genera).

Con la Fase 1 el bot ya es útil en producción: responde todo lo institucional y
deriva bien lo sensible.
