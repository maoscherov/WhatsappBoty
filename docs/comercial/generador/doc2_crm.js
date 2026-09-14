const L = require("./lib");
const { p, small, h1, h2, h3, bullets, numbered, table, callout, spacer, pageBreak, codeBlock, cover, buildDoc, toc, save } = L;

const OUT = process.argv[2];

const children = [
  ...cover({
    title: "Integración de Remedia con el CRM de [CLIENTE]",
    subtitle: "Arquitectura de integración, catálogo de eventos, mapeo de datos y plan de implementación",
    meta: [
      ["Documento", "Respuesta a objeción de implementación — Integración con CRM"],
      ["Producto", "Remedia — asistente conversacional de WhatsApp (verticales farmacia y mutual)"],
      ["Presenta", "[PROVEEDOR]"],
      ["Cliente", "[CLIENTE]"],
      ["Versión", "1.0 — septiembre de 2026"],
      ["Clasificación", "Confidencial — uso comercial y de implementación"],
    ],
  }),
  ...toc(),

  // ───────────────────────────── 1
  h1("1. Resumen ejecutivo"),
  p("La segunda objeción habitual en la evaluación de Remedia viene de Sistemas y de Comercial: **\"ya tenemos un CRM; no queremos otra herramienta con datos de clientes por fuera, ni cargar dos veces la información\".** Es una objeción legítima y la respuesta es de diseño: Remedia no compite con el CRM, lo alimenta."),
  callout("Respuesta corta a la objeción", [
    "**1. El CRM sigue siendo el sistema maestro.** Remedia no pretende ser el repositorio de clientes. Cada contacto, oportunidad, ticket o venta que se origina en WhatsApp se registra en el CRM de [CLIENTE], con el identificador del CRM como referencia.",
    "**2. La integración es por eventos, no por sincronización masiva.** Cada hecho relevante (nueva conversación, derivación a operador, pedido pagado, cliente que pide la baja) se publica en tiempo real hacia el CRM como un webhook firmado, con reintentos e idempotencia. No hay procesos batch que se desincronicen.",
    "**3. Funciona con cualquier CRM que tenga API.** HubSpot, Salesforce, Zoho, Pipedrive, Dynamics 365, Odoo o un desarrollo propio. Se conecta de forma directa o a través de una plataforma de integración (n8n, Make, Zapier) si el Cliente ya la usa.",
    "**4. El CRM también puede hablarle a Remedia.** Desde el CRM se pueden disparar mensajes de WhatsApp con plantillas aprobadas, actualizar el padrón de socios y consultar el estado de una conversación o un pedido.",
    "**5. Los datos sensibles no se replican por defecto.** Al CRM viajan datos de contacto, comerciales y de servicio. Las imágenes de recetas y los datos de salud se quedan en el circuito controlado de Remedia salvo decisión escrita del Cliente.",
  ]),
  spacer(),
  p("El documento describe la arquitectura, el catálogo de eventos con sus cargas útiles, el mapeo a los objetos típicos de un CRM, las consideraciones de seguridad y el plan de implementación con roles, requisitos y cronograma. Está pensado para que Sistemas del Cliente pueda dimensionar el trabajo de su lado en la primera lectura."),

  // ───────────────────────────── 2
  h1("2. Principios de la integración"),
  table(
    ["Principio", "Qué significa en la práctica"],
    [
      ["El CRM es la fuente de verdad del cliente", "Remedia no mantiene una ficha de cliente propia más allá de lo que necesita para operar la conversación (teléfono, nombre, número de socio). La identidad comercial, la segmentación y el historial de largo plazo viven en el CRM."],
      ["Remedia es la fuente de verdad de la conversación y la transacción", "Lo que pasó en el chat, cuándo se derivó, qué se cotizó, qué se pagó y qué se entregó se origina en Remedia y se publica al CRM como eventos y actividades."],
      ["Identidad por número de teléfono", "La clave de correlación es el teléfono en formato internacional E.164 (por ejemplo +5491155551234). Si el CRM ya tiene el contacto, se enriquece; si no, se crea como lead. Un identificador externo del CRM se guarda en Remedia para no volver a buscar."],
      ["Eventos, no sincronización", "Cada hecho se publica una vez, en el momento en que ocurre, con un identificador único. El CRM (o el middleware) es idempotente: recibir dos veces el mismo evento no duplica registros."],
      ["Mínimo dato necesario", "Cada evento lleva sólo los campos que el CRM necesita para el caso de uso acordado. Se puede ampliar por configuración, nunca por defecto."],
      ["Separación de datos sensibles", "Imágenes de recetas, datos de obra social y diagnóstico no viajan al CRM salvo que el Cliente lo decida por escrito y su CRM tenga controles de acceso equivalentes."],
      ["Sin dependencia en línea", "Si el CRM no responde, el asistente sigue atendiendo. Los eventos se encolan y se reintentan; nunca se pierde una venta por una caída del CRM."],
    ],
    [2600, 6426]
  ),

  // ───────────────────────────── 3
  h1("3. Arquitectura de integración"),
  p("La integración se apoya en cuatro mecanismos complementarios. Los dos primeros cubren la mayoría de los casos; los otros dos se suman según el escenario del Cliente."),
  h2("3.1 Webhooks salientes (Remedia → CRM)"),
  p("Es el mecanismo principal. Cada evento de negocio se envía como una petición HTTPS POST con cuerpo JSON al endpoint que el Cliente indique (su CRM, su middleware o un servicio propio). Características:"),
  ...bullets([
    "**Firma HMAC-SHA256** del cuerpo con un secreto por integración, en la cabecera `X-Remedia-Signature`, para que el receptor verifique origen e integridad.",
    "**Idempotencia:** cada evento lleva un `event_id` único y una marca de tiempo. El receptor puede descartar duplicados con seguridad.",
    "**Reintentos con espera exponencial** durante 24 horas ante respuestas distintas de 2xx; los eventos no entregados quedan visibles en el panel para reenvío manual.",
    "**Suscripción por tipo de evento:** el Cliente elige qué eventos recibe. Se pueden configurar varios destinos (por ejemplo, ventas al CRM y derivaciones a la mesa de ayuda).",
    "**Orden garantizado por conversación:** los eventos de un mismo teléfono se entregan en el orden en que ocurrieron.",
  ]),
  h2("3.2 API de consulta (CRM → Remedia, lectura)"),
  p("Una API REST autenticada permite al CRM (o a un operador desde el CRM) consultar el estado actual sin esperar un evento: conversaciones abiertas y derivadas, historial de un teléfono, pedidos por estado, detalle de un pedido, y verificación de si un teléfono figura en el padrón de socios. Se usa para enriquecer la ficha del contacto en el CRM con un panel embebido o para reconciliaciones periódicas."),
  h2("3.3 API de acción (CRM → Remedia, escritura)"),
  p("Permite que el CRM dispare acciones en el canal de WhatsApp:"),
  ...bullets([
    "**Enviar una plantilla aprobada** por WhatsApp a un contacto con opt-in registrado (recordatorio de retiro, aviso de vencimiento de cuota, campaña segmentada). La plantilla y sus variables se definen en Meta; Remedia valida el opt-in antes de enviar.",
    "**Enviar un mensaje de operador** dentro de una conversación derivada, para que el operador pueda responder desde el CRM sin abrir el panel de Remedia.",
    "**Actualizar el padrón de socios** de forma incremental (alta, baja o modificación de un socio) en lugar de recargar el archivo completo.",
    "**Cerrar o derivar una conversación** desde el CRM.",
  ]),
  h2("3.4 Plataforma de integración (opcional)"),
  p("Si el Cliente ya opera n8n, Make, Zapier, Power Automate o un bus corporativo, los webhooks de Remedia se conectan allí y la lógica de mapeo al CRM vive en la herramienta que el Cliente controla. Es la opción recomendada cuando el Cliente quiere autonomía total sobre las reglas de negocio de la integración o cuando el CRM tiene un conector nativo en esa plataforma."),
  h2("3.5 Diagrama de flujo"),
  codeBlock(
`Cliente final        Remedia                     CRM de [CLIENTE]
(WhatsApp)           (despliegue del Cliente)    (HubSpot, Salesforce, propio)
    |                     |                             |
    | mensaje ----------->|                             |
    |                     |-- conversacion.iniciada --->| crea/actualiza Contacto
    | foto de receta ---->|                             |
    |                     |-- conversacion.derivada --->| crea Ticket / Caso
    | paga -------------->|                             |
    |                     |-- pedido.creado ----------->| crea Deal ganado + Orden
    |                     |<-- POST /mensajes/plantilla | "Tu pedido esta listo"
    |<-- plantilla WA ----|                             |
    |                     |<-- GET /pedidos?estado=...  | reconciliacion / ficha 360`
  ),

  // ───────────────────────────── 4
  h1("4. Catálogo de eventos"),
  p("Los eventos que Remedia registra hoy en su tablero de métricas son los mismos que se publican al CRM. La tabla indica el objeto del CRM que típicamente se ve afectado; el mapeo final se define en el discovery."),
  table(
    ["Evento", "Cuándo se dispara", "Datos principales", "Objeto CRM típico"],
    [
      ["conversacion.iniciada", "Primer mensaje de un teléfono sin sesión activa", "teléfono, nombre (si figura en el padrón), número de socio, vertical, canal", "Contacto / Lead (crear o actualizar)"],
      ["conversacion.intencion", "El asistente clasifica un mensaje (consulta, precio, stock, pedido, reclamo)", "teléfono, intención, producto buscado", "Actividad / Nota"],
      ["busqueda.sin_resultado", "El cliente pidió un producto que no está en el catálogo", "teléfono, término buscado", "Actividad; insumo para compras"],
      ["producto.ofrecido", "El asistente ofreció un producto con precio", "teléfono, SKU, nombre, precio", "Actividad; línea de oportunidad"],
      ["conversacion.derivada", "Se deriva a operador (receta, reclamo, pago manual, sin stock, sentimiento negativo, fuera de horario)", "teléfono, motivo, resumen breve de la conversación", "Ticket / Caso (crear)"],
      ["conversacion.atendida", "Un operador toma la conversación derivada", "teléfono, operador, tiempo de espera", "Ticket (asignar)"],
      ["conversacion.cerrada", "Cierre por el operador o por inactividad", "teléfono, motivo, duración, cantidad de mensajes", "Ticket (cerrar); Actividad"],
      ["cotizacion.enviada", "Un operador envía la cotización de una receta", "teléfono, monto total, cantidad de ítems, cobertura aplicada (%)", "Oportunidad (crear, etapa cotizado)"],
      ["pago.link_enviado", "Se envía el link de pago", "teléfono, monto, referencia", "Oportunidad (etapa link enviado)"],
      ["pago.aprobado", "La pasarela confirma el cobro", "teléfono, monto, pasarela, medio de pago, identificador de pago", "Oportunidad (ganada)"],
      ["pago.rechazado", "La pasarela rechaza el cobro", "teléfono, monto, motivo", "Actividad; tarea de seguimiento"],
      ["pedido.creado", "Con el pago aprobado se genera el pedido y el código de retiro", "número de pedido, teléfono, ítems, total, tipo de entrega, dirección (si envío)", "Venta / Orden (crear)"],
      ["pedido.preparado", "El operador marca el pedido como preparado", "número de pedido, operador", "Orden (actualizar estado)"],
      ["pedido.retirado", "El pedido se entrega", "número de pedido, operador", "Orden (cerrar)"],
      ["conversacion.sentimiento", "Se detecta sentimiento negativo o pedido de queja", "teléfono, sentimiento, fragmento", "Ticket prioritario / alerta"],
      ["contacto.baja", "El cliente pide no recibir más mensajes", "teléfono, fecha", "Contacto (marcar sin consentimiento de marketing)"],
      ["mensaje.fallo_envio", "WhatsApp no pudo entregar un mensaje", "teléfono, tipo, detalle", "Actividad; alerta"],
    ],
    [2000, 2600, 2600, 1826]
  ),
  spacer(),
  h2("4.1 Estructura de un evento"),
  p("Todos los eventos comparten un sobre común. Ejemplo de `pedido.creado`:"),
  codeBlock(
`POST https://crm.cliente.com/webhooks/remedia
Content-Type: application/json
X-Remedia-Event: pedido.creado
X-Remedia-Signature: sha256=3f1c...9a7e
X-Remedia-Delivery: 7c0e4a1e-2b7f-4a9a-9c2c-0d1b2e3f4a5b

{
  "event_id": "evt_01J8Q4W9K2M3N4P5R6S7T8U9V0",
  "type": "pedido.creado",
  "occurred_at": "2026-09-07T14:32:10-03:00",
  "tenant": "farmacia-mutual",
  "contact": {
    "phone": "+5491155551234",
    "name": "María Pérez",
    "member_id": "12345",
    "crm_id": "hs-0034567"
  },
  "data": {
    "order_id": "ORD-20260907-143210-A7K2Q",
    "pickup_code": "482913",
    "status": "pendiente",
    "delivery": { "type": "envio", "address": "Av. Rivadavia 1234, CABA" },
    "items": [
      { "sku": "SKU-0001", "name": "Ibuprofeno 600 mg x 30", "qty": 1, "unit_price": 8500.00 }
    ],
    "total": 8500.00,
    "currency": "ARS",
    "payment": {
      "provider": "mercadopago",
      "payment_id": "123456789",
      "method": "credit_card"
    }
  }
}`
  ),
  spacer(),
  p("Respuesta esperada: cualquier código 2xx. Opcionalmente el receptor devuelve el identificador que creó en el CRM (`{\"crm_id\": \"...\"}`) y Remedia lo guarda asociado al teléfono para incluirlo en los eventos siguientes."),

  // ───────────────────────────── 5
  h1("5. Mapeo de datos a objetos del CRM"),
  p("La mayoría de los CRM comparten el mismo modelo: Contacto (o Lead), Empresa, Oportunidad (Deal), Ticket (Caso) y Actividad (Nota, Tarea, Llamada). El mapeo genérico es el siguiente; el Anexo A lo particulariza para los CRM más frecuentes."),
  table(
    ["Dato de Remedia", "Contacto / Lead", "Oportunidad / Deal", "Ticket / Caso", "Actividad"],
    [
      ["Teléfono E.164", "Campo teléfono móvil (clave de búsqueda)", "Asociación al contacto", "Asociación al contacto", "Asociación al contacto"],
      ["Nombre y número de socio", "Nombre, apellido, campo personalizado \"N° socio\"", "—", "—", "—"],
      ["Origen del contacto", "Campo \"Origen\" = WhatsApp Remedia", "Fuente = WhatsApp", "Canal = WhatsApp", "Tipo = WhatsApp"],
      ["Intención / producto buscado", "Última intención (campo personalizado)", "Nombre del deal, línea de producto", "—", "Nota con el resumen"],
      ["Cotización de receta", "—", "Monto, etapa \"Cotizado\"", "—", "Nota"],
      ["Pago aprobado", "Fecha de última compra", "Etapa \"Ganado\", fecha de cierre, monto", "—", "Actividad de venta"],
      ["Pedido (número, código de retiro, entrega)", "—", "Campos personalizados en el deal, o objeto Orden si el CRM lo tiene", "—", "Tarea \"Preparar pedido\" para el operador"],
      ["Derivación (motivo)", "—", "—", "Asunto, prioridad según motivo, pipeline de soporte", "Nota con el resumen de la conversación"],
      ["Sentimiento negativo", "Marca \"Atención prioritaria\"", "—", "Prioridad alta", "Alerta al responsable"],
      ["Baja de comunicaciones", "Suscripción de marketing = No; fecha de baja", "—", "—", "Nota"],
      ["Transcripción de la conversación", "—", "—", "Adjunto o nota (opcional, según política de datos)", "Nota"],
      ["Imagen de receta / datos de salud", "No se envía por defecto", "No", "Enlace seguro al panel de Remedia (requiere usuario), no la imagen", "No"],
    ],
    [2000, 1900, 1800, 1700, 1626]
  ),
  spacer(),
  p("**Reglas de deduplicación.** La búsqueda del contacto se hace por teléfono normalizado. Si el CRM tiene varios contactos con el mismo teléfono, se toma el más reciente y se registra una advertencia para revisión manual. Si el Cliente prefiere que Remedia nunca cree contactos (sólo actualice existentes), se configura así."),

  // ───────────────────────────── 6
  h1("6. Escenarios de uso"),
  h2("6.1 Captura de leads desde WhatsApp"),
  p("Un cliente nuevo escribe preguntando por un producto. Remedia publica `conversacion.iniciada` y `conversacion.intencion`; el CRM crea el lead con origen WhatsApp y el producto de interés. Si compra, el lead pasa a cliente con el deal ganado. Comercial ve en su pipeline todo lo que entra por WhatsApp sin que nadie lo cargue."),
  h2("6.2 Derivaciones como tickets"),
  p("Cada receta, reclamo o consulta que el asistente deriva se convierte en un ticket con el motivo, el resumen y un enlace a la conversación en el panel de Remedia. Cuando el operador la toma y la cierra, el ticket se actualiza. Los tiempos de respuesta quedan medidos en el CRM con el resto de los canales."),
  h2("6.3 Ventas y pedidos"),
  p("Cada pago aprobado genera un deal ganado con monto, medio de pago y los ítems, y una orden (o campos del deal) con el estado del pedido. Administración concilia con la pasarela usando el identificador de pago que viaja en el evento."),
  h2("6.4 Campañas y notificaciones desde el CRM"),
  p("El CRM segmenta (por ejemplo, socios con cuota vencida o clientes que compraron un producto de reposición mensual) y llama a la API de acción de Remedia para enviar una plantilla aprobada de WhatsApp. Remedia verifica el opt-in, envía, y devuelve el estado de entrega. Si el cliente responde, la conversación sigue con el asistente y los eventos vuelven al CRM."),
  h2("6.5 Padrón de socios sincronizado"),
  p("Para mutuales y farmacias con padrón, el CRM (o el sistema de socios) mantiene el padrón actualizado en Remedia con llamadas incrementales, en lugar de cargar un archivo. El descuento de socio se aplica siempre con datos vigentes."),
  h2("6.6 Ficha 360 en el CRM"),
  p("Un panel embebido o un bloque de la ficha del contacto consulta la API de lectura y muestra la última conversación, los pedidos abiertos y el estado de derivación, sin salir del CRM."),

  // ───────────────────────────── 7
  h1("7. Seguridad de la integración"),
  table(
    ["Aspecto", "Medida"],
    [
      ["Autenticación de la API", "Clave de API por integración con alcance definido (lectura, acción, padrón), rotable desde el panel sin corte de servicio. Se envía en cabecera, nunca en la URL."],
      ["Integridad de los webhooks", "Firma HMAC-SHA256 con secreto por destino; marca de tiempo para rechazar repeticiones antiguas."],
      ["Transporte", "TLS 1.2 o superior obligatorio en ambos sentidos. Se puede restringir por lista de IP de origen."],
      ["Mínimo privilegio", "Cada clave accede sólo a los recursos del comercio que la emitió. Las claves de sólo lectura no pueden enviar mensajes."],
      ["Datos sensibles", "Excluidos de los eventos por defecto. Su inclusión requiere decisión escrita del Cliente y se registra en el contrato de encargo de tratamiento (ver documento de adecuación a la Ley 25.326)."],
      ["Trazabilidad", "Cada entrega de webhook y cada llamada a la API quedan registradas (fecha, destino, resultado, reintentos) y visibles en el panel por 90 días."],
      ["Consentimiento para mensajes proactivos", "La API de envío rechaza plantillas a teléfonos sin opt-in o con baja registrada, y lo informa al CRM."],
      ["Aislamiento", "La integración corre dentro del despliegue del Cliente; no hay un intermediario compartido con otros comercios."],
    ],
    [2600, 6426]
  ),

  // ───────────────────────────── 8
  h1("8. Plan de implementación"),
  h2("8.1 Fases y cronograma estándar"),
  table(
    ["Fase", "Duración", "Actividades", "Entregable"],
    [
      ["0. Discovery", "1 semana", "Relevamiento del CRM (versión, API, campos personalizados, pipelines), casos de uso priorizados, eventos a suscribir, política de datos sensibles, cuestionario del Anexo B", "Documento de diseño de integración aprobado"],
      ["1. Configuración", "1 semana", "Alta de destinos de webhook y claves; creación de campos personalizados en el CRM; usuario de integración en el CRM; entorno sandbox", "Entornos conectados"],
      ["2. Mapeo y desarrollo", "1 a 3 semanas según CRM", "Implementación del receptor (conector directo, middleware o servicio del Cliente); reglas de deduplicación; manejo de errores", "Conector en sandbox"],
      ["3. Pruebas", "1 semana", "Casos de prueba por evento con datos sintéticos; prueba de reintentos y de caída del CRM; validación de campos por Comercial y Sistemas", "Acta de pruebas"],
      ["4. Puesta en producción", "1 día", "Activación de webhooks en producción; carga inicial opcional de contactos históricos; monitoreo intensivo 72 h", "Acta de go-live"],
      ["5. Estabilización", "2 semanas", "Revisión de entregas fallidas, ajustes de mapeo, capacitación a usuarios del CRM", "Informe de cierre"],
    ],
    [1700, 1300, 3800, 2226]
  ),
  spacer(),
  p("Duración total típica: **4 a 7 semanas**, en paralelo con la implementación del asistente. Una integración por middleware con un CRM que tenga conector nativo (por ejemplo HubSpot en n8n o Make) se ubica en el extremo corto; un CRM propio con API a medida, en el largo."),
  h2("8.2 Roles y responsabilidades"),
  table(
    ["Actividad", "Proveedor", "Cliente – Sistemas", "Cliente – Comercial / Operaciones"],
    [
      ["Definir casos de uso y eventos", "Propone", "Valida", "Decide"],
      ["Diseño del mapeo de campos", "Ejecuta", "Valida", "Aprueba"],
      ["Campos personalizados y usuario de integración en el CRM", "Asiste", "Ejecuta", "—"],
      ["Publicación de eventos y API de Remedia", "Ejecuta", "—", "—"],
      ["Receptor / conector en el CRM o middleware", "Ejecuta (opción llave en mano) o asiste (opción Cliente)", "Ejecuta (opción Cliente)", "—"],
      ["Pruebas funcionales", "Ejecuta", "Ejecuta", "Valida"],
      ["Plantillas de WhatsApp para campañas", "Gestiona la aprobación en Meta", "—", "Redacta"],
      ["Política de datos sensibles en el CRM", "Recomienda", "Valida controles", "Decide (con Legales)"],
      ["Monitoreo post go-live", "Ejecuta", "Recibe alertas", "—"],
    ],
    [3000, 2100, 2000, 1926]
  ),
  h2("8.3 Requisitos del lado del Cliente"),
  ...bullets([
    "Acceso a un entorno de pruebas (sandbox) del CRM, o a un espacio de trabajo de prueba si el CRM no lo ofrece.",
    "Un usuario de integración en el CRM con permisos para crear y actualizar contactos, deals, tickets y actividades, y su clave de API.",
    "Un referente de Sistemas y uno de Comercial con capacidad de decisión sobre campos y pipelines.",
    "Si se usa middleware: cuenta y acceso a la plataforma de integración.",
    "Definición de la política de datos sensibles (por defecto, no se replican).",
  ]),
  h2("8.4 Modalidades de contratación"),
  table(
    ["Modalidad", "Descripción", "Recomendada cuando"],
    [
      ["Llave en mano", "El Proveedor construye y mantiene el conector con el CRM del Cliente", "El Cliente no tiene equipo de integración o quiere un único responsable"],
      ["Middleware del Cliente", "El Proveedor publica los eventos y la API; el Cliente arma los flujos en su plataforma de integración", "El Cliente ya opera n8n, Make, Zapier o Power Automate y quiere autonomía"],
      ["Desarrollo del Cliente", "El equipo del Cliente consume la API y los webhooks con la documentación técnica", "CRM propio o políticas que exigen que el código de integración sea del Cliente"],
    ],
    [2000, 3900, 3126]
  ),
  h2("8.5 Soporte y nivel de servicio"),
  ...bullets([
    "**Disponibilidad de la API y de la publicación de eventos:** objetivo mensual del 99,5 %, medido en el despliegue del Cliente.",
    "**Latencia de publicación:** los eventos se emiten dentro de los 5 segundos de ocurrido el hecho, en condiciones normales.",
    "**Entregas fallidas:** reintentos automáticos por 24 horas y reenvío manual desde el panel; alerta al Cliente si un destino acumula fallos por más de 30 minutos.",
    "**Cambios en la API:** versionada (`/api/v1`); las versiones anteriores se mantienen por 12 meses tras el aviso de baja. Los cambios compatibles (campos nuevos) no cambian de versión.",
    "**Soporte:** canal de soporte técnico en horario hábil, con escalamiento para incidentes que afecten ventas.",
  ]),

  // ───────────────────────────── ANEXOS
  pageBreak(),
  h1("Anexo A — Notas por CRM"),
  table(
    ["CRM", "Conexión recomendada", "Particularidades del mapeo"],
    [
      ["HubSpot", "Conector directo por API (Private App) o vía n8n/Make (conector nativo)", "Contacto por `phone`; deal en pipeline \"WhatsApp\"; ticket en pipeline de soporte; propiedades personalizadas para número de socio y código de retiro. Suscripción de marketing gestionada por la propiedad de estado legal."],
      ["Salesforce", "API REST con usuario de integración (Connected App) o vía middleware", "Lead vs. Contact según regla del Cliente; Opportunity con Products; Case para derivaciones; Task para actividades. Campos personalizados `Remedia_Order_Id__c`, `Remedia_Member_Id__c`. Manejo de límites de API por volumen."],
      ["Zoho CRM", "API v2 con OAuth o vía Zoho Flow", "Leads/Contacts, Deals, Cases; campos personalizados; Zoho Flow simplifica el receptor de webhooks."],
      ["Pipedrive", "API con token o vía middleware", "Person por teléfono; Deal en pipeline WhatsApp; no tiene tickets nativos: las derivaciones se mapean a Activities o a un pipeline de soporte."],
      ["Microsoft Dynamics 365", "Dataverse Web API con app registrada, o Power Automate", "Contact, Opportunity, Case, Activity. Power Automate como receptor de webhooks es la vía más rápida en entornos Microsoft."],
      ["Odoo", "XML-RPC/JSON-RPC con usuario de integración", "res.partner por teléfono; crm.lead; helpdesk.ticket si el módulo está instalado; sale.order para pedidos si el Cliente quiere que Odoo sea el ERP de ventas."],
      ["CRM propio / a medida", "Webhooks al endpoint del Cliente y consumo de la API de Remedia", "El Cliente define el receptor; el Proveedor entrega la especificación OpenAPI y una colección de pruebas."],
    ],
    [1700, 2900, 4426]
  ),

  pageBreak(),
  h1("Anexo B — Cuestionario de discovery"),
  p("Preguntas que se responden en la primera reunión con Sistemas y Comercial del Cliente. Con estas respuestas el Proveedor cierra el diseño de integración en la semana 1."),
  h3("Sobre el CRM"),
  ...numbered([
    "¿Qué CRM usan, en qué versión o edición, y desde cuándo?",
    "¿Tiene API disponible en su plan? ¿Hay límites de llamadas que debamos considerar?",
    "¿Existe un entorno de pruebas?",
    "¿Quién administra el CRM y puede crear campos, pipelines y usuarios de integración?",
    "¿Ya usan alguna plataforma de integración (n8n, Make, Zapier, Power Automate)?",
  ]),
  h3("Sobre los datos"),
  ...numbered([
    "¿Cómo identifican hoy a un cliente en el CRM? ¿El teléfono está cargado y normalizado?",
    "¿Qué pasa si un teléfono no existe en el CRM: crear lead, crear contacto o no crear nada?",
    "¿Qué campos personalizados necesitan (número de socio, obra social, sucursal habitual)?",
    "¿Quieren la transcripción de la conversación en el CRM o sólo el resumen?",
    "¿Qué política adoptan para datos de salud en el CRM? (Recomendación: no replicar.)",
  ]),
  h3("Sobre los procesos"),
  ...numbered([
    "¿Qué eventos quieren en el CRM en la primera etapa? (Recomendación: contacto, derivación, pago y pedido.)",
    "¿Cómo se reparten las derivaciones entre operadores? ¿Quieren que el CRM asigne?",
    "¿Van a enviar campañas por WhatsApp desde el CRM? ¿Tienen opt-in registrado hoy?",
    "¿Quién debe recibir alertas de fallos de integración?",
    "¿Hay un ERP o sistema de facturación que también deba recibir los pedidos?",
  ]),

  pageBreak(),
  h1("Anexo C — Resumen de la API de Remedia"),
  p("Especificación resumida; la especificación OpenAPI completa y una colección de pruebas se entregan en la fase de configuración. Base: `https://[dominio-del-cliente]/api/v1`. Autenticación: cabecera `Authorization: Bearer <clave>`."),
  table(
    ["Método y ruta", "Alcance", "Descripción"],
    [
      ["GET /conversaciones", "lectura", "Conversaciones activas y derivadas, con estado, operador y última actividad"],
      ["GET /conversaciones/{telefono}", "lectura", "Estado actual de una conversación"],
      ["GET /conversaciones/{telefono}/historial", "lectura", "Mensajes de la conversación (paginado)"],
      ["GET /pedidos?estado=", "lectura", "Pedidos por estado (pendiente, preparado, retirado)"],
      ["GET /pedidos/{id}", "lectura", "Detalle de un pedido"],
      ["GET /socios/{telefono}", "lectura", "Si el teléfono figura en el padrón y con qué número de socio"],
      ["POST /mensajes/plantilla", "acción", "Envía una plantilla aprobada de WhatsApp a un contacto con opt-in"],
      ["POST /conversaciones/{telefono}/mensaje", "acción", "Mensaje de operador dentro de una conversación derivada"],
      ["POST /conversaciones/{telefono}/derivar", "acción", "Deriva la conversación a operador con motivo"],
      ["POST /conversaciones/{telefono}/cerrar", "acción", "Cierra la conversación"],
      ["PUT /socios/{numero}", "padrón", "Alta o modificación de un socio"],
      ["DELETE /socios/{numero}", "padrón", "Baja de un socio"],
      ["GET /webhooks/entregas?estado=fallida", "lectura", "Entregas de webhook fallidas, para reenvío"],
      ["POST /webhooks/entregas/{id}/reenviar", "acción", "Reenvía una entrega"],
    ],
    [3400, 1200, 4426]
  ),
  spacer(),
  h3("Ejemplo: envío de plantilla desde el CRM"),
  codeBlock(
`POST /api/v1/mensajes/plantilla
Authorization: Bearer rmd_live_...
Content-Type: application/json

{
  "phone": "+5491155551234",
  "template": "pedido_listo_v2",
  "language": "es_AR",
  "variables": { "1": "María", "2": "482913" },
  "crm_reference": "hs-0034567"
}

→ 202 Accepted
{ "message_id": "wamid.HBgN...", "status": "sent" }

→ 409 Conflict (sin opt-in o con baja registrada)
{ "error": "no_opt_in", "phone": "+5491155551234" }`
  ),

  pageBreak(),
  h1("Anexo D — Preguntas frecuentes de Sistemas"),
  h3("¿Necesitamos abrir puertos o instalar algo en nuestra red?"),
  p("No. Remedia envía webhooks a una URL pública del CRM o del middleware, y el CRM consume la API de Remedia por HTTPS. Si el CRM es interno y no expone URL pública, el middleware o un servicio de relay en la nube del Cliente hace de puente."),
  h3("¿Qué pasa si cambiamos de CRM?"),
  p("Los eventos y la API son los mismos; se reemplaza el conector. Con la modalidad de middleware el cambio es un flujo nuevo en la plataforma de integración."),
  h3("¿Podemos empezar sin integración y agregarla después?"),
  p("Sí. El asistente opera de forma autónoma con su panel. La integración se activa por configuración y puede incluir una carga inicial de los contactos y pedidos históricos del período que el Cliente indique."),
  h3("¿Cómo evitamos que WhatsApp sea un canal \"a ciegas\" para Comercial?"),
  p("Con los eventos de contacto, intención, derivación y pago en el CRM, Comercial ve el embudo de WhatsApp con las mismas métricas que los demás canales: leads generados, tasa de conversión, ticket promedio y tiempos de atención."),
  h3("¿La integración afecta la velocidad de respuesta del bot?"),
  p("No. La publicación de eventos es asincrónica: el asistente responde al cliente y el evento se despacha por detrás. Un CRM lento o caído no demora la conversación."),
];

(async () => {
  const doc = buildDoc({
    title: "Integración con CRM — Remedia",
    shortTitle: "Remedia · Integración con el CRM del Cliente",
    children,
  });
  await save(doc, OUT);
})();
