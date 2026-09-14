const L = require("./lib");
const { p, small, h1, h2, h3, bullets, numbered, table, callout, spacer, pageBreak, codeBlock, cover, buildDoc, toc, save } = L;

const OUT = process.argv[2];

const W = L.CONTENT_W;

const children = [
  ...cover({
    title: "Adecuación a la Ley 25.326 de Protección de Datos Personales",
    subtitle: "Modelo de cumplimiento del asistente de WhatsApp Remedia para su implementación en [CLIENTE]",
    meta: [
      ["Documento", "Respuesta a objeción de implementación — Privacidad y datos personales"],
      ["Producto", "Remedia — asistente conversacional de WhatsApp (verticales farmacia y mutual)"],
      ["Encargado del tratamiento", "[PROVEEDOR]"],
      ["Cliente", "[CLIENTE] (responsable de la base de datos)"],
      ["Versión", "1.0 — septiembre de 2026"],
      ["Clasificación", "Confidencial — uso comercial y de implementación"],
    ],
  }),
  ...toc(),

  // ───────────────────────────── 1
  h1("1. Resumen ejecutivo"),
  p("Este documento responde a la objeción más frecuente en la evaluación de Remedia por parte de áreas de Legales, Compliance y Seguridad de la Información: **cómo un asistente automatizado de WhatsApp, que procesa datos de salud, se acopla a la Ley 25.326 y a las exigencias de la Agencia de Acceso a la Información Pública (AAIP).**"),
  callout("Respuesta corta a la objeción", [
    "**1. Los datos siguen siendo del Cliente.** [CLIENTE] es el responsable de la base de datos; [PROVEEDOR] actúa como encargado del tratamiento en los términos del artículo 25 de la Ley 25.326. El Proveedor no usa los datos para ningún fin propio y los devuelve o destruye al finalizar el contrato.",
    "**2. Cada dato tiene una finalidad concreta y un plazo de vida.** El asistente sólo recolecta lo que necesita para atender la consulta, cotizar y entregar un pedido. Las sesiones expiran automáticamente, las imágenes de recetas se eliminan a los 7 días y el historial se depura según la política de retención acordada con el Cliente.",
    "**3. Los datos de salud tienen un circuito propio.** Una receta médica nunca es resuelta por el bot: se deriva a un operador humano del Cliente, que es quien tiene la habilitación legal para tratarla (artículo 8). El modelo de lenguaje no recibe DNI, domicilio ni diagnóstico en la conversación.",
    "**4. El titular ejerce sus derechos por el mismo canal.** Aviso de privacidad en el primer contacto, baja inmediata con una palabra, y procedimiento de acceso, rectificación y supresión con los plazos legales.",
    "**5. Seguridad documentada y verificable.** Cifrado en tránsito y en reposo, acceso al panel con usuarios individuales y roles, registro de auditoría, subencargados listados y cubiertos por cláusulas contractuales para transferencia internacional.",
  ]),
  spacer(),
  p("El resto del documento desarrolla cada punto con el detalle que requieren los equipos de Legales, Compliance y Seguridad, e incluye los anexos operativos (aviso de privacidad, contrato de encargo, matriz de subencargados y plan de adecuación con evidencias)."),

  // ───────────────────────────── 2
  h1("2. Marco normativo aplicable"),
  p("El tratamiento de datos que realiza Remedia se encuadra en el siguiente marco. Las referencias deben ser validadas por el área legal de cada parte; el objetivo aquí es mostrar que cada obligación tiene una medida concreta asociada en el producto o en el contrato."),
  table(
    ["Norma", "Qué exige", "Cómo lo cubre la implementación"],
    [
      ["Ley 25.326 (Protección de Datos Personales) y Decreto reglamentario 1558/2001", "Principios de licitud, finalidad, calidad, consentimiento, información, seguridad y confidencialidad; derechos de acceso, rectificación y supresión; cesión y transferencia internacional; inscripción de bases; figura del encargado (art. 25).", "Secciones 3 a 11 de este documento."],
      ["Art. 7 y 8 — datos sensibles y datos de salud", "Los datos de salud sólo pueden tratarse con consentimiento expreso o por establecimientos sanitarios y profesionales vinculados a las ciencias de la salud, respetando el secreto profesional.", "Circuito de receta con derivación obligatoria a operador del Cliente (sección 6)."],
      ["Resolución AAIP 47/2018 — Medidas de seguridad recomendadas", "Controles sobre recolección, acceso, cambios, respaldo, vulnerabilidades, destrucción, incidentes y entornos de desarrollo.", "Matriz de controles de la sección 9."],
      ["Resolución AAIP 4/2019 — Criterios orientadores", "Interpretación sobre consentimiento, decisiones automatizadas, datos biométricos y disociación.", "Aviso de privacidad y transparencia sobre el uso de un agente automatizado (sección 7)."],
      ["Disposición DNPDP 60-E/2016 — Transferencias internacionales", "Contratos modelo para transferir datos a países sin nivel adecuado de protección (por ejemplo, Estados Unidos).", "Cláusulas incorporadas con cada subencargado (sección 10)."],
      ["Art. 21 — Registro Nacional de Bases de Datos", "Inscripción de las bases de datos por parte del responsable ante la AAIP.", "El Proveedor entrega la descripción técnica de la base para la inscripción del Cliente (sección 11)."],
      ["Ley 26.529 — Derechos del paciente", "Confidencialidad de la información de salud y de la historia clínica.", "El asistente no constituye historia clínica; los datos de receta se tratan como transitorios (sección 6)."],
      ["Ley 27.483 — Convenio 108 del Consejo de Europa", "Argentina adhiere al estándar internacional de protección de datos, base de su reconocimiento de nivel adecuado por la Unión Europea.", "Las medidas de este documento están alineadas con ese estándar."],
    ],
    [2400, 3300, 3326]
  ),
  spacer(),
  small("Nota: existe un proyecto de reforma integral de la Ley 25.326 impulsado por la AAIP. A la fecha de este documento la ley vigente sigue siendo la 25.326; las medidas descritas fueron diseñadas para ser compatibles con el estándar más exigente del proyecto (notificación de incidentes, evaluación de impacto, delegado de protección de datos), de modo que la adecuación no requiera rehacer la implementación si la reforma se aprueba."),

  // ───────────────────────────── 3
  h1("3. Roles y responsabilidades"),
  p("La Ley 25.326 distingue entre quien decide sobre la base de datos y quien presta un servicio sobre ella. Definir esto con claridad al inicio de la implementación evita la mayoría de las discusiones posteriores."),
  table(
    ["Rol", "Quién", "Responsabilidades principales"],
    [
      ["Responsable de la base de datos (art. 2)", "[CLIENTE]", ["Decide la finalidad y los medios del tratamiento.", "Inscribe la base ante la AAIP.", "Es el interlocutor del titular para el ejercicio de derechos.", "Define la política de retención y aprueba la lista de subencargados."]],
      ["Encargado del tratamiento (art. 25)", "[PROVEEDOR]", ["Trata los datos exclusivamente según las instrucciones del Cliente y el contrato.", "Implementa y mantiene las medidas de seguridad.", "Asiste al Cliente en el ejercicio de derechos (ventana de 48 h para localizar y entregar datos).", "Notifica incidentes de seguridad dentro de las 72 h de detectados.", "Devuelve o destruye los datos al finalizar el contrato."]],
      ["Subencargados", "Proveedores de infraestructura, mensajería, inteligencia artificial y pagos (sección 10)", ["Prestan servicios técnicos bajo contrato con el Proveedor.", "Sujetos a las mismas obligaciones de confidencialidad y seguridad.", "Listados y aprobados previamente por el Cliente."]],
      ["Titular del dato", "Cliente final que escribe al WhatsApp del Cliente (socio, paciente, comprador)", ["Recibe información sobre el tratamiento en el primer contacto.", "Puede darse de baja, acceder, rectificar o suprimir sus datos."]],
      ["Operadores del Cliente", "Personal de la farmacia o mutual que atiende derivaciones desde el panel", ["Tratan los datos de salud bajo secreto profesional.", "Acceden con usuario individual y rol asignado; sus acciones quedan auditadas."]],
    ],
    [2200, 2000, 4826]
  ),

  // ───────────────────────────── 4
  h1("4. Descripción del tratamiento"),
  h2("4.1 Qué hace el asistente"),
  p("Remedia atiende por WhatsApp a los clientes de [CLIENTE] con dos modalidades según el vertical contratado:"),
  ...bullets([
    "**Farmacia:** consulta de precios y stock, armado del pedido, cotización de recetas por un operador (con descuento de obra social y de socio), elección de retiro o envío, cobro por Mercado Pago o Payway y aviso con código de retiro.",
    "**Mutual:** información institucional sobre productos y trámites, simuladores de préstamos y plazo fijo, y derivación a una sucursal o a un operador. En este vertical el asistente no vende ni cobra.",
  ]),
  p("En ambos casos el asistente reconoce cuándo no debe resolver una consulta (receta médica, reclamo, cliente molesto, pago manual) y la deriva a una persona del Cliente, que la continúa desde el panel de operaciones."),
  h2("4.2 Flujo de datos"),
  p("El siguiente recorrido describe por dónde pasa cada dato desde que el cliente escribe hasta que el pedido se entrega. Sirve de base para la inscripción de la base de datos y para la evaluación de riesgos del Cliente."),
  ...numbered([
    "**Ingreso del mensaje.** El cliente escribe al número de WhatsApp Business del Cliente. Meta (o el proveedor de conectividad autorizado) entrega el mensaje al servicio de Remedia por un webhook cifrado (HTTPS) y con firma verificada.",
    "**Sesión de conversación.** El servicio guarda el estado de la conversación (últimos mensajes, carrito, etapa del pedido) en una memoria de sesión con vencimiento automático de 25 horas desde el último mensaje.",
    "**Identificación del socio (opcional).** Si el Cliente cargó su padrón, el número de teléfono se cruza contra él para aplicar el descuento y saludar por nombre. El padrón reside en el despliegue del Cliente; al modelo de lenguaje sólo se le entrega nombre y número de socio, nunca DNI ni domicilio.",
    "**Interpretación por inteligencia artificial.** El texto del mensaje y los últimos turnos de la conversación se envían a un proveedor de modelos de lenguaje para clasificar la intención y redactar la respuesta. Los datos se transmiten cifrados y bajo contrato que prohíbe su uso para entrenamiento (sección 10).",
    "**Imágenes y audios.** Una foto se clasifica (receta, credencial, producto, otro). Una receta se guarda de forma temporal (7 días) para que el operador la cotice y se deriva de inmediato; nunca es resuelta por el asistente. Los audios se transcriben y el audio original no se conserva.",
    "**Cobro.** El cliente paga en la página de la pasarela. Los datos de tarjeta se tokenizan en el navegador del cliente contra la pasarela; el servicio de Remedia nunca ve el número de tarjeta.",
    "**Pedido y entrega.** Con el pago aprobado se crea el pedido con un código de retiro. El pedido, con teléfono y dirección de envío si corresponde, es visible para los operadores del Cliente durante el plazo de retención de pedidos.",
    "**Historial y métricas.** Los mensajes y eventos de negocio se conservan en la base de datos del despliegue para el panel de conversaciones y el tablero de métricas, con la retención definida en la sección 5.",
  ]),

  // ───────────────────────────── 5
  h1("5. Inventario de datos personales"),
  p("La tabla siguiente es el inventario que el Cliente necesita para inscribir la base y para responder a un pedido de acceso. Se indica para cada categoría su origen, finalidad, base de licitud, dónde reside y cuánto tiempo se conserva. Los plazos son los valores estándar del despliegue y se ajustan por contrato."),
  table(
    ["Categoría", "Origen", "Finalidad", "Base de licitud", "Retención estándar"],
    [
      ["Número de teléfono (WhatsApp)", "El titular, al escribir", "Identificar la conversación, notificar pedidos y pagos", "Ejecución de la relación con el titular; consentimiento informado en el primer contacto (art. 5)", "Sesión: 25 h. Pedidos: 7 días. Historial: según política del Cliente (valor sugerido 12 meses)"],
      ["Contenido de la conversación (texto y transcripción de audio)", "El titular", "Atender la consulta, cotizar, dar trazabilidad a los operadores", "Ídem", "Ídem historial"],
      ["Nombre, apellido, número de socio", "Padrón del Cliente", "Aplicar descuento de socio y personalizar la atención", "Relación contractual previa entre el Cliente y su socio (art. 5 inc. 2)", "Mientras el Cliente mantenga el padrón cargado"],
      ["DNI y domicilio del padrón", "Padrón del Cliente", "Sólo verificación interna; no se muestran ni se envían al modelo de IA", "Ídem", "Ídem. Se recomienda cargar el padrón sin estas columnas si el Cliente no las necesita"],
      ["Dirección de envío", "El titular, en el chat", "Entregar el pedido a domicilio", "Ejecución del pedido", "Pedido: 7 días. Se enmascara en el historial al vencer el pedido"],
      ["Imagen de receta o credencial de obra social (dato sensible de salud)", "El titular", "Que un operador del Cliente cotice y valide la receta", "Art. 8: tratamiento por establecimiento sanitario / profesional habilitado del Cliente", "7 días, eliminación automática"],
      ["Datos extraídos de la receta (obra social, plan, número de afiliado, medicamento, médico, diagnóstico si figura)", "Lectura automática de la imagen (función opcional, desactivada por defecto)", "Prearmar la cotización para el operador", "Art. 8", "Sesión: 25 h. No se incorpora al historial permanente"],
      ["Datos de pago", "Pasarela (Mercado Pago o Payway)", "Confirmar el cobro y crear el pedido", "Ejecución del pedido", "Se conserva sólo el identificador de pago, marca de tarjeta y primeros 6 dígitos (BIN). Nunca el número completo ni el código de seguridad"],
      ["Datos del operador (nombre de usuario, acciones)", "El Cliente", "Trazabilidad y auditoría de quién atendió, preparó o entregó", "Relación laboral con el Cliente", "Registro de auditoría: 24 meses"],
      ["Métricas de uso (intención, tiempos, sentimiento)", "Generación automática", "Tablero de gestión del Cliente", "Interés legítimo del Cliente en la mejora del servicio; datos disociables", "Se agregan y disocian según la política de retención"],
    ],
    [1900, 1400, 1900, 2000, 1826]
  ),
  spacer(),
  p("**Principio de minimización aplicado.** El asistente pide únicamente los datos indispensables para cada paso: no solicita DNI, correo electrónico ni fecha de nacimiento, y sólo pide una dirección cuando el cliente eligió envío a domicilio. Al modelo de lenguaje se le oculta la información que no necesita para responder: DNI, domicilio, paquete de datos de la receta y referencias a las imágenes."),

  // ───────────────────────────── 6
  h1("6. Tratamiento de datos de salud"),
  p("Es el punto que más atención recibe de Legales y Compliance, y donde el diseño del producto marca la diferencia. La regla es simple: **el asistente reconoce el dato de salud, lo protege y lo entrega a una persona habilitada; nunca lo interpreta ni decide sobre él.**"),
  h2("6.1 Circuito de la receta médica"),
  ...numbered([
    "El cliente envía una foto. Un clasificador de imágenes determina si es una receta, una credencial, un producto u otra cosa.",
    "Si es una receta o credencial, la imagen se guarda en un almacenamiento temporal, accesible sólo desde el panel del Cliente con usuario autenticado, y con eliminación automática a los 7 días.",
    "La conversación se deriva de inmediato a un operador del Cliente. El asistente informa al cliente que una persona continuará la atención y deja de responder de forma automática en esa conversación.",
    "El operador cotiza la receta desde el panel (precio, cobertura de obra social, descuento de socio) y el sistema arma el mensaje de cotización. Ninguna decisión de dispensa la toma el asistente.",
    "Cuando el pedido vence, el dato de receta desaparece de la sesión; en el historial permanente sólo queda la constancia de que hubo una derivación por receta, sin la imagen ni sus datos.",
  ]),
  h2("6.2 Lectura automática de recetas (opcional)"),
  p("El producto incluye una función de lectura estructurada de la receta para acelerar la cotización. Está **desactivada por defecto** y sólo se habilita por decisión escrita del Cliente, porque implica enviar la imagen a un proveedor de inteligencia artificial. Si se habilita, se aplica lo siguiente:"),
  ...bullets([
    "El proveedor de IA opera bajo contrato con retención cero: no conserva las imágenes ni las usa para entrenar modelos.",
    "Los datos extraídos viven sólo en la sesión (25 horas) y no se incorporan al historial permanente.",
    "El operador siempre valida contra la imagen original; la lectura automática es un borrador, nunca una fuente de verdad.",
  ]),
  h2("6.3 Habilitación legal del tratamiento"),
  p("El artículo 8 de la Ley 25.326 permite que los establecimientos sanitarios y los profesionales vinculados a las ciencias de la salud traten datos de salud de sus pacientes, respetando el secreto profesional. En este modelo, quien trata la receta es el farmacéutico u operador del Cliente; [PROVEEDOR] sólo provee la infraestructura como encargado. Para los mutuales que no son establecimientos sanitarios, el asistente no procesa recetas: cualquier imagen se deriva a un operador sin ser almacenada."),

  // ───────────────────────────── 7
  h1("7. Información, consentimiento y baja"),
  h2("7.1 Aviso en el primer contacto"),
  p("En el primer mensaje de cada conversación nueva (o cuando pasaron más de 30 días desde la última), el asistente se presenta, informa que es un agente automatizado, indica quién es el responsable de los datos y cómo darse de baja o ejercer derechos. El texto es configurable por el Cliente desde el panel; el modelo del Anexo A cumple con el artículo 6 de la ley (finalidad, destinatarios, carácter obligatorio u optativo, consecuencias de no proveer los datos y derechos del titular)."),
  h2("7.2 Consentimiento"),
  ...bullets([
    "**Para atender la consulta y el pedido:** el titular inicia la conversación de forma voluntaria y recibe el aviso de privacidad. Continuar la conversación tras el aviso constituye consentimiento informado para esa finalidad. Además, buena parte del tratamiento se apoya en la relación previa (socio o cliente del Cliente) y en la ejecución del pedido, supuestos previstos en el artículo 5 inciso 2.",
    "**Para datos de salud:** el envío de la receta es un acto voluntario del titular con la finalidad explícita de obtener el medicamento, y el tratamiento posterior lo realiza personal sanitario del Cliente (art. 8). Si el Cliente quiere reforzar la posición, el asistente puede pedir una confirmación expresa (\"Sí, autorizo\") antes de guardar la imagen; es una opción de configuración.",
    "**Para mensajes proactivos (promociones, recordatorios):** requieren opt-in explícito y registrado, y respetan las políticas de plantillas de WhatsApp Business. El artículo 27 de la ley garantiza al titular el derecho a que se lo retire de cualquier base con fines publicitarios; el asistente lo cumple con la baja inmediata.",
  ]),
  h2("7.3 Baja inmediata"),
  p("El titular puede escribir **BAJA** (o \"no quiero recibir más mensajes\") en cualquier momento. El asistente confirma la baja, marca el número como excluido de todo mensaje proactivo y cierra la conversación. La marca de exclusión se conserva aun cuando el resto de los datos se depure, para no volver a contactarlo por error."),
  h2("7.4 Transparencia sobre el uso de inteligencia artificial"),
  p("El asistente se identifica como agente automatizado en el aviso inicial y responde con sinceridad cuando el cliente pregunta si está hablando con un bot. Toda decisión con efecto para el titular (validación de una receta, resolución de un reclamo, reintegro) la toma una persona del Cliente, en línea con los criterios de la AAIP sobre decisiones automatizadas."),

  // ───────────────────────────── 8
  h1("8. Derechos de los titulares"),
  p("La ley otorga a los titulares los derechos de acceso (art. 14), rectificación, actualización y supresión (art. 16). El Cliente es quien responde al titular; el Proveedor le entrega las herramientas y la información en los plazos necesarios para cumplir."),
  table(
    ["Derecho", "Plazo legal", "Cómo se resuelve", "Herramienta"],
    [
      ["Acceso", "10 días corridos desde la solicitud", "El operador exporta el historial y los pedidos del número desde el panel y lo entrega al titular por el canal que éste indique", "Panel: ficha del contacto y exportación"],
      ["Rectificación / actualización", "5 días hábiles", "Datos del padrón: el Cliente corrige la fuente y recarga. Dirección de envío: se corrige en el pedido", "Panel: pedidos y padrón"],
      ["Supresión", "5 días hábiles", "El operador ejecuta la supresión del número: borra sesión, pedidos vencidos, imágenes e historial; conserva sólo lo que una obligación legal exija (por ejemplo, comprobantes de pago) y la marca de exclusión", "Panel: acción \"Suprimir titular\" con registro de auditoría"],
      ["Baja de comunicaciones (art. 27)", "Inmediato", "Palabra BAJA en el chat o pedido al operador", "Automático"],
      ["Constancia", "—", "Cada ejercicio de derecho queda registrado (fecha, operador, acción) para responder ante la AAIP", "Registro de auditoría"],
    ],
    [1900, 1600, 3400, 2126]
  ),
  spacer(),
  p("Canal de recepción de solicitudes: el mismo WhatsApp, el correo de contacto del Cliente y el que el Cliente designe en su aviso de privacidad. El Proveedor responde a los pedidos internos del Cliente en 48 horas hábiles, lo que deja margen para cumplir el plazo legal."),

  // ───────────────────────────── 9
  h1("9. Medidas de seguridad"),
  p("El artículo 9 obliga a adoptar las medidas técnicas y organizativas necesarias para garantizar la seguridad y confidencialidad de los datos. La Resolución AAIP 47/2018 detalla las medidas recomendadas; la tabla siguiente muestra cómo se cubre cada una en el despliegue de Remedia. La columna \"Evidencia\" indica qué se entrega al Cliente para verificarlo."),
  table(
    ["Dominio (Res. 47/2018)", "Medida implementada", "Evidencia entregable"],
    [
      ["Recolección de datos", "Minimización por diseño; datos de salud en circuito separado; aviso de privacidad en el primer contacto", "Este documento; capturas del flujo; configuración del aviso"],
      ["Control de acceso", ["Panel con usuarios individuales, contraseña con política de complejidad y roles (administrador, dueño, operador).", "Principio de mínimo privilegio: el operador ve sólo las conversaciones y pedidos de su comercio.", "Sesiones con expiración; sin claves compartidas."], "Listado de usuarios y roles; captura de la pantalla de administración"],
      ["Control de cambios", "Todo cambio de código pasa por control de versiones, revisión y pruebas automatizadas antes del despliegue; las migraciones de base de datos son versionadas", "Política de despliegue; registro de versiones"],
      ["Respaldo y recuperación", "Copias diarias de la base de datos con retención de 30 días, cifradas; restauración probada trimestralmente", "Informe de prueba de restauración"],
      ["Gestión de vulnerabilidades", "Dependencias monitoreadas; parches críticos aplicados dentro de los 7 días; revisión de seguridad de la aplicación previa al go-live y anual", "Informe de revisión de seguridad"],
      ["Destrucción de información", "Vencimientos automáticos (sesiones, imágenes, pedidos); job de depuración del historial según política; supresión por titular; borrado seguro al fin del contrato con certificado", "Política de retención firmada; certificado de destrucción"],
      ["Gestión de incidentes", "Procedimiento con detección, contención, análisis y notificación al Cliente dentro de las 72 h; registro de incidentes; el Cliente decide la comunicación a titulares y a la AAIP", "Procedimiento de incidentes; plantilla de notificación"],
      ["Entornos de desarrollo", "Entornos de prueba separados de producción, sin datos reales de titulares (datos sintéticos); simulador de conversaciones sólo en entorno de prueba", "Descripción de entornos"],
      ["Cifrado en tránsito", "TLS 1.2+ en todas las comunicaciones: WhatsApp, panel, pasarelas, proveedores de IA, base de datos y caché", "Configuración de infraestructura"],
      ["Cifrado en reposo", "Discos de base de datos y caché cifrados por el proveedor de nube; imágenes de recetas y padrón cifrados adicionalmente a nivel de aplicación con clave gestionada por el Proveedor y rotable a pedido del Cliente", "Descripción técnica; procedimiento de rotación de claves"],
      ["Registros y auditoría", "Registro de acciones de operadores (quién vio, tomó, cotizó, preparó, entregó, suprimió); registros técnicos sin contenido de mensajes ni teléfonos completos (se conservan los últimos 4 dígitos); retención de 24 meses", "Muestra del registro de auditoría"],
      ["Aislamiento entre clientes", "Cada Cliente opera en un despliegue propio con base de datos, caché y credenciales separadas. No existe una base compartida entre comercios", "Diagrama de despliegue"],
      ["Personal", "Acuerdos de confidencialidad con todo el personal del Proveedor con acceso a producción; acceso nominal y revocable; capacitación anual en protección de datos", "Modelo de acuerdo de confidencialidad"],
    ],
    [2100, 4400, 2526]
  ),

  // ───────────────────────────── 10
  h1("10. Subencargados y transferencias internacionales"),
  p("Para prestar el servicio, el Proveedor se apoya en proveedores tecnológicos. Varios están radicados en Estados Unidos, país que la AAIP no considera con nivel adecuado de protección, por lo que la transferencia se ampara en los contratos modelo de la Disposición 60-E/2016 (o en el consentimiento informado del titular, como salvaguarda adicional). La lista es cerrada: incorporar un subencargado nuevo requiere aviso previo al Cliente con 30 días y derecho de objeción."),
  table(
    ["Subencargado", "Función", "Datos que recibe", "País", "Salvaguarda"],
    [
      ["Meta Platforms (WhatsApp Business Platform)", "Canal de mensajería", "Teléfono, contenido de mensajes y archivos", "EE. UU.", "Términos de WhatsApp Business y cláusulas de transferencia; cifrado de extremo a extremo entre el cliente y Meta"],
      ["Proveedor de conectividad a WhatsApp (opcional, cuando el Cliente lo elija)", "Conexión del número sin trámite directo en Meta", "Ídem Meta", "Según proveedor", "Contrato de encargo y cláusulas de transferencia"],
      ["Anthropic", "Modelo de lenguaje principal (interpretación y redacción); clasificación de imágenes", "Últimos turnos de la conversación, nombre y número de socio; imágenes sólo si el Cliente habilita la lectura de recetas", "EE. UU.", "Términos comerciales de API: no entrena con los datos del Cliente; retención cero configurable; cláusulas 60-E/2016"],
      ["OpenAI", "Modelo de respaldo si el principal no responde; búsqueda semántica del catálogo", "Ídem Anthropic; catálogo de productos (sin datos personales)", "EE. UU.", "Ídem"],
      ["Groq", "Transcripción de audios de voz", "Audio del mensaje (no se conserva)", "EE. UU.", "Términos de API sin retención; cláusulas 60-E/2016"],
      ["Mercado Pago", "Cobro por link de pago", "Ítem, monto y una referencia interna del pedido", "Argentina", "Términos de Mercado Pago Developers; la referencia se envía disociada del teléfono"],
      ["Payway / Prisma Medios de Pago", "Cobro con tarjeta en página propia", "Token de tarjeta generado en el navegador, monto, identificación del dispositivo", "Argentina", "Contrato comercial; certificación PCI DSS de la pasarela"],
      ["Proveedor de nube (Railway u otro acordado)", "Alojamiento de la aplicación, base de datos, caché, almacenamiento y registros", "Todos los datos del despliegue, cifrados en reposo", "EE. UU. (región configurable)", "Términos de servicio y cláusulas de transferencia; posibilidad de alojar en proveedor con región en Sudamérica a pedido del Cliente"],
    ],
    [1900, 1600, 2100, 1100, 2326]
  ),
  spacer(),
  p("**Alternativa de alojamiento local.** Para Clientes cuya política interna exige residencia de datos en Argentina o en la región, el despliegue puede realizarse en un proveedor con región en San Pablo o en infraestructura del propio Cliente. Los proveedores de IA y de mensajería seguirán siendo internacionales; en ese caso la transferencia se limita al contenido conversacional, cubierto por las cláusulas contractuales."),

  // ───────────────────────────── 11
  h1("11. Inscripción de la base de datos y documentación contractual"),
  h2("11.1 Registro Nacional de Bases de Datos"),
  p("El artículo 21 obliga al responsable a inscribir sus bases de datos ante la AAIP. Es una obligación del Cliente, y en la práctica muchos ya tienen inscripta la base de socios o clientes. El Proveedor entrega, en la semana 1 de la implementación, la descripción técnica necesaria para incorporar el tratamiento por WhatsApp a la inscripción existente o para una inscripción nueva: finalidad, categorías de datos, ubicación, medidas de seguridad, cesiones y transferencias internacionales."),
  h2("11.2 Contrato de encargo de tratamiento"),
  p("El artículo 25 exige que la prestación de servicios de tratamiento se regule por contrato. El Anexo B contiene las cláusulas mínimas que el Proveedor incorpora al contrato de servicio; el Cliente puede reemplazarlas por su propio modelo siempre que mantenga el mismo contenido."),
  h2("11.3 Evaluación de impacto"),
  p("Aunque la ley vigente no la exige, el Proveedor entrega un informe de evaluación de impacto en la privacidad del tratamiento (riesgos, probabilidad, medidas) como parte del kit de implementación. Facilita la aprobación interna en Clientes con áreas de Compliance y anticipa la exigencia del proyecto de reforma."),

  // ───────────────────────────── 12
  h1("12. Plan de adecuación e hitos de la implementación"),
  p("La adecuación no es un documento sino un conjunto de configuraciones, controles y entregables que se completan antes del inicio en producción. El cronograma estándar es el siguiente; cada hito tiene un responsable y una evidencia que se adjunta al acta de aceptación."),
  table(
    ["Semana", "Hito", "Responsable", "Evidencia"],
    [
      ["1", "Definición de roles, política de retención y lista de subencargados aprobada. Entrega de la descripción técnica para la inscripción ante la AAIP", "Cliente + Proveedor", "Acta de kick-off; política firmada"],
      ["1", "Firma del contrato de encargo (Anexo B) y acuerdos de confidencialidad", "Legales de ambas partes", "Contrato firmado"],
      ["2", "Configuración del aviso de privacidad, palabra de baja y textos de derivación", "Proveedor (con textos aprobados por el Cliente)", "Capturas del flujo"],
      ["2", "Alta de usuarios y roles del panel; desactivación de accesos genéricos; registro de auditoría activo", "Proveedor", "Listado de usuarios; muestra del registro"],
      ["3", "Cifrado en reposo de imágenes y padrón; retención automática del historial; acción de supresión por titular", "Proveedor", "Descripción técnica; prueba de supresión"],
      ["3", "Revisión de seguridad de la aplicación (accesos, endpoints, configuración de webhooks)", "Proveedor", "Informe de revisión con hallazgos cerrados"],
      ["4", "Prueba integral del ejercicio de derechos (acceso, rectificación, supresión, baja) con datos sintéticos", "Cliente + Proveedor", "Acta de prueba"],
      ["4", "Simulacro de incidente y validación del procedimiento de notificación", "Proveedor", "Acta del simulacro"],
      ["4", "Capacitación de operadores en tratamiento de datos de salud y uso del panel", "Proveedor", "Registro de asistencia; material"],
      ["Go-live", "Acta de aceptación con el checklist de cumplimiento (Anexo D) completo", "Cliente", "Acta firmada"],
      ["Continuo", "Revisión anual de medidas, subencargados y política de retención; prueba trimestral de restauración", "Proveedor", "Informes periódicos"],
    ],
    [900, 4000, 1900, 2226]
  ),

  // ───────────────────────────── ANEXOS
  pageBreak(),
  h1("Anexo A — Modelo de aviso de privacidad para el primer contacto"),
  p("Texto sugerido para el primer mensaje de cada conversación nueva. Se configura desde el panel y el Cliente puede adaptarlo; se recomienda mantener los cinco elementos del artículo 6 (responsable, finalidad, destinatarios, carácter de las respuestas y derechos)."),
  codeBlock(
`Hola 👋 Soy el asistente virtual de [CLIENTE]. Soy un sistema automatizado;
si en algún momento preferís hablar con una persona, escribí "operador".

Para atenderte usamos tu número de WhatsApp y lo que nos escribas en este
chat. [CLIENTE] es el responsable de esos datos y los usa sólo para
responder tu consulta, armar tu pedido y avisarte cuando esté listo.
Podés escribir BAJA cuando quieras para que no te contactemos más, y
pedir acceso, corrección o eliminación de tus datos en [correo/teléfono].
Más información: [enlace a la política de privacidad de CLIENTE].

¿En qué te puedo ayudar?`
  ),
  spacer(),
  p("Texto sugerido al recibir una receta (se muestra antes de derivar):"),
  codeBlock(
`Recibí tu receta. Para cuidar tus datos de salud, la va a revisar una
persona de nuestro equipo y te va a responder por acá con la cotización.
La imagen se guarda de forma segura y se elimina automáticamente a los
7 días.`
  ),

  pageBreak(),
  h1("Anexo B — Cláusulas mínimas del contrato de encargo de tratamiento"),
  p("Cláusulas a incorporar al contrato de servicio entre [CLIENTE] (Responsable) y [PROVEEDOR] (Encargado), conforme al artículo 25 de la Ley 25.326."),
  ...numbered([
    "**Objeto e instrucciones.** El Encargado tratará los datos personales exclusivamente para prestar el servicio de asistente conversacional descrito en el Anexo técnico y conforme a las instrucciones documentadas del Responsable. No los aplicará ni utilizará con un fin distinto ni los cederá a terceros, ni siquiera para su conservación.",
    "**Confidencialidad.** El Encargado garantiza que las personas autorizadas a tratar los datos se han comprometido a respetar la confidencialidad, obligación que subsiste aun después de finalizada la relación.",
    "**Medidas de seguridad.** El Encargado adoptará las medidas técnicas y organizativas descritas en la sección 9 de este documento, alineadas con la Resolución AAIP 47/2018, y las mantendrá actualizadas durante la vigencia del contrato.",
    "**Subencargados.** El Encargado sólo recurrirá a los subencargados listados en el Anexo C. Toda incorporación o reemplazo se notificará al Responsable con 30 días de antelación, quien podrá oponerse por motivos fundados. El Encargado impondrá a cada subencargado obligaciones equivalentes y responderá por su cumplimiento.",
    "**Transferencias internacionales.** Las transferencias a países sin nivel adecuado de protección se realizarán al amparo de los contratos modelo de la Disposición 60-E/2016 o del instrumento que la reemplace.",
    "**Asistencia al Responsable.** El Encargado asistirá al Responsable en la atención de los derechos de acceso, rectificación, actualización y supresión, entregando la información o ejecutando la acción dentro de las 48 horas hábiles de solicitada, y en la atención de requerimientos de la AAIP.",
    "**Incidentes de seguridad.** El Encargado notificará al Responsable todo incidente que afecte datos personales dentro de las 72 horas de detectado, con la información disponible sobre naturaleza, categorías y volumen afectado, consecuencias probables y medidas adoptadas, y colaborará en la comunicación a titulares y autoridades que el Responsable decida.",
    "**Retención y devolución.** Los datos se conservarán conforme a la política de retención acordada. Al finalizar el contrato el Encargado, a elección del Responsable, devolverá los datos en formato estructurado o los destruirá, y certificará la destrucción de todas las copias dentro de los 30 días, salvo las que deba conservar por obligación legal.",
    "**Auditoría.** El Responsable podrá solicitar una vez al año, con 15 días de aviso, la información y evidencias necesarias para verificar el cumplimiento de estas cláusulas, incluida la realización de una auditoría por un tercero independiente a su costo.",
    "**Registro de tratamientos.** El Encargado mantendrá un registro de las categorías de tratamiento realizadas por cuenta del Responsable y lo pondrá a su disposición.",
    "**Responsabilidad.** El Encargado responderá por los daños derivados del incumplimiento de estas cláusulas o de las instrucciones del Responsable. Si el Encargado destinara los datos a otra finalidad, los cediera o los utilizara incumpliendo el contrato, será considerado responsable del tratamiento y responderá personalmente por las infracciones en que hubiera incurrido.",
  ]),

  pageBreak(),
  h1("Anexo C — Matriz de subencargados"),
  p("Versión controlada. Toda modificación se notifica al Cliente conforme a la cláusula 4 del Anexo B."),
  table(
    ["#", "Subencargado", "Servicio", "País", "Activo por defecto", "Puede desactivarse"],
    [
      ["1", "Meta Platforms, Inc.", "WhatsApp Business Platform", "EE. UU.", "Sí", "No (es el canal)"],
      ["2", "Proveedor de conectividad a WhatsApp", "Conexión del número (alternativa a la integración directa con Meta)", "Según proveedor", "No", "Sí"],
      ["3", "Anthropic, PBC", "Modelo de lenguaje y visión", "EE. UU.", "Sí", "Sí, reemplazable por otro proveedor acordado"],
      ["4", "OpenAI, L.L.C.", "Modelo de respaldo, embeddings del catálogo", "EE. UU.", "Sí", "Sí"],
      ["5", "Groq, Inc.", "Transcripción de audio", "EE. UU.", "Sí", "Sí (se desactivan los mensajes de voz)"],
      ["6", "Mercado Libre S.R.L. (Mercado Pago)", "Cobro por link", "Argentina", "Según elección del Cliente", "Sí"],
      ["7", "Prisma Medios de Pago S.A. (Payway)", "Cobro con tarjeta", "Argentina", "Según elección del Cliente", "Sí"],
      ["8", "Proveedor de nube acordado", "Alojamiento y base de datos", "EE. UU. o región acordada", "Sí", "Reemplazable por infraestructura del Cliente"],
    ],
    [500, 2100, 2300, 1300, 1400, 1426]
  ),

  pageBreak(),
  h1("Anexo D — Checklist de cumplimiento para el acta de aceptación"),
  p("Cada ítem se marca con la evidencia adjunta antes del inicio en producción."),
  table(
    ["#", "Control", "Referencia legal", "Estado", "Evidencia"],
    [
      ["1", "Contrato de encargo firmado (Anexo B)", "Art. 25", "☐", ""],
      ["2", "Lista de subencargados aprobada por el Cliente (Anexo C)", "Art. 11, 12, 25", "☐", ""],
      ["3", "Descripción técnica entregada para la inscripción ante la AAIP", "Art. 21", "☐", ""],
      ["4", "Aviso de privacidad configurado en el primer contacto", "Art. 6", "☐", ""],
      ["5", "Palabra de baja operativa y probada", "Art. 27", "☐", ""],
      ["6", "Política de retención firmada y depuración automática activa", "Art. 4 inc. 7", "☐", ""],
      ["7", "Acción de supresión por titular probada", "Art. 16", "☐", ""],
      ["8", "Exportación de datos del titular probada", "Art. 14", "☐", ""],
      ["9", "Usuarios individuales con roles; sin claves compartidas", "Art. 9; Res. 47/2018", "☐", ""],
      ["10", "Registro de auditoría de operadores activo", "Art. 9; Res. 47/2018", "☐", ""],
      ["11", "Cifrado en tránsito y en reposo verificado", "Art. 9", "☐", ""],
      ["12", "Imágenes de recetas accesibles sólo con usuario autenticado y con vencimiento automático", "Art. 7, 8, 9", "☐", ""],
      ["13", "Lectura automática de recetas: decisión escrita del Cliente (activada o no)", "Art. 7, 8, 12", "☐", ""],
      ["14", "Registros técnicos sin contenido de mensajes ni teléfonos completos", "Art. 9", "☐", ""],
      ["15", "Procedimiento de incidentes entregado y simulacro realizado", "Res. 47/2018", "☐", ""],
      ["16", "Copias de respaldo cifradas y restauración probada", "Res. 47/2018", "☐", ""],
      ["17", "Entorno de pruebas sin datos reales", "Res. 47/2018", "☐", ""],
      ["18", "Acuerdos de confidencialidad del personal del Proveedor", "Art. 10", "☐", ""],
      ["19", "Capacitación de operadores realizada", "Art. 9, 10", "☐", ""],
      ["20", "Informe de evaluación de impacto entregado", "Buena práctica / proyecto de reforma", "☐", ""],
    ],
    [500, 3800, 1900, 800, 2026]
  ),

  pageBreak(),
  h1("Anexo E — Preguntas frecuentes de Legales y Seguridad"),
  h3("¿Los mensajes de WhatsApp viajan cifrados?"),
  p("Entre el teléfono del cliente y los servidores de Meta, sí, con cifrado de extremo a extremo. Meta entrega el mensaje al servicio de Remedia por un canal TLS; a partir de ahí el contenido es visible para el servicio, que es lo que permite atenderlo. Este esquema es el mismo para cualquier empresa que use la WhatsApp Business Platform, y Meta lo documenta públicamente."),
  h3("¿La inteligencia artificial \"aprende\" con los datos de nuestros clientes?"),
  p("No. Los proveedores de modelos se contratan por API bajo términos comerciales que prohíben usar el contenido para entrenar modelos, con retención cero o mínima para fines de abuso. Además, el modelo recibe sólo lo necesario para responder cada mensaje: no recibe DNI, domicilio ni el paquete de datos de la receta."),
  h3("¿Podemos pedir que ningún dato salga de Argentina?"),
  p("El alojamiento puede radicarse en la región o en infraestructura del Cliente. Lo que no puede evitarse es que el canal (Meta) y los proveedores de IA sean internacionales; esa transferencia se limita al contenido conversacional y se cubre con las cláusulas de la Disposición 60-E/2016. Si el Cliente prefiere no usar IA generativa, el asistente puede operar en modo de menú guiado sin envío de texto a proveedores externos, con menor capacidad de comprensión."),
  h3("¿Qué pasa si un operador se va de la empresa?"),
  p("El Cliente desactiva su usuario desde el panel y el acceso cae de inmediato. Sus acciones anteriores quedan en el registro de auditoría con su nombre."),
  h3("¿Qué pasa con nuestros datos si dejamos de usar el servicio?"),
  p("El Cliente elige entre recibir una exportación completa en formato estructurado o la destrucción certificada. En ambos casos el Proveedor elimina todas las copias en 30 días, salvo las que una obligación legal le exija conservar."),
  h3("¿Remedia sustituye nuestra política de privacidad?"),
  p("No. El Cliente mantiene su política y su inscripción; Remedia es un canal más dentro de ella. El Proveedor entrega el texto y la descripción técnica para que el Cliente incorpore el canal a su documentación existente."),
];

(async () => {
  const doc = buildDoc({
    title: "Adecuación a la Ley 25.326 — Remedia",
    shortTitle: "Remedia · Adecuación a la Ley 25.326 de Protección de Datos Personales",
    children,
  });
  await save(doc, OUT);
})();
