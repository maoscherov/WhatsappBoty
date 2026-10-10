# Radar de conversaciones — onboarding por QR y primer dashboard

**Estado:** propuesta de diseño, sin implementar · **Fecha:** 2026-09-21 · **Nombre de trabajo:** "Radar" · **Revisión:** v5.2

Historial de revisiones:

- **v2**: revisión adversarial con cinco lentes (fidelidad al código, factibilidad WAHA, privacidad/legales, producto/métricas, API de Claude).
- **v3**: sonda real de historial y primeras decisiones de producto.
- **v4**: duración del vínculo configurable y modelo por línea.
- **v5.2**: suma la **Consola KIS** (D10): KIS vincula líneas, ve estado y sincronización de todas, y sus métricas agregadas.
- **v5.1**: verificación de la v5 con la lente de un vínculo de meses. Define la retención con los valores iniciales, la purga por línea, la reconciliación sin leer chats excluidos, el costo de IA en régimen y la capacidad del servidor WAHA.
- **v5**: consolida todo después de la aclaración de que **eliminar la fuente es una política opcional, no una regla del diseño**. El segundo escaneo y la sesión sin store salen del flujo principal y pasan a ser un modo opcional de segunda etapa.

**Decisiones tomadas (2026-09-21)**

| # | Decisión | Efecto en el diseño |
|---|---|---|
| D1 | Radar es el **módulo permanente de onboarding para clientes y líneas nuevas**, no un gancho para prospectos. | Se entra por invitación durante el alta. La unidad es la **línea**: un cliente puede dar de alta varias, y cada una tiene su vínculo, sus parámetros de línea y su tablero. El acuerdo de datos va en el contrato. |
| D2 | **Se acepta el riesgo** del cliente no oficial de WhatsApp sobre líneas de clientes. | El riesgo se declara en el consentimiento y se monitorea la salud de la cuenta. |
| D3 | La sensibilidad del rubro es un **parámetro configurable**. | `perfil_de_datos` por tenant: `estandar` o `sensible`. No se hardcodea "farmacia". |
| D4 | **Se usa el motor de WAHA que mejor funcione.** | El spike decide con criterios explícitos (§6.2). |
| D5 | **Ningún cliente firmó** los documentos de Ley 25.326 ni de compromisos. | Se corrigen los documentos comerciales **antes de la primera firma**, sin adendas ni preavisos. |
| D6 | La **duración del vínculo es configurable en días; 0 significa permanente**. | `duracion_vinculo_dias` por línea. No hay un segundo escaneo en el flujo principal, y la sesión de WAHA tiene store durante todo el vínculo. |
| D7 | **Eliminar los datos fuente es una política opcional**, configurable en días. | `retencion_fuente_dias` por línea; 0 = no se eliminan mientras la línea siga dada de alta, tenga o no un vínculo vivo. No condiciona el resto del diseño. |
| D8 | Cuando esa política está activa, se acepta que el **drill-down muestre fichas sin texto** para lo ya purgado. | Los fragmentos de evidencia son un parámetro aparte, apagado por defecto. |
| D10 | Hay una **Consola KIS**, de uso exclusivo de KIS (sin partners ni revendedores), para vincular líneas y operar. | §3.1. El backend de Radar automatiza todo el ciclo con WAHA por API; solo escanear el QR es manual. Entra primero en el tramo 2. |
| D9 | La **retención de las fichas por conversación es configurable**, 12 meses en principio. | `retencion_fichas_meses` por tenant. Aplica a las conversaciones cuyo texto ya no existe. |

Convenciones usadas en todo el documento:

- **[EXISTE]** verificado en el código o en el servidor WAHA.
- **[FALTA]** no existe y hay que construirlo.
- **[VALIDAR]** sale de documentación o inferencia; hay que probarlo en staging antes de comprometerlo.
- **[DICTAMEN]** requiere opinión legal antes del piloto.
- Los ejemplos de hallazgos y números son **ilustrativos e inventados**, y están marcados como tales.

---

## 0. Resumen y hallazgos que cambian la premisa

La revisión del repo y de WAHA fue de solo lectura: no se crearon sesiones ni se enviaron mensajes. La sonda de historial se hizo después, con tu aprobación, y reporta solo agregados.

**1. WAHA no está implementado en el proyecto.**

- No hay ninguna referencia a WAHA en el repo.
- El bot integra WhatsApp por **Meta Cloud API**, con un segundo proveedor, **Kapso**, enchufado como adaptador (`app/routers/webhook.py:443-547`).
- El MCP de WAHA es un conector de esta sesión de Claude, atado a una sola sesión con una clave acotada.
- El endpoint de administración que se probó (`server-environment`) devolvió 403.
- Toda la integración WAHA (crear sesión, QR, webhooks, importación) es trabajo nuevo.

**2. El servidor WAHA actual es de desarrollo.**

- Versión 2026.8.2, motor NOWEB, tier CORE.
- La clave del MCP solo ve la sesión `MaroSession`: WORKING, `store.enabled=true`, `fullSync=true`, `markOnline=true`. No se puede saber si el servidor tiene otras sesiones.
- Esa sesión no tiene webhooks, metadata ni `ignore` propios.

**3. La licencia no limita las sesiones.**

- Según la documentación, desde 2026.6.1 las funciones de Plus están en Core: sesiones ilimitadas, multimedia, storages y seguridad.
- [VALIDAR] en staging creando una segunda sesión.

**4. El historial existe, pero es corto.**

- En NOWEB hay que activar el store **antes** de escanear el QR, y no se puede cambiar después.
- La documentación indica ~3 meses sin `fullSync` y ~1 año con `fullSync`. GOWS sincroniza todo lo disponible.
- Lo más probable es que el historial no llegue por webhook y haya que leerlo por API [VALIDAR].
- No hay señal documentada de "sincronización terminada".
- Borrar la sesión borra el store de WAHA, así que el sistema de registro tiene que ser nuestra base.
- Issues abiertos en nuestra versión exacta:
  - #2267: a veces no se emite ni se guarda el primer mensaje de contactos `@lid` que llegan por anuncios.
  - #2265: los medios de más de ~18 días no se pueden descargar.
- **Sonda real sobre `MaroSession`** (cuenta personal, `fullSync=true`, sin `ignore`):
  - Hay entre 561 y 600 chats. La lista llega a 2014, pero los chats viejos **no tienen mensajes guardados**: la lista de chats no indica profundidad.
  - Hay entre 40.000 y 50.000 mensajes. El más antiguo es de marzo de 2025, ~18 meses atrás.
  - **El historial viejo es una muestra fina**:
    - solo ~200 mensajes tienen más de 16 semanas;
    - más del 98 % cae en los últimos ~76 días.
  - Conclusión: la ventana con densidad real es de ~2,5 meses, aun con `fullSync=true`.
  - Es un solo caso y no se sabe cuándo se vinculó la sesión. El spike lo repite sobre una cuenta Business.
  - `chatId=all` funciona en NOWEB, y acepta `offset` alto.
  - En mensajes históricos, `ack` viene en `-1/ERROR` para los entrantes: **"visto" no se puede usar sobre historial**.
  - `source` aparece en mensajes guardados, siempre como `app`, así que no distingue nada.
  - El id de mensaje lleva adentro el teléfono o el lid.

**5. `markOnline=true`, el valor por defecto, silencia las notificaciones del teléfono del cliente.**

- Un producto que mide tiempos de respuesta no puede empeorarlos.
- Toda sesión se crea con `config.noweb.markOnline:false`, y el servidor con `WAHA_PRESENCE_AUTO_ONLINE=False`.

**6. No hay lista blanca de chats, y el store de WAHA guarda todo.**

- `config.ignore` solo filtra estados, grupos, canales y difusión, tanto en eventos como en almacenamiento.
- Un vínculo trae al store de WAHA **todos** los chats individuales del número, incluidos los personales.
- No se pueden borrar chats sueltos de ese store.
- Lo que sí controlamos es qué pasa a **nuestra** base y a la IA. El consentimiento lo dice así.

**7. Los datos actuales del bot no sirven para estos KPI.**

- `messages` guarda `phone`, `role` (user/assistant/operator), `content`, `autor` y `created_at`.
  - No tiene id del proveedor, ni timestamp original, ni tipo, ni chat id.
  - `created_at` es la hora de inserción, y la fila del usuario y la del bot se insertan juntas.
- Las respuestas del operador desde el panel sí quedan con su hora real. Las escritas desde la app de WhatsApp no se capturan (`session_service.py:380-382`).
- Los mensajes recibidos fuera de horario no se guardan: solo queda un evento `fuera_horario`.
- No hay columna de tenant en ninguna tabla conversacional.
- Lo reutilizable son patrones, no tablas.

**8. El backoffice no puede ser la base de un dashboard para clientes.**

- Usa una clave única compartida, aceptada por query string y *fail-open* si no está configurada (`backoffice.py:41-46`).
- No tiene usuarios ni roles.
- Hay inyección HTML sin escapar en `dashboard.html:487, 527-528`.

**9. Hay tensión con los documentos comerciales ya escritos.**

- Los textos están en `docs/comercial/generador/doc1_ley25326.js` y `doc3_compromisos.js`. El estado real está en `NOTA-INTERNA-brechas-vs-codigo.md`.
- Como nadie los firmó (D5), se reescriben antes de la primera firma. Ver §7.

**Consecuencia de producto [VALIDAR].**

- Un número que opera *solo* por Cloud API no existe en la app de WhatsApp y no se puede vincular por QR.
- Pero el propio repo indica que en los despliegues actuales el operador contesta desde la app sobre el mismo número. Eso sugiere modo coexistencia.
- Si se confirma, Radar también aplicaría a líneas que ya tienen el bot, y vería las respuestas humanas que hoy el bot no captura.
- Si no se confirma, el vínculo de una línea termina de hecho cuando esa línea migra a la API oficial.
- Secuencia propuesta:
  - WAHA para el onboarding de la línea;
  - Meta Cloud API / Kapso para el bot en producción;
  - el tablero de Radar queda como línea de base "antes del bot".
- Es coherente con la Fase 4 de `docs/plan-multitenant.md`.

---

## 1. Qué existe, qué falta y qué hay que validar

| Tema | Existe | Falta | Validar |
|---|---|---|---|
| Sesiones por QR | API de WAHA: crear/iniciar/parar/borrar; `GET /api/{session}/auth/qr?format=image\|raw`; código de vinculación (`POST …/auth/request-code`, pide el teléfono); estados `STOPPED, STARTING, SCAN_QR_CODE, WORKING, FAILED` (+2 de passkey, solo GOWS). | Cliente WAHA en el repo, gestor de sesiones, pantalla de QR, tabla de líneas. | Multi-sesión en CORE; tiempos del QR (docs: 60 s el primero, 20 s cada uno de los siguientes, 6 códigos, luego `FAILED`); en qué estado se puede pedir el código de vinculación. |
| Datos consultables | `GET /chats` (id, nombre, fecha del último mensaje); mensajes con `filter.timestamp.gte/lte`, `filter.fromMe`, paginado; `chatId=all` (verificado en NOWEB); mapeo `@lid`↔teléfono; etiquetas (solo cuentas Business). `WAMessage` trae `timestamp`, `fromMe`, `body`, `hasMedia`, `replyTo`. | Tipo de mensaje normalizado; conteos por chat (no hay endpoint); transcripción de audios. | Tope de `limit`; que leer historial no genere tildes azules ni presencia. |
| Eventos | `message.any` (incluye `fromMe` y `source` app/api; en el historial `source` viene siempre `app`), `message.ack`, `message.edited`, `message.revoked`, `session.status`, y cuatro de etiquetas (`label.upsert`, `label.deleted`, `label.chat.added`, `label.chat.deleted`; **no existe** el comodín `label.*`). Webhooks por sesión con HMAC sha512 (`X-Webhook-Hmac`), reintentos y `metadata` de sesión en cada evento. | Receptor `/webhook/waha`, cola, reconciliación. | Si `message` y `message.any` duplican; defaults de reintentos (las páginas de docs se contradicen); si el historial se emite como eventos. |
| Historial | Store NOWEB + `fullSync`, configurables solo al crear la sesión. Densidad real medida: ~2,5 meses. | Importador idempotente en pasadas. | Profundidad, completitud y duración reales del sync por motor, en una cuenta Business. |

### Componentes reutilizables (con reservas)

| Componente | Ubicación | Uso en Radar | Reserva |
|---|---|---|---|
| Patrón adaptador de proveedor + verificación HMAC | `app/routers/webhook.py:443-547` | Molde para `POST /webhook/waha`. | **No** pasar por `procesar_mensajes()`: marca leído, responde y muta sesiones del bot. La firma de Kapso es sha256 y *fail-open*; la de WAHA debe ser sha512 y *fail-closed*. |
| Importación idempotente por lotes | `app/services/catalog_store.py:17-116`, `app/models/sync.py` | Molde del backfill: `ON CONFLICT`, lotes ≤1000, reconciliación, heartbeat. | Es por `branch`, no por tenant. |
| Tokens hasheados *fail-closed* | `app/services/branch_auth.py`, `backoffice_branches.py` | Links de acceso, secretos de webhook. | No tiene expiración ni scopes. |
| `Database.transaction()/executemany()` | `app/services/db.py:97-109` | Único camino válido para tablas de Radar (ver RLS en §6.4). | `execute()/fetch()` **tragan errores** y toman una conexión por llamada (`db.py:67-85`). Pool único de 5 conexiones, con el mismo rol que migra. |
| Migraciones Alembic en SQL crudo | `migrations/versions/` | Migraciones 0007+. | Si la migración falla, el arranque sigue (solo loguea). |
| pgvector + `EmbeddingService` (1536 dims, HNSW) | `rag_service.py`, `embeddings.py` | Agrupar preguntas frecuentes. | Traga errores; envía texto a OpenAI (subencargado). |
| Contrato `_kpi(valor, anterior, badge)` con badges medido/propuesto/sin_dato | `metrics_store.py:676-700`, `tablero.html` | Base del badge "Medido / Estimado por IA / Datos insuficientes". | Posible bug silencioso: fechas ISO como `$n::date` con `fetch()` que traga el error [VALIDAR]. |
| Lista de conversaciones con fragmento coincidente + historial paginado | `metrics_store.py:602-656`, `message_store.py:26-46`, `backoffice.py:1210-1256` | Contrato del drill-down indicador → conversaciones → mensajes. | Por teléfono, sin tenant, `ILIKE` sin índice. |
| Detectores regex (pide humano, precios mencionados, efectivo, cancelar…) | `checkout_helper.py` | Pre-pasada determinista y heurística local de "chat personal". | Español rioplatense y farmacia. |
| Prompt de resumen con fallback de proveedor | `backoffice.py:1047-1097` | Esqueleto de la llamada de análisis. | Texto libre, no persiste, modelos hardcodeados. |
| KPI de SLA de derivación (`derivacion_atendida`) | `backoffice.py:991-1005`, `metrics_store.py:512-519` | Definición reutilizable de SLA con umbral. | Solo mide derivaciones bot→humano. |
| Servicio Railway separado con volumen | `image_server/` | Precedente para desplegar WAHA. | No se sabe si sigue desplegado. |

---

## 2. Concepto y propuesta de valor

**Promesa al cliente:** "Conectá tu WhatsApp y, en la misma sesión, mirá qué te preguntan, qué consultas quedaron sin responder y qué preguntas repetidas podrías automatizar. No enviamos ni modificamos nada: solo leemos."

**Para quién:** clientes nuevos de KIS, y líneas nuevas de clientes existentes, que atienden a mano por WhatsApp o WhatsApp Business (D1). Es el primer paso del alta de cada línea, no una demo pública.

**Qué lo hace creíble:**

1. El dashboard separa visiblemente lo **medido** de lo **estimado por IA**.
2. Cada número se abre y muestra las conversaciones que lo explican.
3. El cliente puede corregir a la IA, y la corrección cambia el número.
4. Nunca afirmamos una venta perdida ni una conversión que los mensajes no prueban.

**Rol en el negocio de KIS:**

- Entrega el caso de negocio y los insumos para configurar el bot: preguntas frecuentes, horarios de demanda y flujos.
- Después queda como tablero permanente de la línea, y como línea de base "antes del bot".
- La finalidad se declara en el contrato y en el consentimiento.
- El personal comercial de KIS solo ve agregados que el cliente comparte (§7).

### 2.1 Parámetros

- Los de ámbito **Tenant** se fijan una vez, al crear la cuenta.
- Los de ámbito **Línea** se fijan al dar de alta cada línea.
- Los fija un admin de KIS. El cliente los ve en P2, en lenguaje llano.
  - En P2 solo puede endurecer los de esa línea.
  - Endurecer uno del tenant se hace desde la configuración de la cuenta, con el aviso de que afecta a todas sus líneas.
- "Más estricto" significa:
  - en duración y retención, cualquier N > 0 es más estricto que 0, y entre valores mayores que 0 gana el menor;
  - en los demás, apagar es más estricto que encender.
- Pasar un parámetro de una línea viva a un valor **más laxo** exige un nuevo consentimiento del Dueño, con una fila nueva en `consents`, antes de aplicarse. Hacia más estricto se aplica directo y queda auditado.

| Parámetro | Ámbito | Valor inicial | Qué controla |
|---|---|---|---|
| `duracion_vinculo_dias` | Línea | 0 | Cuánto vive el vínculo con WhatsApp. **0 = permanente**, hasta que termine por alguna de las causas de §2.2 (D6). |
| `retencion_fuente_dias` | Línea | 0 | **Política opcional.** Días que el texto y la identidad permanecen en nuestra base, contados desde su **ingesta**. 0 = no se eliminan mientras la línea siga dada de alta, **tenga o no un vínculo vivo** (D7). |
| `retencion_tras_desvinculo_dias` | Línea | 0 | **Política opcional.** Borra la fuente N días después de que la línea queda sin vínculo. 0 = sin plazo. Existe por si el dictamen pide una condición de fin. |
| `retencion_fichas_meses` | Tenant | 12 | Cuánto se conserva el registro de una conversación **cuando su texto ya no existe**, antes de quedar solo agregados (D9). 0 = sin plazo. |
| `tope_ia_mensual_usd` | Línea | a definir en el piloto | Techo de gasto de IA por línea y por mes (§6.5). |
| `perfil_de_datos` | Tenant | según rubro | `estandar` o `sensible` (D3). Propone valores más estrictos para los demás parámetros. |
| `retener_fragmentos` | Tenant | no | Extractos de evidencia de conversaciones ya purgadas (D8). Solo tiene efecto si la purga está activa. |
| `ia_habilitada`, `via_llm` | Tenant | sí, `lotes` | Si corre el análisis con IA, y por qué vía (§6.5). |

- Los valores iniciales son los que asumí a partir de tus respuestas. Cambiarlos es configuración, con una salvedad: activar la purga en una línea que ya tiene datos es una migración entre almacenes (§6.4).
- Mientras el texto de una conversación siga en nuestra base, su ficha no vence: se recalcula desde la fuente, y borrarla no protegería nada.
- Para el perfil `sensible` recomiendo `retencion_fuente_dias = 7`, pero es una recomendación, no una regla.

**Estados de una línea**

| Estado | Significado |
|---|---|
| `vinculada` | Tiene un vínculo vivo. |
| `sin_vinculo` | El vínculo terminó, pero la línea sigue dada de alta. Su tablero y, según los parámetros, su texto se conservan. |
| `de_baja` | El cliente la dio de baja. Se borra todo a los 30 días. |

### 2.2 Ciclo de vida de una línea

| Momento | Qué pasa |
|---|---|
| Día 0 | El cliente escanea el QR. Se importa el historial (~2,5 meses de densidad real, §0.4) y empieza la captura en vivo. |
| Día 0–1 | Métricas medidas y hallazgos preliminares en la misma sesión. El análisis completo llega por lotes. |
| Primera semana | El cliente hace la **revisión guiada**: corrige etiquetas y valida los hallazgos. Recibe recordatorios por email con conteos. |
| Mientras dure el vínculo | Pendientes y tiempos de respuesta en vivo. Las conversaciones nuevas se analizan a medida que se cierran. Se habilitan las tendencias reales entre períodos. |
| Fin del vínculo | Ocurre por cinco causas: se cumplió `duracion_vinculo_dias`; lo pidió el dueño; la sesión quedó caída más de 72 h; la línea migró a la API oficial y no hay coexistencia [VALIDAR, spike punto 11]; o hubo una baja. Las cinco disparan el mismo job (§6.3, punto 6). Se borra la sesión de WAHA, con su copia técnica. La línea queda `sin_vinculo` y el tablero, como línea de base. |

- No hay un segundo escaneo en el flujo principal. La sesión de WAHA tiene store durante todo el vínculo, que es lo que permite importar el historial y reconciliar.
- Se vuelve a escanear solo por decisión del cliente o por una caída:
  - ampliar la profundidad (P4);
  - reconectar un vínculo caído o volver a vincular una línea, que son la misma operación (§6.6);
  - el modo opcional de §2.3.

### 2.3 Políticas opcionales

Ninguna está activa por defecto, y ninguna condiciona el resto del diseño.

**Purga de fuente** (`retencion_fuente_dias` > 0)

- Un job diario elimina de nuestra base el texto, los ids de proveedor y la identidad de los contactos con más de N días.
- Antes de purgar, materializa una ficha sin texto por conversación (§6.3).
- Para lo ya purgado, el drill-down muestra fichas sin texto (D8).
- Alcanza a **nuestra** base. La copia técnica de WAHA sigue viva mientras dure el vínculo, y el consentimiento lo dice.

**Modo sin copia en WAHA** (segunda etapa)

- Es para quien exija que tampoco persista la copia técnica.
- Después de importar el historial, el cliente hace un segundo escaneo sobre una sesión **sin store de mensajes**. Desde ahí los mensajes llegan solo por webhook.
- Costos:
  - un escaneo extra;
  - sin lectura por API no hay reconciliación ni cobertura verificable.
- Depende del punto 15 del spike, que no bloquea el MVP.

### 2.4 Riesgos propios de un vínculo largo

Están aceptados (D2) y se declaran en el consentimiento.

- La exposición al riesgo de bloqueo es continua. Ni WAHA ni WhatsApp documentan que un vínculo corto o de solo lectura sea más seguro.
- El teléfono tiene que conectarse a internet con regularidad. WhatsApp desvincula los dispositivos si el teléfono principal pasa ~14 días inactivo [VALIDAR].
- Ocupa de forma permanente uno de los dispositivos vinculados de la línea.
- La única contraindicación documentada por WAHA es re-vincular con una restricción de cuenta activa.
- El tablero muestra un indicador de salud de la cuenta (§3, estados especiales).

---

## 3. Flujo de onboarding, pantalla por pantalla

**Cambio respecto del orden pedido.** En NOWEB la profundidad del historial se fija **al crear la sesión**, antes del QR, y no se puede cambiar después. Por eso la elección de período se divide en dos:

- **Profundidad**: va antes del QR, dentro de la pantalla de alcance (P2).
- **Selección de chats y fechas**: va después del QR (P4).

### P0 — Entrada

- No hay landing pública (D1).
- Un admin de KIS crea la cuenta durante el alta del cliente, registra la línea con sus parámetros (§2.1) y le envía una **invitación** al dueño.
- La invitación abre una pantalla con la promesa, una captura del dashboard de ejemplo (marcada como ejemplo) y el CTA "Analizar mi WhatsApp".
- Para sumar otra línea a un cliente existente se repite el flujo desde P2, sin volver a cargar los datos del negocio.
- Acceso por email con link mágico de un solo uso y con vencimiento.
- La sesión web es una cookie HttpOnly atada al tenant. Nunca viajan tokens en la URL.
- La titularidad de la línea queda respaldada por el contrato de alta. Además, el dueño ve el dispositivo "vinculado" en su teléfono y puede quitarlo en cualquier momento.

### P1 — Tu negocio

Son 60 segundos y 4 campos. Se cargan una vez por cliente.

| Campo | Para qué se usa |
|---|---|
| Nombre del negocio y rubro (lista + "otro") | Elegir taxonomía. El rubro propone el `perfil_de_datos` por defecto (D3). Un admin de KIS lo puede cambiar; el cliente solo puede hacerlo más estricto. |
| Qué vendés, en una línea | Contexto para distinguir consulta comercial de charla. |
| Horario de atención (por día) | Tiempo de respuesta "en horario" y demanda fuera de horario. |
| Cuántas personas responden el WhatsApp | Interpretar tiempos y pendientes. |

Texto: "Con esto interpretamos mejor tus conversaciones. Podés cambiarlo después."

### P2 — Qué vamos a analizar y qué no (consentimiento y alcance)

Texto sugerido:

> **Qué hacemos:** leemos los mensajes de tus chats individuales para calcular métricas y detectar patrones.
> **Qué no hacemos:** no enviamos mensajes, no marcamos chats como leídos, no aparecemos "en línea", no leemos grupos, estados ni canales, no descargamos fotos, audios ni documentos.
> **Importante:** mientras tu línea esté conectada, WhatsApp le entrega a nuestro servidor de conexión una copia técnica de **todos** tus chats individuales, incluidos los personales, y la sigue actualizando con cada mensaje nuevo. No podemos filtrarla chat por chat. Un proceso automático la recorre una vez, al conectar, para contar mensajes y sugerirte qué excluir; no guarda el texto de los chats que excluyas, y ninguna persona los ve. Después de eso solo leemos los chats que elegiste. La copia se borra completa cuando desconectás desde Radar. Si quitás el dispositivo desde tu teléfono, se borra dentro de las 72 horas.
> **Cuánto dura:** *[según los parámetros de la línea; este es el texto para los valores iniciales]* la conexión no vence sola: queda activa hasta que la desconectes. Conservamos el texto de los chats que elegiste mientras tu línea siga dada de alta en Keep IT Simple, **aunque la desconectes de WhatsApp**. Para borrarlo usá "Desconectar y borrar todo". *[Variantes: "la conexión dura N días"; "el texto se borra a los N días y queda una ficha sin texto por conversación".]*
> **Quién procesa y dónde:** Keep IT Simple. Los datos se alojan en Railway (EE. UU.). Para el análisis se transfieren a Anthropic y, para agrupar preguntas, a OpenAI (ambos en EE. UU.). Antes de enviar nada reemplazamos teléfonos y nombres de contacto por códigos y tachamos DNI, tarjetas, CBU y direcciones que detectamos. El texto de los mensajes sí se envía: puede incluir datos que tus clientes escribieron y no garantizamos detectarlos todos. Anthropic y OpenAI pueden conservar hasta 30 días lo que les enviamos, por seguridad y prevención de abuso [VALIDAR plazo y retención cero]. No lo usan para entrenar sus modelos.
> **Para qué usamos el resultado:** para mostrarte este diagnóstico y configurar el servicio que contrataste. No usamos tus conversaciones para ningún otro fin ni las cruzamos con otros clientes.
> **Riesgo que tenés que conocer:** la conexión usa "dispositivos vinculados" mediante un cliente no oficial, que WhatsApp no avala. No enviamos mensajes ni hacemos las acciones de envío que se conocen como causa de bloqueo. Aun así, nadie garantiza que un vínculo de solo lectura sea seguro: el riesgo existe y no lo podemos cuantificar. Mientras dure la conexión el riesgo es continuo, no de una sola vez. La conexión ocupa uno de tus dispositivos vinculados, y si el teléfono pasa unos 14 días sin internet, WhatsApp la corta.

Controles de la pantalla:

- **Duración y retención**: la pantalla muestra los valores configurados para la línea. El cliente puede elegir valores **más estrictos**, nunca más laxos.
- **Profundidad** (radio): "Últimos 3 meses" o "Hasta 12 meses". Se mapea a `fullSync` false/true.
  - Aclaración visible: "No se puede cambiar después de escanear. Para ampliarla hay que desvincular y escanear de nuevo."
  - La sonda real (§0.4) sugiere que más allá de ~2,5 meses el historial es muy ralo aun con `fullSync`.
  - Si el spike lo confirma en cuentas Business:
    - esta opción se elimina;
    - se crea siempre con `fullSync:false`;
    - se comunica "analizamos tus últimos ~3 meses".
  - Este control existe solo si el spike elige NOWEB. En GOWS la profundidad la fijan variables del servidor.
- Checkbox obligatorio: "Soy titular o responsable de esta línea y confirmo el acuerdo de tratamiento de datos de mi contrato (versión X)".

Qué se registra:

- Se guarda `consents`, por línea: versión del texto, hash, fecha, IP y opciones elegidas.
- **No se crea ninguna sesión WAHA antes de este consentimiento.**
- La sugerencia de exclusión por IA externa quedó **fuera del MVP**: mandaría a un tercero justamente los chats personales que el paso existe para proteger. Se reemplaza por la heurística local de P4.

### P3 — Conectar WhatsApp (QR)

**Preparación en servidor.** El backend crea la sesión con la clave admin, que nunca llega al navegador.

- El nombre de sesión es **por vínculo** (`v_<id corto del vínculo>`), nunca por tenant ni por línea. Así ningún residuo de un vínculo anterior coincide con una sesión nueva.
- Cuerpo de ejemplo para NOWEB. Con GOWS el bloque es `config.gows`, y la verificación posterior compara los campos de ese motor.

```json
{"name":"v_<id corto del vínculo>","start":true,"config":{
  "metadata":{"tenant_id":"…","line_id":"…","link_id":"…"},
  "ignore":{"status":true,"groups":true,"channels":true,"broadcast":true},
  "noweb":{"markOnline":false,"store":{"enabled":true,"fullSync":false}},
  "webhooks":[{"url":"…/webhook/waha",
    "events":["message.any","message.ack","message.edited","message.revoked","session.status"],
    "hmac":{"key":"…"},
    "retries":{"policy":"exponential","delaySeconds":2,"attempts":15}}]}}
```

- Tras crear la sesión, el gestor relee `GET /api/sessions/{name}`.
  - Si `config.noweb.markOnline !== false`, o `store` o `ignore` no coinciden con lo pedido, **aborta antes de mostrar el QR**.
  - Motivo: `events` no valida nombres, y un `markOnline` mal ubicado deja el valor por defecto `true`.
  - Esta verificación la cubre un test de contrato.
- Las etiquetas no se suscriben en el MVP.
- El QR se pide desde el servidor y se sirve como imagen por nuestro endpoint autenticado.
- No se personaliza `deviceName`, porque rompe el código de vinculación.

**Pantalla**

- QR grande con los 4 pasos: "Abrí WhatsApp → Dispositivos vinculados → Vincular un dispositivo → Escaneá".
- Cuenta regresiva visible.
- Link secundario: "Estoy en el mismo teléfono → vincular con código".
  - Pide el número de la línea en formato internacional.
  - Llama a `auth/request-code` desde el servidor.
  - Muestra el código de 8 caracteres (`ABCD-ABCD`) con los pasos "Dispositivos vinculados → Vincular con número de teléfono".
  - Si falla, vuelve al QR sin perder la sesión.
  - [VALIDAR] en qué estado se puede pedir, si convive con la rotación del QR y cuánto dura.

**Estados**

| Estado WAHA | UI |
|---|---|
| `STARTING` | "Preparando conexión segura…" |
| `SCAN_QR_CODE` (se reemite en cada rotación) | QR nuevo automático + cuenta regresiva. |
| Ventana agotada → `FAILED` (~160 s según docs) | "El código venció. [Generar uno nuevo]". Reinicia la sesión y permite hasta 3 reinicios; después ofrece ayuda. |
| `WORKING` | "Conectado: **…1234** (Farmacia X)". Pide confirmación: "¿Es esta la línea del negocio?". Si no lo es: "Desconectar y borrar". |
| `PASSKEY_REQUIRED` (defensivo) | "WhatsApp pide una verificación adicional que todavía no soportamos desde acá." Ofrece contacto asistido. En NOWEB no está soportado. |
| Sin eventos por 20 s | Polling de respaldo a `GET /api/sessions/{name}`. |

- El límite de 4 dispositivos vinculados [VALIDAR] se muestra como ayuda si el escaneo falla del lado del teléfono.

### P4 — Elegí qué analizar

**Qué pasa entre `WORKING` y "Empezar análisis"**

- El vínculo queda en estado `pendiente_de_selección`.
- En ese estado, los eventos `message.*` que llegan por webhook **se descartan sin guardar el contenido**. Solo se guarda id de evento, tipo y motivo. Esos mensajes se recuperan después por backfill.
- Se hace una **pasada de conteo**:
  - La lista de chats sale de `GET /api/{session}/chats` paginado: id, nombre y fecha del último mensaje.
  - Nunca se usa `chats/overview`, porque devuelve el cuerpo del último mensaje.
  - La lista de chats no indica profundidad (§0.4): los conteos salen de leer los mensajes del período con `downloadMedia=false`.
  - De cada mensaje se conserva en memoria solo `{chat, timestamp, fromMe, tiene_texto}`. El `body` se descarta antes de cualquier escritura o log.
  - Texto para el cliente: "Contamos tus mensajes sin guardar su texto."
- Estabilización:
  - Se consulta hasta que el conteo de chats **y** de mensajes no cambia por 60–90 s, con tope de 10 minutos.
  - Mientras tanto la pantalla muestra "Encontramos 312 chats… seguimos buscando".
  - La duración real del sync es [VALIDAR].

**Pantalla**

- Un selector de período acotado por la profundidad elegida en P2.
- Una lista de chats con buscador y un toggle "Excluir" por chat.
- **Sugerencia local de exclusión**, sin IA externa y sin guardar contenido. Usa señales calculadas en nuestro servidor:
  - chats sin mensajes entrantes;
  - chats sin ningún mensaje propio y con patrón de notificación (códigos, links de seguimiento, cuentas de empresa);
  - chats sin palabras de consulta comercial (regex de `checkout_helper`) ni precios.
- Las sugerencias vienen preseleccionadas como "excluir" y siempre son editables.
- Contador: "Vamos a analizar **187 chats**". La cifra de conversaciones se muestra recién en P5, porque depende de los timestamps importados.
- CTA: "Empezar análisis".

**Reglas**

- El contenido de los chats excluidos **no pasa a nuestra base de análisis ni a la IA**. Permanece en la copia técnica de WAHA mientras dure el vínculo.
- En `wa_chats`, los chats excluidos conservan solo su `contact_hmac` (§6.4) y el motivo, sin nombre ni teléfono. Eso permite que la exclusión valga también en vínculos futuros de la misma línea.
- Un chat nuevo que aparece después de P4 entra como "pendiente de decisión":
  - cuenta para las métricas medidas con `{id, timestamp, fromMe}`, sin cuerpo;
  - no pasa a la IA hasta que el dueño lo confirme;
  - el tablero avisa: "3 chats nuevos esperan tu decisión".
- **Ampliar el período**:
  - si en P2 se eligieron 3 meses, el botón dice "Ampliar a 12 meses (requiere volver a escanear el QR)";
  - borra la sesión (`DELETE`), crea una nueva con `fullSync:true` y vuelve a P3;
  - nuestra base se conserva, y el nuevo vínculo importa además el tramo más antiguo que el anterior no cubría;
  - la deduplicación es por línea, así que los solapamientos se absorben (§6.3, punto 4).

### P5 — Progreso con resultados parciales

La pantalla tiene tres carriles que se van llenando:

1. **Importación**: mensajes importados, con checkpoint por chat. Ejemplo: "Importamos 412 de 530 chats; reintentando 118". La cobertura se rotula "provisoria" hasta que dos pasadas consecutivas coinciden (§6.3).
2. **Métricas medidas**: sin IA, apenas hay datos. Volumen, tasa y tiempos de respuesta, pendientes y mapa de calor horario.
3. **Análisis con IA**, por la vía rápida (§6.5):
   - 100 conversaciones más recientes, para pendientes y oportunidades;
   - 100 al azar estratificadas por semana, para intenciones y preguntas frecuentes;
   - los porcentajes se calculan solo sobre la muestra al azar;
   - el resto va por lotes en segundo plano.

Otros elementos:

- Los primeros hallazgos se generan al terminar la vía rápida, con la etiqueta "**Preliminar — 200 de N**". Se regeneran al completar el lote.
- El chequeo de poco volumen (<30 conversaciones) ocurre acá, después de importar.
- El cliente puede cerrar la pestaña. Recibe un email cuando hay hallazgos. Los emails llevan solo conteos y un link autenticado, nunca citas ni datos de contactos.
- Si la purga de fuente está activa, hay un contador visible: "Te quedan N días para revisar las conversaciones originales de este período".

### P6 — Primer dashboard

Copy de cabecera: "Esto es lo que encontramos en tus últimas N conversaciones. Tocá cualquier número para ver las conversaciones que lo explican."

Estados de la pantalla:

| Estado | Qué muestra |
|---|---|
| Preliminar | Banda amarilla "Análisis en curso: 200 de 1.140. Los porcentajes pueden moverse." |
| Completo, vínculo vivo | Fecha de corte, cobertura y "Midiendo en vivo desde el DD/MM". |
| Vínculo terminado (línea `sin_vinculo`) | "Tu línea se desconectó el DD/MM. El tablero muestra lo medido hasta esa fecha. Conservamos las conversaciones ya importadas, porque así está configurada esta línea." Botones "Volver a vincular" y "Borrar todo". |
| Perfil `sensible` sin IA habilitada | Solo paneles medidos + explicación (§7). |

Contenido: §4 y §5.

### 3.1 Consola KIS (D10)

Uso exclusivo de admins de KIS. Todo lo que hace contra WAHA lo hace el backend por API; ninguna clave de WAHA llega al navegador.

**C1 — Líneas**

- Tabla de todas las líneas de todos los clientes: cliente, línea (enmascarada), estado de la línea (`vinculada`, `sin_vinculo`, `de_baja`), estado de la sesión WAHA, `observado_hasta`, último mensaje recibido, cobertura de la sincronización (provisoria / estable, %), huecos abiertos, salud de la cuenta, worker de WAHA, gasto de IA del mes contra el tope.
- Semáforo: verde (`WORKING` con tráfico), amarillo (sincronizando, sospecha de silencio, cobertura < 90 %), rojo (caída, `FAILED`, restricción, worker lleno).
- Filtros por estado y por cliente. Se refresca sola (polling cada 15 s o eventos del servidor).

**C2 — Vincular línea**

1. Elegir cliente y línea, o crearlos (alta del tramo 1).
2. **Consentimiento asistido.** Se muestra el texto de P2 con los parámetros de la línea. El admin marca "El titular leyó y aceptó en esta sesión", indica el modo (presencial / videollamada) y el nombre de quien aceptó. Queda en `consents` con `cargado_por` = el admin y `modo = asistido`, y se envía copia por email al dueño. Sin esto no se crea la sesión.
3. **Profundidad** (si el motor es NOWEB) y botón "Generar QR".
4. El backend elige worker por capacidad, crea la sesión, verifica la configuración y muestra el QR con cuenta regresiva, rotación automática, reintento y código de vinculación, igual que P3.
5. **Estado en vivo en la misma pantalla:** `STARTING` → `SCAN_QR_CODE` → `WORKING` (con confirmación del número enmascarado) → pasada de conteo → selección de chats (P4, que el admin puede hacer junto al dueño o dejar para el dueño) → importación con progreso → cobertura estable.
6. Al terminar: link al tablero de la línea y email de invitación al dueño.

El QR se muestra solo en esta pantalla, solo mientras dura el intento, nunca se loguea ni se manda por mensaje.

**C3 — Métricas de la línea**

- El tablero de §4 en modo agregado: KPI medidos, KPI estimados, hallazgos y cobertura.
- **Sin conversaciones ni fragmentos**: el drill-down muestra solo conteos. Ver conversaciones sigue requiriendo el permiso temporal de Soporte KIS que otorga el dueño (§4.4).

**C4 — Acciones de operación** (cada una auditada)

- Reconectar (nuevo QR), desconectar, desconectar y borrar todo (con doble confirmación), reintentar sincronización, re-analizar la línea (con costo estimado a la vista), mover de worker (re-escaneo coordinado).
- Bloqueadas mientras haya una restricción de cuenta activa, según §3 "Estados especiales".

**Permisos.** Solo el rol `admin` del tenant KIS. Todas las vistas y acciones pasan por las funciones `SECURITY DEFINER` del tramo 1 y quedan en `access_audit_log`.

### Estados especiales

| Situación | Detección | Experiencia |
|---|---|---|
| **Vínculo caído** (dispositivo quitado desde el teléfono, teléfono inactivo, `FAILED`) | `session.status` + chequeo de salud cada 5 min. | Banner "Tu WhatsApp se desconectó el DD/MM. [Reconectar]" + email. Reconectar crea un vínculo y una sesión nuevos de la misma línea; el tablero es de la línea y no se reinicia. El tramo sin captura **se rellena con el historial del teléfono**; solo queda como "sin datos" lo que el historial no devuelva. Quitar el dispositivo desde el teléfono **no borra nada en WAHA**: el job de fin del vínculo anterior corre cuando el nuevo llega a `WORKING`, o a las 72 h, lo que ocurra primero. [VALIDAR] qué estado emite cada motor ante ese desvínculo. |
| **Sesión conectada sin tráfico** (socket colgado, teléfono sin conexión) | Regla de silencio: sin ningún mensaje nuevo, por webhook ni por reconciliación, durante más de max(4 h dentro del horario de P1, 3 veces el p95 del intervalo entre entrantes de esa línea para esa franja). | Estado interno `sospecha_de_silencio` y alerta interna. **Un** reinicio automático de la sesión, como máximo cada 24 h y nunca con una restricción activa. `observado_hasta` (§6.3) deja de avanzar desde el último evento real. Si tras el reinicio llega el atraso, el tramo se da por recuperado. Si no: banner y email "No recibimos mensajes desde el DD/MM HH:MM. Revisá que el teléfono tenga conexión", y el tramo queda como "posible hueco" en las series, fuera de los días observados. |
| **Poco volumen** (<30 conversaciones, medido en P5) | Conteo tras importar. | Modo lista: conversaciones una por una con sus etiquetas, en conteos y sin porcentajes. Hallazgos redactados como "5 consultas sin responder", nunca "el 17 %". Se ofrece ampliar la profundidad (con re-escaneo, si el motor lo permite). Con el vínculo vivo, el tablero se completa solo con el paso de las semanas. |
| **Historial no disponible** (store vacío tras el tope, sync fallido) | 0 mensajes anteriores a la vinculación. | **Modo recolección**: "WhatsApp no nos entregó tu historial. Empezamos a medir desde hoy." Día 1: bandeja de pendientes y tiempo de respuesta en vivo. Días 3 y 7: resumen por email con conteos. Los estados finales ("sin responder", KPI 7) aparecen a partir del día ~10, cuando las primeras conversaciones completan su ventana de observación (§4.2). Etapa 2: subir un "Exportar chat" (.txt). |
| **Errores de sincronización** | Checkpoints por chat; contabilidad pedido/guardado/fallido por pasada. | Reintentos con backoff, cobertura parcial declarada y botón "Reintentar". Nunca se muestra un KPI como completo con menos del 90 % de cobertura sin aviso. La contabilidad no detecta lo que WAHA nunca guardó (§4.1). |
| **Restricción de cuenta** | Restricción activa = `reachoutTimelock` vigente, con fecha de fin futura. `messageCapping` se muestra en "Salud de la cuenta" y solo cuenta como restricción si el cupo está agotado [VALIDAR semántica en el spike]. | Indicador "Salud de la cuenta" y alerta interna. Mientras esté activa, el sistema **no** reinicia la sesión y no ofrece "Reconectar": WAHA indica que re-vincular no levanta la restricción. La reconciliación **sigue**, porque solo lee. El fin de vínculo y "Desconectar y borrar todo" sí se ejecutan. Lo que se bloquea es volver a vincular: se guarda en `links.restriccion_hasta` la fecha de fin vigente, y si no hay fecha, habilitarlo requiere un admin de KIS. El esquema incluye sanciones por política de comercio de medicamentos. |
| **El cliente quiere desconectar** | Botones visibles en todas las pantallas. | Dos acciones distintas: "**Desconectar**", que conserva el tablero y el texto según los parámetros, y "**Desconectar y borrar todo**" (§7). Mientras una línea esté `sin_vinculo`, un email cada 6 meses, solo con conteos, recuerda que el texto sigue guardado y ofrece borrarlo. |

---

## 4. Dashboard inicial

### 4.1 Estructura

1. **Encabezado**
   - Línea conectada (enmascarada), estado del vínculo y salud de la cuenta.
   - Período.
   - **Cobertura**: conversaciones analizadas, excluidas, "no son consultas de clientes", "sin poder leer (audios)" y "en observación".
   - Frescura del dato. Si la purga está activa, hasta qué fecha hay texto disponible.
2. **Lo que encontramos**: 3–5 hallazgos (§5).
3. **Medido**, con badge gris "Medido — calculado sobre los mensajes que WhatsApp nos entregó": volumen, tasa y tiempo de primera respuesta, pendientes y demanda por hora.
4. **Estimado por IA**, con badge violeta "Estimado por IA — revisable": intenciones, oportunidades y preguntas frecuentes. Flujos y potencial de automatización aparecen cuando se alcanza su umbral de datos; hasta entonces muestran "Datos insuficientes: faltan N conversaciones".
5. **Panel lateral de conversaciones** (drill-down), que se abre desde cualquier número.

**Reglas transversales**

- Todo porcentaje muestra su `n`.
- Con `n < 30` se muestran conteos en lugar de porcentajes.
- Con `n < 10` la tarjeta pasa a "Datos insuficientes" y dice qué falta.
- Limitación general visible: "No vemos chats borrados, mensajes temporales ni mensajes eliminados antes de vincular. Si borrás los chats resueltos, los pendientes quedan sobrerrepresentados."
- La ventana efectiva del historial empieza en la fecha desde la cual ≥ 80 % de los chats incluidos ya tienen mensajes importados.
- Si las conversaciones "no legibles" superan el 40 %:
  - los paneles de IA avisan "Tu negocio usa muchos audios: estos porcentajes describen solo las conversaciones escritas";
  - los hallazgos priorizan los medidos.

### 4.2 Definiciones base (deterministas, etapa E0)

**Conversación**

- Es una secuencia de mensajes de un chat.
- Se cierra a las **24 h** del último mensaje del negocio.
- Se cierra a las **72 h** si el último mensaje es del cliente.
- Una respuesta tardía del negocio se adjunta a la conversación que quedó abierta.
- Las ventanas se configuran por tenant.

**Hilo**

- Es el conjunto de todas las conversaciones de un mismo chat.
- Los estados finales (sin responder, pendiente, sin seguimiento) se evalúan **a nivel hilo**.
- Una conversación solo cuenta como tal si en los **7 días siguientes** (configurable) nadie volvió a escribir en ese chat.
- Si alguien vuelve, la conversación se marca "continuó después" y sale de KPI 3 y 7.

**Ventana de observación**

- Los 7 días siguientes tienen que ser días **observados**.
- `fin_de_datos` es el momento actual mientras el vínculo está en `WORKING` y con tráfico. Si está caído, mudo o terminado, es `observado_hasta`: el último instante con captura verificada (§6.3). Así los días sin captura no cuentan como observados.
- Una conversación cuyo último mensaje es posterior a `fin_de_datos − 7 días` queda **"en observación"**: cuenta en KPI 1, 2 y 4, pero no en "sin responder" (KPI 3) ni en KPI 7.
- Con el vínculo vivo, esa ventana se va corriendo sola: las conversaciones en vivo llegan a su estado final con ~10 días de demora.
- Cuando un vínculo termina, toda conversación todavía abierta pasa a "abierta al cierre" y sale de la tasa de respuesta.

**Otras definiciones**

- **Iniciada por el cliente / por el negocio.** Las iniciadas por el negocio se cuentan aparte y no entran en KPI 2, 3 ni 4.
- **Conversación abierta.** Pasaron menos de 72 h desde el último mensaje del cliente. Va a la bandeja de pendientes, pero no al contador "sin responder" ni a la tasa de respuesta.
- **No requiere respuesta.** Se cumple una de dos condiciones:
  - el chat no tiene ningún mensaje propio en toda la ventana y sus entrantes tienen patrón de notificación;
  - el usuario lo marcó como personal o proveedor.
- **No legible.** La consulta inicial del cliente, o más del 50 % de sus mensajes, son audio o imagen sin texto. Queda fuera de los denominadores de KPI 6–10 y se cuenta en Cobertura.
- **Respuesta automática.** Es un texto del negocio repetido literal en muchos chats y enviado a segundos del mensaje entrante. Se excluye de la "primera respuesta humana". En el historial `source` viene siempre como `app` y no distingue nada, así que ahí solo vale la heurística [VALIDAR tasa de falsos positivos].
- **Identidad.** `@lid` y `@c.us` se unifican con `merge=true` más el mapeo de lids.

### 4.3 Tabla de KPI

| # | Indicador | Pregunta de negocio | Cálculo y datos | Visualización | Acción del cliente | Limitaciones / cuándo no mostrar |
|---|---|---|---|---|---|---|
| 1 | **Volumen de conversaciones**. *Medido* | ¿Cuánta demanda entra por WhatsApp? | Conversaciones iniciadas por el cliente, por día y semana. Necesita `timestamp`, chat y `fromMe`. | Serie temporal. El tramo importado por historial se dibuja distinto del tramo en vivo. | Dimensionar el equipo; detectar picos. | **Sin variación ni tendencia sobre historial importado**: el sync es desparejo y un "+40 %" puede ser un artefacto. La variación se habilita con ≥ 2 períodos completos medidos en vivo. Período = semana ISO o mes calendario; es **completo** si sus tramos "sin datos" o "posible hueco" no suman más del 5 % de sus horas de atención. "Nuevo vs. recurrente" queda fuera del MVP: la profundidad útil del historial es de ~2,5 meses. |
| 2 | **Tasa de respuesta y tiempo de primera respuesta humana**. *Medido* | ¿A cuántos les contestamos y cuánto esperan? | Universo: conversaciones iniciadas por el cliente y ya cerradas. Tasa = con ≥1 respuesta humana / universo. Tiempo = Δ entre el primer entrante y el primer `fromMe` no automático; versión "en horario" con P1. | Frase única: "Respondiste el 82 % (236 de 288); de esas, mediana 3 h 10 min". Histograma con tramo **"sin respuesta"**; denominador = todo el universo. | Fijar objetivo de respuesta; cubrir franjas lentas. | No ve respuestas por llamada o mostrador (hay motivo de corrección). Con #2267 pueden faltar primeros mensajes de leads por anuncios. No mostrar el tiempo con <10 respondidas. |
| 3 | **Sin responder y pendientes**. *Medido*; *inferido* solo para "es consulta de cliente" y "es una pregunta" | ¿A quién le debemos una respuesta? | Sin responder: conversación cerrada, iniciada por el cliente, sin `fromMe` humano, a nivel hilo y fuera de la ventana de observación. Pendientes: conversaciones abiertas cuyo último mensaje es del cliente. | "Sin respuesta: 23 (medido); de ellas, 15 parecen consultas de clientes (estimado por IA)". **Bandeja accionable** por antigüedad, en vivo mientras dure el vínculo. | Responder hoy desde su WhatsApp; es el valor del día 1. | `ack` no se usa: en historial viene `-1/ERROR` para los entrantes (verificado), y "visto" tampoco implica atendido. Cortesías ("gracias", 👍) excluidas por regla + IA. Si el último mensaje es un audio: "pendiente — audio sin leer" (medido, sin IA). Excluye "no requiere respuesta". Con el vínculo terminado queda solo el conteo "Pendientes al cierre: N". |
| 4 | **Demanda por día y hora, y fuera de horario**. *Medido* | ¿Cuándo escriben y cuánto llega con el local cerrado? | Entrantes por día de semana × hora (zona Buenos Aires); % fuera del horario de P1. | Mapa de calor 7×24 + KPI. | Ajustar turnos; mensaje de ausencia o bot fuera de horario. | Necesita el horario cargado; si no, solo el mapa de calor. |
| 5 | **Cobertura y calidad del análisis**. *Medido* | ¿Cuánto puedo confiar en este tablero? | % importado (provisorio/estable), % analizado, % no legible, % "no es consulta de cliente", % en observación; tasa de acuerdo usuario–IA. | Medidor en el encabezado. | Ampliar el período; revisar etiquetas. | La tasa de acuerdo se muestra solo con n ≥ 30, con su n, rotulada "sobre conversaciones revisadas". |
| 6 | **Top de intenciones**. *Estimado por IA* | ¿Qué consultan mis clientes? | Intención principal por conversación, con taxonomía cerrada: precio, disponibilidad/stock, compra/pedido, seguimiento de pedido, soporte/uso, reclamo, turnos/horarios/ubicación, medios de pago/envío, otro (etiqueta libre), **no es una consulta de cliente**. Lleva evidencia y confianza. | Barras horizontales; "otro" desplegable. | Decidir qué estandarizar o automatizar primero. | "No es consulta de cliente" y "no legibles" salen del denominador de KPI 6–10. No leer de `interacciones.intencion` del bot. Si "otro" > 35 %, ofrecer afinar la taxonomía. Porcentajes solo sobre muestra al azar o lote completo. |
| 7 | **Oportunidades posiblemente desaprovechadas**. *Estimado por IA sobre un estado medido* | ¿Dónde hubo interés de compra sin respuesta o seguimiento? | Señal de compra (IA + regex) **y** estado final a nivel hilo: **A** sin respuesta; **B** pregunta pendiente; **C** el negocio informó por escrito, el cliente no volvió a escribir en 7 días y no hubo seguimiento. Excluye "cierre mencionado", "continuó después" y "en observación". | Primera sesión: "**N conversaciones para revisar**", no un hallazgo cuantificado, hasta que el usuario revise ≥ 10. Después: contadores A/B/C + lista priorizada. | A y B: responder. C: rutina de seguimiento. | **Nunca "venta perdida"**. C no se asigna si el último mensaje del negocio fue un audio. Sin montos. Entra al piloto solo si pasa el eval offline (§8). **Regla de ocultamiento:** con ≥ 20 oportunidades revisadas de la muestra al azar y "no es correcto" ≥ 30 %, los contadores A/B/C se reemplazan por "N conversaciones para revisar". Con < 20 revisadas, KPI 7 queda rotulado "sin validar por el cliente" y no genera hallazgos cuantificados. |
| 8 | **Flujos detectados y puntos de corte**. *Estimado por IA* | ¿Cómo se desarrolla una consulta típica y dónde se corta? | Secuencia de etapas por conversación: consulta → precio → disponibilidad → objeción → negociación → cierre mencionado → seguimiento → posventa. Conteo de transiciones y "termina acá" por etapa. | Embudo del flujo más frecuente con % de corte por etapa; top 3 de secuencias. Sankey en etapa 2. | Atacar la etapa con más cortes. | **Umbral:** ≥ 50 conversaciones legibles con ≥ 3 mensajes. Un corte no es una pérdida. Las etapas informadas por audio no se ven. |
| 9 | **Preguntas frecuentes**. *Estimado por IA* | ¿Qué preguntas se repiten y cómo las contestamos? | Pregunta normalizada (IA) → embedding → agrupado (pgvector) → nombre del grupo. Por grupo: frecuencia, **respuesta típica** (texto que E4 genera a partir de las respuestas normalizadas del grupo; nunca un extracto literal) y **consistencia** (% de respuestas normalizadas del grupo que coinciden con la dominante). | Tabla ordenada: pregunta, cantidad, respuesta típica y consistencia. | Respuestas rápidas; base de conocimiento del bot. | Grupos con <5 casos no se listan. Sin "consistencia" si > 30 % de las respuestas son audio. Solo texto seudonimizado sale a embeddings. |
| 10 | **Potencial de automatización**. *Estimado por IA* | ¿Qué parte de la atención podría resolver un bot? | Clases: **automatizable ya** (cae en un grupo de FAQ con respuesta consistente), **automatizable con integración** (precio/stock necesitan catálogo), **requiere persona**. | Barra apilada, siempre como **rango**. | Decidir alcance del bot y prioridad de integración. | **Umbral:** ≥ 100 conversaciones analizadas. Es la inferencia más blanda: nunca un punto. |

### 4.4 Drill-down con permisos

**Filtro**

- Cada tarjeta abre el panel lateral con **las conversaciones que componen ese número**.
- El filtro se resuelve en el servidor a partir de la definición del KPI.
- No depende de ids que mande el navegador.

**Fila y vista**

- Cada fila muestra: contacto enmascarado (…1234), fecha, etiqueta, confianza y **fragmento de evidencia**. Es el patrón `coincidencia` que ya existe.
- Al abrir una conversación, los mensajes de evidencia aparecen resaltados y los medios se ven como marcador ("[audio]", "[imagen]").
- Si la purga de fuente está activa, las conversaciones ya purgadas se abren como ficha sin texto (§4.5).

**Permisos**

| Rol | Acceso |
|---|---|
| Dueño | Todo, en todas las líneas del cliente. Ve el texto original, revela teléfonos, invita usuarios y cambia roles. |
| Gestor | Dashboard y conversaciones en versión **redactada por E1** (DNI, dirección, tarjeta y CBU tachados), con teléfono enmascarado. Se puede limitar a líneas determinadas. |
| Lector | Solo números. Sin fragmentos, respuestas típicas ni hallazgos con citas. |
| Soporte KIS | Sin acceso a contenido por defecto. El Dueño lo otorga desde el panel, con vencimiento de 24–72 h, visible para el cliente y auditado. |
| Comercial KIS | Solo agregados que el cliente comparte. |
| Admin KIS (Consola, §3.1) | Todas las líneas: estado, sincronización y métricas agregadas. Sin conversaciones salvo con permiso de Soporte otorgado por el dueño. |

- El modelo de usuarios es **único para Radar y Remedia**:
  - `plan-multitenant.md` es una propuesta sin implementar, y Radar construye `tenants/users/memberships` por primera vez.
  - Roles unificados: `admin` (KIS), `dueño`, `operador/gestor`, `lector`.
  - Autenticación: link mágico o contraseña con política, según el producto.
- El rol se valida en el servidor en cada consulta.
- Los ids son UUID opacos. Nunca hay teléfonos ni claves en la URL.
- `access_audit_log` se retiene 24 meses y registra:
  - apertura de conversación;
  - revelado de teléfono;
  - búsqueda de texto;
  - exportación;
  - corrección;
  - exclusión y supresión;
  - cambios de rol y de parámetros;
  - todo acceso de personal de KIS.
- Cada fila guarda actor, rol, acción, tipo de objeto, UUID interno del objeto, fecha e IP. **Nunca** guarda teléfonos, JID, nombres ni texto.
  - En "búsqueda de texto" guarda solo la longitud del término y la cantidad de resultados.
  - En "revelado de teléfono" guarda el UUID del chat.
  - En "supresión" guarda el `contact_hmac`.

### 4.5 Evidencia, confianza y corrección

**Evidencia**

- Cada campo inferido guarda los mensajes que lo sustentan.
- El servidor valida que los índices citados existan en esa conversación. Si no existen, descarta el campo.

**Confianza por conversación: alta, media o baja**

- Se deriva de tres cosas:
  - el acuerdo entre la regla determinista y la IA;
  - la cantidad de evidencia;
  - la autoevaluación del modelo.
- La autoevaluación sola no alcanza, porque está mal calibrada.

**Confianza de un agregado**

- Es el % de sus conversaciones con confianza alta.
- El valor se muestra como rango: [solo confianza alta ; todas].

**Corrección**

- En cada conversación aparece "¿Es correcto?" con opciones Sí / No.
- Si la respuesta es No, se elige la etiqueta correcta o uno de estos motivos:
  - "ya compró por otro canal";
  - "sí respondimos por otro medio (llamada, mostrador)";
  - "no requería respuesta";
  - "es personal";
  - "es proveedor";
  - "otro".
- La etiqueta humana pisa a la IA y el agregado se recalcula en el momento.

**Revisión guiada**

- El producto propone **rondas de 10** conversaciones al azar, con al menos 5 de KPI 7 cuando existan.
- Hay una ronda en la primera sesión y una con cada recordatorio (días 2 y 5). Tres rondas dan las 30 revisadas que pide KPI 5.
- Con el vínculo vivo, se propone una ronda por mes sobre conversaciones nuevas.
- Solo estas muestras alimentan la medición de precisión. Las que el usuario eligió abrir no, porque sesgan.

**Uso de las correcciones**

- Se guardan en `analysis_feedback` como: conversación, campo, valor de la IA, valor humano, motivo de lista cerrada, usuario y fecha.
- **No hay texto libre**: el motivo "otro" no pide explicación.
- Las conversaciones corregidas sirven como ejemplos **solo del mismo tenant**, leyendo el texto desde el almacén de fuente. No se copian a ninguna otra tabla, archivo ni caché.
- Un ejemplo deja de existir cuando se purga o se suprime su conversación.
- El conjunto de evaluación con texto se arma únicamente con corpus sintético y cuentas de prueba consentidas (§8), nunca con conversaciones de clientes.

**Excluir y suprimir**

- "Excluir este chat" y "Suprimir contacto" hacen lo mismo sobre nuestra base:
  - borran mensajes, análisis, evidencia y feedback de ese contacto;
  - lo sacan de los grupos de FAQ;
  - recalculan los agregados;
  - quedan auditados.
- **Suprimir contacto** es la herramienta para los derechos del titular:
  - el Dueño ingresa el teléfono;
  - el servidor calcula su `contact_hmac` (§6.4), borra todo lo asociado y guarda solo esa clave en `suppressions`;
  - el teléfono ingresado no se guarda ni se escribe en auditoría;
  - funciona aunque la identidad del contacto ya haya sido purgada.
- El webhook, la pasada de conteo y el backfill calculan el HMAC de cada contacto **antes de cualquier escritura**, y descartan los excluidos y los suprimidos.
- Limitación: un contacto visto solo como `@lid`, sin mapeo a teléfono, no se puede encontrar por teléfono [VALIDAR cobertura del mapeo].
- **La supresión y la exclusión alcanzan a nuestra base, no a la copia técnica de WAHA**, que no permite borrar chats sueltos (§0.6).
  - Esa copia, incluidos los mensajes nuevos del contacto suprimido, persiste mientras dure el vínculo. Con un vínculo permanente, hasta que la línea se desconecte.
  - La constancia de supresión lo dice con esas palabras.
  - Si el titular exige el borrado también ahí, las salidas son terminar el vínculo o el modo sin copia en WAHA (§2.3).
  - [DICTAMEN] si esto alcanza para el derecho de supresión con un vínculo permanente.

**Fichas sin texto (solo si la purga de fuente está activa)**

Para una conversación ya purgada, el drill-down muestra:

- fecha y franja horaria;
- contacto como código interno, sin teléfono;
- etiquetas de taxonomía cerrada y secuencia de etapas;
- estado, tiempos de respuesta y confianza;
- si fue corregida por una persona.

Consecuencias:

- Una ficha solo se puede marcar como "no corresponde", lo que la saca del agregado.
- Por eso, con la purga activa, la revisión guiada tiene fecha límite y el tablero la muestra.

**Fragmentos de evidencia** (hasta 3 por conversación, de no más de 200 caracteres):

- Solo importan si la purga está activa, y están **apagados por defecto** (`retener_fragmentos`).
- E1 no tacha nombres propios ni medicamentos escritos dentro del texto. Por eso, antes de guardarse se descartan los fragmentos que tengan:
  - secuencias de 6 o más dígitos;
  - emails;
  - el nombre del contacto;
  - un nombre propio después de "soy", "me llamo" o "a nombre de".
- Si se activan, P2 lo dice de forma explícita.

---

## 5. Primer momento de valor

### 5.1 Cómo se eligen los 3–5 hallazgos

- Un generador arma los candidatos con **plantillas cuyos números salen de SQL**. La IA solo redacta; no produce cifras.
- Los ordena por impacto (conversaciones afectadas) × confianza × accionabilidad.
- Reglas de composición:
  - Siempre se incluye al menos uno **medido**.
  - A lo sumo dos son inferidos.
  - No hay dos hallazgos del mismo tipo.
  - Cada uno tiene un botón "Ver las N conversaciones" y una acción sugerida.
  - Mientras el análisis es parcial, llevan la etiqueta "Preliminar — 200 de N".

### 5.2 Ejemplos

**⚠️ EJEMPLOS ILUSTRATIVOS — datos inventados, no son resultados reales.**

| Tipo | Texto del hallazgo (ejemplo) | Siguiente paso que dispara |
|---|---|---|
| Medido (+ estimado) | "**23 conversaciones entrantes de los últimos 30 días no tuvieron respuesta**; 15 parecen consultas de clientes. 14 entraron fuera de tu horario." [Ver las 23] | Responder las recientes hoy + mensaje de ausencia → **atención fuera de horario**. |
| Medido | "**Respondiste el 82 % de las consultas (236 de 288); la mediana de espera fue 3 h 10 min**, y los sábados 7 h." [Ver las más lentas] | **Derivar al equipo**: un responsable por franja. |
| Estimado por IA | "**Entre el 36 % y el 41 % de las consultas escritas son precio o disponibilidad** (muestra de 100)." [Ver ejemplos] | **Automatizar**: conectar catálogo; es el caso central de Remedia. |
| Estimado por IA (para revisar) | "**Encontramos 17 conversaciones con interés de compra donde pasaste el precio y nadie volvió a escribir en 7 días.** No sabemos si compraron por otro canal. Revisalas." [Revisar las 17] | **Mejorar seguimiento comercial**: lista + plantilla para enviar a mano. |
| Estimado por IA | "**6 preguntas se repiten 94 veces** (horarios, obras sociales, envíos, medios de pago…) y las respondés casi siempre igual." [Ver preguntas y respuestas] | **Automatizar FAQ**: copiar o descargar los pares pregunta/respuesta. |

### 5.3 Del hallazgo al siguiente paso

Hay un único CTA principal, según el hallazgo dominante:

| Patrón dominante | CTA | Insumo que entrega Radar | Destino en el MVP |
|---|---|---|---|
| Alta proporción de FAQ y precio/stock | "Armar mi bot con estas respuestas" | FAQ con respuestas típicas; intenciones priorizadas; horarios de demanda. | Brief precargado + agendar llamada. Aclara que el bot requiere migrar el número a la API oficial. |
| Muchas sin respuesta fuera de horario | "Activar atención fuera de horario" | Franja horaria; top de consultas de esa franja. | Instructivo + texto sugerido para el mensaje de ausencia de WhatsApp Business. |
| Precio informado y sin seguimiento (KPI 7-C) | "Crear rutina de seguimiento" | Lista C + plantilla. | Copiar plantilla y enviar a mano. El envío automático es etapa 2 y **solo por API oficial**. |
| Tiempos altos con equipo chico | "Repartir consultas en el equipo" | Carga por franja; pendientes por antigüedad. | Guía de etiquetas y turnos. |

---

## 6. Enfoque técnico

### 6.1 Roles: MCP vs. API vs. webhooks

| Pieza | Rol | No usar para |
|---|---|---|
| **MCP de WAHA** | Exploración en diseño, consulta del OpenAPI y QA en staging, **solo con cuentas de prueba consentidas**. | Producción. **Nunca se conecta a la sesión de un cliente**: pasaría chats reales por una sesión de agente, fuera de la seudonimización y de la lista de subencargados. Además está atado a una sesión, devuelve JSON en string con tope de tamaño, no tiene push y expone decenas de herramientas de escritura. |
| **API HTTP de WAHA** | Ciclo de vida de sesiones (clave admin, solo en el módulo "gestor de sesiones"); QR; pasada de conteo, backfill y reconciliación con **clave de solo lectura por sesión**. | Enviar, marcar leído, presencia, archivar: nunca. |
| **Webhooks de WAHA** | Flujo incremental en vivo: `message.any`, `message.ack`, `message.edited`, `message.revoked`, `session.status`. | Historial (probablemente no se emite). `engine.event` (no respeta `ignore`). |

- El soporte en producción se hace desde nuestro backend, con el rol Soporte KIS.

**Garantía de "solo lectura"**

- La clave de solo lectura lleva `actions` explícito. Si queda en `null`, aplican todos los permisos por defecto.
- El cliente HTTP propio usa una lista blanca de rutas.
- Un test de contrato falla si aparece una ruta de escritura.
- Cualquier clave de sesión puede bajar medios, y el scope `control` incluye logout.
- Por eso el navegador nunca recibe una clave de WAHA.

### 6.2 Topología

**Radar corre como despliegue propio.** Usa el mismo repo y la misma imagen, pero con servicio web, worker, Postgres y Redis propios. Nunca usa la base de un cliente de Remedia: hoy hay un despliegue por cliente, y eso está escrito en los documentos comerciales. En ese despliegue el router del bot no se monta.

**`web`**

- Routers nuevos: `onboarding`, `radar_api` y `webhook_waha`.

**`worker` — proceso nuevo, otro comando**

- Consume una cola en Postgres: tabla `jobs` con `FOR UPDATE SKIP LOCKED`, sin infraestructura nueva.
- Reclama jobs desde una tabla sin contenido, y recién entonces entra al contexto del tenant.
- El bot procesa todo dentro del request y depende de locks en memoria de un solo proceso (`webhook.py:67-77`, `Dockerfile:12`). Ese patrón no se hereda.

**`waha` — servicio aparte para clientes**

- No es la instancia de desarrollo.
- Corre en Railway con volumen (precedente: `image_server/`) o con sesiones en PostgreSQL.
- Si usa PostgreSQL, es **un servidor Postgres propio de WAHA, distinto del de Radar**:
  - WAHA crea una base por sesión y **él mismo ejecuta `DROP DATABASE … WITH (FORCE)`** al borrar la sesión;
  - por eso su usuario necesita permisos de crear y borrar bases, y no debe alcanzar la base de Radar;
  - requiere PostgreSQL ≥ 13 y `max_connections` dimensionado por sesión.
- Ese servidor o volumen **no tiene backups ni snapshots**:
  - contiene todos los chats de cada línea, incluidos los que el cliente excluyó;
  - un respaldo sobreviviría a la desconexión, y P2 promete que la copia "se borra completa al desconectar";
  - si se pierde, se reimporta con un nuevo escaneo.

Endurecimiento obligatorio:

- clave de API hasheada;
- dashboard y Swagger deshabilitados o con credenciales propias (por defecto vienen `admin/admin`);
- `WAHA_PRINT_QR=False`, porque el QR en los logs es una credencial;
- `WAHA_PRESENCE_AUTO_ONLINE=False`;
- `WAHA_SESSION_CONFIG_IGNORE_*=true` como defensa en profundidad;
- descarga de medios apagada en eventos y en API, con `downloadMedia=false` explícito en cada lectura, porque el valor por defecto es `true`;
- sin exposición pública;
- Apps deshabilitadas (`WAHA_APPS_ENABLED=False`): el borrado de sesión no purga el almacenamiento de Apps;
- nivel de log sin cuerpos de mensajes [VALIDAR que el nivel elegido no imprime contenido];
- [VALIDAR] que la imagen no envíe telemetría.

Otros puntos del servicio:

- Capacidad según docs: ~50 sesiones NOWEB en 2 CPU / 4 GB. Con vínculos permanentes, las sesiones vivas son las líneas dadas de alta, y no bajan solas.
- **Admisión.**
  - `waha_workers` existe desde el día uno, con `max_sesiones` y `disco_max_gb`.
  - El gestor elige worker al crear el vínculo.
  - **No muestra el QR** si el worker elegido supera el 80 % de sus sesiones o el 70 % de su disco: alerta interna y "Estamos preparando tu conexión, te avisamos por email".
  - Esta regla de elección entra en el tramo 2 del MVP.
- **Disco.**
  - El store de cada sesión crece mientras dure el vínculo, y no se puede purgar por partes.
  - El punto 13 del spike reporta bytes por cada 1.000 mensajes, por motor y por tipo de almacenamiento. Con eso se fijan `disco_max_gb` y la proyección a 12 meses por línea.
  - [VALIDAR] el tope de volumen del plan de Railway.
- **Reciclado del store.** Es una operación, no una política de datos.
  - Si una sesión supera el umbral, un admin de KIS coordina con el dueño un re-escaneo.
  - La sesión nueva se crea con `fullSync:false` e importa desde `observado_hasta`.
  - Nuestra base no cambia.
- **Pérdida del volumen de WAHA.**
  - Nuestra base es el sistema de registro y no pierde nada.
  - Se pierden las credenciales y el store de **todas** las sesiones de ese worker, y cada línea necesita un re-escaneo.
  - Por eso conviene repartir las líneas en más de un worker apenas el piloto crezca.
- **Motor (D4: el que mejor funcione).**
  - El motor es una propiedad de cada servidor WAHA, no de cada sesión. Por eso `waha_workers.engine` existe desde el día uno, y cambiar de motor equivale a levantar otro servicio.
  - El modelo de datos y el análisis no dependen del motor.
  - Sí dependen del motor:
    - el bloque `config.noweb` / `config.gows` al crear la sesión;
    - la verificación posterior a la creación;
    - el control "Profundidad" de P2 y "Ampliar" de P4, que con GOWS desaparecen;
    - el normalizador de payloads.
  - El spike elige con estos criterios, en orden:
    1. completitud del historial denso contra el teléfono (§8, punto 2);
    2. si se reproduce #2267, la pérdida del primer mensaje de contactos `@lid`;
    3. estabilidad de un vínculo largo;
    4. soporte de passkey, que hoy es solo GOWS;
    5. consumo por sesión.
  - En GOWS:
    - La profundidad no es por sesión. Por defecto sincroniza todo el historial disponible, que puede ir muchos años atrás.
    - Se acota con variables de entorno **del servidor**, que la documentación rotula como experimentales. Son indicaciones que se envían a WhatsApp al registrar el dispositivo, no un filtro de WAHA:
      - `WAHA_GOWS_DEVICE_REQUIRE_FULL_SYNC=false`;
      - `WAHA_GOWS_DEVICE_HISTORY_SYNC_FULL_SYNC_DAYS_LIMIT=90`;
      - `WAHA_GOWS_DEVICE_HISTORY_SYNC_RECENT_SYNC_DAYS_LIMIT`;
      - `WAHA_GOWS_DEVICE_HISTORY_SYNC_INITIAL_SYNC_MAX_MESSAGES_PER_CHAT`.
    - [VALIDAR] que WhatsApp las respete. Si no las respeta, la copia técnica contiene años de chats.
    - Existe `config.gows.storage` por sesión, con seis flags: `messages`, `chats`, `groups`, `labels`, `contacts`, `messageSecrets`. Exige WAHA ≥ 2026.8.1, porque antes desactivarlos todos se ignoraba en silencio.

### 6.3 Ingesta

**1. Webhook `POST /webhook/waha`**

- Verifica el HMAC sha512 sobre el cuerpo crudo. Es *fail-closed*: si falta el secreto, rechaza.
- Resuelve línea, vínculo y chat con `metadata` más el nombre de sesión.
- **Filtro de exclusión antes de persistir**:
  - Vínculo en `pendiente_de_selección`: descarta todos los `message.*` sin guardar `payload`.
  - Chat excluido o contacto suprimido: descarta sin persistir.
  - Chat pendiente de decisión: guarda solo `{id, timestamp, fromMe}`.
  - `webhook_inbox` nunca guarda el `payload` de un evento descartado.
- Responde 200 en menos de 100 ms. El worker normaliza después.
- Ojo con los timestamps: el del sobre viene en ms y `payload.timestamp` en segundos.

**2. Backfill en pasadas (job por vínculo)**

- La ventana se fija con `filter.timestamp.lte = t_vínculo` y `gte = inicio del período`, con `downloadMedia=false`.
- Si hay al menos un chat excluido, recorre **chat por chat** y solo los incluidos.
- `chats/all/messages` se usa únicamente en el backfill inicial, y solo cuando no hay exclusiones.
- `offset` siempre avanza en `limit`. El fin de datos es una **página vacía**, nunca una página corta.
- La pasada completa se repite con upsert idempotente a los **+30 min, +6 h y +24 h** del vínculo. El historial puede seguir llegando al store después de `WORKING`.
- Se corta cuando dos pasadas consecutivas dan el mismo conteo por chat. Hasta entonces la cobertura es "provisoria".

**3. Reconciliación (mientras dure el vínculo)**

Con un vínculo permanente es la operación dominante, así que nunca usa `chats/all/messages`: eso traería a nuestro proceso el cuerpo de los chats excluidos, 96 veces por día.

- **Paso 1.** `GET /api/{session}/chats` ordenado por fecha de último mensaje, descendente. Se pagina hasta el primer chat cuyo último mensaje sea anterior a `observado_hasta − 10 min`. Devuelve id y fecha, sin cuerpo.
- **Paso 2.** Solo para los chats **incluidos**, y para los pendientes de decisión: `GET /chats/{id}/messages` con `filter.timestamp.gte = observado_hasta − 10 min`, `downloadMedia=false` y `limit` fijo, hasta una página vacía. Los chats excluidos y los suprimidos no se leen nunca.
- **Presupuesto.** Hay un máximo de páginas por corrida. Si se agota, la siguiente continúa y la cobertura queda "provisoria".
- **Cadencia.** Cada 15 minutos dentro del horario de P1 y cada 60 fuera. Cada línea tiene un desfase fijo de 0 a 15 minutos, para no consultar todas las sesiones de un worker a la vez.
- Corre también al volver a `WORKING`. Cubre solo lo posterior a `t_vínculo`.
- Motivos: los reintentos de webhook duran segundos, y hay reportes de mensajes que llegan por un canal y no por el otro.
- `links.observado_hasta` es el fin de la última reconciliación exitosa con la sesión en `WORKING` y con tráfico. Es la referencia para las ventanas de observación, para las reimportaciones y para los huecos.

**4. Idempotencia**

- `UNIQUE(line_id, provider_msg_id)` en el almacén de fuente. La deduplicación es **por línea**, no por vínculo, porque ampliar, reconectar y re-vincular crean un vínculo nuevo con otra sesión.
- El id de proveedor lleva el teléfono adentro. Por eso vive solo en el almacén de fuente y nunca entra a un prompt.
- Un vínculo nuevo de la misma línea importa lo que la línea todavía no tiene:
  - por defecto, `filter.timestamp.gte = max(inicio del período, observado_hasta del vínculo anterior − 10 min, fuente_purgada_hasta)`;
  - en una ampliación de profundidad importa además el tramo más antiguo que el vínculo anterior no cubría;
  - lo ya purgado no vuelve a entrar.
- El SET NX de Redis (`session_service.py:394-414`) queda solo como atajo. En Redis no se guarda contenido de mensajes ni QR.

**5. Medios**

- En el MVP no se descargan.
- Se guarda `has_media` y el tipo.

**6. Fin de vínculo (job idempotente y auditado)**

Corre por cualquiera de las cinco causas de §2.2.

1. **Marca el vínculo como `cerrando`.** Desde ese momento el receptor ignora sus `session.status`: no dispara banner, email ni "Reconectar".
2. **Borra la sesión de WAHA** con un único `DELETE /api/sessions/{name}`.
   - No se usa `POST …/logout`: sobre una sesión en marcha, WAHA hace logout y la vuelve a arrancar desde cero, con QR nuevo y los mismos webhooks.
   - El `DELETE` desvincula el dispositivo en WhatsApp, para la sesión, y borra credenciales, store y configuración.
   - Con almacenamiento PostgreSQL, WAHA mismo ejecuta el `DROP DATABASE` de esa sesión.
   - El desvínculo del lado de WhatsApp solo ocurre si la sesión está `WORKING`.
     - Si está `STOPPED` o `FAILED` y no hay restricción activa, se intenta `start` y se espera hasta 3 minutos.
     - Si no vuelve, se hace `DELETE` igual y se registra `desvinculo_confirmado = false`.
3. **Borra las claves de API de esa sesión** con `DELETE /api/keys/{id}`. WAHA no las elimina al borrar la sesión.
4. **Verifica**:
   - `GET /api/sessions/{name}` devuelve 404;
   - no quedan claves de esa sesión;
   - el directorio o la base de la sesión ya no existen.
5. **Registra** el resultado de cada paso y **avisa** al dueño.

Reglas:

- Un paso fallido no bloquea los siguientes: alerta y reintenta.
- WAHA no informa si el desvínculo falló. Por eso el aviso siempre incluye: "Revisá WhatsApp → Dispositivos vinculados. Si todavía ves este dispositivo, quitalo desde ahí. Las credenciales ya fueron destruidas y no se puede volver a usar."
- Nuestra base **no se toca** en este job: el texto importado sigue disponible según `retencion_fuente_dias` y `retencion_tras_desvinculo_dias`.
- La línea pasa a `sin_vinculo`, y el aviso al dueño dice qué se conserva y cómo borrarlo.

**7. Purga de fuente (política opcional)**

Solo corre para las líneas con `retencion_fuente_dias` > 0, que viven en el almacén purgable (§6.4).

- La purga es **por línea**: un job diario borra, para cada una, lo que superó su N.
- N se cuenta desde la **ingesta**:
  - el historial importado cuenta desde el día del vínculo;
  - lo capturado en vivo, desde el día del mensaje.
- Sin esa regla, con N = 7 casi todo el historial importado se borraría la primera noche, antes de terminar el análisis y de cualquier revisión.
- Antes de cada purga se materializa lo que debe sobrevivir:
  - `conversation_facts`, una fila por conversación: fecha, franja horaria de inicio, iniciador, cantidad de entrantes y salientes, tiempo de primera respuesta, si fue en horario, estado a nivel hilo y si es no legible;
  - fragmentos, solo si `retener_fragmentos` está activo.
- Una conversación abierta o en observación se purga recién al llegar a su estado final, con un tope de N + 10 días.
- Junto con el texto se purgan:
  - las filas por mensaje, porque la secuencia exacta de timestamps de un chat permite re-identificarlo;
  - la pregunta normalizada por conversación, la etiqueta libre cruda y los embeddings por pregunta.
- La identidad de un contacto (teléfono y nombre) se purga cuando no le quedan mensajes dentro de la ventana. Queda su `contact_hmac`.
- Cada línea guarda `fuente_purgada_hasta`, para que ninguna reimportación vuelva a traer lo purgado.

**7 bis. Vencimiento de fichas (job diario)**

- Solo alcanza a conversaciones **sin fuente**, es decir ya purgadas.
- Borra `conversation_facts`, `conversation_analysis`, evidencia y feedback con más de `retencion_fichas_meses`.
- Antes congela los agregados del período en `kpi_period_snapshots`: línea, período, KPI, valor, n y versión de la definición. Las series del tablero leen de ahí para los períodos ya vencidos.
- Con los valores iniciales no hay purga, así que este job no borra nada.

**8. Higiene de logs**

- Los modelos Pydantic de payloads de WAHA y de salidas de IA usan `hide_input_in_errors=True`. Sin eso, un error de validación imprime el valor de entrada, es decir el cuerpo del mensaje.
- Está prohibido loguear `payload`, `body`, prompts o respuestas del modelo. Los errores se registran con UUID interno y tipo de error.
- No se usa ningún servicio externo de errores que no esté en el Anexo C.
- La retención de logs de Railway se declara en el Anexo C.

### 6.4 Modelo de datos (migraciones 0007+)

| Grupo | Tablas |
|---|---|
| Identidad y acceso | `tenants`, `users`, `memberships` (rol y líneas permitidas), `login_tokens`, `consents` (por línea), `support_grants`, `access_audit_log` |
| Líneas y vínculos | `lines` (una por línea de WhatsApp: estado `vinculada\|sin_vinculo\|de_baja`, parámetros de ámbito Línea de §2.1, almacén de fuente asignado, `fuente_purgada_hasta`), `links` (un vínculo por cada escaneo: proveedor `waha\|meta\|kapso`, clave externa, profundidad, estado, `observado_hasta`, `restriccion_hasta`, resultado del job de fin), `link_status_events`, `waha_workers` (con `engine`, `max_sesiones`, `disco_max_gb`) |
| Política | En `tenants`: `perfil_de_datos`, `retencion_fichas_meses`, `ia_habilitada`, `retener_fragmentos`, `via_llm`. En `lines`: `duracion_vinculo_dias`, `retencion_fuente_dias`, `retencion_tras_desvinculo_dias`, `tope_ia_mensual_usd`. No hay parámetros que se pisen entre niveles. El rubro solo propone valores por defecto. |
| Conversaciones (base de resultados) | `wa_chats` (UUID aleatorio, `contact_hmac`, `lid_hmac`, estado incluido / excluido / pendiente, motivo), `wa_messages` (UUID, chat, `from_me`, `provider_ts`, tipo, `has_media`, referencia interna al mensaje citado, vía de ingesta; **sin texto ni id de proveedor**), `conversations`, `conversation_facts`, `suppressions` (solo HMAC) |
| Almacén de fuente (dos almacenes físicos; ver abajo) | `wa_message_bodies` (texto original y redactado), `wa_message_provider_ids` (id de proveedor, `reply_to` crudo), `wa_contact_identities` (JID, lid, teléfono, nombre), `webhook_inbox`, índice de búsqueda de texto |
| Análisis | `analysis_runs` (modelo, versión de prompt, esfuerzo, tokens, costo, conteos, `batch_id`, fecha de borrado del lote), `conversation_analysis`, `analysis_evidence`, `faq_clusters` (+vector), `findings`, `analysis_feedback`, `kpi_period_snapshots` |
| Operación | `jobs` (sin contenido), `product_events` (solo tenant, línea, usuario, nombre de evento, UUID internos y valores numéricos; un test de esquema rechaza propiedades de texto libre) |

Todas las tablas llevan `tenant_id NOT NULL`. Las de conversaciones, análisis y consentimiento llevan además `line_id`.

**Almacén de fuente separado**

- El texto, los ids de proveedor y la identidad de los contactos viven fuera de la base de resultados.
- Separarlos sirve siempre:
  - la base de resultados nunca contiene texto de conversación ni teléfonos;
  - el acceso a contenido pasa por un único camino, que es el auditado.
- La retención es por línea, pero los backups son una propiedad del servidor. Por eso hay **dos almacenes físicos**, y cada línea vive en uno según su política:

| Almacén | Para qué líneas | Backups | Particionado |
|---|---|---|---|
| `fuente_permanente` | `retencion_fuente_dias = 0`, que es el caso inicial | Sí, con el plazo declarado en el contrato | Por mes |
| `fuente_purgable` | `retencion_fuente_dias > 0` | **No**: ni backups, ni snapshots, ni PITR. Un respaldo sobreviviría a la purga. | Por línea y día |

- Pueden ser dos servidores Postgres, o uno solo con dos esquemas y respaldo lógico de `fuente_permanente` únicamente, sin snapshots de volumen.
- Activar la purga en una línea que ya tiene datos es un job de migración entre almacenes. La constancia declara que el texto anterior sigue en los respaldos hasta que venzan.
- `webhook_inbox` se purga siempre a los 7 días, con o sin política.
- **Tamaño.**
  - Sin purga, el almacén permanente crece sin tope.
  - El piloto mide bytes por mensaje (original, redactado e índice de texto) y proyecta 12 y 24 meses por línea.
  - Hay una alerta al 70 % del volumen.

**Identidad seudonimizada**

- `contact_hmac = HMAC-SHA256(k_tenant, teléfono en formato E.164)` y `lid_hmac = HMAC-SHA256(k_tenant, lid)`.
- `k_tenant` son 32 bytes aleatorios por tenant.
  - Se guardan **fuera de la base y de sus backups**, en un gestor de secretos.
  - Se destruyen **solo en la baja del cliente**. "Desconectar y borrar todo" es por línea y no las toca: `suppressions` es por tenant y tiene que sobrevivir al borrado de una línea.
- Un hash sin clave no sirve: los teléfonos son ~10^10 valores y se revierte por enumeración en segundos.
- Durante el vínculo se resuelve lid → teléfono con `/lids` y se guardan ambos HMAC. Si no se pudo resolver, el contacto queda marcado como `pn_resuelto = false`.
- Exclusiones, supresiones y reconocimiento entre vínculos se resuelven siempre por estas claves, nunca por el id crudo.
- Es dato personal **seudonimizado, no anónimo** [DICTAMEN]: quien aporte el teléfono puede reconocer al contacto.

**Reconciliación con `plan-multitenant.md`**

- Tenant pasa a ser la cuenta del cliente, y debajo están sus líneas.
- `phone_number_id` (Meta/Kapso) y el nombre de sesión (WAHA) son claves de `links`.
- Usuarios y roles son un modelo único (§4.4).

**RLS efectiva, no declarativa.** Hoy las migraciones y la app usan el mismo rol, que es dueño de las tablas, y `fetch()/execute()` toman una conexión cualquiera del pool. Así RLS no protege nada. Requisitos:

- Dos roles:
  - `radar_migrator`: dueño de las tablas, solo para Alembic.
  - `radar_app`: sin superusuario, sin `BYPASSRLS`, no dueño, con URL propia.
- `ENABLE` + `FORCE ROW LEVEL SECURITY` en cada tabla.
- Las políticas se definen sobre `current_setting('app.tenant_id', true)` y devuelven cero filas si el valor está vacío.
- Todo acceso pasa por un helper `tenant_tx(tenant_id)`: abre una transacción y ejecuta `set_config('app.tenant_id', $1, true)`.
- `execute()/fetch()` quedan prohibidos para estas tablas. Un test lo verifica.
- Tests obligatorios:
  - una conexión devuelta al pool no conserva el tenant;
  - el tenant A no lee al B ni por SQL directo con `radar_app`.

**Otros puntos**

- **Servicios**: los singletons actuales se atan a las primeras credenciales que reciben (`metrics_store.py:703-710`, `message_store.py:121-128`). Los servicios nuevos reciben `tenant_id` y `line_id` en cada llamada.
- **Búsqueda de texto**: `pg_trgm` o `tsvector`, dentro del almacén de fuente de cada línea.

### 6.5 Análisis

| Etapa | Qué hace | IA |
|---|---|---|
| E0 | Conversaciones, hilos, iniciador, "no requiere respuesta", "no legible", respuestas automáticas, ventana de observación, métricas medidas, pre-pasada con regex. | No |
| E1 | Seudonimización: teléfonos y nombres de contacto → códigos; redacción por regex de DNI, tarjetas, CBU y direcciones. Los mensajes se numeran por conversación (`m1…mN`). El `provider_msg_id` de WAHA **nunca** entra al prompt, porque lleva el teléfono adentro (`false_<tel>@c.us_<hash>`). | No |
| E2 | Análisis por conversación con **salida estructurada** (esquema JSON con enums): intenciones, señal de compra, secuencia de etapas, estado inferido, pregunta normalizada, **respuesta normalizada del negocio** (una oración generalizada, sin datos del cliente), clase de automatización, evidencia (índices `mN`), autoevaluación. Como contexto de solo lectura lleva los últimos 10 mensajes de la conversación anterior del mismo chat si terminó hace menos de 7 días; la evidencia solo puede citar la conversación analizada. Las conversaciones con señal de receta o credencial (regex + `has_media`) quedan fuera de E2 y E3 siempre. | Sí |
| E3 | Embeddings de preguntas normalizadas → agrupado → nombre del grupo. | Sí (mínima) |
| E4 | Agregados SQL, flujos (conteo de transiciones), respuestas típicas por grupo, generador de hallazgos. | Solo redacción |

**Llamadas a Claude** (datos cotejados con la referencia de la API)

- **Prerrequisito.**
  - Subir `anthropic==0.40.0` a la última versión publicada con `output_config.format` y `messages.batches`. Si es 1.x, verificar con `pip index versions anthropic`.
  - El cambio exige Python ≥ 3.10 (el `Dockerfile` usa 3.12) y reemplaza `httpx` por `httpx2`.
  - El pin es compartido con el bot: el bump se prueba contra `intent_service`, `image_service` y `backoffice`, aunque su código no cambie.
- **Sin prefill ni parámetros de muestreo.**
  - En Sonnet 5 y Opus 5 el prefill devuelve 400, igual que `temperature`/`top_p`/`top_k` con valores no default.
  - El prefill del bot (`intent_service.py:249-260`) sigue funcionando en sus modelos actuales y no es parte de este trabajo.
  - Su reintento a ciegas, que duplica llamadas en silencio, no se hereda.
- **Salidas estructuradas.**
  - `messages.parse` en la vía rápida.
  - En lotes va el mismo esquema JSON crudo en `output_config.format`, validado con Pydantic al leer.
  - Están soportadas en Haiku 4.5, Sonnet 5 y Opus 5, y funcionan dentro de lotes.
  - El esquema garantiza el formato, no que los índices citados existan. Eso lo valida el servidor.
- **Vía rápida.**
  - Llamadas sincrónicas sobre las 200 conversaciones de P5. Es la única vía sincrónica por defecto.
  - Se manda 1 pedido, se espera su respuesta y recién después se abre el semáforo de concurrencia. Así los pedidos siguientes leen la caché en lugar de pagar todos la escritura.
- **Vía masiva: Message Batches.**
  - 50 % de descuento.
  - Hasta 100.000 pedidos o 256 MB por lote.
  - La mayoría termina en menos de 1 h. A las 24 h el lote **vence**; no es un SLA.
  - Cada pedido termina `succeeded | errored | canceled | expired`. Los `expired` no se cobran y se reenvían una vez.
  - Los resultados llegan desordenados: se cruzan por `custom_id`, que es el UUID de la conversación (cumple `^[a-zA-Z0-9_-]{1,64}$`).
- **Retención en el proveedor.**
  - Anthropic guarda **pedidos y respuestas** de un lote hasta 29 días, y Message Batches **no es elegible para retención cero**.
  - Por eso, siempre: al terminar, el mismo job copia los resultados a Postgres y llama a `DELETE /v1/messages/batches/{id}`. `analysis_runs` guarda `batch_id` y fecha de borrado.
  - Las llamadas sincrónicas y los embeddings **no se pueden borrar por API**: quedan bajo la retención operativa del proveedor. La retención cero se tramita y verifica antes del piloto.
  - Los tenants con perfil `sensible`, o con `via_llm = sincronica`, usan **solo la vía sincrónica**, aunque cueste el doble.
- **Pensamiento y esfuerzo.** Es una decisión explícita por ruta, fija por versión de prompt.
  - En Sonnet 5 y Opus 5, si no se indica nada, corre pensamiento adaptativo, y se factura como salida.
  - E2 arranca con `thinking: {type: "adaptive"}` + `output_config.effort: "low"`.
  - `max_tokens` va holgado (≥ 8.000), porque limita pensamiento más JSON. El hábito del repo (`max_tokens=512`) truncaría.
  - El eval barre `effort` antes de comparar modelos.
- **Caché de prompt.**
  - Orden del prefijo: instrucciones + taxonomía (global, 1.er breakpoint) → ejemplos **del tenant** (2.º breakpoint) → conversación.
  - Mínimos cacheables: 1.024 tokens en Sonnet 5, 4.096 en Haiku 4.5 y 512 en Opus 5. Por debajo no cachea y no avisa.
  - En lotes los aciertos son *best-effort*; se usa `ttl: "1h"`.
  - Cambiar `output_config.format`, `thinking` o `effort` invalida la caché.
  - Se verifica con `usage.cache_read_input_tokens` en un test de integración.
- **Resultados no válidos.**
  - Se revisa `stop_reason` antes de parsear: `refusal` y `max_tokens` no garantizan el esquema.
  - En farmacias hay conversaciones sobre medicación.
  - Todo lo no válido cuenta como "no analizada" en KPI 5, con reintento acotado.
- **Costo.**
  - Capturar `usage` de cada respuesta en `analysis_runs`. Hoy el repo no registra tokens ni costo.
  - El tope de gasto por tenant se aplica **en nuestra cola antes de crear el lote**, porque los lotes pueden exceder levemente el límite del workspace.
  - Con vínculos permanentes el análisis es un costo recurrente por línea.
- **Análisis en régimen (vínculo vivo).**
  - Una conversación se analiza **una vez, al cerrarse** (24/72 h, §4.2). No hay re-análisis por cada pausa.
  - La bandeja de pendientes usa solo E0: regex de consulta comercial y de cortesías.
  - Las conversaciones cerradas del día van en **un lote nocturno por línea**, con el 50 % de descuento.
  - La vía sincrónica en vivo queda solo para `via_llm = sincronica`.
  - Una respuesta tardía que se adjunta a una conversación ya analizada la re-analiza una sola vez, en el lote siguiente.
- **Tope mensual por línea** (`tope_ia_mensual_usd`; el del tenant es la suma).
  - Se aplica en nuestra cola a **las dos vías**, con el costo estimado por `count_tokens` antes de enviar.
  - Al 80 %, alerta interna.
  - Al 100 %, las conversaciones nuevas quedan como "no analizada — tope del mes" en KPI 5 y los KPI medidos siguen. Se retoman el día 1, de la más nueva a la más vieja.
- **Modelos** (ids en configuración, nunca constantes).
  - El repo tiene hardcodeados `claude-haiku-4-5-20251001` y `claude-sonnet-4-5`.
  - Propuesta: `claude-sonnet-5` para E2, `claude-opus-5` para nombrar grupos y redactar hallazgos.
  - `claude-haiku-4-5` queda como opción de ahorro **solo si** el eval mantiene la calidad, y esa decisión es tuya.
- **Orden de magnitud ilustrativo del costo.**
  - Supuestos: 5.000 conversaciones por lotes, precio de lista con 50 % de descuento, sin caché, ~2.100 tokens de entrada y ~300 de salida visible contados con el tokenizador de Haiku 4.5.
  - Haiku 4.5: ~USD 9.
  - Sonnet 5 y Opus 5: el mismo texto rinde entre 1× y 1,35× más tokens, y el sesgo es solo hacia arriba.
    - Sin pensamiento: ~USD 18–24 y ~USD 45–60.
    - Con ~700 tokens de pensamiento por conversación: ~USD 40 y ~USD 100.
  - La cifra real sale de `count_tokens` por modelo más una corrida de 200 conversaciones.
  - El costo no obliga a bajar de modelo.
- **Aislamiento.** Una conversación por pedido (más el contexto del mismo chat), y nunca ejemplos de otro tenant.
- **Sin fallback silencioso a otro proveedor.** Hoy el fallback a OpenAI es automático (`intent_service.py:231-244`), y eso vuelve indeterminado quién ve los datos.

### 6.6 Actualización de resultados

- **Flujo**: el webhook pasa por el filtro de exclusión, se normaliza, se re-arman las conversaciones afectadas y esas se re-analizan.
- **Análisis**: una vez por conversación, al cerrarse, en el lote nocturno de la línea (§6.5).
- **Agregados**: se recalculan cada 15 minutos.
- **Tiempo real**: lo medido se actualiza casi en tiempo real.
- **Hallazgos**:
  - la primera tanda sale al terminar la vía rápida, como "Preliminar";
  - se regeneran al completar el lote;
  - después, una vez por día sobre una ventana móvil, y tras una tanda de correcciones.
- **Versionado**:
  - `analysis_runs` versiona prompt, modelo y esfuerzo.
  - Un cambio de prompt re-analiza por lotes, por defecto, solo los últimos 90 días de cada línea. Lo anterior conserva su versión. Ampliarlo es una decisión explícita, con su costo estimado a la vista.
  - Lo ya purgado conserva la versión con la que se analizó, y el tablero la muestra.
- **Cuando el vínculo termina**, el tablero queda congelado como línea de base.
- **Reconectar y volver a vincular** son la misma operación: un vínculo y una sesión nuevos de la misma línea.
  - importa desde `observado_hasta` del vínculo anterior, así el tramo sin captura se rellena con el historial del teléfono;
  - hereda exclusiones y supresiones por `contact_hmac`;
  - si pasó más tiempo que la profundidad del historial, el hueco se marca como "sin datos";
  - queda bloqueado mientras haya una restricción de cuenta vigente.
- Cuando el bot esté en producción se agrega, como serie aparte, lo que llegue por la API oficial. No se mezcla.

---

## 7. Aislamiento, privacidad, retención y eliminación

**Aislamiento entre clientes**

- Despliegue propio de Radar, separado de los de Remedia (§6.2).
- `tenant_id NOT NULL` y RLS efectiva (§6.4).
- Claves Redis con prefijo `t:{tenant}:` y sin contenido.
- Una sesión WAHA por vínculo, con nombre opaco y clave propia de solo lectura.
- Sesión web atada al tenant dentro del token. Está la advertencia de origen compartido en `plan-multitenant.md:78-81`.
- Test de aceptación: un usuario del tenant A no puede leer nada del B por ninguna ruta.
- Escapar **todo** texto importado (nombres de contacto, mensajes, etiquetas generadas) y aplicar CSP.
- Ningún endpoint de medios público. Hoy `/media/chat/{id}` no pide autenticación.

**Marco legal (Ley 25.326) [DICTAMEN antes del piloto]**

- **Hipótesis de trabajo**: el cliente es el responsable y KIS el encargado (art. 25).
- El abogado debe confirmar si se sostiene, porque KIS usa el resultado para configurar y ofrecer su servicio. El Anexo B vigente dice que destinar datos a otro fin convierte al encargado en responsable.
- Para sostenerla:
  - el análisis se define como un servicio al cliente para mejorar su atención;
  - el personal comercial de KIS solo ve agregados compartidos;
  - KIS no usa datos de un cliente para mejorar prompts ni evaluaciones fuera de su tenant.
- **Titulares (los clientes finales del comercio)**:
  - No hay base de licitud resuelta para el historial previo: nunca recibieron un aviso.
  - Opciones a evaluar:
    - relación comercial previa;
    - actualizar la política de privacidad del comercio y su mensaje de bienvenida antes de vincular;
    - en rubros sensibles, consentimiento expreso.
- **Derechos**: acción "Suprimir contacto" (§4.5).
- **Acuerdo de datos**:
  - Como Radar es para clientes y líneas nuevas (D1), el acuerdo va **dentro del contrato de alta**.
  - El click de P2 confirma el alcance de cada línea; no reemplaza al contrato.
- **Conservar el texto sin plazo**, incluso después de desconectar la línea, es el valor inicial que asumí (§2.1).
  - Es el punto que más conviene llevar al dictamen, junto con la base de licitud.
  - Si el dictamen pide una condición de fin, ya existe el parámetro: `retencion_tras_desvinculo_dias`.

**Perfil de datos (D3), parámetro por tenant:**

| | `estandar` | `sensible` |
|---|---|---|
| Rubros que lo proponen por defecto | comercio general, pet shop, servicios | farmacia, salud, mutual con datos de salud |
| IA (E2–E4) | Habilitada | **Apagada por defecto**: solo KPI medidos 1–5. Se habilita por tenant con cláusula contractual y [DICTAMEN] sobre los arts. 7 y 8. |
| Vía de LLM | Lotes o sincrónica | Solo sincrónica, **con retención cero contratada y verificada** para cada proveedor usado. Sin eso, `ia_habilitada` no se puede activar. E3 (OpenAI) sigue la misma regla. |
| `retencion_fuente_dias` | 0 (sin purga) | Recomendado: 7 |
| Fragmentos de evidencia | Apagados; solo con habilitación contractual | Nunca |

- En **ambos** perfiles:
  - las conversaciones con señal de receta o credencial quedan fuera de la IA;
  - no se corre OCR de recetas;
  - no se descargan medios.
- El cliente puede pasar de `estandar` a `sensible` por su cuenta. El camino inverso solo lo hace un admin de KIS, y queda auditado.

**Subencargados de Radar**, como lista propia en el Anexo C del contrato:

- Anthropic: análisis, EE. UU.
- OpenAI: embeddings de preguntas seudonimizadas, EE. UU.
- Railway: aloja la app, Postgres, Redis y el servidor WAHA; EE. UU.
- Proveedor de email transaccional [definir]. Los emails llevan solo conteos y un link autenticado.
- Meta/WhatsApp: es el canal, no un subencargado.

Verificar el estado de retención cero con Anthropic y OpenAI antes del piloto (fila 22 de la nota de brechas).

**Documentos comerciales a corregir antes de la primera firma**

Ningún cliente firmó estos documentos (D5), así que **no son contractuales todavía**. Se reescriben doc1 y doc3 con Radar incluido desde el origen.

| Lo que dicen hoy | Conflicto | Cómo queda |
|---|---|---|
| "Cada Cliente opera en un despliegue propio; no existe una base compartida" | Radar es multi-tenant sobre una base. | Despliegue separado de Radar. Reescribir doc1 §9 y doc3 §6 para describir el aislamiento lógico de Radar (RLS + tests) junto al despliegue dedicado del bot. |
| "Usuarios individuales con contraseña y roles (administrador, dueño, operador)" | Radar usa link mágico y roles Dueño/Gestor/Lector. | Modelo de acceso único con ambos mecanismos. El documento describe el link mágico (un solo uso, vencimiento) como control equivalente. |
| "El LLM no recibe diagnóstico ni datos de receta; los datos de salud los trata una persona habilitada" | E2 enviaría conversaciones completas. | Perfil `sensible` sin IA por defecto (D3). Exclusión permanente de conversaciones con señal de receta o credencial, en ambos perfiles. |
| "No conservamos audios" | Importar notas de voz. | MVP sin medios. La transcripción pasa a etapa 2 con opt-in y sin guardar el audio. |
| "Sin datos reales en dev/test" | Desarrollar contra `MaroSession`: tus contactos no consintieron. | Corpus sintético + cuentas de prueba con consentimiento. |
| "Sin fin propio ni cruce entre clientes" | Usar el resultado para configurar el servicio; benchmarks entre clientes. | La finalidad se declara en el contrato y en P2. Comercial KIS solo ve agregados compartidos. Sin benchmarks en el MVP. |
| Métricas "disociables" | El drill-down re-identifica. | Roles, vista redactada para Gestor, Lector sin citas, auditoría de 24 meses. |
| Lista cerrada de subencargados, 30 días de aviso y derecho de objeción | Radar suma alojamiento de WAHA, email y un uso nuevo de Anthropic/OpenAI (historial completo). | Anexo C con los subencargados de Radar incluidos desde la primera versión que se firme. |
| Canal = WhatsApp Business Platform; cifrado de extremo a extremo | WAHA es otro canal, no oficial, y un dispositivo vinculado descifra todos los chats. | Sección específica de Radar, separada del texto de Remedia-bot. |
| Plazos de conservación | Radar conserva texto de conversaciones según parámetros. | El documento remite a los parámetros de cada línea, que figuran en el contrato. |

**Retención y eliminación**

| Dato | Dónde vive | Qué pasa |
|---|---|---|
| Copia técnica en WAHA: todos los chats individuales, incluidos los excluidos | Servidor WAHA, sin backups ni snapshots | Vive mientras dure el vínculo. Se borra con el `DELETE` de la sesión, en el job de fin de vínculo. |
| Texto de mensajes (original y redactado), ids de proveedor, `reply_to` crudo, identidad de contactos, índice de búsqueda | Almacén de fuente | Según `retencion_fuente_dias`, contados desde la ingesta. Con 0: mientras la línea no esté `de_baja`, **tenga o no un vínculo vivo**, salvo que aplique `retencion_tras_desvinculo_dias`. |
| Filas por mensaje, pregunta normalizada, etiqueta libre, embeddings por pregunta | Base de resultados | Con la purga activa, junto con el texto de su conversación. Sin purga, duran lo mismo que el texto. |
| `webhook_inbox` | Almacén de fuente | 7 días, siempre. Nunca guarda contenido de eventos descartados. |
| Lotes en Anthropic | Proveedor | Se borran por API al terminar cada lote, siempre. |
| Llamadas sincrónicas a Anthropic y embeddings en OpenAI | Proveedor | No los podemos borrar. Vencen solos según contrato: hasta ~30 días sin retención cero [VALIDAR]. Se declara en P2. |
| Logs de plataforma | Railway | Sin contenido de mensajes por diseño (§6.3, punto 8). Plazo declarado en el Anexo C. |
| Registros por conversación (`conversation_facts`, `conversation_analysis`, correcciones): dato seudonimizado, no anónimo [DICTAMEN] | Base de resultados, con backups de 30 días | Mientras exista su texto, no vencen. Sin texto: `retencion_fichas_meses`, 12 por defecto y configurable (D9). Después quedan solo agregados, congelados en `kpi_period_snapshots`. |
| Fragmentos de evidencia | Base de resultados | Apagados por defecto. Mismo plazo que los registros por conversación. |
| Agregados, hallazgos y grupos de FAQ con n ≥ 5 **contactos distintos**, por línea. Los textos de grupo y la "respuesta típica" son generados por la IA, con la instrucción de no incluir nombres, teléfonos, direcciones ni números de pedido | Base de resultados | Mientras la línea no esté `de_baja`. |
| Línea o cliente que se da de baja | Todo | Borrado total a los 30 días de la baja. Solo en la baja del cliente se destruye además la clave `k_tenant`. |

**"Desconectar y borrar todo"**

- Está disponible en cualquier momento, por línea. Borra la fuente **y también los derivados** de esa línea.
- En menos de 72 h se borran:
  - la sesión y el store de WAHA;
  - el texto y la identidad en el almacén de fuente;
  - las tablas de la línea, incluidos vectores y feedback;
  - los lotes vivos en el proveedor de IA.
- La constancia lista las copias residuales, con su plazo:
  - backups;
  - retención operativa de los proveedores de IA.

**Estado actual del repo**

- Hoy solo vence lo que está en Redis: sesión a las 25 h, fotos a los 7 días.
- En Postgres, `messages`, `interacciones` y `eventos` no tienen purga ni supresión por titular (nota de brechas #8 y #10).
- Retención y borrado son trabajo nuevo.

---

## 8. MVP priorizado y segunda etapa

**Semana 0 — spike de validación en staging, con cuentas de prueba consentidas.** Cada punto cierra un [VALIDAR]. Dura unas dos semanas, porque incluye sostener un vínculo.

1. Multi-sesión en CORE.
2. Profundidad, completitud y **duración** del sync: NOWEB con `fullSync` on/off vs. GOWS.
   - Criterio de cierre: comparar contra el teléfono los conteos mensuales de entrantes y `fromMe` de 20 chats.
   - Punto de partida (sonda sobre `MaroSession`): densidad real de solo ~2,5 meses con `fullSync=true`.
   - Hay que repetirla en una cuenta Business con fecha de vinculación conocida.
3. `chats/all/messages`: **ya verificado en NOWEB**. Falta el tope de `limit` y la latencia de la pasada de conteo con más de 3.000 chats.
4. Si el historial llega por webhook; tiempo desde `WORKING` hasta la lista estable; mensajes por minuto del backfill.
5. `markOnline:false` no afecta las notificaciones.
6. Las lecturas no generan tildes ni presencia.
7. `ignore` excluye del store.
8. Tiempos reales del QR y flujo de código de vinculación.
9. `message` vs. `message.any`; defaults de reintentos.
10. Reproducir #2267.
11. Coexistencia: confirmar con Kapso/Meta si los números en producción están en coexistencia y si admiten un dispositivo vinculado de WAHA.
12. Cómo aparecen en el historial los mensajes de bienvenida y ausencia de WhatsApp Business; tasa de falsos positivos de la heurística de respuesta automática.
13. **Estabilidad de un vínculo largo**: una sesión por motor vinculada durante todo el spike, con registro de caídas, RAM y disco por sesión, y bytes de store por cada 1.000 mensajes. También la latencia de la reconciliación en dos pasos sobre un store grande.
14. Borrado verificable: después del `DELETE` de la sesión, comprobar en el volumen o en la base que no quedan mensajes, y que el dispositivo desaparece del teléfono.
15. Sesión sin store de mensajes, para el modo opcional de §2.3. No bloquea el MVP.
16. Retención real de Anthropic y OpenAI sobre llamadas sincrónicas, y trámite de retención cero.

El motor se elige con los cinco criterios de §6.2. Los alimentan los puntos 2, 10, 13 y 14.

**MVP**, en orden de construcción. Cada tramo es entregable:

1. **Despliegue y acceso**: despliegue propio, almacén de fuente separado, clave `k_tenant` en gestor de secretos, tenants y **líneas con sus parámetros**, alta por invitación, usuarios, roles, link mágico, RLS efectiva, `consents`, auditoría sin identificadores.
2. **Vínculo y Consola KIS**: primero la Consola (§3.1: C1 líneas, C2 vincular con estado en vivo, C4 acciones), después la pantalla P3 del cliente sobre el mismo backend; gestor de sesiones WAHA con verificación posterior a la creación y **admisión por capacidad del worker**, P1–P3, estados de la línea, reconexión, detección de sesión muda, salud de la cuenta, servicio WAHA endurecido y **job de fin de vínculo**.
3. **Ingesta**: webhook con filtro de exclusión, pasada de conteo, backfill en pasadas, reconciliación en dos pasos con `observado_hasta`, P4.
4. **Medido**: E0, KPI 1–5, bandeja de pendientes en vivo, P5. *Primer valor sin IA; único valor para el perfil `sensible`.*
5. **IA**: E1–E4, KPI 6, 7 y 9, hallazgos, drill-down con evidencia, corrección, revisión guiada, lote nocturno por línea y tope mensual de IA. KPI 8 y 10 se calculan en la misma llamada y **se muestran solo al alcanzar su umbral de datos**.
   - **Criterio de salida:** eval offline de KPI 6 y 7 sobre ≥ 200 conversaciones etiquetadas a mano (corpus sintético + cuentas consentidas).
   - Precisión ≥ 80 % en A/B y ≥ 70 % en C, o KPI 7 no entra al piloto.
6. **Cierre**:
   - "Suprimir contacto" y "borrar todo" con constancia;
   - acción "Desconectar" simple y recordatorio semestral para líneas `sin_vinculo`;
   - **políticas opcionales**: purga de fuente con almacén purgable, fichas sin texto y vencimiento de fichas; `retencion_tras_desvinculo_dias`;
   - emails e instrumentación;
   - reescritura de doc1 y doc3 antes de la primera firma (D5);
   - dictamen legal.

**Segunda etapa**

- Modo sin copia en WAHA (§2.3), si el punto 15 del spike lo habilita.
- Sankey de flujos.
- Comparación "antes / después del bot": línea de base de Radar contra las métricas del bot por API oficial.
- Transcripción de audios con opt-in.
- Carga de "Exportar chat".
- Estimación de montos con ticket promedio.
- Exportar FAQ a `kb_documents`.
- Seguimientos automáticos **solo por API oficial**.
- Benchmarks con opt-in.
- Etiquetas de WhatsApp Business (`label.upsert`, `label.deleted`, `label.chat.added`, `label.chat.deleted`).
- Alertas y resumen semanal.
- Taxonomía por rubro y descubierta por cliente.
- Reparto de sesiones en varios workers.
- Sugerencia de exclusión con IA, solo si hay base legal clara.

---

## 9. Criterios de éxito

Son hipótesis a calibrar en el piloto, medidas con `product_events`, por línea.

| Etapa | Métrica | Objetivo inicial |
|---|---|---|
| Conexión | QR mostrado → `WORKING` | ≥ 70 % |
| Conexión | Mediana P0 → `WORKING` | ≤ 5 min |
| Conexión | `WORKING` → lista de chats lista para elegir | Línea base en el spike [VALIDAR]; hipótesis ≤ 3 min |
| Valor | "Empezar análisis" → primera métrica medida visible | ≤ 2 min (mediana) |
| Valor | "Empezar análisis" → primer hallazgo de IA (preliminar) | ≤ 10 min |
| Valor | P0 → primer hallazgo, extremo a extremo | Línea base en piloto |
| Valor | Usuarios que abren ≥ 1 drill-down en la primera sesión | ≥ 60 % |
| Confianza | "No es correcto" sobre la **muestra al azar** de revisión guiada | < 20 % (oportunidades: < 30 %). La regla de ocultamiento está en §4.3, KPI 7. |
| Confianza | Clientes que completan al menos una ronda de revisión guiada en la primera semana; los que completan las tres | ≥ 50 % / línea base |
| Adopción | **Siguiente paso completado** (llamada agendada, plantilla copiada, mensaje de ausencia configurado —autodeclarado—) en 14 días | Línea base en piloto. El clic en el CTA es métrica secundaria. |
| Adopción | Líneas cuyo dueño vuelve al tablero en la semana 4 | Línea base en piloto |
| Salud | Vínculos vivos a los 30 y a los 90 días, sin contar los que terminaron por `duracion_vinculo_dias` o por pedido del dueño; vínculos que completan las 3 pasadas de importación | ≥ 85 % y línea base / ≥ 90 % |
| Salud | Workers de WAHA por encima del 80 % de sesiones o del 70 % de disco | 0 |
| Costo | Gasto de IA por línea y por mes; líneas que llegan al tope | Línea base en piloto |
| Salud | Líneas con huecos de eventos de más de N horas en horario de atención | Línea base |
| Guardas | Mensajes enviados / chats marcados leídos por Radar | **0** |
| Guardas | Filas con contenido de chats excluidos en cualquier tabla (test de integración) | **0** |
| Guardas | Tablas fuera del almacén de fuente con columnas de texto de conversación o identificadores de WhatsApp (test de esquema) | **0** |
| Guardas | Muestra diaria de logs de web, worker y WAHA con coincidencias de `@c.us`, `@lid` o 10+ dígitos | **0** |
| Guardas | Sesiones de WAHA vivas de vínculos ya terminados | **0** |
| Guardas | Con la purga activa: datos fuente con más de `retencion_fuente_dias` + 10 días desde su ingesta | **0** |
| Guardas | Lecturas de chats excluidos o suprimidos en la reconciliación (test de contrato del cliente WAHA) | **0** |
| Guardas | Incidentes entre tenants | **0** |
| Guardas | Borrados completados según la definición de §7 | 100 % |

---

## 10. Supuestos explícitos

- **S1.** La unidad de trabajo es la línea. Un cliente tiene una o más.
- **S2.** Valores iniciales de los parámetros (§2.1):
  - vínculo permanente;
  - sin purga de fuente;
  - fichas por 12 meses;
  - fragmentos apagados.
  - Todos se pueden cambiar sin tocar el diseño.
- **S3.** No hay un segundo escaneo en el flujo principal, y la sesión de WAHA tiene store durante todo el vínculo.
  - La copia técnica completa vive mientras la línea esté conectada.
  - Con los valores iniciales, el texto de los chats elegidos se conserva mientras la línea siga dada de alta, aunque se desconecte.
  - El consentimiento dice las dos cosas.
- **S4.** El objetivo son líneas atendidas a mano desde la app. El caso coexistencia con Cloud API queda por validar.
- **S5.** MVP sin medios. Los audios e imágenes cuentan para los tiempos, no para el contenido.
- **S6.** Instancia WAHA dedicada a clientes, separada de la de desarrollo, sin backups ni snapshots. El motor lo decide el spike (D4).
- **S7.** Mismo repo e imagen, pero **despliegue, Postgres y Redis propios de Radar**. Dentro de Radar, el texto y la identidad viven en un almacén aparte.
- **S8.** Taxonomía genérica con variantes por rubro.
  - La sensibilidad es un parámetro por tenant (D3).
  - El perfil `sensible` arranca sin IA, y para habilitarla exige retención cero verificada.
- **S9.** Sin benchmarks entre clientes, sin estimación de montos y sin sugerencia de exclusión por IA externa en el MVP.
- **S10.** Los documentos comerciales se reescriben antes de la primera firma (D5).

---

## 11. Preguntas abiertas

No queda ninguna que cambie el diseño. Hay dos cosas para confirmar cuando quieras, y ninguna frena el trabajo:

1. **Los valores iniciales de S2.** Los elegí yo a partir de tus respuestas. Si preferís otros (por ejemplo, vínculo de 30 días o purga a 90 días), es un cambio de configuración.
2. **El dictamen legal.** Conviene que cubra cuatro puntos:
   - la base de licitud para leer el historial de los clientes finales;
   - conservar texto de conversaciones sin plazo, incluso con la línea desconectada;
   - si la supresión de un contacto es suficiente cuando su chat sigue en la copia técnica de WAHA mientras dure el vínculo;
   - si los registros por conversación, que son seudonimizados, necesitan un plazo menor a 12 meses.
3. **El tope mensual de IA por línea.** Lo fija el piloto, con el costo real medido.
