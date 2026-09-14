const L = require(process.env.THEME === "dark" ? "./lib_dark" : "./lib");
const { p, small, h1, h2, h3, bullets, numbered, table, callout, spacer, pageBreak, cover, buildDoc, save, run } = L;
const { Paragraph, AlignmentType, BorderStyle } = require("docx");

const OUT = process.argv[2];

const children = [
  ...cover({
    title: "Compromisos de cumplimiento en materia de datos personales",
    subtitle: "Declaración de [PROVEEDOR] como encargado del tratamiento para el servicio Remedia en [CLIENTE]",
    meta: [
      ["Documento", "Declaración de cumplimiento — Ley 25.326 de Protección de Datos Personales"],
      ["Servicio", "Remedia — asistente conversacional de WhatsApp"],
      ["Encargado del tratamiento", "[PROVEEDOR]"],
      ["Responsable de la base de datos", "[CLIENTE]"],
      ["Versión", "1.0 — septiembre de 2026"],
      ["Documento relacionado", "Adecuación a la Ley 25.326 (análisis completo, 20 páginas)"],
    ],
  }),

  // ───────────────────────────── 1
  h1("1. Declaración"),
  p("[PROVEEDOR] declara que el servicio Remedia, en su implementación para [CLIENTE], opera bajo los compromisos que se detallan en este documento. Cada compromiso es verificable: indica qué se cumple, cómo se cumple y qué evidencia se entrega al Cliente antes del inicio en producción."),
  callout("En una frase", [
    "Los datos son del Cliente, se usan sólo para atender a sus clientes por WhatsApp, viven el tiempo mínimo necesario, los de salud los trata siempre una persona habilitada, y el titular puede informarse, darse de baja y ejercer sus derechos por el mismo canal.",
  ]),
  spacer(),
  p("Este documento resume los compromisos; el análisis normativo, el inventario de datos y los anexos contractuales están en el documento de adecuación completo. Ambos forman parte de la propuesta de implementación."),

  // ───────────────────────────── 2
  h1("2. Qué cumplimos, artículo por artículo"),
  p("La tabla recorre las obligaciones de la Ley 25.326 y su reglamentación que alcanzan al servicio, y el compromiso concreto frente a cada una."),
  table(
    ["Obligación legal", "Compromiso de [PROVEEDOR]", "Cómo se verifica"],
    [
      ["**Art. 4 — Calidad y finalidad.** Los datos deben ser ciertos, adecuados, pertinentes y no excesivos respecto de la finalidad, y destruirse cuando dejan de ser necesarios.", ["El asistente recolecta sólo lo indispensable para cada paso: teléfono, lo que el cliente escribe, y una dirección únicamente si eligió envío a domicilio. No pide DNI, correo ni fecha de nacimiento.", "Cada dato tiene vencimiento: sesión 25 horas, imágenes 7 días, pedidos 7 días, historial según la política acordada con el Cliente."], "Inventario de datos con retención por categoría; job de depuración activo; captura de la configuración."],
      ["**Art. 5 — Consentimiento.** El tratamiento requiere consentimiento libre, expreso e informado, salvo excepciones (relación contractual, datos de contacto básicos).", ["El titular inicia la conversación de forma voluntaria y recibe el aviso de privacidad antes de que se le pida ningún dato.", "Para socios y clientes existentes se apoya además en la relación previa con el Cliente (art. 5 inc. 2).", "Los mensajes proactivos (promociones, recordatorios) sólo se envían con opt-in registrado."], "Texto del aviso configurado; prueba de flujo; registro de opt-in."],
      ["**Art. 6 — Información.** Al recolectar datos hay que informar finalidad, destinatarios, carácter obligatorio u optativo, consecuencias de no proveerlos, y derechos.", "Aviso en el primer mensaje de cada conversación nueva con los cinco elementos del artículo, más la identificación del asistente como sistema automatizado y el enlace a la política de privacidad del Cliente.", "Captura del primer mensaje; texto aprobado por el Cliente."],
      ["**Art. 7 y 8 — Datos sensibles y de salud.** Sólo pueden tratarse con consentimiento expreso o por establecimientos sanitarios y profesionales de la salud, bajo secreto profesional.", ["El asistente nunca interpreta ni resuelve una receta: la reconoce, la guarda de forma temporal y la deriva a un operador habilitado del Cliente.", "El modelo de lenguaje no recibe DNI, domicilio, diagnóstico ni el paquete de datos de la receta.", "La lectura automática de recetas está desactivada por defecto y sólo se habilita por decisión escrita del Cliente."], "Prueba del circuito de receta; configuración de la función de lectura; registro de la decisión del Cliente."],
      ["**Art. 9 — Seguridad.** Medidas técnicas y organizativas para garantizar seguridad y confidencialidad, evitar adulteración, pérdida y acceso no autorizado.", "Medidas alineadas con la Resolución AAIP 47/2018, detalladas en la sección 6 de este documento: cifrado en tránsito y en reposo, usuarios individuales con roles, auditoría, respaldos, gestión de vulnerabilidades e incidentes.", "Matriz de controles con evidencia por control; informe de revisión de seguridad previo al go-live."],
      ["**Art. 10 — Deber de confidencialidad.** Quienes intervienen en el tratamiento están obligados al secreto profesional, aun después de finalizada la relación.", "Todo el personal del Proveedor con acceso a producción firma acuerdo de confidencialidad; el acceso es nominal, revocable y registrado. Los operadores del Cliente acceden con usuario propio.", "Modelo de acuerdo firmado; listado de accesos vigentes."],
      ["**Art. 11 — Cesión.** Los datos sólo pueden cederse para fines relacionados con el interés legítimo del cedente y del cesionario, con consentimiento del titular.", "[PROVEEDOR] no cede datos a nadie. Los terceros que intervienen son subencargados que prestan un servicio técnico bajo contrato, no cesionarios. La lista es cerrada y la aprueba el Cliente.", "Matriz de subencargados aprobada; cláusula de no cesión en el contrato."],
      ["**Art. 12 — Transferencia internacional.** Prohibida hacia países sin nivel adecuado de protección, salvo consentimiento o cláusulas contractuales.", "Los subencargados radicados en Estados Unidos (mensajería, inteligencia artificial, nube) operan bajo los contratos modelo de la Disposición 60-E/2016 y sus términos de API prohíben el uso de los datos para entrenamiento. El aviso de privacidad informa la transferencia.", "Cláusulas incorporadas; términos de cada proveedor archivados."],
      ["**Art. 14 — Derecho de acceso.** El titular puede pedir sus datos y obtenerlos dentro de los 10 días corridos.", "El Cliente exporta desde el panel el historial y los pedidos de un número. El Proveedor responde cualquier pedido interno del Cliente en 48 horas hábiles.", "Prueba de exportación; procedimiento escrito."],
      ["**Art. 16 — Rectificación, actualización y supresión.** Dentro de los 5 días hábiles de la solicitud.", "Acción \"Suprimir titular\" en el panel: borra sesión, pedidos, imágenes e historial, conserva sólo lo exigido por ley y una marca de exclusión para no volver a contactarlo. Rectificación de padrón desde la fuente del Cliente.", "Prueba de supresión con datos sintéticos; registro de auditoría de la acción."],
      ["**Art. 21 — Registro de bases de datos.** El responsable debe inscribir sus bases ante la AAIP.", "El Proveedor entrega en la primera semana la descripción técnica completa (finalidad, categorías, ubicación, medidas, transferencias) para que el Cliente incorpore el canal a su inscripción.", "Descripción técnica entregada."],
      ["**Art. 25 — Prestación de servicios de tratamiento.** El encargado no puede aplicar los datos a otro fin ni cederlos; el contrato regula el tratamiento; los datos se destruyen al terminar.", "Contrato de encargo con las cláusulas mínimas (objeto e instrucciones, confidencialidad, seguridad, subencargados, transferencias, asistencia, incidentes, devolución y destrucción, auditoría). Al fin del contrato: devolución en formato estructurado o destrucción certificada en 30 días.", "Contrato firmado; certificado de destrucción al cierre."],
      ["**Art. 27 — Archivos con fines publicitarios.** El titular puede pedir en cualquier momento el retiro o bloqueo de su nombre.", "Palabra BAJA en el chat: confirmación inmediata, exclusión de todo mensaje proactivo y marca que sobrevive a la depuración del resto de los datos.", "Prueba de baja; registro de exclusiones."],
      ["**Res. AAIP 47/2018 — Medidas de seguridad recomendadas.**", "Adoptadas en los nueve dominios de la resolución (ver sección 6).", "Matriz de controles."],
      ["**Res. AAIP 4/2019 — Decisiones automatizadas y transparencia.**", "El asistente se identifica como sistema automatizado y toda decisión con efecto para el titular (validación de receta, reclamo, reintegro) la toma una persona del Cliente.", "Aviso inicial; circuito de derivación."],
    ],
    [2500, 4100, 2426]
  ),

  // ───────────────────────────── 3
  h1("3. Qué datos tratamos y por cuánto tiempo"),
  table(
    ["Dato", "Para qué", "Hasta cuándo"],
    [
      ["Teléfono de WhatsApp", "Identificar la conversación y avisar pedidos", "Sesión 25 h; pedidos 7 días; historial según política del Cliente (sugerido 12 meses)"],
      ["Contenido de la conversación", "Atender, cotizar, dar trazabilidad al operador", "Ídem historial"],
      ["Nombre y número de socio (del padrón del Cliente)", "Descuento de socio y saludo por nombre", "Mientras el Cliente mantenga el padrón"],
      ["Dirección de envío", "Entregar a domicilio", "7 días; luego se enmascara"],
      ["Imagen de receta o credencial (dato de salud)", "Que un operador habilitado la cotice", "7 días, borrado automático"],
      ["Datos de pago", "Confirmar el cobro", "Sólo identificador de pago, marca y primeros 6 dígitos. Nunca el número completo"],
      ["Acciones de operadores", "Auditoría", "24 meses"],
    ],
    [3000, 3000, 3026]
  ),
  spacer(),
  p("**Lo que no tratamos:** no pedimos DNI, correo ni fecha de nacimiento; no leemos el DNI ni el domicilio del padrón para conversar; no conservamos audios; no guardamos datos de tarjeta; no usamos los datos para ningún fin propio ni los cruzamos con otros clientes."),

  // ───────────────────────────── 4
  h1("4. Datos de salud: el compromiso específico"),
  ...numbered([
    "Una foto de receta o credencial se reconoce automáticamente y se guarda en un espacio temporal, accesible sólo con usuario autenticado del Cliente.",
    "La conversación se deriva de inmediato a un operador. El asistente informa al cliente que una persona continuará y deja de responder de forma automática.",
    "El operador cotiza y valida desde el panel. Ninguna decisión sobre la receta la toma el asistente.",
    "A los 7 días la imagen se elimina. En el historial queda sólo la constancia de que hubo una derivación por receta.",
    "Si el Cliente no es un establecimiento sanitario (por ejemplo, una mutual), el asistente no procesa recetas: deriva sin almacenar.",
  ]),

  // ───────────────────────────── 5
  h1("5. Derechos del titular: cómo se ejercen"),
  table(
    ["Derecho", "Cómo", "Plazo"],
    [
      ["Información", "Aviso en el primer mensaje", "Inmediato"],
      ["Baja de comunicaciones", "Escribir BAJA en el chat", "Inmediato"],
      ["Acceso", "Pedido al Cliente por WhatsApp, correo o el canal designado; exportación desde el panel", "10 días corridos"],
      ["Rectificación / actualización", "Pedido al Cliente; corrección en padrón o pedido", "5 días hábiles"],
      ["Supresión", "Pedido al Cliente; acción de supresión en el panel con auditoría", "5 días hábiles"],
    ],
    [2400, 4600, 2026]
  ),

  // ───────────────────────────── 6
  h1("6. Medidas de seguridad comprometidas"),
  table(
    ["Dominio", "Compromiso"],
    [
      ["Cifrado", "TLS 1.2+ en toda comunicación. Discos cifrados por el proveedor de nube; imágenes de recetas y padrón cifrados además a nivel de aplicación con clave rotable."],
      ["Control de acceso", "Usuarios individuales con contraseña y roles (administrador, dueño, operador). Sin claves compartidas. Sesiones con expiración. Baja inmediata al desvincular a un operador."],
      ["Auditoría", "Registro de quién vio, tomó, cotizó, preparó, entregó, exportó o suprimió, con fecha y hora. Retención 24 meses."],
      ["Registros técnicos", "Sin contenido de mensajes ni teléfonos completos (se conservan los últimos 4 dígitos)."],
      ["Respaldo", "Copias diarias cifradas, retención 30 días, restauración probada trimestralmente."],
      ["Vulnerabilidades y cambios", "Dependencias monitoreadas; parches críticos en 7 días; todo cambio pasa por control de versiones y pruebas; revisión de seguridad previa al go-live y anual."],
      ["Incidentes", "Procedimiento documentado; notificación al Cliente dentro de las 72 horas de detectado, con naturaleza, alcance y medidas; simulacro antes del go-live."],
      ["Entornos", "Desarrollo y pruebas separados de producción, sin datos reales de titulares."],
      ["Aislamiento", "Cada Cliente en un despliegue propio con base de datos, caché y credenciales separadas."],
      ["Personal", "Acuerdos de confidencialidad, acceso nominal y revocable, capacitación anual."],
    ],
    [2400, 6626]
  ),

  // ───────────────────────────── 7
  h1("7. Terceros que intervienen"),
  p("Lista cerrada. Cualquier cambio se notifica al Cliente con 30 días de antelación y derecho de objeción."),
  table(
    ["Tercero", "Para qué", "Qué recibe", "País"],
    [
      ["Meta (WhatsApp Business Platform)", "Canal de mensajería", "Teléfono, mensajes y archivos", "EE. UU."],
      ["Anthropic / OpenAI", "Interpretar mensajes y redactar respuestas; clasificar imágenes", "Últimos turnos de la conversación, nombre y número de socio. Imágenes sólo si el Cliente habilita la lectura de recetas", "EE. UU."],
      ["Groq", "Transcribir audios", "El audio, sin conservarlo", "EE. UU."],
      ["Mercado Pago / Payway", "Cobrar", "Ítem, monto, token de tarjeta generado en el navegador", "Argentina"],
      ["Proveedor de nube", "Alojar el servicio", "Todos los datos, cifrados en reposo", "EE. UU. o región acordada"],
    ],
    [2200, 2300, 3100, 1426]
  ),
  spacer(),
  p("Con todos ellos: contrato o términos que prohíben usar los datos para fines propios o entrenamiento de modelos, y cláusulas de transferencia internacional según la Disposición 60-E/2016."),

  // ───────────────────────────── 8
  h1("8. Cuándo se cumple cada cosa"),
  p("Los compromisos se completan durante la implementación y se verifican en el acta de aceptación. Ninguno queda para después del inicio en producción."),
  table(
    ["Momento", "Compromisos que quedan cumplidos"],
    [
      ["Semana 1", "Contrato de encargo firmado; lista de subencargados aprobada; política de retención definida; descripción técnica para la inscripción ante la AAIP entregada; acuerdos de confidencialidad."],
      ["Semana 2", "Aviso de privacidad y palabra BAJA operativos; usuarios y roles del panel; registro de auditoría."],
      ["Semana 3", "Cifrado en reposo de imágenes y padrón; depuración automática del historial; acción de supresión y exportación por titular; revisión de seguridad."],
      ["Semana 4", "Pruebas de ejercicio de derechos con datos sintéticos; simulacro de incidente; capacitación de operadores."],
      ["Go-live", "Acta de aceptación con el checklist de 20 controles firmado por ambas partes."],
      ["Continuo", "Revisión anual de medidas y subencargados; restauración trimestral; notificación de incidentes en 72 h; asistencia al Cliente en 48 h hábiles."],
    ],
    [1800, 7226]
  ),

  // ───────────────────────────── 9
  pageBreak(),
  h1("9. Firmas"),
  p("Las partes suscriben la presente declaración como parte integrante de la propuesta de implementación de Remedia. Los compromisos aquí enunciados se incorporan al contrato de servicio y al contrato de encargo de tratamiento."),
  spacer(), spacer(), spacer(),
  table(
    ["Por [PROVEEDOR]", "Por [CLIENTE]"],
    [
      [["", "", "", "Firma: ______________________________", "", "Aclaración: __________________________", "", "Cargo: ______________________________", "", "Fecha: ____ / ____ / ________"],
       ["", "", "", "Firma: ______________________________", "", "Aclaración: __________________________", "", "Cargo: ______________________________", "", "Fecha: ____ / ____ / ________"]],
    ],
    [4513, 4513],
    { plain: true }
  ),
];

(async () => {
  const doc = buildDoc({
    title: "Compromisos de cumplimiento — Datos personales — Remedia",
    shortTitle: "Remedia · Compromisos de cumplimiento en materia de datos personales",
    children,
  });
  await save(doc, OUT);
})();
