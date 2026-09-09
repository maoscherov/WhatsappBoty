# Nota interna — Qué prometen los documentos comerciales vs. qué hay en el código

> **Uso interno. No enviar al cliente.** Redactada el 2026-09-07 junto con los dos documentos
> de objeciones (`Remedia - Adecuacion Ley 25326 Datos Personales.docx` y
> `Remedia - Integracion con CRM.docx`).

Los dos documentos están escritos como **compromisos de implementación**: describen cómo
queda el despliegue del cliente al go-live, no cómo está el repo hoy. Eso es lo normal en
una propuesta corporativa, pero obliga a cerrar las brechas antes de firmar el acta de
aceptación (Anexo D del doc de Ley 25.326). Esta tabla es el backlog que sale de ahí.

## Documento 1 — Ley 25.326

| # | Compromiso en el documento | Estado en el código hoy | Esfuerzo |
|---|---|---|---|
| 1 | Aviso de privacidad en el primer contacto (Anexo A) | No existe. No hay ningún texto de privacidad ni identificación de bot en el vertical farmacia (`intent_service.py` dice "no sos un bot genérico"). | Bajo: clave de config + envío en primer mensaje de sesión nueva o >30 días. |
| 2 | Palabra **BAJA** con exclusión persistente | No existe. Búsqueda de `baja|opt-out|STOP` en el repo: cero. | Bajo-medio: detección + set en Redis/Postgres `opt_out:{phone}` + bloqueo de envíos proactivos. |
| 3 | Panel con usuarios individuales, contraseña y roles; sin claves compartidas | Hoy una sola `BO_KEY` por query string o header; si está vacía, `/bo/*` y `/orders/api/*` quedan abiertos (`backoffice.py:41`). | Alto. Es la Fase 3 de `docs/plan-multitenant.md`. |
| 4 | Registro de auditoría de operadores (24 meses) | Parcial: los pedidos guardan `agente/preparado_por/retirado_por` en Redis con TTL 7 días. No hay tabla de auditoría ni registro de quién vio/exportó/suprimió. | Medio: tabla `audit_log` + middleware en endpoints `/bo/*`. |
| 5 | Imágenes de recetas accesibles sólo con usuario autenticado | `GET /media/chat/{key}` es **público**, sin auth (`media.py:111`). | Bajo: exigir `BO_KEY`/sesión; el panel ya manda la clave. **Prioridad 1.** |
| 6 | Vencimiento de imágenes a 7 días | Cumplido: `blob:chat:{msg_id}` con TTL 7 días (`webhook.py:641`). | — |
| 7 | Sesión vence a 25 h | Cumplido (`session_service.py`). | — |
| 8 | Historial con retención definida (sugerido 12 meses) y depuración automática | `messages`, `interacciones` y `eventos` crecen sin límite; no hay job de purga (`plan-metricas.md:190`). | Bajo-medio: job diario `DELETE ... WHERE created_at < now() - interval`. |
| 9 | Dirección de envío enmascarada en el historial al vencer el pedido | No. Queda en texto libre en `messages.content` para siempre. | Medio: depende de #8 o de un enmascarado al cierre. |
| 10 | Acción "Suprimir titular" (habeas data) | No existe. Sólo `POST /bo/sessions/clear` (borra todo) y `DELETE /simulate/session/{phone}` (**sin auth**). | Medio: endpoint `DELETE /bo/titular/{phone}` que borre sesión, pedidos, blobs, `messages`, `interacciones`, `eventos` y deje marca de exclusión + auditoría. |
| 11 | Exportación de datos del titular (derecho de acceso) | Parcial: `GET /bo/history/{phone}` + `GET /orders/api/list`. No hay export consolidado. | Bajo: endpoint que junte historial + pedidos en JSON/CSV. |
| 12 | Cifrado en reposo a nivel aplicación de imágenes y padrón | No. Blobs en Redis en base64 plano; padrón con DNI y domicilio en `blob:socios` sin TTL. | Medio: cifrado simétrico (Fernet/AES-GCM) con clave en env var antes de guardar en Redis. |
| 13 | Cifrado en tránsito a Redis/Postgres | Depende de la URL: `REDIS_URL` sin `rediss://` por defecto. | Bajo: configuración en Railway. |
| 14 | Logs sin contenido de mensajes ni teléfonos completos | No cumplido: se loguean teléfonos completos (`webhook.py:724`, `order_service.py:99`, `orders_api.py:134`), fragmentos de mensajes (`audio_service.py:52`, `webhook.py:1147`) y el body completo de respuestas de MP (`payment_service.py:73`). | Bajo: helper `mask_phone()` y quitar contenidos. |
| 15 | Endpoints de simulación sólo en entorno de prueba | `/simulate`, `/simulate/image`, `DELETE /simulate/session/{phone}` están **sin auth en producción** (`simulate.py`). | Bajo: montar el router sólo si `ENV != production`, o exigir `BO_KEY`. **Prioridad 1.** |
| 16 | Firmas de webhook bloqueantes | MP: se valida pero no rechaza (`mp_webhook.py:70`). Kapso: si el secret está vacío acepta todo (`webhook.py:505`). | Bajo. |
| 17 | CORS restringido | `allow_origins=["*"]` (`main.py:197`). | Bajo. |
| 18 | Teléfono no enviado a Mercado Pago | Hoy `external_reference = "{phone}_{sku_id}"` (`payment_service.py:47`). El doc dice "referencia disociada del teléfono". | Bajo: usar un id opaco y resolverlo en Redis. |
| 19 | Lectura automática de recetas desactivada por defecto | Cumplido: `receta_ocr_enabled` default `false`. | — |
| 20 | El LLM no recibe DNI ni domicilio | Cumplido (`socio_service.py:134`, `intent_service.py:91`). | — |
| 21 | Aislamiento entre clientes por despliegue separado | Cumplido hoy (un deploy por cliente). Si se avanza con multitenant, el doc debe actualizarse. | — |
| 22 | Contratos con proveedores de IA con "no entrenamiento" y retención cero | Es cierto para los términos comerciales de API de Anthropic y OpenAI; **retención cero (ZDR) en Anthropic/OpenAI hay que solicitarla** explícitamente. Groq: verificar términos. | Gestión, no código. |
| 23 | Backups diarios 30 días, restore trimestral | Depende de Railway; verificar el plan contratado. | Gestión. |
| 24 | Procedimiento de incidentes, NDA con personal, capacitación, evaluación de impacto | No hay documentos. Hay que redactarlos. | Documental. |
| 25 | Referencias normativas (Res. AAIP 47/2018, 4/2019, Disp. 60-E/2016, Ley 27.483, proyecto de reforma) | Correctas a mi conocimiento, pero **pasarlas por un abogado** antes de enviar. El doc ya lo advierte en la portada. | Legal. |

**Orden sugerido para poder firmar el Anexo D:** 5 y 15 (una tarde, cierran las dos exposiciones
más graves) → 14, 16, 17, 18 (un día) → 1, 2 (un día) → 8, 10, 11 (dos o tres días) → 12, 13 →
4 → 3 (el grande, alineado con el plan multitenant).

## Documento 2 — Integración con CRM

| # | Compromiso en el documento | Estado en el código hoy | Esfuerzo |
|---|---|---|---|
| 1 | Webhooks salientes por evento, firmados con HMAC, con reintentos e idempotencia | **No existe ningún emisor saliente.** Los eventos sólo se insertan en la tabla `eventos` (`metrics_store.py:27`). | Medio-alto: cola en Redis + worker de despacho + tabla `webhook_deliveries` + config de destinos por evento. |
| 2 | Catálogo de eventos (sección 4) | Los eventos que ya se registran: `derivacion`, `derivacion_atendida`, `producto_ofrecido`, `busqueda_sin_resultado`, `link_enviado`, `pago_aprobado`, `pago_rechazado`, `sentimiento`, `fuera_horario`, `wa_send_fallo`. **Faltan:** `conversacion.iniciada`, `conversacion.intencion`, `conversacion.cerrada`, `cotizacion.enviada`, `pedido.creado/preparado/retirado`, `contacto.baja`. Los nombres del doc son nuevos (con punto); habría que mapear. | Bajo por evento: cada uno es una llamada a `evento()` en el punto correcto (`OrderService.create`, `set_estado`, cierre por inactividad en `main.py`). |
| 3 | API de lectura `/api/v1/*` con Bearer token | Existe el equivalente bajo `/bo/*` y `/orders/api/*` con `BO_KEY` (`/bo/conversaciones`, `/bo/history/{phone}`, `/bo/session/{phone}`, `/orders/api/list`, `/bo/socios/check/{phone}`). Sin versionado ni claves por integración. | Medio: router `/api/v1` que reexponga eso con claves de API con alcance. |
| 4 | API de acción: enviar plantilla, mensaje de operador, derivar, cerrar | Existen `POST /bo/session/{phone}/message|takeover|close`. **No hay envío de plantillas (templates de Meta)**; `whatsapp_service` manda texto/imagen libre, que Meta sólo permite dentro de la ventana de 24 h. | Medio: soporte de `type: template` en `whatsapp_service` + verificación de opt-in (#1 del doc 1, ítem 2). |
| 5 | Padrón incremental `PUT/DELETE /socios/{numero}` | Sólo carga masiva por archivo (`POST /bo/socios/import`). | Medio: `SocioService` en memoria; habría que mover el padrón a Postgres o reconstruir el blob. |
| 6 | Guardar `crm_id` devuelto por el receptor | No existe. | Bajo: campo en sesión/Postgres. |
| 7 | Panel de entregas fallidas y reenvío | No existe (depende de #1). | Incluido en #1. |
| 8 | SLA 99,5 %, latencia de publicación 5 s, versionado 12 meses | Compromisos comerciales; no hay medición hoy. `PerfService` mide latencia del bot, no de integraciones. | Gestión + métricas. |
| 9 | Notas por CRM (Anexo A) | Genéricas y correctas; ninguna está construida. La única integración externa esbozada es Mercurio ERP, no operativa (`mercurio_service.py`, zeep comentado). | Por proyecto. |

**Camino más corto para una primera integración real sin construir todo:** ítem 2 (completar
eventos) + ítem 1 en su versión mínima (un worker que lea `eventos` nuevos y haga POST a una
URL con HMAC). Con eso el cliente ya puede enchufar n8n/Make, que es la modalidad
"Middleware del Cliente" del doc. La API de acción (#4) es lo siguiente porque desbloquea
campañas desde el CRM.

## Placeholders a completar antes de enviar

- `[CLIENTE]` y `[PROVEEDOR]` en portada y cuerpo de ambos documentos.
- Datos de contacto para ejercicio de derechos en el Anexo A del doc 1.
- Región de alojamiento real (Railway) en la tabla de subencargados si el cliente pregunta.
- Nombre del proveedor de conectividad a WhatsApp si se usa Kapso (el doc lo deja genérico a propósito).
