# Radar tramo 2 — Vínculo con WAHA y Consola KIS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que un admin de KIS pueda vincular la línea de WhatsApp de un cliente desde la Consola (consentimiento asistido → QR → `WORKING`), ver el estado de todas las líneas con semáforo y operarlas (reconectar, desconectar, desconectar y borrar, marcar restricción), y que el dueño pueda hacer lo mismo con su línea desde P3; todo sobre un único gestor de sesiones WAHA con lista blanca de rutas, verificación posterior a la creación, clave de solo lectura por sesión, admisión por capacidad del worker, máquina de estados del vínculo alimentada por `session.status` y por un chequeo de salud cada 5 min, y un job idempotente de fin de vínculo (un único `DELETE`, borrado de claves y verificación 404; nunca `logout`).

**Architecture:** Todo vive en el despliegue de Radar del tramo 1 (`APP_MODE=radar`). `app/radar/waha/` es el único paquete que habla con WAHA (cliente httpx con lista blanca, cuerpo de sesión de P3 y gestor del ciclo de vida); `app/radar/workers.py` es el único módulo que lee la clave admin de un worker, que vive en el `SecretStore` (`waha_admin:<worker_id>`), nunca en la base ni en el navegador. La migración `r0003` agrega `waha_workers`, `links`, `link_status_events` y `jobs` (todas con `tenant_id`, RLS forzada y acceso por `tenant_tx`) y las columnas del consentimiento asistido en `consents`. Lo que cruza tenants (ocupación de workers, listado de la Consola, reclamo de jobs, programación de chequeos) son funciones `SECURITY DEFINER` de `radar_admin`, como en el tramo 1. Un worker (`app/radar/worker.py`: embebido en el servicio web por defecto, o como proceso `python -m app.radar.worker`) consume la cola `jobs` con `FOR UPDATE SKIP LOCKED` (fin de vínculo, chequeo de salud y aviso de caída al dueño). El receptor `POST /webhook/waha` verifica HMAC sha512 fail-closed; en este tramo solo aplica `session.status` y descarta los `message.*` sin persistir nada. La Consola y la pantalla del dueño son HTML estático + un único `radar.js`, servidos por Radar con CSP estricta.

**Tech Stack:** Python 3.12, FastAPI, asyncpg (SQL crudo), Alembic con SQL crudo (`migrations_radar/`), PostgreSQL ≥ 15 con RLS, httpx (`AsyncClient`; `MockTransport` en tests), pytest + pytest-asyncio (`asyncio_mode=auto`), pgserver, HTML + JavaScript sin dependencias.

**Spec:** `docs/superpowers/specs/2026-09-21-onboarding-radar-whatsapp-design.md` (v5.2): D2, D4, D6, D10; §2.1–2.4; §3 P2–P3, §3.1 (C1, C2, C4) y Estados especiales; §4.4; §6.1–6.4; §8 tramo 2; §9.

**Base:** `docs/superpowers/plans/2026-09-21-radar-tramo1-acceso-y-datos.md` (todo lo que este plan consume de ahí se nombra exactamente igual) y `docs/superpowers/plans/2026-09-21-radar-spike-waha.md` (molde del cliente con lista blanca, del cuerpo de sesión, de la verificación posterior y del fin de sesión; **no se importa** `scripts/` desde `app/`: se reimplementa en `app/radar/waha/`).

**Rama sugerida:** `feature/radar-tramo2`, creada desde `feature/radar-tramo1` con el tramo 1 completo y en verde.

## Global Constraints

- Todo lo del tramo 1 sigue vigente: `tenant_tx(tenant_id)` para todo acceso, `ENABLE` + `FORCE ROW LEVEL SECURITY` en cada tabla nueva, `tenant_id NOT NULL DEFAULT radar_tenant_actual()`, nada de `app.services.db` en `app/radar/`, SQL con `$n`, operaciones entre tenants solo por funciones `SECURITY DEFINER` de `radar_admin` creadas con `definir_funcion_admin`.
- **Clave admin de WAHA**: solo la lee `app/radar/workers.py::cliente_de`, desde `ctx.secretos` (`waha_admin:<worker_id>`). Nunca va a la base, a un log, a una respuesta HTTP ni al navegador; un test lo verifica recorriendo el código. La registra `scripts/radar_workers.py`, que la toma de la variable de entorno `WAHA_ADMIN_KEY` (nunca por argumento).
- **Lista blanca del cliente WAHA** (`app/radar/waha/cliente.py`): `GET /api/server/version`, `POST /api/sessions`, `GET|DELETE /api/sessions/{s}`, `POST /api/sessions/{s}/(start|stop|restart)`, `GET /api/{s}/auth/qr`, `POST /api/{s}/auth/request-code`, `POST|GET /api/keys`, `DELETE /api/keys/{id}`. Cualquier otra ruta lanza `RutaNoPermitida` antes de tocar la red: `send*`, `sendSeen`, presencia, typing, `logout`, `chats/overview`, lecturas de chats y mensajes (llegan en el tramo 3, con la clave de lectura), `server/environment`, `PUT` de sesión. Solo sesiones que cumplen `^v_[0-9a-f]{12}$`.
- **Cuerpo de sesión** exactamente el de P3: nombre `v_<12 hex del link_id>` (por vínculo, nunca por tenant ni por línea), `metadata {tenant_id, line_id, link_id}`, `ignore {status, groups, channels, broadcast: true}`, webhook con `events ["message.any", "message.ack", "message.edited", "message.revoked", "session.status"]`, `hmac.key` y `retries {policy: exponential, delaySeconds: 2, attempts: 15}`; bloque `noweb {markOnline: false, store {enabled: true, fullSync}}` o `gows {storage {...}}` según `waha_workers.engine`. Sin `deviceName`. Tras crear se relee `GET /api/sessions/{name}`; si `markOnline`, `store`/`storage`, `ignore`, `metadata` o el webhook no coinciden, **se borra la sesión y se aborta antes de mostrar el QR**.
- **No se crea ninguna sesión WAHA sin una fila en `consents`** para la línea: lo exige la app y `links.consent_id NOT NULL`.
- **Clave de solo lectura por sesión**: `POST /api/keys` con `isAdmin: false` y `actions` explícito (`ACCIONES_CLAVE_LECTURA`); con `actions` vacío el cliente lanza. El valor va al `SecretStore` (`waha_lectura:<link_id>`); en la base solo queda el `key_id`. En este tramo nadie la usa: la usan la pasada de conteo y el backfill del tramo 3.
- **QR**: se pide desde el servidor y se sirve como imagen por un endpoint autenticado con `Cache-Control: no-store`. Nunca se loguea, nunca se guarda, nunca viaja por email. El código de vinculación tampoco se guarda ni se loguea, y el teléfono con que se pide no se guarda.
- **Fin de vínculo**: marca `cerrando` (desde ahí el receptor ignora sus `session.status`); si la sesión estaba `STOPPED` o `FAILED` y no hay restricción activa, intenta `start` y espera hasta 3 min; hace **un único** `DELETE /api/sessions/{name}` por intento; borra las claves de esa sesión (`DELETE /api/keys/{id}`, solo las suyas); verifica 404 y cero claves; borra la clave local; registra el resultado en `links.fin_resultado` y avisa al dueño. **Nunca `POST …/logout`.** Un paso fallido no bloquea los siguientes: el vínculo queda en `cerrando` y el job se reintenta con backoff. Nuestra base no se toca (retención y borrado de datos son del tramo 6).
- **Restricción de cuenta**: bloquea reconectar y cualquier `start` automático; **no** bloquea el fin de vínculo ni "Desconectar y borrar todo".
- **Admisión**: el gestor elige, entre los workers activos que con la sesión nueva quedan en ≤ 80 % de `max_sesiones` y con disco usado ≤ 70 % de `disco_max_gb`, el de menor ocupación. Si no hay ninguno, no se crea sesión ni se muestra QR: `503 sin_capacidad`, alerta en el log (sin identificadores) y auditoría `admision_rechazada`.
- **Webhook**: HMAC sha512 del cuerpo crudo en `X-Webhook-Hmac`, fail-closed (sin `RADAR_WAHA_WEBHOOK_HMAC_KEY` de 32+ caracteres responde 401 a todo). En este tramo solo aplica `session.status`; los `message.*` responden 200 y **no se persiste nada** de ellos (ni payload ni id de evento: `webhook_inbox` vive en el almacén de fuente y llega en el tramo 3). El receptor no llama a WAHA.
- **Logs**: prohibido loguear cuerpos de webhook, QR, códigos, claves, teléfonos, JID, `me.id` y nombres. Solo UUID internos, tipo de error y método + ruta. `httpx` y `httpcore` quedan en `WARNING` (loguean la URL completa). Del número de la línea solo se guarda el sufijo de 4 dígitos (`links.numero_sufijo`) para mostrar "…1234".
- **UI**: HTML estático sin scripts ni estilos inline, un único `radar.js` y un `radar.css` servidos por Radar; CSP `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'`. El JS escribe solo con `textContent` y `createElement`; un test prohíbe `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval(` y `new Function`.
- Tests: `python -m pytest tests/radar_tests -q`. **Nunca contra un WAHA real**: todo va contra `tests/radar_tests/waha_falso.py` (`httpx.MockTransport`). La prueba contra el contenedor GOWS de staging es manual, con el runbook de la Task 17 y una cuenta de prueba consentida.
- Prohibido durante la ejecución de este plan: llamar herramientas MCP de WAHA, leer chats reales, imprimir secretos.

---

## Decisiones de este plan

> **Enmienda 2026-09-24.** El plan se escribió antes de que existiera el tramo 1; el preflight contra el código real (`app/radar/`, `migrations_radar/versions/r0001|r0002`, `tests/radar_tests/`) encontró 16 diferencias, corregidas acá antes de ejecutar:
>
> 1. **Routers en `app.py`** (Tasks 13–16): `crear_app_radar` no tiene una tupla de routers; cada tarea suma su módulo al `from app.radar.routers import …` y agrega `app.include_router(<módulo>.router)` después de `soporte`.
> 2. **`test_esquema.py` tiene 14 tests**, no 12 (Task 2, "Esperado").
> 3. **`waha_workers`**: `GRANT UPDATE` solo sobre `base_url, max_sesiones, disco_max_gb, disco_usado_gb, activo, updated_at` (Task 2, `UPDATES_POR_COLUMNA`).
> 4. **`links`**: `GRANT UPDATE` solo sobre las columnas de estado y ciclo de vida; nunca `id, tenant_id, line_id, worker_id, consent_id, proveedor, session_name, engine, full_sync, creado_por, created_at` (Task 2).
> 5. **`jobs`**: `GRANT UPDATE` solo sobre `estado, intentos, ejecutar_desde, bloqueado_hasta, ultimo_error, updated_at`; `radar_admin` conserva `SELECT, INSERT, UPDATE` (Task 2).
> 6. **`lines.borrado_solicitado_at`**: r0003 agrega `GRANT UPDATE (borrado_solicitado_at) ON lines TO radar_app` (y lo revoca en el downgrade); sin eso "Desconectar y borrar todo" fallaba con `InsufficientPrivilegeError` (Task 2).
> 7. **Tests que escriben columnas no actualizables** (`links.created_at`, `jobs.max_intentos`) pasan por el helper nuevo `como_superusuario(radar_urls, sql, *args)` (Tasks 2, 8 y 11), y `test_vinculos_esquema.py` suma `test_grant_update_no_alcanza_columnas_de_identidad` (Task 2).
> 8. **`marcar_restriccion` sin vínculo responde `409 sin_vinculo`**, como dice la interfaz y como `pedir_fin` (Task 9, con test).
> 9. **Los `CHECK` de `links.estado` y `links.fin_causa` se arman desde `ESTADOS_LINK` y `CAUSAS_FIN`** (Task 2), como r0002 hace con `TENANT_KIS_STR`.
> 10. **Vínculo caído** (Estados especiales): `aplicar_status` encola un job nuevo `aviso_caida` cuando la caída sale de `vinculado` (nunca de `cerrando`, que la máquina ignora); `fin_vinculo.avisar_caida` manda el email al dueño con la fecha DD/MM de `caido_desde` (hora de Argentina) si el vínculo sigue caído; el worker lo despacha; `radar.js` muestra "Tu WhatsApp se desconectó el DD/MM" y el botón "Reconectar" (Tasks 2, 9, 10, 12, 15 y runbook de la 17).
> 11. **`sin_capacidad` ya no promete email**: el texto pasa a "En este momento no hay lugar para una conexión nueva. Probá de nuevo más tarde o escribinos." (Task 15 y runbook de la 17). Avisar cuando se libere lugar queda como decisión abierta (Self-Review, (e) 7).
> 12. **`reiniciar_qr` con restricción activa responde `409 restriccion_activa`** sin llamar a WAHA (Task 9, con test).
> 13. **Vínculos huérfanos en `creando`**: `radar_jobs_programar_salud` incluye `creando` (Task 2); el chequeo de salud no toca uno de menos de 15 min y, pasados 15 min (`salud.CREANDO_HUERFANO`), lo pasa a `abortado` si la sesión no existe en WAHA o encola su fin con causa `qr_abandonado` si existe (Tasks 8 y 11, con tests).
> 14. **El consentimiento asistido arrastra la propuesta de tenant ya aceptada**: `_tenant_ya_consentido` se mueve de `routers/parametros.py` a `parametros_service.tenant_ya_consentido` y lo usan los dos consentimientos (Task 14, con test).
> 15. **Los mails nunca rompen una operación ya confirmada**: la copia del consentimiento asistido y los avisos de fin y de caída van con `try/except` (solo se loguea el tipo de error); `copia_enviada` refleja si salió y el evento `vinculo_cerrado` lleva `aviso_enviado` 0/1 (Tasks 10 y 14, con tests sobre `MailerQueFalla`).
> 16. **Las rutas de línea de la Consola solo operan sobre un tenant cliente**: la dependencia `_tenant_cliente` responde `404 tenant_inexistente` si el tenant no existe o es el de KIS, como `admin.py` del tramo 1 (Task 14, con test).
>
> Conteos: los "Esperado" de las Tasks 2, 9, 10, 11, 12, 14 y 15 y el total de la Task 17 están actualizados. La decisión 15 (worker embebido) no cambia: sigue pendiente del OK del dueño porque §6.2 pide un proceso aparte.

1. **Webhook con una clave HMAC global.** `RADAR_WAHA_WEBHOOK_HMAC_KEY` (32+ caracteres) firma los webhooks de todas las sesiones. Una clave por vínculo obligaría a leer `metadata` de un cuerpo todavía no verificado para elegir con qué clave verificarlo; con una global se verifica antes de parsear nada. La clave solo vive en Radar y en la configuración de sesiones del WAHA privado.
2. **Cola de jobs propia.** El tramo 1 no trae cola. Se agrega la tabla `jobs` (sin contenido: tipo, `link_id`, causa, estado, intentos, `ejecutar_desde`, `bloqueado_hasta`, tipo de error) y la función `radar_jobs_reclamar(lote, lease_s)` (`SECURITY DEFINER`, `FOR UPDATE SKIP LOCKED`): reclama entre tenants desde una tabla sin contenido y recién después el worker entra a `tenant_tx(tenant_id)`, como pide §6.2. Hay un solo job pendiente o corriendo por `(link_id, tipo)` (índice único parcial). El chequeo de salud es recurrente: al terminar se reprograma a +5 min en la misma fila, así la tabla no crece con cada chequeo.
3. **`waha_workers` pertenece al tenant KIS.** Toda tabla lleva `tenant_id`; los workers son infraestructura de KIS, así que `CHECK (tenant_id = TENANT_KIS)`. Contar sesiones cruza tenants: lo hace `radar_admin_ocupacion_workers()`. El disco usado se carga a mano (`scripts/radar_workers.py disco`) hasta que el punto 13 del spike defina cómo medirlo.
4. **Estados del vínculo** (`links.estado`): `creando` → `esperando_qr` → `vinculado`; `caido` (estuvo vivo y se perdió); `cerrando` → `cerrado` (job de fin); `abortado` (falló la creación o la verificación). "Activos" = `creando|esperando_qr|vinculado`, con índice único parcial: uno por línea. Un vínculo `caido` no es activo, así que reconectar crea un vínculo nuevo de la misma línea mientras el caído espera; cuando el nuevo llega a `WORKING` se encola el fin del anterior con causa `reemplazado` (spec: "el job de fin del vínculo anterior corre cuando el nuevo llega a `WORKING`, o a las 72 h"). La línea pasa a `vinculada` en el primer `WORKING` y a `sin_vinculo` cuando el job de fin termina y no le queda ningún vínculo activo.
5. **Máquina de estados pura** (`app/radar/vinculo_estados.py::transicion`), la misma para webhook, polling de respaldo y chequeo de salud. `FAILED`/`STOPPED` durante el QR no bajan el vínculo: queda en `esperando_qr` con "el código venció" y la persona pide un reinicio (hasta 3, `links.qr_reinicios`). `PASSKEY_REQUIRED` queda en `esperando_qr` con el mensaje de ayuda. Una sesión que desapareció de WAHA (404) se trata como el pseudo-estado `AUSENTE`.
6. **Polling de respaldo** (P3, "sin eventos por 20 s"): lo hace el servidor al atender `GET …/vinculo` si el vínculo está en `esperando_qr` y no hubo un `session.status` en 20 s. El navegador nunca habla con WAHA.
7. **QR abandonado.** El spec no lo dice, pero una sesión en `esperando_qr` que nadie escanea sigue viva en WAHA y ocupa lugar. El chequeo de salud encola su fin (causa `qr_abandonado`, sin email) a los 30 min de creada.
8. **Restricción de cuenta manual en este tramo.** La detección automática (`reachoutTimelock` / `messageCapping`) necesita rutas de WAHA que el spike todavía no confirmó y que no están en la lista blanca. En este tramo un admin de KIS la marca y la levanta desde C4 (`links.restriccion_hasta`, o `restriccion_sin_fecha` si no hay fecha) y el sistema la respeta en todas partes. Sumar la lectura automática queda como decisión abierta.
9. **"Desconectar y borrar todo" en este tramo** hace la parte de WAHA (el mismo job de fin, causa `pedido_kis` o `pedido_dueno`) y marca `lines.borrado_solicitado_at`; el borrado de nuestra base (fuente, derivados, lotes de IA) y la constancia son del tramo 6, que toma esa marca. La UI lo dice. Doble confirmación: en el navegador (`confirm` + escribir el nombre de la línea) y en el servidor (`confirmar: true` y `nombre_linea` idéntico al guardado).
10. **Consentimiento asistido.** `consents` suma `modo` (`propio|asistido`), `cargado_por` (el admin), `modo_asistencia` (`presencial|videollamada`) y `aceptado_por_nombre`; un `CHECK` exige los tres juntos cuando `modo = 'asistido'`. `user_id` es el dueño de la cuenta (quien acepta). La copia va por email al dueño con el `Mailer` del tramo 1, con el texto completo, la versión y la huella del hash. El texto es `TEXTO_V1` del tramo 1 (valores iniciales); las variantes del párrafo "Cuánto dura" para otros parámetros no existen todavía, así que la pantalla muestra los parámetros de la línea al lado del texto.
11. **Campos de C1 que dependen de tramos futuros.** `ultimo_mensaje`, `sincronizacion` (cobertura) y `huecos` (tramo 3) y `gasto_ia_mes` (tramo 5) se devuelven `null` con `"pendiente": {"campo": "tramo N"}` y la tabla muestra "—". El semáforo usa lo que existe: verde = `vinculado` + `WORKING`; amarillo = creando, esperando QR o cerrando; rojo = `caido`, `FAILED` sobre un vínculo vivo, restricción activa o worker al 80 %; gris = sin vínculo. "Con tráfico", "sospecha de silencio" y "cobertura < 90 %" se suman en el tramo 3.
12. **UI estática + JS mínimo**, no HTML armado en el servidor: el HTML no interpola nada (ni un UUID), así que no hay nada que escapar del lado del servidor; todo dato llega por JSON y el JS lo escribe con `textContent`. Eso permite una CSP sin `unsafe-inline` y sin hashes que mantener. La Consola refresca C1 cada 15 s y el estado del vínculo cada 3 s, por polling.
13. **Auditoría de la Consola.** Se auditan la apertura de la Consola (`consola_abierta`) y **todas** las acciones (consentimiento asistido, vincular, reiniciar QR, pedir código, desconectar, borrar, restricción). El polling de C1 y del estado no se audita por pedido (serían miles de filas por día sin información nueva) y no expone conversaciones.
14. **P1 del cliente queda fuera.** El pedido acotó el tramo a P3 del dueño; P1 (qué vende, horario, cuántas personas responden) no condiciona el vínculo y la usan los tramos 3–4. La pantalla del dueño incluye un paso P2 mínimo que usa el endpoint de consentimiento del tramo 1.
15. **Worker embebido por defecto.** §6.2 pide un proceso aparte, pero las claves de WAHA (y las `k_tenant`) viven en el `FileSecretStore` de un volumen de Railway, y un volumen se monta en un solo servicio. Por eso `app/radar/worker.py` se puede correr como proceso (`python -m app.radar.worker`) y, con `RADAR_WORKER_EMBEBIDO=true` (default), el lifespan del servicio web lo corre como tarea. Con un gestor de secretos externo se apaga el embebido y se levanta el proceso aparte, sin cambiar código.

---

## File Structure

```
app/radar/
├── constantes.py                 # MOD: ESTADOS_LINK, CAUSAS_FIN
├── settings.py                   # MOD: waha_webhook_url, waha_webhook_hmac_key, waha_timeout_s
├── app.py                        # MOD: validar_settings (HMAC >= 32) y routers nuevos
├── contexto.py                   # MOD: RadarContexto.waha_transport (solo tests)
├── auditoria.py                  # MOD: acciones, tipos y claves de detalle del tramo 2
├── eventos_producto.py           # MOD: vinculo_iniciado, vinculo_working, vinculo_cerrado
├── waha/
│   ├── __init__.py
│   ├── cliente.py                # WahaCliente: lista blanca, prefijo v_, sin logs de contenido
│   ├── sesion.py                 # nombre_sesion, cuerpo_sesion (P3), verificar_config, ACCIONES_CLAVE_LECTURA
│   └── gestor.py                 # crear_sesion_verificada, crear_clave_lectura, esperar_estado, terminar_sesion
├── workers.py                    # Worker, admisión, registrar_worker, cliente_de (único lector de la clave admin)
├── vinculo_estados.py            # transicion, sufijo_de, restriccion_activa, semaforo (puro)
├── jobs.py                       # Job, encolar, reclamar, completar, reprogramar, fallar, programar_salud
├── vinculos.py                   # iniciar_vinculo, aplicar_status, estado_de_linea, qr_png, reiniciar_qr, pedir_codigo, pedir_fin, restricción
├── fin_vinculo.py                # job de fin de vínculo
├── salud.py                      # job de chequeo de salud (5 min, 72 h, duración, QR abandonado)
├── worker.py                     # proceso `python -m app.radar.worker`: bucle de la cola
├── consentimiento_asistido.py    # texto_para_linea, registrar_consentimiento_asistido, email_copia_consentimiento
├── consola.py                    # fila_consola, listar_lineas_consola
├── static/
│   ├── consola.html              # C1 + C2 + C4
│   ├── conectar.html             # P2 mínimo + P3 del dueño
│   ├── radar.js
│   └── radar.css
└── routers/
    ├── webhook_waha.py           # POST /webhook/waha
    ├── vinculo_comun.py          # modelos y respuestas compartidas por consola.py y vinculo.py
    ├── consola.py                # /radar/admin/consola/lineas y /radar/admin/tenants/{t}/lineas/{l}/…
    ├── paginas.py                # GET /radar/consola, /radar/conectar, /radar/estaticos/*
    └── vinculo.py                # /radar/api/lineas/{l}/vinculo… (dueño)
migrations_radar/versions/r0003_vinculos_workers_y_jobs.py
scripts/radar_workers.py          # registrar worker (clave admin desde WAHA_ADMIN_KEY) y cargar disco usado
docs/radar-despliegue.md          # MOD: sección del tramo 2 (variables, servicio worker, WAHA privado)
docs/radar-waha-runbook-tramo2.md # NUEVO: prueba manual contra el GOWS de staging
tests/radar_tests/
├── conftest.py                   # MOD: TABLAS, fixtures waha y ctx_waha
├── helpers.py                    # MOD: HMAC_TEST, crear_worker_directo, crear_consentimiento_directo, crear_link_directo, escenario_vinculable, vincular_de_prueba
├── waha_falso.py                 # WahaFalso (MockTransport)
├── test_esquema.py               # MOD: TABLAS_TENANT incluye las tablas nuevas
├── test_tramo2_config.py         # Task 1
├── test_vinculos_esquema.py      # Task 2
├── test_waha_cliente.py          # Task 3
├── test_waha_sesion.py           # Task 4
├── test_waha_gestor.py           # Task 5
├── test_workers.py               # Task 6
├── test_vinculo_estados.py       # Task 7
├── test_jobs.py                  # Task 8
├── test_vinculos.py              # Task 9
├── test_fin_vinculo.py           # Task 10
├── test_salud.py                 # Task 11
├── test_worker.py                # Task 12
├── test_webhook_waha.py          # Task 13
├── test_consola_api.py           # Task 14
├── test_paginas.py               # Task 15
└── test_vinculo_cliente.py       # Task 16
```

---

### Task 1: Configuración, contexto y vocabulario de auditoría del tramo 2

**Files:**
- Modify: `app/radar/constantes.py`, `app/radar/settings.py`, `app/radar/app.py` (`validar_settings`), `app/radar/contexto.py`, `app/radar/auditoria.py`, `app/radar/eventos_producto.py`
- Test: `tests/radar_tests/test_tramo2_config.py`

**Interfaces:**
- Consumes: `RadarSettings`, `validar_settings`, `RadarContexto`, `ACCIONES`, `TIPOS_OBJETO`, `CLAVES_DETALLE`, `validar_detalle`, `DetalleProhibido`, `EVENTOS` (tramo 1).
- Produces: `ESTADOS_LINK: tuple[str, ...]` y `CAUSAS_FIN: tuple[str, ...]` en `app.radar.constantes`; `RadarSettings.waha_webhook_url: str`, `.waha_webhook_hmac_key: str`, `.waha_timeout_s: float`; `app.radar.app.MIN_WEBHOOK_HMAC = 32`; `RadarContexto.waha_transport: httpx.AsyncBaseTransport | None = None`; acciones de auditoría `consentimiento_asistido`, `vinculo_iniciado`, `vinculo_abortado`, `admision_rechazada`, `qr_reiniciado`, `codigo_solicitado`, `desconexion_pedida`, `borrado_pedido`, `restriccion_marcada`, `restriccion_levantada`, `vinculo_cerrado`, `consola_abierta`, `worker_registrado`; tipos de objeto `link`, `waha_worker`; claves de detalle `causa` (una de `CAUSAS_FIN`), `modo` (`presencial|videollamada`), `ok` (bool), `motivo` (`sin_capacidad|config_no_coincide|waha_error`); eventos de producto `vinculo_iniciado`, `vinculo_working`, `vinculo_cerrado`.

- [ ] **Step 1: Tests (fallan)**

`tests/radar_tests/test_tramo2_config.py`:

```python
"""Configuración y vocabulario de auditoría que agrega el tramo 2."""
import pytest

from app.radar import auditoria
from app.radar.app import validar_settings
from app.radar.auditoria import ACCIONES, TIPOS_OBJETO, DetalleProhibido
from app.radar.constantes import CAUSAS_FIN
from app.radar.eventos_producto import EVENTOS
from app.radar.settings import RadarSettings

BASE = dict(_env_file=None, database_url="x", migrator_database_url="x", fuente_database_url="x",
            cookie_secret="secreto-de-test-de-32-caracteres!")


def test_hmac_de_webhook_corta_no_arranca():
    with pytest.raises(RuntimeError, match="WAHA_WEBHOOK_HMAC_KEY"):
        validar_settings(RadarSettings(**BASE, waha_webhook_hmac_key="corta"))


def test_hmac_vacia_o_larga_arranca():
    validar_settings(RadarSettings(**BASE))                      # vacía: el receptor rechaza todo
    validar_settings(RadarSettings(**BASE, waha_webhook_hmac_key="h" * 32))


def test_contexto_sin_transporte_waha_por_defecto(radar_ctx):
    assert radar_ctx.waha_transport is None


def test_auditoria_acepta_el_vocabulario_del_tramo2():
    nuevas = {"consentimiento_asistido", "vinculo_iniciado", "vinculo_abortado", "admision_rechazada",
              "qr_reiniciado", "codigo_solicitado", "desconexion_pedida", "borrado_pedido",
              "restriccion_marcada", "restriccion_levantada", "vinculo_cerrado", "consola_abierta",
              "worker_registrado"}
    assert nuevas <= ACCIONES
    assert {"link", "waha_worker"} <= TIPOS_OBJETO
    detalle = {"causa": "caida_72h", "modo": "presencial", "ok": True, "motivo": "sin_capacidad"}
    assert auditoria.validar_detalle(detalle) == detalle
    assert set(CAUSAS_FIN) >= {"pedido_kis", "pedido_dueno", "caida_72h", "reemplazado", "qr_abandonado"}


@pytest.mark.parametrize("malo", [{"causa": "porque_si"}, {"ok": "si"}, {"modo": "telefono"},
                                  {"motivo": "5493411234567"}])
def test_auditoria_rechaza_valores_fuera_de_lista(malo):
    with pytest.raises(DetalleProhibido):
        auditoria.validar_detalle(malo)


def test_eventos_de_producto_del_vinculo():
    assert {"vinculo_iniciado", "vinculo_working", "vinculo_cerrado"} <= EVENTOS
```

- [ ] **Step 2: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_tramo2_config.py -q
```
Esperado: `ImportError: cannot import name 'CAUSAS_FIN' from 'app.radar.constantes'`.

- [ ] **Step 3: Constantes, settings y validación**

Agregar al final de `app/radar/constantes.py`:

```python
# Estados de un vínculo (links.estado) y causas del job de fin de vínculo (§2.2, §6.3 punto 6).
ESTADOS_LINK = ("creando", "esperando_qr", "vinculado", "caido", "cerrando", "cerrado", "abortado")
CAUSAS_FIN = ("duracion", "pedido_dueno", "pedido_kis", "caida_72h", "migracion_api", "baja",
              "reemplazado", "qr_abandonado")
```

En `app/radar/settings.py`, dentro de `RadarSettings`, después de `remitente`:

```python
    # WAHA (tramo 2). La clave admin de cada worker NO va acá: vive en el
    # SecretStore (waha_admin:<worker_id>) y la registra scripts/radar_workers.py.
    waha_webhook_url: str = ""        # URL de /webhook/waha que alcanza WAHA (red privada de Railway)
    waha_webhook_hmac_key: str = ""   # 32+ caracteres; vacía = el receptor rechaza todo (fail-closed)
    waha_timeout_s: float = 20.0
```

En `app/radar/app.py`, debajo de `MIN_COOKIE_SECRET = 32` agregar `MIN_WEBHOOK_HMAC = 32` y reemplazar `validar_settings` por:

```python
def validar_settings(rs: RadarSettings) -> None:
    faltantes = [n for n in OBLIGATORIAS if not getattr(rs, n)]
    if faltantes:
        raise RuntimeError("Faltan variables RADAR_: " + ", ".join(faltantes))
    if len(rs.cookie_secret) < MIN_COOKIE_SECRET:
        raise RuntimeError(f"RADAR_COOKIE_SECRET: mínimo {MIN_COOKIE_SECRET} caracteres aleatorios")
    # Vacía se admite (el receptor responde 401 a todo); corta no: sería una firma débil.
    if rs.waha_webhook_hmac_key and len(rs.waha_webhook_hmac_key) < MIN_WEBHOOK_HMAC:
        raise RuntimeError(f"RADAR_WAHA_WEBHOOK_HMAC_KEY: mínimo {MIN_WEBHOOK_HMAC} caracteres aleatorios")
```

- [ ] **Step 4: Contexto con transporte de WAHA para tests**

`app/radar/contexto.py` completo:

```python
"""
Contexto de Radar: lo que los routers necesitan y que en tests se reemplaza
por fakes. Vive en app.state.radar; lo arma el lifespan (producción) o la
fixture (tests).
"""

from dataclasses import dataclass
from typing import Optional

import httpx
from fastapi import HTTPException, Request

from app.radar.db import RadarDB
from app.radar.fuente import FuenteStore
from app.radar.mailer import Mailer
from app.radar.secrets import SecretStore
from app.radar.settings import RadarSettings


@dataclass
class RadarContexto:
    settings: RadarSettings
    db: RadarDB
    fuente: FuenteStore
    secretos: SecretStore
    mailer: Mailer
    # Solo tests: transporte httpx del servidor WAHA falso. En producción es
    # None y el cliente usa la red.
    waha_transport: Optional[httpx.AsyncBaseTransport] = None

    async def cerrar(self) -> None:
        await self.db.close()
        await self.fuente.close()


def contexto(request: Request) -> RadarContexto:
    ctx = request.app.state.radar
    if ctx is None:
        raise HTTPException(status_code=503, detail="Radar arrancando")
    return ctx
```

- [ ] **Step 5: Vocabulario de auditoría y eventos**

En `app/radar/auditoria.py`, agregar `from app.radar.constantes import CAUSAS_FIN` a los imports y reemplazar `ACCIONES`, `TIPOS_OBJETO`, `ROLES_ACTOR` y `CLAVES_DETALLE` por:

```python
ACCIONES = frozenset({
    "tenant_creado", "tenants_listados", "linea_creada", "usuario_invitado", "invitacion_reenviada",
    "rol_cambiado", "parametro_cambiado", "parametro_propuesto", "consentimiento_registrado", "login_canjeado",
    "sesion_cerrada", "sesion_revocada", "soporte_otorgado", "soporte_revocado", "acceso_soporte",
    # tramo 2: vínculo y Consola KIS
    "consentimiento_asistido", "vinculo_iniciado", "vinculo_abortado", "admision_rechazada",
    "qr_reiniciado", "codigo_solicitado", "desconexion_pedida", "borrado_pedido",
    "restriccion_marcada", "restriccion_levantada", "vinculo_cerrado", "consola_abierta",
    "worker_registrado",
})
TIPOS_OBJETO = frozenset({
    "tenant", "line", "user", "membership", "consent", "session", "support_grant", "login_token",
    "link", "waha_worker",
})
ROLES_ACTOR = frozenset({"admin", "dueno", "gestor", "lector", "soporte", "sistema"})

# clave -> tipo admitido
CLAVES_DETALLE = {
    "longitud_termino": "entero", "resultados": "entero", "cantidad": "entero", "horas": "entero",
    "contact_hmac": "hmac",
    "parametro": "parametro", "valor_anterior": "valor_parametro", "valor_nuevo": "valor_parametro",
    "rol_anterior": "rol", "rol_nuevo": "rol",
    "ambito": "ambito", "proposito": "proposito",
    "causa": "causa", "modo": "modo_asistencia", "ok": "booleano", "motivo": "motivo",
}
_MOTIVOS = frozenset({"sin_capacidad", "config_no_coincide", "waha_error"})
```

y reemplazar `_validar_valor` por:

```python
def _validar_valor(clave: str, tipo: str, valor: Any) -> Any:
    ok = {
        "entero": lambda v: _es_entero(v),
        "hmac": lambda v: isinstance(v, str) and bool(_HMAC.match(v)),
        "parametro": lambda v: v in PARAMETROS,
        "valor_parametro": lambda v: v is None or isinstance(v, (bool, int, float, Decimal))
                                     or v in _ENUMS_PARAMETROS,
        "rol": lambda v: v in _ROLES,
        "ambito": lambda v: v in ("tenant", "linea"),
        "proposito": lambda v: v in ("login", "invitacion"),
        "causa": lambda v: v in CAUSAS_FIN,
        "modo_asistencia": lambda v: v in ("presencial", "videollamada"),
        "booleano": lambda v: isinstance(v, bool),
        "motivo": lambda v: v in _MOTIVOS,
    }[tipo](valor)
    if not ok:
        raise DetalleProhibido(f"detalle.{clave}: valor no admitido")
    return float(valor) if isinstance(valor, Decimal) else valor
```

En `app/radar/eventos_producto.py`, reemplazar `EVENTOS` por:

```python
EVENTOS = frozenset({
    "invitacion_enviada", "login_canjeado", "consentimiento_registrado", "linea_creada",
    # tramo 2 (§9: QR mostrado → WORKING, mediana P0 → WORKING)
    "vinculo_iniciado", "vinculo_working", "vinculo_cerrado",
})
```

- [ ] **Step 6: Correr**

```bash
python -m pytest tests/radar_tests -q
```
Esperado: todo verde; `test_tramo2_config.py` suma 9 (6 funciones, una parametrizada en 4 casos).

- [ ] **Step 7: Commit**

```bash
git add app/radar tests/radar_tests/test_tramo2_config.py
git commit -m "Radar tramo 2: configuracion de WAHA y vocabulario de auditoria del vinculo" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Migración r0003: workers, vínculos, eventos de estado, jobs y consentimiento asistido

**Files:**
- Create: `migrations_radar/versions/r0003_vinculos_workers_y_jobs.py`
- Modify: `tests/radar_tests/conftest.py` (`TABLAS`), `tests/radar_tests/helpers.py`, `tests/radar_tests/test_esquema.py` (`TABLAS_TENANT`)
- Test: `tests/radar_tests/test_vinculos_esquema.py`

**Interfaces:**
- Consumes: `politica_por_tenant`, `grants_app`, `definir_funcion_admin`, `TENANT_KIS_STR`, `TENANT_KIS`, `hash_texto` (tramo 1).
- Produces: tablas `waha_workers`, `links`, `link_status_events`, `jobs`; columnas `consents.modo|cargado_por|modo_asistencia|aceptado_por_nombre` y `lines.borrado_solicitado_at`; funciones `radar_admin_ocupacion_workers() RETURNS TABLE (worker_id uuid, nombre text, base_url text, engine text, max_sesiones int, disco_max_gb numeric, disco_usado_gb numeric, activo bool, sesiones int)`, `radar_admin_consola_lineas() RETURNS TABLE (tenant_id uuid, tenant_nombre text, line_id uuid, line_nombre text, line_estado text, link_id uuid, link_estado text, waha_status text, numero_sufijo text, observado_hasta timestamptz, restriccion_hasta timestamptz, restriccion_sin_fecha bool, caido_desde timestamptz, ultimo_status_at timestamptz, engine text, worker_nombre text, worker_max_sesiones int, worker_sesiones int)`, `radar_jobs_reclamar(p_lote int, p_lease_s int) RETURNS TABLE (id uuid, tenant_id uuid, tipo text, link_id uuid, causa text, intentos int)`, `radar_jobs_programar_salud() RETURNS int` (incluye los vínculos en `creando`); `GRANT UPDATE` por columna (`UPDATES_POR_COLUMNA`) en `waha_workers`, `links` y `jobs`, y sobre `lines.borrado_solicitado_at`; helpers de test `como_superusuario(radar_urls, sql, *args)`, `crear_worker_directo(db, nombre="w1", engine="NOWEB", max_sesiones=50, disco_max_gb=10) -> uuid.UUID`, `crear_consentimiento_directo(db, tenant_id, line_id, user_id) -> uuid.UUID`, `crear_link_directo(db, tenant_id, line_id, worker_id, consent_id, estado="esperando_qr", caido_desde=None, restriccion_hasta=None) -> uuid.UUID`.

- [ ] **Step 1: Helpers de test**

En `tests/radar_tests/helpers.py`, sumar a los imports `import asyncpg`, `from datetime import datetime`, `from typing import Optional`, `from app.radar.constantes import TENANT_KIS` y `from app.radar.consentimiento import hash_texto`, y agregar al final:

```python
async def como_superusuario(radar_urls: dict, sql: str, *args) -> None:
    """Para tests: escribe columnas que radar_app no puede actualizar (created_at,
    max_intentos). No sirve el migrator: con FORCE RLS no ve filas."""
    con = await asyncpg.connect(radar_urls["super"])
    try:
        await con.execute(sql, *args)
    finally:
        await con.close()


async def crear_worker_directo(db: RadarDB, nombre: str = "w1", engine: str = "NOWEB",
                               max_sesiones: int = 50, disco_max_gb: int = 10) -> uuid.UUID:
    async with db.tenant_tx(TENANT_KIS) as con:
        return await con.fetchval(
            "INSERT INTO waha_workers (nombre, base_url, engine, max_sesiones, disco_max_gb) "
            "VALUES ($1, 'http://waha.interno', $2, $3, $4) RETURNING id", nombre, engine, max_sesiones, disco_max_gb)


async def crear_consentimiento_directo(db: RadarDB, tenant_id: uuid.UUID, line_id: uuid.UUID,
                                       user_id: uuid.UUID) -> uuid.UUID:
    async with db.tenant_tx(tenant_id) as con:
        return await con.fetchval(
            "INSERT INTO consents (line_id, user_id, version_texto, hash_texto, opciones) "
            "VALUES ($1, $2, 'v1', $3, '{}'::jsonb) RETURNING id", line_id, user_id, hash_texto("v1"))


async def crear_link_directo(db: RadarDB, tenant_id: uuid.UUID, line_id: uuid.UUID, worker_id: uuid.UUID,
                             consent_id: uuid.UUID, estado: str = "esperando_qr",
                             caido_desde: Optional[datetime] = None,
                             restriccion_hasta: Optional[datetime] = None) -> uuid.UUID:
    link_id = uuid.uuid4()
    async with db.tenant_tx(tenant_id) as con:
        await con.execute(
            "INSERT INTO links (id, line_id, worker_id, consent_id, session_name, engine, estado, caido_desde, "
            "restriccion_hasta) VALUES ($1, $2, $3, $4, $5, 'NOWEB', $6, $7, $8)",
            link_id, line_id, worker_id, consent_id, "v_" + link_id.hex[:12], estado, caido_desde,
            restriccion_hasta)
    return link_id
```

- [ ] **Step 2: Tests de esquema (fallan)**

`tests/radar_tests/test_vinculos_esquema.py`:

```python
"""
Esquema del tramo 2: RLS forzada en las tablas nuevas, un vínculo activo por
línea, consentimiento obligatorio, nada de identificadores de WhatsApp, y las
funciones SECURITY DEFINER que cruzan tenants.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from app.radar.constantes import TENANT_KIS

from .helpers import (crear_consentimiento_directo, crear_link_directo, crear_linea_directa, crear_tenant_directo,
                      crear_usuario, crear_worker_directo, como_superusuario)

NUEVAS = ("waha_workers", "links", "link_status_events", "jobs")


async def _base(db, nombre="A"):
    t = await crear_tenant_directo(db, nombre)
    u = await crear_usuario(db, t, f"dueno-{uuid.uuid4().hex[:6]}@cliente.com", "dueno")
    li = await crear_linea_directa(db, t, "Local")
    c = await crear_consentimiento_directo(db, t, li, u)
    return t, u, li, c


async def test_tablas_nuevas_con_tenant_y_rls_forzada(radar_urls):
    con = await asyncpg.connect(radar_urls["migrator"])
    try:
        for t in NUEVAS:
            col = await con.fetchrow("SELECT is_nullable, column_default FROM information_schema.columns "
                                     "WHERE table_name = $1 AND column_name = 'tenant_id'", t)
            assert col["is_nullable"] == "NO" and "radar_tenant_actual()" in col["column_default"], t
            r = await con.fetchrow("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = $1", t)
            assert r["relrowsecurity"] and r["relforcerowsecurity"], t
    finally:
        await con.close()


async def test_workers_solo_en_el_tenant_kis(radar_db):
    t, *_ = await _base(radar_db)
    with pytest.raises(asyncpg.CheckViolationError):
        async with radar_db.tenant_tx(t) as con:
            await con.execute("INSERT INTO waha_workers (nombre, base_url, engine, max_sesiones, disco_max_gb) "
                              "VALUES ('x', 'http://w', 'NOWEB', 5, 1)")
    await crear_worker_directo(radar_db)


async def test_link_exige_consentimiento(radar_db):
    t, _, li, _ = await _base(radar_db)
    w = await crear_worker_directo(radar_db)
    with pytest.raises(asyncpg.NotNullViolationError):
        async with radar_db.tenant_tx(t) as con:
            await con.execute("INSERT INTO links (line_id, worker_id, session_name, engine) "
                              "VALUES ($1, $2, 'v_0123456789ab', 'NOWEB')", li, w)


async def test_un_solo_vinculo_activo_por_linea(radar_db):
    t, _, li, c = await _base(radar_db)
    w = await crear_worker_directo(radar_db)
    await crear_link_directo(radar_db, t, li, w, c, estado="caido")
    await crear_link_directo(radar_db, t, li, w, c, estado="vinculado")
    with pytest.raises(asyncpg.UniqueViolationError):
        await crear_link_directo(radar_db, t, li, w, c, estado="esperando_qr")


async def test_nombre_de_sesion_y_sufijo(radar_db):
    t, _, li, c = await _base(radar_db)
    w = await crear_worker_directo(radar_db)
    with pytest.raises(asyncpg.CheckViolationError):
        async with radar_db.tenant_tx(t) as con:
            await con.execute("INSERT INTO links (line_id, worker_id, consent_id, session_name, engine) "
                              "VALUES ($1, $2, $3, 'MaroSession', 'NOWEB')", li, w, c)
    k = await crear_link_directo(radar_db, t, li, w, c)
    with pytest.raises(asyncpg.CheckViolationError):
        async with radar_db.tenant_tx(t) as con:
            await con.execute("UPDATE links SET numero_sufijo = '5493411234567' WHERE id = $1", k)


async def test_consentimiento_asistido_completo(radar_db):
    t, u, li, _ = await _base(radar_db)
    admin = await crear_usuario(radar_db, TENANT_KIS, "admin@keepitsimple.com.ar", "admin")
    with pytest.raises(asyncpg.CheckViolationError):
        async with radar_db.tenant_tx(t) as con:
            await con.execute("INSERT INTO consents (line_id, user_id, version_texto, hash_texto, opciones, modo) "
                              "VALUES ($1, $2, 'v1', repeat('a', 64), '{}'::jsonb, 'asistido')", li, u)
    async with radar_db.tenant_tx(t) as con:
        await con.execute(
            "INSERT INTO consents (line_id, user_id, version_texto, hash_texto, opciones, modo, cargado_por, "
            "modo_asistencia, aceptado_por_nombre) VALUES ($1, $2, 'v1', repeat('a', 64), '{}'::jsonb, "
            "'asistido', $3, 'videollamada', 'Ana')", li, u, admin)


async def test_fin_resultado_sin_identificadores(radar_db):
    t, _, li, c = await _base(radar_db)
    w = await crear_worker_directo(radar_db)
    k = await crear_link_directo(radar_db, t, li, w, c)
    async with radar_db.tenant_tx(t) as con:
        await con.execute("UPDATE links SET fin_resultado = $2::jsonb WHERE id = $1", k,
                          json.dumps({"ok": True, "error_claves": "WahaHttpError", "claves_borradas": 2}))
    with pytest.raises(asyncpg.CheckViolationError):
        async with radar_db.tenant_tx(t) as con:
            await con.execute("UPDATE links SET fin_resultado = $2::jsonb WHERE id = $1", k,
                              json.dumps({"me": "5493411234567@c.us"}))


async def test_un_job_vivo_por_link_y_tipo(radar_db):
    t, _, li, c = await _base(radar_db)
    w = await crear_worker_directo(radar_db)
    k = await crear_link_directo(radar_db, t, li, w, c)
    async with radar_db.tenant_tx(t) as con:
        await con.execute("INSERT INTO jobs (tipo, link_id) VALUES ('fin_vinculo', $1)", k)
        await con.execute("INSERT INTO jobs (tipo, link_id) VALUES ('chequeo_salud', $1)", k)
    with pytest.raises(asyncpg.UniqueViolationError):
        async with radar_db.tenant_tx(t) as con:
            await con.execute("INSERT INTO jobs (tipo, link_id) VALUES ('fin_vinculo', $1)", k)


async def test_ocupacion_de_workers_cuenta_todos_los_tenants(radar_db):
    w = await crear_worker_directo(radar_db, max_sesiones=5)
    for nombre in ("A", "B"):
        t, _, li, c = await _base(radar_db, nombre)
        await crear_link_directo(radar_db, t, li, w, c, estado="vinculado")
    async with radar_db.sin_tenant() as con:
        assert await con.fetchval("SELECT count(*) FROM links") == 0          # RLS: sin tenant, nada
        filas = await con.fetch("SELECT * FROM radar_admin_ocupacion_workers()")
    assert [(f["nombre"], f["sesiones"], f["max_sesiones"]) for f in filas] == [("w1", 2, 5)]


async def test_radar_app_no_borra_vinculos_ni_jobs(radar_db):
    t, *_ = await _base(radar_db)
    for sql in ("DELETE FROM links", "DELETE FROM jobs", "DELETE FROM link_status_events"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with radar_db.tenant_tx(t) as con:
                await con.execute(sql)


async def test_consola_devuelve_el_ultimo_vinculo_de_cada_linea(radar_db, radar_urls):
    t, _, li, c = await _base(radar_db, "Farmacia A")
    w = await crear_worker_directo(radar_db)
    viejo = await crear_link_directo(radar_db, t, li, w, c, estado="caido")
    # created_at no es actualizable por radar_app (y el migrator, con FORCE RLS,
    # no ve filas): se envejece con el superusuario.
    await como_superusuario(radar_urls, "UPDATE links SET estado = 'cerrado', "
                                        "created_at = now() - interval '1 day' WHERE id = $1", viejo)
    nuevo = await crear_link_directo(radar_db, t, li, w, c, estado="vinculado",
                                     restriccion_hasta=datetime.now(timezone.utc) + timedelta(days=2))
    async with radar_db.sin_tenant() as con:
        filas = await con.fetch("SELECT * FROM radar_admin_consola_lineas()")
    assert len(filas) == 1
    f = filas[0]
    assert (f["tenant_nombre"], f["line_id"], f["link_id"], f["link_estado"]) == ("Farmacia A", li, nuevo, "vinculado")
    assert f["worker_nombre"] == "w1" and f["worker_sesiones"] == 1 and f["restriccion_hasta"] is not None


async def test_grant_update_no_alcanza_columnas_de_identidad(radar_db):
    t, _, li, c = await _base(radar_db)
    w = await crear_worker_directo(radar_db)
    k = await crear_link_directo(radar_db, t, li, w, c)
    async with radar_db.tenant_tx(t) as con:          # lo que sí se actualiza
        await con.execute("INSERT INTO jobs (tipo, link_id) VALUES ('fin_vinculo', $1)", k)
        await con.execute("UPDATE jobs SET estado = 'corriendo', intentos = 1 WHERE link_id = $1", k)
        await con.execute("UPDATE links SET estado = 'vinculado', numero_sufijo = '1234' WHERE id = $1", k)
        await con.execute("UPDATE lines SET borrado_solicitado_at = now() WHERE id = $1", li)
    async with radar_db.tenant_tx(TENANT_KIS) as con:
        await con.execute("UPDATE waha_workers SET disco_usado_gb = 1 WHERE id = $1", w)
    prohibidos = [(t, "UPDATE links SET line_id = line_id"), (t, "UPDATE links SET consent_id = consent_id"),
                  (t, "UPDATE links SET session_name = session_name"), (t, "UPDATE jobs SET link_id = link_id"),
                  (t, "UPDATE jobs SET tipo = tipo"), (TENANT_KIS, "UPDATE waha_workers SET engine = engine")]
    for tenant, sql in prohibidos:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with radar_db.tenant_tx(tenant) as con:
                await con.execute(sql)
```

En `tests/radar_tests/test_esquema.py`, reemplazar la constante por:

```python
TABLAS_TENANT = {"users", "memberships", "lines", "login_tokens", "sessions", "consents",
                 "support_grants", "access_audit_log", "product_events",
                 "waha_workers", "links", "link_status_events", "jobs"}
```

En `tests/radar_tests/conftest.py`:

```python
# Orden de TRUNCATE: hijas antes que padres.
TABLAS = ["jobs", "link_status_events", "links", "waha_workers",
          "product_events", "access_audit_log", "support_grants", "consents", "sessions",
          "login_tokens", "memberships", "lines", "users", "tenants"]
```

- [ ] **Step 3: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_vinculos_esquema.py -q
```
Esperado: errores en la fixture `radar_db`: `asyncpg.exceptions.UndefinedTableError: relation "jobs" does not exist`.

- [ ] **Step 4: Migración r0003**

`migrations_radar/versions/r0003_vinculos_workers_y_jobs.py`:

```python
"""Radar r0003: workers de WAHA, vínculos, eventos de estado, cola de jobs y
consentimiento asistido (§3.1, §6.2, §6.3 punto 6, §6.4).

Ninguna tabla guarda teléfonos, JID, QR, códigos ni claves de WAHA: del número
solo queda el sufijo de 4 dígitos (numero_sufijo) y de la clave de lectura su
id (key_id). La clave admin de cada worker vive en el SecretStore.

Revision ID: r0003
Revises: r0002
Create Date: 2026-09-23
"""
from alembic import op

from app.radar.constantes import CAUSAS_FIN, ESTADOS_LINK, TENANT_KIS_STR
from app.radar.rls_sql import definir_funcion_admin, grants_app, politica_por_tenant

revision = "r0003"
down_revision = "r0002"
branch_labels = None
depends_on = None

# Sin DELETE en nada: vínculos, eventos y jobs son evidencia operativa.
GRANTS = {
    "waha_workers": "SELECT, INSERT",
    "links": "SELECT, INSERT",
    "link_status_events": "SELECT, INSERT",
    "jobs": "SELECT, INSERT",
}
# Columnas actualizables por radar_app, con el criterio de r0002: nunca id,
# tenant_id, identidad, FK de pertenencia ni created_at. El motor de un worker
# no cambia (cambiarlo es un worker nuevo, §6.2); un vínculo no cambia de línea,
# worker, consentimiento ni sesión; un job no cambia de tipo, link ni causa.
UPDATES_POR_COLUMNA = {
    "waha_workers": "base_url, max_sesiones, disco_max_gb, disco_usado_gb, activo, updated_at",
    "links": ("estado, waha_status, qr_reinicios, key_id, numero_sufijo, conectado_at, caido_desde, "
              "ultimo_status_at, ultimo_chequeo_at, observado_hasta, restriccion_hasta, restriccion_sin_fecha, "
              "fin_causa, fin_resultado, desvinculo_confirmado, updated_at, cerrado_at"),
    "jobs": "estado, intentos, ejecutar_desde, bloqueado_hasta, ultimo_error, updated_at",
}
# Listas de los CHECK derivadas de las constantes (como TENANT_KIS_STR en
# r0002), para que el esquema y el código no se separen.
_ESTADOS = "(" + ", ".join(f"'{e}'" for e in ESTADOS_LINK) + ")"
_CAUSAS = "(" + ", ".join(f"'{c}'" for c in CAUSAS_FIN) + ")"
# Estados en los que la sesión existe en WAHA y ocupa lugar en el worker.
_CON_SESION = "('creando', 'esperando_qr', 'vinculado', 'caido', 'cerrando')"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE waha_workers (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id      UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id)
                           CHECK (tenant_id = '""" + TENANT_KIS_STR + """'),
            nombre         TEXT NOT NULL UNIQUE CHECK (nombre ~ '^[a-z0-9_-]{1,40}$'),
            base_url       TEXT NOT NULL CHECK (base_url ~ '^https?://[^\\s]+$'),
            engine         TEXT NOT NULL CHECK (engine IN ('NOWEB', 'GOWS')),
            max_sesiones   INTEGER NOT NULL CHECK (max_sesiones > 0),
            disco_max_gb   NUMERIC(8, 2) NOT NULL CHECK (disco_max_gb > 0),
            disco_usado_gb NUMERIC(8, 2) NOT NULL DEFAULT 0 CHECK (disco_usado_gb >= 0),
            activo         BOOLEAN NOT NULL DEFAULT TRUE,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        ALTER TABLE lines ADD COLUMN borrado_solicitado_at TIMESTAMPTZ NULL;

        -- §3.1 C2: consentimiento asistido por un admin de KIS.
        ALTER TABLE consents
            ADD COLUMN modo TEXT NOT NULL DEFAULT 'propio' CHECK (modo IN ('propio', 'asistido')),
            ADD COLUMN cargado_por UUID NULL REFERENCES users(id),
            ADD COLUMN modo_asistencia TEXT NULL CHECK (modo_asistencia IN ('presencial', 'videollamada')),
            ADD COLUMN aceptado_por_nombre TEXT NULL
                CHECK (aceptado_por_nombre IS NULL OR length(aceptado_por_nombre) BETWEEN 1 AND 120),
            ADD CONSTRAINT consents_asistido_completo CHECK (
                (modo = 'asistido') = (cargado_por IS NOT NULL AND modo_asistencia IS NOT NULL
                                       AND aceptado_por_nombre IS NOT NULL));

        CREATE TABLE links (
            id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id             UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            line_id               UUID NOT NULL REFERENCES lines(id),
            worker_id             UUID NOT NULL REFERENCES waha_workers(id),
            consent_id            UUID NOT NULL REFERENCES consents(id),
            proveedor             TEXT NOT NULL DEFAULT 'waha' CHECK (proveedor IN ('waha', 'meta', 'kapso')),
            session_name          TEXT NOT NULL UNIQUE CHECK (session_name ~ '^v_[0-9a-f]{12}$'),
            engine                TEXT NOT NULL CHECK (engine IN ('NOWEB', 'GOWS')),
            full_sync             BOOLEAN NOT NULL DEFAULT FALSE,
            estado                TEXT NOT NULL DEFAULT 'creando' CHECK (estado IN """ + _ESTADOS + """),
            waha_status           TEXT NULL CHECK (waha_status ~ '^[A-Z_]{1,40}$'),
            qr_reinicios          INTEGER NOT NULL DEFAULT 0 CHECK (qr_reinicios BETWEEN 0 AND 3),
            key_id                TEXT NULL CHECK (key_id ~ '^[A-Za-z0-9_-]{1,80}$'),
            numero_sufijo         TEXT NULL CHECK (numero_sufijo ~ '^[0-9]{4}$'),
            creado_por            UUID NULL,
            conectado_at          TIMESTAMPTZ NULL,
            caido_desde           TIMESTAMPTZ NULL,
            ultimo_status_at      TIMESTAMPTZ NULL,
            ultimo_chequeo_at     TIMESTAMPTZ NULL,
            observado_hasta       TIMESTAMPTZ NULL,
            restriccion_hasta     TIMESTAMPTZ NULL,
            restriccion_sin_fecha BOOLEAN NOT NULL DEFAULT FALSE,
            fin_causa             TEXT NULL CHECK (fin_causa IN """ + _CAUSAS + """),
            fin_resultado         JSONB NULL CHECK (fin_resultado IS NULL OR (
                                      jsonb_typeof(fin_resultado) = 'object'
                                      AND NOT jsonb_path_exists(fin_resultado,
                                          '$.** ? (@.type() == "string" && (@ like_regex "[0-9]{6}" || @ like_regex "@"))'))),
            desvinculo_confirmado BOOLEAN NULL,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            cerrado_at            TIMESTAMPTZ NULL
        );
        CREATE UNIQUE INDEX links_un_activo_por_linea ON links (line_id)
            WHERE estado IN ('creando', 'esperando_qr', 'vinculado');
        CREATE INDEX links_linea ON links (tenant_id, line_id, created_at DESC);
        CREATE INDEX links_worker ON links (worker_id) WHERE estado IN """ + _CON_SESION + """;

        CREATE TABLE link_status_events (
            id          BIGSERIAL PRIMARY KEY,
            tenant_id   UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            link_id     UUID NOT NULL REFERENCES links(id),
            waha_status TEXT NOT NULL CHECK (waha_status ~ '^[A-Z_]{1,40}$'),
            origen      TEXT NOT NULL CHECK (origen IN ('webhook', 'polling', 'salud', 'accion')),
            estado_link TEXT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX link_status_events_link ON link_status_events (tenant_id, link_id, created_at);

        -- §6.2: cola en Postgres, sin contenido.
        CREATE TABLE jobs (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            tipo            TEXT NOT NULL CHECK (tipo IN ('fin_vinculo', 'chequeo_salud', 'aviso_caida')),
            link_id         UUID NOT NULL REFERENCES links(id),
            causa           TEXT NULL CHECK (causa ~ '^[a-z_]{1,40}$'),
            estado          TEXT NOT NULL DEFAULT 'pendiente'
                            CHECK (estado IN ('pendiente', 'corriendo', 'hecho', 'fallido')),
            intentos        INTEGER NOT NULL DEFAULT 0,
            max_intentos    INTEGER NOT NULL DEFAULT 8,
            ejecutar_desde  TIMESTAMPTZ NOT NULL DEFAULT now(),
            bloqueado_hasta TIMESTAMPTZ NULL,
            ultimo_error    TEXT NULL CHECK (ultimo_error ~ '^[A-Za-z_]{1,60}$'),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE UNIQUE INDEX jobs_uno_vivo_por_link_y_tipo ON jobs (link_id, tipo)
            WHERE estado IN ('pendiente', 'corriendo');
        CREATE INDEX jobs_listos ON jobs (ejecutar_desde) WHERE estado IN ('pendiente', 'corriendo');
    """)

    for tabla, privilegios in GRANTS.items():
        op.execute(politica_por_tenant(tabla))
        op.execute(grants_app(tabla, privilegios))
        columnas = UPDATES_POR_COLUMNA.get(tabla)
        if columnas:
            op.execute(f"GRANT UPDATE ({columnas}) ON {tabla} TO radar_app;")
    # Columna nueva de lines: el GRANT UPDATE por columna de r0002 no la incluye
    # y sin esto "Desconectar y borrar todo" falla con InsufficientPrivilege.
    op.execute("GRANT UPDATE (borrado_solicitado_at) ON lines TO radar_app;")
    op.execute("GRANT USAGE ON SEQUENCE link_status_events_id_seq TO radar_app;")

    # radar_admin lee entre tenants solo lo que necesitan las funciones de abajo.
    op.execute("""
        GRANT SELECT ON waha_workers, links, lines TO radar_admin;
        GRANT SELECT, INSERT, UPDATE ON jobs TO radar_admin;
        CREATE POLICY waha_workers_admin ON waha_workers FOR SELECT TO radar_admin USING (true);
        CREATE POLICY links_admin ON links FOR SELECT TO radar_admin USING (true);
        CREATE POLICY lines_admin ON lines FOR SELECT TO radar_admin USING (true);
        CREATE POLICY jobs_admin ON jobs FOR ALL TO radar_admin USING (true) WITH CHECK (true);
    """)

    op.execute(definir_funcion_admin(
        "radar_admin_ocupacion_workers()",
        """
        CREATE FUNCTION radar_admin_ocupacion_workers()
            RETURNS TABLE (worker_id uuid, nombre text, base_url text, engine text, max_sesiones int,
                           disco_max_gb numeric, disco_usado_gb numeric, activo bool, sesiones int)
            LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
            AS $f$
                SELECT w.id, w.nombre, w.base_url, w.engine, w.max_sesiones, w.disco_max_gb,
                       w.disco_usado_gb, w.activo,
                       (SELECT count(*)::int FROM links l
                        WHERE l.worker_id = w.id AND l.estado IN """ + _CON_SESION + """)
                FROM waha_workers w ORDER BY w.nombre
            $f$;
        """,
    ))
    op.execute(definir_funcion_admin(
        "radar_admin_consola_lineas()",
        """
        CREATE FUNCTION radar_admin_consola_lineas()
            RETURNS TABLE (tenant_id uuid, tenant_nombre text, line_id uuid, line_nombre text, line_estado text,
                           link_id uuid, link_estado text, waha_status text, numero_sufijo text,
                           observado_hasta timestamptz, restriccion_hasta timestamptz, restriccion_sin_fecha bool,
                           caido_desde timestamptz, ultimo_status_at timestamptz, engine text,
                           worker_nombre text, worker_max_sesiones int, worker_sesiones int)
            LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
            AS $f$
                SELECT t.id, t.nombre, li.id, li.nombre, li.estado,
                       lk.id, lk.estado, lk.waha_status, lk.numero_sufijo, lk.observado_hasta,
                       lk.restriccion_hasta, lk.restriccion_sin_fecha, lk.caido_desde, lk.ultimo_status_at,
                       lk.engine, w.nombre, w.max_sesiones,
                       (SELECT count(*)::int FROM links x
                        WHERE x.worker_id = w.id AND x.estado IN """ + _CON_SESION + """)
                FROM lines li
                JOIN tenants t ON t.id = li.tenant_id AND NOT t.es_kis
                LEFT JOIN LATERAL (SELECT * FROM links l WHERE l.line_id = li.id
                                   ORDER BY l.created_at DESC LIMIT 1) lk ON true
                LEFT JOIN waha_workers w ON w.id = lk.worker_id
                ORDER BY t.nombre, li.nombre
            $f$;
        """,
    ))
    op.execute(definir_funcion_admin(
        "radar_jobs_reclamar(integer, integer)",
        """
        CREATE FUNCTION radar_jobs_reclamar(p_lote integer, p_lease_s integer)
            RETURNS TABLE (id uuid, tenant_id uuid, tipo text, link_id uuid, causa text, intentos int)
            LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
            AS $f$
                UPDATE jobs j
                   SET estado = 'corriendo', intentos = j.intentos + 1,
                       bloqueado_hasta = now() + make_interval(secs => p_lease_s), updated_at = now()
                 WHERE j.id IN (
                       SELECT x.id FROM jobs x
                        WHERE (x.estado = 'pendiente' AND x.ejecutar_desde <= now())
                           OR (x.estado = 'corriendo' AND x.bloqueado_hasta < now())
                        ORDER BY x.ejecutar_desde
                        FOR UPDATE SKIP LOCKED
                        LIMIT p_lote)
                RETURNING j.id, j.tenant_id, j.tipo, j.link_id, j.causa, j.intentos
            $f$;
        """,
    ))
    op.execute(definir_funcion_admin(
        "radar_jobs_programar_salud()",
        """
        CREATE FUNCTION radar_jobs_programar_salud() RETURNS integer
            LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
            AS $f$
                WITH nuevos AS (
                    INSERT INTO jobs (tenant_id, tipo, link_id)
                    SELECT l.tenant_id, 'chequeo_salud', l.id FROM links l
                     WHERE l.estado IN ('creando', 'esperando_qr', 'vinculado', 'caido')
                    ON CONFLICT DO NOTHING
                    RETURNING 1)
                SELECT count(*)::int FROM nuevos
            $f$;
        """,
    ))


def downgrade() -> None:
    op.execute("""
        DROP FUNCTION IF EXISTS radar_jobs_programar_salud();
        DROP FUNCTION IF EXISTS radar_jobs_reclamar(integer, integer);
        DROP FUNCTION IF EXISTS radar_admin_consola_lineas();
        DROP FUNCTION IF EXISTS radar_admin_ocupacion_workers();
        DROP POLICY IF EXISTS lines_admin ON lines;
        REVOKE SELECT ON lines FROM radar_admin;
        DROP TABLE IF EXISTS jobs, link_status_events, links, waha_workers;
        ALTER TABLE consents DROP CONSTRAINT IF EXISTS consents_asistido_completo,
            DROP COLUMN IF EXISTS aceptado_por_nombre, DROP COLUMN IF EXISTS modo_asistencia,
            DROP COLUMN IF EXISTS cargado_por, DROP COLUMN IF EXISTS modo;
        REVOKE UPDATE (borrado_solicitado_at) ON lines FROM radar_app;
        ALTER TABLE lines DROP COLUMN IF EXISTS borrado_solicitado_at;
    """)
```

- [ ] **Step 5: Correr**

```bash
python -m pytest tests/radar_tests -q
```
Esperado: todo verde; `test_vinculos_esquema.py` suma 12 y `test_esquema.py` sigue en 14 (con las tablas nuevas en `TABLAS_TENANT`, y `test_sin_columnas_de_conversacion_ni_identificadores_de_whatsapp` pasa sobre las columnas nuevas).

- [ ] **Step 6: Commit**

```bash
git add migrations_radar tests/radar_tests
git commit -m "Radar tramo 2: tablas de workers, vinculos, eventos de estado y jobs con RLS" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Cliente HTTP de WAHA con lista blanca y servidor WAHA falso

**Files:**
- Create: `app/radar/waha/__init__.py`, `app/radar/waha/cliente.py`, `tests/radar_tests/waha_falso.py`
- Test: `tests/radar_tests/test_waha_cliente.py`

**Interfaces:**
- Consumes: httpx.
- Produces: `PATRON_SESION`, `RUTAS_PERMITIDAS`; excepciones `WahaError(RuntimeError)`, `RutaNoPermitida(WahaError)`, `SesionProhibida(WahaError)`, `WahaHttpError(WahaError)` (con `.status`); `verificar_ruta(metodo: str, ruta: str) -> str | None`; `verificar_nombre_sesion(nombre) -> str`; `WahaCliente(base_url: str, admin_key: str, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 20.0)` (context manager async) con `version_servidor() -> dict`, `crear_sesion(cuerpo: dict) -> dict`, `leer_sesion(nombre: str) -> dict | None`, `borrar_sesion(nombre: str) -> int`, `iniciar_sesion(nombre) -> dict`, `detener_sesion(nombre) -> dict`, `reiniciar_sesion(nombre) -> dict`, `qr_png(nombre) -> bytes | None`, `pedir_codigo(nombre, telefono_digitos: str) -> str | None`, `crear_clave(sesion: str, *, actions: dict[str, bool]) -> tuple[str, str]` (id, valor), `listar_claves() -> list[dict]`, `borrar_clave(key_id: str) -> int`. La vista de sesión es `{"name", "status", "engine", "config", "me_id"}`. Test: `WahaFalso` (`transporte() -> httpx.MockTransport`, atributos `sesiones`, `claves`, `llamadas`, `cuerpos`, `mutar_eco`, `estados`, `start_da`, `falla_claves`, `falla_codigo`, `falla_crear`, `falla_leer`, `me_id`) y `PNG`.

- [ ] **Step 1: Servidor WAHA falso**

`tests/radar_tests/waha_falso.py`:

```python
"""
Servidor WAHA falso para los tests de Radar (httpx.MockTransport). Los tests
NUNCA hablan con un WAHA real: la prueba contra el contenedor de staging es
manual (docs/radar-waha-runbook-tramo2.md).

Guarda sesiones y claves en memoria, registra cada llamada como "METODO ruta"
y permite simular ecos distintos, estados, errores de claves y de código.
"""
import copy
import json

import httpx

PNG = b"\x89PNG\r\n\x1a\nqr-falso"


class WahaFalso:
    def __init__(self):
        self.sesiones: dict[str, dict] = {}
        self.claves: list[dict] = []
        self.llamadas: list[str] = []
        self.cuerpos: list[dict] = []
        self.mutar_eco = None                      # callable(config) que altera lo que "guarda" WAHA
        self.estados: dict[str, list[str]] = {}    # estados a devolver, en orden, en cada GET de sesión
        self.start_da = "WORKING"                  # estado tras POST .../start
        self.falla_claves = False
        self.falla_codigo = False
        self.falla_crear = False
        self.falla_leer = False
        self.me_id = "5493411234567@c.us"
        self._n = 0

    def transporte(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, req: httpx.Request) -> httpx.Response:
        m, p = req.method, req.url.path
        self.llamadas.append(f"{m} {p}")
        partes = p.split("/")
        if m == "GET" and p == "/api/server/version":
            return httpx.Response(200, json={"version": "2026.8.2", "engine": "NOWEB", "tier": "CORE"})
        if m == "POST" and p == "/api/sessions":
            if self.falla_crear:
                return httpx.Response(500, json={})
            cuerpo = json.loads(req.content)
            self.cuerpos.append(cuerpo)
            config = copy.deepcopy(cuerpo["config"])
            if self.mutar_eco:
                self.mutar_eco(config)
            self.sesiones[cuerpo["name"]] = {"name": cuerpo["name"], "status": "STARTING", "engine": "NOWEB",
                                             "config": config, "me": None}
            return httpx.Response(201, json=self.sesiones[cuerpo["name"]])
        if p.startswith("/api/sessions/"):
            nombre = partes[3]
            s = self.sesiones.get(nombre)
            if len(partes) == 4 and m == "GET":
                if self.falla_leer:
                    return httpx.Response(500, json={})
                if s is None:
                    return httpx.Response(404, json={})
                if self.estados.get(nombre):
                    s["status"] = self.estados[nombre].pop(0)
                if s["status"] == "WORKING":
                    s["me"] = {"id": self.me_id, "pushName": "Negocio"}
                return httpx.Response(200, json=s)
            if len(partes) == 4 and m == "DELETE":
                if s is None:
                    return httpx.Response(404, json={})
                del self.sesiones[nombre]
                return httpx.Response(200, json={})
            if len(partes) == 5 and m == "POST":
                if s is None:
                    return httpx.Response(404, json={})
                s["status"] = {"start": self.start_da, "restart": "SCAN_QR_CODE", "stop": "STOPPED"}[partes[4]]
                return httpx.Response(201, json=s)
        if len(partes) == 5 and partes[3] == "auth":
            if partes[2] not in self.sesiones:
                return httpx.Response(404, json={})
            if partes[4] == "qr" and m == "GET":
                return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
            if partes[4] == "request-code" and m == "POST":
                if self.falla_codigo:
                    return httpx.Response(500, json={})
                return httpx.Response(200, json={"code": "ABCD-EFGH"})
        if p == "/api/keys":
            if self.falla_claves:
                return httpx.Response(403, json={})
            if m == "POST":
                cuerpo = json.loads(req.content)
                self._n += 1
                clave = {"id": f"k{self._n}", "session": cuerpo["session"], "isAdmin": cuerpo["isAdmin"],
                         "actions": cuerpo["actions"], "key": f"valor-secreto-{self._n}"}
                self.claves.append(clave)
                return httpx.Response(201, json={"id": clave["id"], "key": clave["key"]})
            if m == "GET":
                return httpx.Response(200, json=[{"id": k["id"], "session": k["session"], "isAdmin": k["isAdmin"]}
                                                 for k in self.claves])
        if p.startswith("/api/keys/") and m == "DELETE":
            if self.falla_claves:
                return httpx.Response(403, json={})
            self.claves = [k for k in self.claves if k["id"] != partes[3]]
            return httpx.Response(200, json={})
        return httpx.Response(500, json={})
```

- [ ] **Step 2: Tests del cliente (fallan)**

`tests/radar_tests/test_waha_cliente.py`:

```python
"""
Cliente de WAHA (§6.1): lista blanca de rutas, solo sesiones v_<12 hex>,
clave de lectura con actions explícito y nada de contenido en los logs.
"""
import logging

import pytest

from app.radar.waha.cliente import (RutaNoPermitida, SesionProhibida, WahaCliente, WahaHttpError,
                                    verificar_ruta)

from .waha_falso import PNG, WahaFalso

S = "v_0123456789ab"
ACC = {"read": True, "control": False, "send": False, "media": False}


def _cli(waha):
    return WahaCliente("http://waha.interno", "clave-admin", transport=waha.transporte())


@pytest.mark.parametrize("metodo,ruta", [
    ("POST", "/api/sendText"),
    ("POST", "/api/sendSeen"),
    ("POST", "/api/startTyping"),
    ("POST", f"/api/{S}/presence"),
    ("POST", f"/api/sessions/{S}/logout"),
    ("GET", f"/api/{S}/chats/overview"),
    ("GET", f"/api/{S}/chats"),
    ("GET", f"/api/{S}/chats/x/messages"),
    ("POST", f"/api/{S}/chats/x/messages/read"),
    ("DELETE", f"/api/{S}/chats/x"),
    ("PUT", f"/api/sessions/{S}"),
    ("GET", "/api/server/environment"),
])
async def test_rutas_fuera_de_la_lista_blanca_no_salen(metodo, ruta):
    waha = WahaFalso()
    async with _cli(waha) as cli:
        with pytest.raises(RutaNoPermitida):
            await cli._request(metodo, ruta)
    assert waha.llamadas == []


def test_rutas_permitidas_devuelven_la_sesion():
    assert verificar_ruta("GET", f"/api/sessions/{S}") == S
    assert verificar_ruta("GET", f"/api/{S}/auth/qr") == S
    assert verificar_ruta("POST", "/api/keys") is None
    assert verificar_ruta("DELETE", "/api/keys/k1") is None


async def test_solo_sesiones_v_con_12_hex():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        for malo in ("MaroSession", "spike_nw0", "v_XYZ", "v_0123456789abc"):
            with pytest.raises(SesionProhibida):
                await cli.leer_sesion(malo)
        with pytest.raises(SesionProhibida):
            await cli.crear_sesion({"name": "MaroSession", "config": {}})
        with pytest.raises(SesionProhibida):
            await cli.crear_clave("MaroSession", actions=ACC)
    assert waha.llamadas == []


async def test_crear_y_leer_sesion_devuelve_solo_la_vista():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        creada = await cli.crear_sesion({"name": S, "start": True, "config": {"ignore": {}}})
        assert creada == {"name": S, "status": "STARTING", "engine": "NOWEB", "config": {"ignore": {}}, "me_id": None}
        waha.sesiones[S]["status"] = "WORKING"
        leida = await cli.leer_sesion(S)
    assert set(leida) == {"name", "status", "engine", "config", "me_id"}
    assert leida["me_id"] == waha.me_id


async def test_leer_sesion_inexistente_es_none():
    async with _cli(WahaFalso()) as cli:
        assert await cli.leer_sesion(S) is None


async def test_borrar_sesion_devuelve_el_status_sin_lanzar():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        assert await cli.borrar_sesion(S) == 404
        await cli.crear_sesion({"name": S, "config": {}})
        assert await cli.borrar_sesion(S) == 200


async def test_qr_y_codigo():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        assert await cli.qr_png(S) is None
        await cli.crear_sesion({"name": S, "config": {}})
        assert await cli.qr_png(S) == PNG
        assert await cli.pedir_codigo(S, "5493411234567") == "ABCD-EFGH"
        waha.falla_codigo = True
        assert await cli.pedir_codigo(S, "5493411234567") is None


async def test_crear_clave_exige_actions_explicito():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        with pytest.raises(ValueError):
            await cli.crear_clave(S, actions={})
        assert await cli.crear_clave(S, actions=ACC) == ("k1", "valor-secreto-1")
    assert waha.claves[0]["actions"] == ACC and waha.claves[0]["isAdmin"] is False


async def test_borrar_clave_de_otra_sesion_se_rechaza():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        kid, _ = await cli.crear_clave(S, actions=ACC)
        waha.claves.append({"id": "k99", "session": "MaroSession", "isAdmin": False})
        with pytest.raises(SesionProhibida):
            await cli.borrar_clave("k99")
        assert await cli.borrar_clave(kid) == 200
    assert [k["id"] for k in waha.claves] == ["k99"]


async def test_error_http_inesperado_lanza_con_status():
    waha = WahaFalso()
    waha.falla_crear = True
    async with _cli(waha) as cli:
        with pytest.raises(WahaHttpError) as e:
            await cli.crear_sesion({"name": S, "config": {}})
    assert e.value.status == 500


def test_httpx_queda_en_warning():
    WahaCliente("http://waha.interno", "clave-admin")
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


async def test_no_loguea_codigo_ni_numero(caplog):
    caplog.set_level(logging.DEBUG)
    waha = WahaFalso()
    async with _cli(waha) as cli:
        await cli.crear_sesion({"name": S, "config": {}})
        waha.sesiones[S]["status"] = "WORKING"
        await cli.leer_sesion(S)
        await cli.qr_png(S)
        await cli.pedir_codigo(S, "5493411234567")
    assert "ABCD" not in caplog.text and "5493411234567" not in caplog.text and "clave-admin" not in caplog.text
```

- [ ] **Step 3: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_waha_cliente.py -q
```
Esperado: `ModuleNotFoundError: No module named 'app.radar.waha'`.

- [ ] **Step 4: Implementar el cliente**

`app/radar/waha/__init__.py`:

```python
"""Integración con WAHA. Único paquete de app/ que habla con WAHA (§6.1)."""
```

`app/radar/waha/cliente.py`:

```python
"""
Cliente HTTP de WAHA de Radar (§6.1). Reimplementa el cliente del spike
(scripts/radar_spike/client.py) sin importarlo.

- Lista blanca de rutas: cualquier otra lanza RutaNoPermitida ANTES de tocar la
  red. Nada de enviar, marcar leído, presencia, typing, archivar ni logout.
  Las lecturas de chats y mensajes llegan en el tramo 3.
- Solo sesiones de Radar: `v_` + 12 hex (una por vínculo). Una sesión ajena
  (p. ej. la personal del dueño) no se puede leer, crear ni borrar.
- httpx y httpcore quedan en WARNING: a nivel INFO loguean la URL completa.
- Este módulo no loguea nada: ni cuerpos, ni QR, ni códigos, ni claves. Un
  error de red se convierte en WahaError sin el mensaje de httpx (trae la URL).
"""

import logging
import re
from typing import Any, Optional

import httpx

PATRON_SESION = re.compile(r"^v_[0-9a-f]{12}$")
_S = r"(?P<sesion>[^/]+)"
RUTAS_PERMITIDAS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GET", re.compile(r"^/api/server/version$")),
    ("POST", re.compile(r"^/api/sessions$")),
    ("GET", re.compile(rf"^/api/sessions/{_S}$")),
    ("DELETE", re.compile(rf"^/api/sessions/{_S}$")),
    ("POST", re.compile(rf"^/api/sessions/{_S}/(start|stop|restart)$")),
    ("GET", re.compile(rf"^/api/{_S}/auth/qr$")),
    ("POST", re.compile(rf"^/api/{_S}/auth/request-code$")),
    ("POST", re.compile(r"^/api/keys$")),
    ("GET", re.compile(r"^/api/keys$")),
    ("DELETE", re.compile(r"^/api/keys/[A-Za-z0-9_-]{1,80}$")),
)
_CODIGO = re.compile(r"^[A-Z0-9]{4}-?[A-Z0-9]{4}$")


class WahaError(RuntimeError):
    pass


class RutaNoPermitida(WahaError):
    pass


class SesionProhibida(WahaError):
    pass


class WahaHttpError(WahaError):
    def __init__(self, metodo: str, status: int) -> None:
        super().__init__(f"{metodo} -> HTTP {status}")
        self.status = status


def verificar_ruta(metodo: str, ruta: str) -> Optional[str]:
    """Devuelve la sesión embebida en la ruta (o None) si está permitida; si no, lanza."""
    for m, patron in RUTAS_PERMITIDAS:
        if m == metodo:
            hallado = patron.match(ruta)
            if hallado:
                return hallado.groupdict().get("sesion")
    raise RutaNoPermitida(f"{metodo} fuera de la lista blanca")


def verificar_nombre_sesion(nombre: Any) -> str:
    if not isinstance(nombre, str) or not PATRON_SESION.match(nombre):
        raise SesionProhibida("solo sesiones de Radar (v_ + 12 hex)")
    return nombre


def _vista(d: dict[str, Any]) -> dict[str, Any]:
    me = d.get("me")
    return {
        "name": d.get("name"),
        "status": d.get("status"),
        "engine": d.get("engine"),
        "config": d.get("config") or {},
        "me_id": me.get("id") if isinstance(me, dict) else None,
    }


class WahaCliente:
    def __init__(self, base_url: str, admin_key: str, *, transport: Optional[httpx.AsyncBaseTransport] = None,
                 timeout: float = 20.0) -> None:
        for nombre in ("httpx", "httpcore"):
            logging.getLogger(nombre).setLevel(logging.WARNING)
        self._http = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=timeout,
                                       headers={"X-Api-Key": admin_key, "Accept": "application/json"})

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "WahaCliente":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _request(self, metodo: str, ruta: str, *, params: Optional[dict] = None, json: Any = None,
                       esperar: Optional[tuple[int, ...]] = (200, 201),
                       headers: Optional[dict] = None) -> httpx.Response:
        sesion = verificar_ruta(metodo, ruta)
        if sesion is not None:
            verificar_nombre_sesion(sesion)
        if metodo == "POST" and ruta == "/api/sessions":
            verificar_nombre_sesion((json or {}).get("name"))
        if metodo == "POST" and ruta == "/api/keys":
            verificar_nombre_sesion((json or {}).get("session"))
        try:
            resp = await self._http.request(metodo, ruta, params=params, json=json, headers=headers)
        except httpx.HTTPError as e:
            # Sin el mensaje: httpx lo arma con la URL completa.
            raise WahaError(f"{metodo}: error de red ({type(e).__name__})") from None
        if esperar and resp.status_code not in esperar:
            raise WahaHttpError(metodo, resp.status_code)
        return resp

    # --- servidor ---------------------------------------------------------------
    async def version_servidor(self) -> dict[str, Any]:
        d = (await self._request("GET", "/api/server/version")).json() or {}
        return {k: d.get(k) for k in ("version", "engine", "tier")}

    # --- sesiones ---------------------------------------------------------------
    async def crear_sesion(self, cuerpo: dict[str, Any]) -> dict[str, Any]:
        return _vista((await self._request("POST", "/api/sessions", json=cuerpo)).json() or {})

    async def leer_sesion(self, nombre: str) -> Optional[dict[str, Any]]:
        r = await self._request("GET", f"/api/sessions/{nombre}", esperar=None)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise WahaHttpError("GET", r.status_code)
        return _vista(r.json() or {})

    async def borrar_sesion(self, nombre: str) -> int:
        return (await self._request("DELETE", f"/api/sessions/{nombre}", esperar=None)).status_code

    async def _accion(self, nombre: str, accion: str) -> dict[str, Any]:
        return _vista((await self._request("POST", f"/api/sessions/{nombre}/{accion}")).json() or {})

    async def iniciar_sesion(self, nombre: str) -> dict[str, Any]:
        return await self._accion(nombre, "start")

    async def detener_sesion(self, nombre: str) -> dict[str, Any]:
        return await self._accion(nombre, "stop")

    async def reiniciar_sesion(self, nombre: str) -> dict[str, Any]:
        return await self._accion(nombre, "restart")

    # --- vinculación ------------------------------------------------------------
    async def qr_png(self, nombre: str) -> Optional[bytes]:
        r = await self._request("GET", f"/api/{nombre}/auth/qr", params={"format": "image"}, esperar=None,
                                headers={"Accept": "image/png"})
        return r.content if r.status_code == 200 else None

    async def pedir_codigo(self, nombre: str, telefono_digitos: str) -> Optional[str]:
        r = await self._request("POST", f"/api/{nombre}/auth/request-code",
                                json={"phoneNumber": telefono_digitos}, esperar=None)
        if r.status_code not in (200, 201):
            return None
        try:
            codigo = (r.json() or {}).get("code")
        except ValueError:
            return None
        return codigo if isinstance(codigo, str) and _CODIGO.match(codigo) else None

    # --- claves -----------------------------------------------------------------
    async def crear_clave(self, sesion: str, *, actions: dict[str, bool]) -> tuple[str, str]:
        """Clave de sesión con actions EXPLÍCITO: con null WAHA aplica todos los permisos (§6.1)."""
        if not actions:
            raise ValueError("actions explícito: con null WAHA aplica todos los permisos")
        r = await self._request("POST", "/api/keys", json={"isAdmin": False, "session": sesion, "isActive": True,
                                                           "actions": dict(actions)})
        d = r.json() or {}
        kid, valor = d.get("id"), d.get("key")
        if not kid or not valor:
            raise WahaError("WAHA no devolvió id y valor de la clave")
        return str(kid), str(valor)

    async def listar_claves(self) -> list[dict[str, Any]]:
        crudo = (await self._request("GET", "/api/keys")).json()
        crudo = crudo if isinstance(crudo, list) else []
        return [{"id": str(k.get("id")), "session": k.get("session"), "isAdmin": bool(k.get("isAdmin"))}
                for k in crudo]

    async def borrar_clave(self, key_id: str) -> int:
        clave = next((k for k in await self.listar_claves() if k["id"] == key_id), None)
        if clave is None:
            raise WahaError("clave inexistente")
        verificar_nombre_sesion(clave["session"])
        return (await self._request("DELETE", f"/api/keys/{key_id}", esperar=None)).status_code
```

- [ ] **Step 5: Correr**

```bash
python -m pytest tests/radar_tests/test_waha_cliente.py -q
```
Esperado: `23 passed` (12 rutas rechazadas + 11 tests).

- [ ] **Step 6: Commit**

```bash
git add app/radar/waha tests/radar_tests/waha_falso.py tests/radar_tests/test_waha_cliente.py
git commit -m "Radar tramo 2: cliente de WAHA con lista blanca y servidor WAHA falso para tests" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Cuerpo de sesión de P3 y verificación posterior

**Files:**
- Create: `app/radar/waha/sesion.py`
- Test: `tests/radar_tests/test_waha_sesion.py`

**Interfaces:**
- Consumes: nada del repo.
- Produces: `EVENTOS_WEBHOOK: list[str]`, `IGNORE: dict[str, bool]`, `REINTENTOS: dict`, `GOWS_STORAGE: dict[str, bool]`, `ACCIONES_CLAVE_LECTURA: dict[str, bool]`; `nombre_sesion(link_id: uuid.UUID) -> str`; `cuerpo_sesion(*, link_id: uuid.UUID, tenant_id: uuid.UUID, line_id: uuid.UUID, engine: str, webhook_url: str, hmac_key: str, full_sync: bool = False) -> dict`; `verificar_config(sesion: dict, cuerpo: dict) -> list[str]` (campos que no coinciden; vacía = OK).

- [ ] **Step 1: Tests (fallan)**

`tests/radar_tests/test_waha_sesion.py`:

```python
"""
Cuerpo de creación de sesión exactamente como P3 y verificación posterior:
si WAHA no guardó lo pedido, se aborta antes del QR.
"""
import copy
import uuid

import pytest

from app.radar.waha.sesion import ACCIONES_CLAVE_LECTURA, cuerpo_sesion, nombre_sesion, verificar_config

L, T, LI = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
ARGS = dict(link_id=L, tenant_id=T, line_id=LI, webhook_url="http://radar.interno/webhook/waha",
            hmac_key="h" * 32)


def test_cuerpo_noweb_es_el_de_p3():
    c = cuerpo_sesion(engine="NOWEB", **ARGS)
    assert c == {
        "name": "v_" + L.hex[:12], "start": True,
        "config": {
            "metadata": {"tenant_id": str(T), "line_id": str(LI), "link_id": str(L)},
            "ignore": {"status": True, "groups": True, "channels": True, "broadcast": True},
            "webhooks": [{"url": "http://radar.interno/webhook/waha",
                          "events": ["message.any", "message.ack", "message.edited", "message.revoked",
                                     "session.status"],
                          "hmac": {"key": "h" * 32},
                          "retries": {"policy": "exponential", "delaySeconds": 2, "attempts": 15}}],
            "noweb": {"markOnline": False, "store": {"enabled": True, "fullSync": False}},
        },
    }
    assert "deviceName" not in str(c)


def test_nombre_por_vinculo_y_opaco():
    a, b = uuid.uuid4(), uuid.uuid4()
    assert nombre_sesion(a) != nombre_sesion(b)
    assert nombre_sesion(a) == "v_" + a.hex[:12]
    assert T.hex[:12] not in nombre_sesion(a) and LI.hex[:12] not in nombre_sesion(a)


def test_cuerpo_gows_sin_bloque_noweb():
    c = cuerpo_sesion(engine="GOWS", **ARGS)
    assert "noweb" not in c["config"]
    assert c["config"]["gows"] == {"storage": {"messages": True, "chats": True, "groups": False, "labels": False,
                                               "contacts": True, "messageSecrets": True}}


def test_gows_no_acepta_profundidad_y_motor_desconocido_falla():
    with pytest.raises(ValueError):
        cuerpo_sesion(engine="GOWS", full_sync=True, **ARGS)
    with pytest.raises(ValueError):
        cuerpo_sesion(engine="WEBJS", **ARGS)
    assert cuerpo_sesion(engine="NOWEB", full_sync=True, **ARGS)["config"]["noweb"]["store"]["fullSync"] is True


def _eco(cuerpo):
    return {"name": cuerpo["name"], "status": "STARTING", "engine": "NOWEB", "config": copy.deepcopy(cuerpo["config"]),
            "me_id": None}


def test_verificar_acepta_eco_exacto_y_claves_de_mas():
    c = cuerpo_sesion(engine="NOWEB", **ARGS)
    eco = _eco(c)
    assert verificar_config(eco, c) == []
    eco["config"]["noweb"]["store"]["extra"] = 1
    eco["config"]["ignore"]["extra"] = True
    eco["config"]["webhooks"][0].pop("hmac")          # WAHA puede no devolver la clave
    assert verificar_config(eco, c) == []


@pytest.mark.parametrize("mutar,campo", [
    (lambda c: c["noweb"].__setitem__("markOnline", True), "noweb.markOnline"),
    (lambda c: c["noweb"].pop("markOnline"), "noweb.markOnline"),
    (lambda c: c["noweb"]["store"].__setitem__("enabled", False), "noweb.store"),
    (lambda c: c["noweb"]["store"].__setitem__("fullSync", True), "noweb.store"),
    (lambda c: c["ignore"].__setitem__("groups", False), "ignore"),
    (lambda c: c.pop("ignore"), "ignore"),
    (lambda c: c["webhooks"][0].__setitem__("url", "http://otro"), "webhooks.url"),
    (lambda c: c["webhooks"][0].__setitem__("events", ["message"]), "webhooks.events"),
    (lambda c: c.__setitem__("webhooks", []), "webhooks.url"),
    (lambda c: c["metadata"].__setitem__("link_id", str(uuid.uuid4())), "metadata"),
])
def test_verificar_reporta_cada_diferencia(mutar, campo):
    c = cuerpo_sesion(engine="NOWEB", **ARGS)
    eco = _eco(c)
    mutar(eco["config"])
    assert campo in verificar_config(eco, c)


def test_verificar_gows_compara_storage():
    c = cuerpo_sesion(engine="GOWS", **ARGS)
    eco = _eco(c)
    assert verificar_config(eco, c) == []
    eco["config"]["gows"]["storage"]["messages"] = False
    assert verificar_config(eco, c) == ["gows.storage"]


def test_acciones_de_la_clave_de_lectura_son_explicitas_y_sin_escritura():
    assert ACCIONES_CLAVE_LECTURA and all(isinstance(v, bool) for v in ACCIONES_CLAVE_LECTURA.values())
    assert [k for k, v in ACCIONES_CLAVE_LECTURA.items() if v] == ["read"]
```

- [ ] **Step 2: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_waha_sesion.py -q
```
Esperado: `ModuleNotFoundError: No module named 'app.radar.waha.sesion'`.

- [ ] **Step 3: Implementar**

`app/radar/waha/sesion.py`:

```python
"""
Cuerpo de creación de sesión (spec §3 P3) y verificación posterior.

- Nombre por VÍNCULO: v_ + 12 hex del link_id. Nunca por tenant ni por línea:
  ningún residuo de un vínculo anterior coincide con una sesión nueva.
- NOWEB: markOnline:false (con el default true el teléfono del cliente deja de
  notificar) y store activo desde antes del QR (no se puede cambiar después).
- GOWS: la profundidad del historial es del servidor (variables
  WAHA_GOWS_DEVICE_*); por sesión solo se elige `storage`.
- Sin deviceName: rompe el código de vinculación.
- La verificación relee la sesión y compara: si WAHA no guardó lo pedido, el
  gestor borra la sesión y aborta antes de mostrar el QR (`events` no se
  valida del lado de WAHA y un markOnline mal ubicado deja el default true).
"""

import uuid
from typing import Any

EVENTOS_WEBHOOK = ["message.any", "message.ack", "message.edited", "message.revoked", "session.status"]
IGNORE = {"status": True, "groups": True, "channels": True, "broadcast": True}
REINTENTOS = {"policy": "exponential", "delaySeconds": 2, "attempts": 15}
GOWS_STORAGE = {"messages": True, "chats": True, "groups": False, "labels": False, "contacts": True,
                "messageSecrets": True}
# Clave de solo lectura por sesión (§6.1). Los nombres de las acciones se
# verifican contra el servidor de staging (runbook del tramo 2, paso 5).
ACCIONES_CLAVE_LECTURA = {"read": True, "control": False, "send": False, "media": False}


def nombre_sesion(link_id: uuid.UUID) -> str:
    return "v_" + link_id.hex[:12]


def cuerpo_sesion(*, link_id: uuid.UUID, tenant_id: uuid.UUID, line_id: uuid.UUID, engine: str,
                  webhook_url: str, hmac_key: str, full_sync: bool = False) -> dict[str, Any]:
    webhook = {"url": webhook_url, "events": list(EVENTOS_WEBHOOK), "hmac": {"key": hmac_key},
               "retries": dict(REINTENTOS)}
    config: dict[str, Any] = {
        "metadata": {"tenant_id": str(tenant_id), "line_id": str(line_id), "link_id": str(link_id)},
        "ignore": dict(IGNORE),
        "webhooks": [webhook],
    }
    if engine == "NOWEB":
        config["noweb"] = {"markOnline": False, "store": {"enabled": True, "fullSync": bool(full_sync)}}
    elif engine == "GOWS":
        if full_sync:
            raise ValueError("en GOWS la profundidad la fijan variables del servidor, no la sesión")
        config["gows"] = {"storage": dict(GOWS_STORAGE)}
    else:
        raise ValueError(f"motor no soportado: {engine}")
    return {"name": nombre_sesion(link_id), "start": True, "config": config}


def _incluye(obtenido: Any, esperado: dict[str, Any]) -> bool:
    return isinstance(obtenido, dict) and all(obtenido.get(k) == v for k, v in esperado.items())


def verificar_config(sesion: dict[str, Any], cuerpo: dict[str, Any]) -> list[str]:
    obtenido = (sesion or {}).get("config") or {}
    pedido = cuerpo["config"]
    problemas: list[str] = []
    if not _incluye(obtenido.get("metadata"), pedido["metadata"]):
        problemas.append("metadata")
    if not _incluye(obtenido.get("ignore"), pedido["ignore"]):
        problemas.append("ignore")
    if "noweb" in pedido:
        noweb = obtenido.get("noweb") or {}
        if noweb.get("markOnline") is not False:
            problemas.append("noweb.markOnline")
        if not _incluye(noweb.get("store"), pedido["noweb"]["store"]):
            problemas.append("noweb.store")
    if "gows" in pedido:
        if not _incluye((obtenido.get("gows") or {}).get("storage"), pedido["gows"]["storage"]):
            problemas.append("gows.storage")
    hooks = obtenido.get("webhooks") or []
    if not hooks or hooks[0].get("url") != pedido["webhooks"][0]["url"]:
        problemas.append("webhooks.url")
    elif set(hooks[0].get("events") or []) != set(pedido["webhooks"][0]["events"]):
        problemas.append("webhooks.events")
    return problemas
```

- [ ] **Step 4: Correr**

```bash
python -m pytest tests/radar_tests/test_waha_sesion.py -q
```
Esperado: `17 passed` (7 funciones + 10 casos de `test_verificar_reporta_cada_diferencia`).

- [ ] **Step 5: Commit**

```bash
git add app/radar/waha/sesion.py tests/radar_tests/test_waha_sesion.py
git commit -m "Radar tramo 2: cuerpo de sesion de P3 y verificacion posterior a la creacion" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Gestor de sesiones: crear verificada, clave de lectura y fin con un único DELETE

**Files:**
- Create: `app/radar/waha/gestor.py`
- Test: `tests/radar_tests/test_waha_gestor.py`

**Interfaces:**
- Consumes: `WahaCliente`, `WahaError`, `verificar_config`, `ACCIONES_CLAVE_LECTURA`, `cuerpo_sesion` (tests), `WahaFalso` (tests).
- Produces: `ConfigNoCoincide(RuntimeError)` (con `.problemas: list[str]`); `crear_sesion_verificada(cli, cuerpo: dict) -> dict`; `crear_clave_lectura(cli, nombre: str) -> tuple[str, str]`; `esperar_estado(cli, nombre: str, *, objetivos: tuple[str, ...], timeout_s: float, intervalo_s: float) -> str | None`; `terminar_sesion(cli, nombre: str, *, intentar_start: bool, espera_start_s: float = 180, intervalo_s: float = 5) -> dict` con claves `status_antes, start_intentado, desvinculo_confirmado, delete_status, sesion_borrada, claves_borradas, claves_restantes, error_claves, ok`.

- [ ] **Step 1: Tests (fallan)**

`tests/radar_tests/test_waha_gestor.py`:

```python
"""
Ciclo de vida de una sesión: creación verificada (aborta y borra si WAHA no
guardó lo pedido), clave de lectura, y fin con un único DELETE, borrado de
las claves de ESA sesión y verificación 404. Nunca logout.
"""
import uuid

import pytest

from app.radar.waha.cliente import WahaCliente
from app.radar.waha.gestor import (ConfigNoCoincide, crear_clave_lectura, crear_sesion_verificada,
                                   terminar_sesion)
from app.radar.waha.sesion import ACCIONES_CLAVE_LECTURA, cuerpo_sesion

from .waha_falso import WahaFalso

CUERPO = cuerpo_sesion(link_id=uuid.uuid4(), tenant_id=uuid.uuid4(), line_id=uuid.uuid4(), engine="NOWEB",
                       webhook_url="http://radar.interno/webhook/waha", hmac_key="h" * 32)
N = CUERPO["name"]
OTRA = "v_ffffffffffff"


def _cli(waha):
    return WahaCliente("http://waha.interno", "clave-admin", transport=waha.transporte())


async def test_crear_verificada_crea_y_relee():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        sesion = await crear_sesion_verificada(cli, CUERPO)
    assert sesion["name"] == N
    assert waha.llamadas == ["POST /api/sessions", f"GET /api/sessions/{N}"]


async def test_aborta_y_borra_si_mark_online_quedo_en_true():
    waha = WahaFalso()
    waha.mutar_eco = lambda c: c["noweb"].__setitem__("markOnline", True)
    async with _cli(waha) as cli:
        with pytest.raises(ConfigNoCoincide) as e:
            await crear_sesion_verificada(cli, CUERPO)
    assert e.value.problemas == ["noweb.markOnline"]
    assert waha.llamadas[-1] == f"DELETE /api/sessions/{N}" and waha.sesiones == {}


async def test_aborta_si_la_metadata_no_coincide():
    waha = WahaFalso()
    waha.mutar_eco = lambda c: c.pop("metadata")
    async with _cli(waha) as cli:
        with pytest.raises(ConfigNoCoincide) as e:
            await crear_sesion_verificada(cli, CUERPO)
    assert "metadata" in e.value.problemas


async def test_clave_de_lectura_con_actions_explicito():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        kid, valor = await crear_clave_lectura(cli, N)
    assert (kid, valor) == ("k1", "valor-secreto-1")
    assert waha.claves[0]["actions"] == ACCIONES_CLAVE_LECTURA and waha.claves[0]["isAdmin"] is False


async def test_fin_un_solo_delete_sin_logout_y_solo_sus_claves():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        await crear_sesion_verificada(cli, CUERPO)
        await crear_clave_lectura(cli, N)
        await crear_clave_lectura(cli, N)
        await crear_clave_lectura(cli, OTRA)
        waha.sesiones[N]["status"] = "WORKING"
        waha.llamadas.clear()
        res = await terminar_sesion(cli, N, intentar_start=True)
    assert res == {"status_antes": "WORKING", "start_intentado": False, "desvinculo_confirmado": True,
                   "delete_status": 200, "sesion_borrada": True, "claves_borradas": 2, "claves_restantes": 0,
                   "error_claves": None, "ok": True}
    assert waha.llamadas.count(f"DELETE /api/sessions/{N}") == 1
    assert not any("logout" in x for x in waha.llamadas)
    assert [k["session"] for k in waha.claves] == [OTRA]


async def test_fin_con_sesion_detenida_intenta_start_y_confirma():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        await crear_sesion_verificada(cli, CUERPO)
        waha.sesiones[N]["status"] = "STOPPED"
        res = await terminar_sesion(cli, N, intentar_start=True, espera_start_s=1, intervalo_s=0)
    assert res["start_intentado"] is True and res["desvinculo_confirmado"] is True
    assert f"POST /api/sessions/{N}/start" in waha.llamadas


async def test_fin_sin_permiso_de_start_borra_igual():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        await crear_sesion_verificada(cli, CUERPO)
        waha.sesiones[N]["status"] = "FAILED"
        res = await terminar_sesion(cli, N, intentar_start=False)
    assert res["start_intentado"] is False and res["desvinculo_confirmado"] is False and res["ok"] is True
    assert f"POST /api/sessions/{N}/start" not in waha.llamadas


async def test_start_que_no_llega_a_working_no_confirma():
    waha = WahaFalso()
    waha.start_da = "STARTING"
    async with _cli(waha) as cli:
        await crear_sesion_verificada(cli, CUERPO)
        waha.sesiones[N]["status"] = "FAILED"
        res = await terminar_sesion(cli, N, intentar_start=True, espera_start_s=0.05, intervalo_s=0.01)
    assert res["start_intentado"] is True and res["desvinculo_confirmado"] is False
    assert res["sesion_borrada"] is True and waha.llamadas.count(f"DELETE /api/sessions/{N}") == 1


async def test_error_en_claves_no_bloquea_la_verificacion():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        await crear_sesion_verificada(cli, CUERPO)
        waha.falla_claves = True
        res = await terminar_sesion(cli, N, intentar_start=False)
    assert res["sesion_borrada"] is True and res["error_claves"] == "WahaHttpError"
    assert res["claves_restantes"] is None and res["ok"] is False


async def test_fin_de_sesion_que_ya_no_existe():
    waha = WahaFalso()
    async with _cli(waha) as cli:
        res = await terminar_sesion(cli, N, intentar_start=True)
    assert res["status_antes"] is None and res["delete_status"] == 404
    assert res["sesion_borrada"] is True and res["ok"] is True
    assert waha.llamadas.count(f"DELETE /api/sessions/{N}") == 1
```

- [ ] **Step 2: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_waha_gestor.py -q
```
Esperado: `ModuleNotFoundError: No module named 'app.radar.waha.gestor'`.

- [ ] **Step 3: Implementar**

`app/radar/waha/gestor.py`:

```python
"""
Gestor del ciclo de vida de una sesión WAHA (§3 P3, §6.3 punto 6).

- crear_sesion_verificada: POST, relee y compara; si no coincide, borra la
  sesión y lanza ConfigNoCoincide (nunca se muestra un QR de una sesión mal
  configurada).
- terminar_sesion: un único DELETE /api/sessions/{name}. Nunca logout: sobre
  una sesión en marcha, WAHA hace logout y la vuelve a arrancar con QR nuevo y
  los mismos webhooks. Si la sesión estaba STOPPED/FAILED y se permite, intenta
  start y espera WORKING antes del DELETE, porque el desvínculo del lado de
  WhatsApp solo ocurre con la sesión WORKING. Después borra las claves de ESA
  sesión (WAHA no las borra solo) y verifica 404. Un paso fallido no impide los
  siguientes.
"""

import asyncio
import time
from typing import Any, Optional

from app.radar.waha.cliente import WahaCliente, WahaError, verificar_nombre_sesion
from app.radar.waha.sesion import ACCIONES_CLAVE_LECTURA, verificar_config


class ConfigNoCoincide(RuntimeError):
    def __init__(self, problemas: list[str]) -> None:
        super().__init__("la sesión no quedó como se pidió: " + ", ".join(problemas))
        self.problemas = problemas


async def crear_sesion_verificada(cli: WahaCliente, cuerpo: dict[str, Any]) -> dict[str, Any]:
    nombre = verificar_nombre_sesion(cuerpo["name"])
    await cli.crear_sesion(cuerpo)
    sesion = await cli.leer_sesion(nombre) or {}
    problemas = verificar_config(sesion, cuerpo)
    if problemas:
        await cli.borrar_sesion(nombre)
        raise ConfigNoCoincide(problemas)
    return sesion


async def crear_clave_lectura(cli: WahaCliente, nombre: str) -> tuple[str, str]:
    return await cli.crear_clave(nombre, actions=ACCIONES_CLAVE_LECTURA)


async def esperar_estado(cli: WahaCliente, nombre: str, *, objetivos: tuple[str, ...], timeout_s: float,
                         intervalo_s: float) -> Optional[str]:
    inicio = time.monotonic()
    while True:
        sesion = await cli.leer_sesion(nombre)
        status = sesion["status"] if sesion else None
        if status in objetivos or time.monotonic() - inicio >= timeout_s:
            return status
        await asyncio.sleep(intervalo_s)


async def terminar_sesion(cli: WahaCliente, nombre: str, *, intentar_start: bool, espera_start_s: float = 180,
                          intervalo_s: float = 5) -> dict[str, Any]:
    verificar_nombre_sesion(nombre)
    antes = await cli.leer_sesion(nombre)
    status_antes = antes["status"] if antes else None
    status_al_borrar = status_antes
    start_intentado = False
    if intentar_start and status_antes in ("STOPPED", "FAILED"):
        start_intentado = True
        try:
            await cli.iniciar_sesion(nombre)
            status_al_borrar = await esperar_estado(cli, nombre, objetivos=("WORKING",), timeout_s=espera_start_s,
                                                    intervalo_s=intervalo_s)
        except WahaError:
            status_al_borrar = status_antes
    delete_status = await cli.borrar_sesion(nombre)

    borradas = 0
    error_claves: Optional[str] = None
    try:
        for clave in [k for k in await cli.listar_claves() if k["session"] == nombre]:
            await cli.borrar_clave(clave["id"])
            borradas += 1
    except WahaError as e:
        error_claves = type(e).__name__

    try:
        borrada = await cli.leer_sesion(nombre) is None
    except WahaError:
        borrada = False
    restantes: Optional[int] = None
    if error_claves is None:
        try:
            restantes = len([k for k in await cli.listar_claves() if k["session"] == nombre])
        except WahaError as e:
            error_claves = type(e).__name__
    return {
        "status_antes": status_antes,
        "start_intentado": start_intentado,
        "desvinculo_confirmado": status_al_borrar == "WORKING",
        "delete_status": delete_status,
        "sesion_borrada": borrada,
        "claves_borradas": borradas,
        "claves_restantes": restantes,
        "error_claves": error_claves,
        "ok": borrada and restantes == 0,
    }
```

- [ ] **Step 4: Correr**

```bash
python -m pytest tests/radar_tests/test_waha_gestor.py -q
```
Esperado: `10 passed`.

- [ ] **Step 5: Commit**

```bash
git add app/radar/waha/gestor.py tests/radar_tests/test_waha_gestor.py
git commit -m "Radar tramo 2: gestor de sesiones con creacion verificada y fin con un unico DELETE" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Workers de WAHA, admisión por capacidad y clave admin en el SecretStore

**Files:**
- Create: `app/radar/workers.py`, `scripts/radar_workers.py`
- Modify: `tests/radar_tests/conftest.py` (fixtures `waha` y `ctx_waha`), `tests/radar_tests/helpers.py` (`HMAC_TEST`)
- Test: `tests/radar_tests/test_workers.py`

**Interfaces:**
- Consumes: `radar_admin_ocupacion_workers()`, `tenant_tx`, `sin_tenant`, `SecretStore`, `auditoria.registrar`, `WahaCliente`, `TENANT_KIS`, `RadarSettings.waha_timeout_s`, `RadarContexto.waha_transport`.
- Produces: `UMBRAL_SESIONES = 0.8`, `UMBRAL_DISCO = 0.7`; `SinCapacidad(RuntimeError)`, `ClaveAdminAusente(RuntimeError)`; `Worker(id, nombre, base_url, engine, max_sesiones, disco_max_gb, disco_usado_gb, activo, sesiones)` con `ocupacion() -> float`; `admite(w: Worker) -> bool`; `elegir(workers: list[Worker]) -> Worker | None`; `nombre_clave_admin(worker_id) -> str`; `nombre_clave_lectura(link_id) -> str`; `async listar_workers(ctx) -> list[Worker]`; `async leer_worker(ctx, worker_id) -> Worker` (lanza `LookupError`); `async elegir_worker(ctx) -> Worker` (lanza `SinCapacidad`); `cliente_de(ctx, w: Worker) -> WahaCliente` (lanza `ClaveAdminAusente`); `async registrar_worker(ctx, *, nombre, base_url, engine, max_sesiones, disco_max_gb, admin_key, actor_user_id=None) -> uuid.UUID`; `async actualizar_disco(ctx, worker_id, usado_gb: float) -> None`; fixtures `waha` (un `WahaFalso`) y `ctx_waha` (`radar_ctx` con webhook configurado, transporte falso y el worker `w1` NOWEB de 50 sesiones y 10 GB); `HMAC_TEST: str`.

- [ ] **Step 1: Fixtures**

Agregar a `tests/radar_tests/helpers.py`:

```python
# Clave HMAC del webhook en tests (32+ caracteres, como exige validar_settings).
HMAC_TEST = "clave-hmac-de-test-de-32-caracteres!!"
```

Agregar al final de `tests/radar_tests/conftest.py` (con los imports `from app.radar.workers import registrar_worker`, `from .helpers import HMAC_TEST` y `from .waha_falso import WahaFalso`):

```python
@pytest.fixture
def waha():
    return WahaFalso()


@pytest.fixture
async def ctx_waha(radar_ctx, waha):
    """radar_ctx con el WAHA falso, el webhook configurado y un worker NOWEB (w1) registrado.
    Muta el mismo objeto que usa la fixture `cliente`, así la app de los tests lo ve."""
    radar_ctx.settings = radar_ctx.settings.model_copy(update={
        "waha_webhook_url": "http://radar.interno/webhook/waha",
        "waha_webhook_hmac_key": HMAC_TEST,
    })
    radar_ctx.waha_transport = waha.transporte()
    await registrar_worker(radar_ctx, nombre="w1", base_url="http://waha.interno", engine="NOWEB",
                           max_sesiones=50, disco_max_gb=10, admin_key="clave-admin-de-test")
    return radar_ctx
```

- [ ] **Step 2: Tests (fallan)**

`tests/radar_tests/test_workers.py`:

```python
"""Admisión por capacidad del worker (§6.2) y clave admin fuera de la base."""
import pathlib
import uuid

import pytest

from app.radar.constantes import TENANT_KIS
from app.radar.workers import (ClaveAdminAusente, SinCapacidad, Worker, admite, cliente_de, elegir, elegir_worker,
                               listar_workers, nombre_clave_admin, registrar_worker)

from .helpers import (crear_consentimiento_directo, crear_link_directo, crear_linea_directa, crear_tenant_directo,
                      crear_usuario)


def _w(nombre="w", sesiones=0, max_s=50, usado=0.0, disco=10.0, activo=True):
    return Worker(id=uuid.uuid4(), nombre=nombre, base_url="http://w", engine="NOWEB", max_sesiones=max_s,
                  disco_max_gb=disco, disco_usado_gb=usado, activo=activo, sesiones=sesiones)


def test_admision_respeta_80_por_ciento_de_sesiones_y_70_de_disco():
    assert admite(_w(sesiones=39)) is True        # con la nueva: 40/50 = 80 %
    assert admite(_w(sesiones=40)) is False       # con la nueva: 41/50 > 80 %
    assert admite(_w(usado=7.0)) is True          # 70 %
    assert admite(_w(usado=7.1)) is False
    assert admite(_w(activo=False)) is False


def test_elegir_el_de_menor_ocupacion():
    assert elegir([_w("a", sesiones=30), _w("b", sesiones=10), _w("c", activo=False)]).nombre == "b"
    assert elegir([_w("d", usado=6.0), _w("e", sesiones=20)]).nombre == "e"   # d: 60 % de disco, e: 40 %


def test_sin_candidatos_devuelve_none():
    assert elegir([_w(sesiones=45), _w(usado=9.0)]) is None
    assert elegir([]) is None


async def test_registrar_guarda_la_clave_admin_fuera_de_la_base(radar_ctx):
    wid = await registrar_worker(radar_ctx, nombre="w1", base_url="http://waha.interno", engine="GOWS",
                                 max_sesiones=50, disco_max_gb=20, admin_key="clave-admin-larga-123")
    assert radar_ctx.secretos.get(nombre_clave_admin(wid)) == b"clave-admin-larga-123"
    async with radar_ctx.db.tenant_tx(TENANT_KIS) as con:
        fila = await con.fetchrow("SELECT * FROM waha_workers WHERE id = $1", wid)
        auditada = await con.fetchval(
            "SELECT count(*) FROM access_audit_log WHERE accion = 'worker_registrado' AND objeto_id = $1", wid)
    assert "clave-admin-larga-123" not in str(dict(fila)) and auditada == 1
    [w] = await listar_workers(radar_ctx)
    assert (w.nombre, w.engine, w.sesiones) == ("w1", "GOWS", 0)


async def test_elegir_worker_cuenta_sesiones_de_todos_los_tenants(radar_ctx):
    wid = await registrar_worker(radar_ctx, nombre="w1", base_url="http://waha.interno", engine="NOWEB",
                                 max_sesiones=5, disco_max_gb=10, admin_key="clave-admin-larga-123")
    assert (await elegir_worker(radar_ctx)).id == wid
    for nombre in ("A", "B"):
        t = await crear_tenant_directo(radar_ctx.db, nombre)
        u = await crear_usuario(radar_ctx.db, t, "dueno@cliente.com", "dueno")
        for i in range(2):
            li = await crear_linea_directa(radar_ctx.db, t, f"Línea {i}")
            c = await crear_consentimiento_directo(radar_ctx.db, t, li, u)
            await crear_link_directo(radar_ctx.db, t, li, wid, c, estado="vinculado")
    with pytest.raises(SinCapacidad):          # 4 sesiones: con una más serían 5/5 > 80 %
        await elegir_worker(radar_ctx)


async def test_cliente_de_sin_clave_admin_lanza(radar_ctx):
    wid = await registrar_worker(radar_ctx, nombre="w1", base_url="http://waha.interno", engine="NOWEB",
                                 max_sesiones=5, disco_max_gb=10, admin_key="clave-admin-larga-123")
    radar_ctx.secretos.delete(nombre_clave_admin(wid))
    [w] = await listar_workers(radar_ctx)
    with pytest.raises(ClaveAdminAusente):
        cliente_de(radar_ctx, w)


def test_la_clave_admin_solo_se_lee_en_workers():
    raiz = pathlib.Path(__file__).resolve().parents[2] / "app" / "radar"

    def con(texto: str) -> list[str]:
        return sorted(a.relative_to(raiz).as_posix() for a in raiz.rglob("*.py")
                      if texto in a.read_text(encoding="utf-8"))

    assert con("waha_admin:") == ["workers.py"]
    assert con("nombre_clave_admin(") == ["workers.py"]
    assert con("X-Api-Key") == ["waha/cliente.py"]
```

- [ ] **Step 3: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_workers.py -q
```
Esperado: `ModuleNotFoundError: No module named 'app.radar.workers'` (en la colección de `conftest.py`).

- [ ] **Step 4: Implementar `workers.py`**

`app/radar/workers.py`:

```python
"""
Workers de WAHA y admisión por capacidad (§6.2).

- El motor es del servidor: cada worker tiene su `engine` y el cuerpo de la
  sesión depende de él.
- Admisión: se elige, entre los activos que con la sesión nueva quedan en
  <= 80 % de max_sesiones y con disco <= 70 %, el de menor ocupación. Si no hay
  ninguno se lanza SinCapacidad y NO se crea sesión ni se muestra QR.
- ÚNICO módulo que lee la clave admin de un worker, desde el SecretStore
  (waha_admin:<worker_id>). Nunca va a la base, a un log ni al navegador.
"""

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from app.radar import auditoria
from app.radar.constantes import TENANT_KIS
from app.radar.contexto import RadarContexto
from app.radar.waha.cliente import WahaCliente

logger = logging.getLogger("app.radar.workers")

UMBRAL_SESIONES = 0.8
UMBRAL_DISCO = 0.7
MOTORES = ("NOWEB", "GOWS")


class SinCapacidad(RuntimeError):
    pass


class ClaveAdminAusente(RuntimeError):
    pass


@dataclass(frozen=True)
class Worker:
    id: uuid.UUID
    nombre: str
    base_url: str
    engine: str
    max_sesiones: int
    disco_max_gb: float
    disco_usado_gb: float
    activo: bool
    sesiones: int

    def ocupacion(self) -> float:
        return max(self.sesiones / self.max_sesiones, self.disco_usado_gb / self.disco_max_gb)


def admite(w: Worker) -> bool:
    return (w.activo
            and (w.sesiones + 1) <= w.max_sesiones * UMBRAL_SESIONES
            and w.disco_usado_gb <= w.disco_max_gb * UMBRAL_DISCO)


def elegir(workers: list[Worker]) -> Optional[Worker]:
    candidatos = [w for w in workers if admite(w)]
    return min(candidatos, key=lambda w: (w.ocupacion(), w.nombre)) if candidatos else None


def nombre_clave_admin(worker_id: uuid.UUID) -> str:
    return f"waha_admin:{worker_id}"


def nombre_clave_lectura(link_id: uuid.UUID) -> str:
    return f"waha_lectura:{link_id}"


async def listar_workers(ctx: RadarContexto) -> list[Worker]:
    async with ctx.db.sin_tenant() as con:
        filas = await con.fetch("SELECT * FROM radar_admin_ocupacion_workers()")
    return [Worker(id=f["worker_id"], nombre=f["nombre"], base_url=f["base_url"], engine=f["engine"],
                   max_sesiones=f["max_sesiones"], disco_max_gb=float(f["disco_max_gb"]),
                   disco_usado_gb=float(f["disco_usado_gb"]), activo=f["activo"], sesiones=f["sesiones"])
            for f in filas]


async def leer_worker(ctx: RadarContexto, worker_id: uuid.UUID) -> Worker:
    for w in await listar_workers(ctx):
        if w.id == worker_id:
            return w
    raise LookupError("worker inexistente")


async def elegir_worker(ctx: RadarContexto) -> Worker:
    elegido = elegir(await listar_workers(ctx))
    if elegido is None:
        logger.warning("ALERTA admisión: ningún worker de WAHA con capacidad (80 %% sesiones / 70 %% disco)")
        raise SinCapacidad("sin worker con capacidad")
    return elegido


def cliente_de(ctx: RadarContexto, w: Worker) -> WahaCliente:
    clave = ctx.secretos.get(nombre_clave_admin(w.id))
    if clave is None:
        raise ClaveAdminAusente(w.nombre)
    return WahaCliente(w.base_url, clave.decode(), transport=ctx.waha_transport, timeout=ctx.settings.waha_timeout_s)


async def registrar_worker(ctx: RadarContexto, *, nombre: str, base_url: str, engine: str, max_sesiones: int,
                           disco_max_gb: float, admin_key: str,
                           actor_user_id: Optional[uuid.UUID] = None) -> uuid.UUID:
    if engine not in MOTORES:
        raise ValueError(f"motor desconocido: {engine}")
    if len(admin_key) < 16:
        raise ValueError("la clave admin de WAHA es demasiado corta")
    async with ctx.db.tenant_tx(TENANT_KIS) as con:
        wid = await con.fetchval(
            "INSERT INTO waha_workers (nombre, base_url, engine, max_sesiones, disco_max_gb) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING id",
            nombre, base_url, engine, max_sesiones, Decimal(str(disco_max_gb)))
        await auditoria.registrar(con, tenant_id=TENANT_KIS, actor_user_id=actor_user_id,
                                  actor_rol="admin" if actor_user_id else "sistema", accion="worker_registrado",
                                  tipo_objeto="waha_worker", objeto_id=wid)
    ctx.secretos.set(nombre_clave_admin(wid), admin_key.encode())
    return wid


async def actualizar_disco(ctx: RadarContexto, worker_id: uuid.UUID, usado_gb: float) -> None:
    async with ctx.db.tenant_tx(TENANT_KIS) as con:
        estado = await con.execute(
            "UPDATE waha_workers SET disco_usado_gb = $2, updated_at = now() WHERE id = $1",
            worker_id, Decimal(str(usado_gb)))
    if estado == "UPDATE 0":
        raise LookupError("worker inexistente")
```

- [ ] **Step 5: Script de operación**

`scripts/radar_workers.py`:

```python
"""
Workers de WAHA de Radar (se corre en la shell del servicio de Radar).

  WAHA_ADMIN_KEY=... python scripts/radar_workers.py registrar --nombre w1 \
      --base-url http://waha-w1.railway.internal:3000 --engine GOWS --max-sesiones 50 --disco-max-gb 20
  python scripts/radar_workers.py disco --nombre w1 --usado-gb 3.5
  python scripts/radar_workers.py listar

La clave admin se lee SOLO de la variable de entorno WAHA_ADMIN_KEY (nunca por
argumento, para que no quede en el historial de la shell) y va al SecretStore,
no a la base. Nunca se imprime.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.radar.app import construir_contexto, validar_settings  # noqa: E402
from app.radar.settings import get_radar_settings  # noqa: E402
from app.radar.workers import actualizar_disco, listar_workers, registrar_worker  # noqa: E402


async def _con_contexto(fn):
    rs = get_radar_settings()
    validar_settings(rs)
    ctx = await construir_contexto(rs)
    try:
        return await fn(ctx)
    finally:
        await ctx.cerrar()


async def _registrar(a: argparse.Namespace) -> None:
    clave = os.environ.get("WAHA_ADMIN_KEY", "")
    if not clave:
        raise SystemExit("Falta WAHA_ADMIN_KEY en el entorno")

    async def fn(ctx):
        wid = await registrar_worker(ctx, nombre=a.nombre, base_url=a.base_url, engine=a.engine,
                                     max_sesiones=a.max_sesiones, disco_max_gb=a.disco_max_gb, admin_key=clave)
        print(f"worker registrado: {a.nombre} ({wid})")
    await _con_contexto(fn)


async def _disco(a: argparse.Namespace) -> None:
    async def fn(ctx):
        w = next((w for w in await listar_workers(ctx) if w.nombre == a.nombre), None)
        if w is None:
            raise SystemExit(f"no existe el worker {a.nombre}")
        await actualizar_disco(ctx, w.id, a.usado_gb)
        print(f"{a.nombre}: {a.usado_gb} GB usados de {w.disco_max_gb}")
    await _con_contexto(fn)


async def _listar(a: argparse.Namespace) -> None:
    async def fn(ctx):
        for w in await listar_workers(ctx):
            print(f"{w.nombre}\t{w.engine}\t{w.sesiones}/{w.max_sesiones} sesiones\t"
                  f"{w.disco_usado_gb}/{w.disco_max_gb} GB\t{'activo' if w.activo else 'inactivo'}")
    await _con_contexto(fn)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Workers de WAHA de Radar")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("registrar")
    r.add_argument("--nombre", required=True)
    r.add_argument("--base-url", required=True)
    r.add_argument("--engine", choices=["NOWEB", "GOWS"], required=True)
    r.add_argument("--max-sesiones", type=int, required=True)
    r.add_argument("--disco-max-gb", type=float, required=True)
    d = sub.add_parser("disco")
    d.add_argument("--nombre", required=True)
    d.add_argument("--usado-gb", type=float, required=True)
    sub.add_parser("listar")
    a = p.parse_args(argv)
    asyncio.run({"registrar": _registrar, "disco": _disco, "listar": _listar}[a.cmd](a))


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Correr**

```bash
python -m pytest tests/radar_tests -q
```
Esperado: todo verde; `test_workers.py` suma 7.

- [ ] **Step 7: Commit**

```bash
git add app/radar/workers.py scripts/radar_workers.py tests/radar_tests
git commit -m "Radar tramo 2: workers de WAHA con admision por capacidad y clave admin en el SecretStore" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Máquina de estados del vínculo, sufijo del número, restricción y semáforo

**Files:**
- Create: `app/radar/vinculo_estados.py`
- Test: `tests/radar_tests/test_vinculo_estados.py`

**Interfaces:**
- Consumes: nada del repo.
- Produces: `ACTIVOS = ("creando", "esperando_qr", "vinculado")`, `VIVOS = ACTIVOS + ("caido",)`, `IGNORADOS = ("cerrando", "cerrado", "abortado")`, `MAX_REINICIOS_QR = 3`, `CAIDA_MAXIMA = timedelta(hours=72)`, `QR_ABANDONADO = timedelta(minutes=30)`, `SIN_EVENTOS_POLLING = timedelta(seconds=20)`, `PATRON_STATUS`; `Transicion(estado: str, efectos: frozenset[str])`; `transicion(estado: str, waha_status: str) -> Transicion` (efectos posibles: `ignorar`, `conectado`, `reemplazar_anteriores`, `caida`, `abandonar`, `qr_vencido`, `passkey`, `desconocido`); `sufijo_de(me_id) -> str | None`; `restriccion_activa(hasta, sin_fecha, ahora) -> bool`; `semaforo(fila: dict, ahora) -> Literal["verde", "amarillo", "rojo", "gris"]`.

- [ ] **Step 1: Tests (fallan)**

`tests/radar_tests/test_vinculo_estados.py`:

```python
"""Máquina de estados del vínculo (P3, Estados especiales, §6.3 punto 6). Pura."""
from datetime import datetime, timedelta, timezone

import pytest

from app.radar.vinculo_estados import restriccion_activa, semaforo, sufijo_de, transicion

CONECTA = {"conectado", "reemplazar_anteriores"}


@pytest.mark.parametrize("estado,status,nuevo,efectos", [
    ("creando", "STARTING", "esperando_qr", set()),
    ("esperando_qr", "SCAN_QR_CODE", "esperando_qr", set()),
    ("esperando_qr", "WORKING", "vinculado", CONECTA),
    ("creando", "WORKING", "vinculado", CONECTA),
    ("caido", "WORKING", "vinculado", CONECTA),
    ("vinculado", "WORKING", "vinculado", set()),
    ("esperando_qr", "FAILED", "esperando_qr", {"qr_vencido"}),
    ("vinculado", "FAILED", "caido", {"caida"}),
    ("vinculado", "STOPPED", "caido", {"caida"}),
    ("vinculado", "SCAN_QR_CODE", "caido", {"caida"}),       # el teléfono quitó el dispositivo
    ("caido", "FAILED", "caido", {"caida"}),
    ("vinculado", "AUSENTE", "caido", {"caida"}),
    ("esperando_qr", "AUSENTE", "caido", {"caida", "abandonar"}),
    ("esperando_qr", "PASSKEY_REQUIRED", "esperando_qr", {"passkey"}),
    ("vinculado", "STARTING", "vinculado", set()),
    ("vinculado", "ALGO_NUEVO", "vinculado", {"desconocido"}),
])
def test_transiciones(estado, status, nuevo, efectos):
    t = transicion(estado, status)
    assert (t.estado, set(t.efectos)) == (nuevo, efectos)


@pytest.mark.parametrize("estado", ["cerrando", "cerrado", "abortado"])
def test_vinculo_que_cierra_ignora_todo(estado):
    t = transicion(estado, "WORKING")
    assert t.estado == estado and t.efectos == {"ignorar"}


def test_sufijo_del_numero_solo_4_digitos():
    assert sufijo_de("5493411234567@c.us") == "4567"
    assert sufijo_de("5493411234567:12@s.whatsapp.net") == "4567"
    assert sufijo_de("123456789@lid") is None
    assert sufijo_de("abc@c.us") is None
    assert sufijo_de(None) is None


AHORA = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


def _fila(**kw):
    base = {"link_estado": "vinculado", "waha_status": "WORKING", "restriccion_hasta": None,
            "restriccion_sin_fecha": False, "worker_max_sesiones": 50, "worker_sesiones": 1}
    base.update(kw)
    return base


@pytest.mark.parametrize("fila,color", [
    (_fila(link_estado=None, waha_status=None, restriccion_sin_fecha=None, worker_max_sesiones=None,
           worker_sesiones=None), "gris"),
    (_fila(link_estado="cerrado"), "gris"),
    (_fila(), "verde"),
    (_fila(link_estado="esperando_qr", waha_status="SCAN_QR_CODE"), "amarillo"),
    (_fila(link_estado="caido", waha_status="FAILED"), "rojo"),
    (_fila(restriccion_hasta=AHORA + timedelta(days=1)), "rojo"),
    (_fila(restriccion_hasta=AHORA - timedelta(days=1)), "verde"),
    (_fila(worker_sesiones=40), "rojo"),
])
def test_semaforo(fila, color):
    assert semaforo(fila, AHORA) == color


def test_restriccion_activa():
    assert restriccion_activa(None, True, AHORA) is True
    assert restriccion_activa(AHORA + timedelta(hours=1), False, AHORA) is True
    assert restriccion_activa(AHORA - timedelta(hours=1), False, AHORA) is False
    assert restriccion_activa(None, None, AHORA) is False
```

- [ ] **Step 2: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_vinculo_estados.py -q
```
Esperado: `ModuleNotFoundError: No module named 'app.radar.vinculo_estados'`.

- [ ] **Step 3: Implementar**

`app/radar/vinculo_estados.py`:

```python
"""
Máquina de estados del vínculo (§3 P3 "Estados", "Estados especiales", §6.3
punto 6). Pura: sin base ni red. La usan el receptor de webhooks, el polling
de respaldo y el chequeo de salud, así las tres fuentes dan el mismo
resultado.

Estados de links.estado: creando -> esperando_qr -> vinculado; caido (estuvo
vivo y se perdió); cerrando -> cerrado (job de fin); abortado (falló la
creación). "AUSENTE" es un pseudo-estado de WAHA: la sesión ya no existe (404).
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal, Optional

ACTIVOS = ("creando", "esperando_qr", "vinculado")
VIVOS = ACTIVOS + ("caido",)
IGNORADOS = ("cerrando", "cerrado", "abortado")
MAX_REINICIOS_QR = 3
CAIDA_MAXIMA = timedelta(hours=72)
QR_ABANDONADO = timedelta(minutes=30)
SIN_EVENTOS_POLLING = timedelta(seconds=20)
UMBRAL_WORKER_LLENO = 0.8
PATRON_STATUS = re.compile(r"^[A-Z_]{1,40}$")


@dataclass(frozen=True)
class Transicion:
    estado: str
    efectos: frozenset = field(default_factory=frozenset)


def _t(estado: str, *efectos: str) -> Transicion:
    return Transicion(estado, frozenset(efectos))


def transicion(estado: str, waha_status: str) -> Transicion:
    if estado in IGNORADOS:
        # §6.3 punto 6.1: un vínculo que cierra no dispara banner, email ni "Reconectar".
        return _t(estado, "ignorar")
    if estado not in VIVOS:
        raise ValueError(f"estado de vínculo desconocido: {estado}")
    en_qr = estado in ("creando", "esperando_qr")
    if waha_status == "WORKING":
        return _t("vinculado") if estado == "vinculado" else _t("vinculado", "conectado", "reemplazar_anteriores")
    if waha_status == "STARTING":
        return _t("esperando_qr" if estado == "creando" else estado)
    if waha_status == "SCAN_QR_CODE":
        return _t("esperando_qr") if en_qr else _t("caido", "caida")
    if waha_status in ("FAILED", "STOPPED"):
        return _t("esperando_qr", "qr_vencido") if en_qr else _t("caido", "caida")
    if waha_status == "AUSENTE":
        return _t("caido", "caida", "abandonar") if en_qr else _t("caido", "caida")
    if waha_status == "PASSKEY_REQUIRED":
        return _t(estado, "passkey")
    return _t(estado, "desconocido")


def sufijo_de(me_id: Any) -> Optional[str]:
    """Últimos 4 dígitos del número de la línea ("…1234"). El resto no se guarda ni se loguea."""
    if not isinstance(me_id, str):
        return None
    usuario, _, dominio = me_id.partition("@")
    usuario = usuario.split(":")[0]
    if dominio not in ("c.us", "s.whatsapp.net") or not usuario.isdigit() or len(usuario) < 8:
        return None
    return usuario[-4:]


def restriccion_activa(hasta: Optional[datetime], sin_fecha: Optional[bool], ahora: datetime) -> bool:
    return bool(sin_fecha) or (hasta is not None and hasta > ahora)


def semaforo(fila: dict, ahora: datetime) -> Literal["verde", "amarillo", "rojo", "gris"]:
    """C1 (§3.1). Tráfico, silencio y cobertura se suman en el tramo 3."""
    estado = fila.get("link_estado")
    if estado is None or estado in ("cerrado", "abortado"):
        return "gris"
    if restriccion_activa(fila.get("restriccion_hasta"), fila.get("restriccion_sin_fecha"), ahora):
        return "rojo"
    if estado == "caido":
        return "rojo"
    maximo = fila.get("worker_max_sesiones")
    if maximo and (fila.get("worker_sesiones") or 0) >= maximo * UMBRAL_WORKER_LLENO:
        return "rojo"
    if estado == "vinculado":
        if fila.get("waha_status") == "WORKING":
            return "verde"
        return "rojo" if fila.get("waha_status") == "FAILED" else "amarillo"
    return "amarillo"
```

- [ ] **Step 4: Correr**

```bash
python -m pytest tests/radar_tests/test_vinculo_estados.py -q
```
Esperado: `29 passed` (16 transiciones + 3 ignorados + 1 sufijo + 8 semáforos + 1 restricción).

- [ ] **Step 5: Commit**

```bash
git add app/radar/vinculo_estados.py tests/radar_tests/test_vinculo_estados.py
git commit -m "Radar tramo 2: maquina de estados del vinculo, semaforo y restriccion de cuenta" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Cola de jobs en Postgres

**Files:**
- Create: `app/radar/jobs.py`
- Test: `tests/radar_tests/test_jobs.py`

**Interfaces:**
- Consumes: tabla `jobs`, `radar_jobs_reclamar`, `radar_jobs_programar_salud`, `RadarDB`.
- Produces: `LEASE_S = 300`; `Job(id, tenant_id, tipo, link_id, causa, intentos)`; `async encolar(con, *, tipo: str, link_id: uuid.UUID, causa: str | None = None, en_segundos: float = 0) -> uuid.UUID | None` (None si ya había uno vivo); `async reclamar(db, *, lote: int = 10, lease_s: int = LEASE_S) -> list[Job]`; `async completar(db, job) -> None`; `async reprogramar(db, job, en_segundos: float) -> None`; `async fallar(db, job, error: BaseException) -> str` (`pendiente|fallido`; backoff `min(30·2^(intentos−1), 3600)` s); `async programar_salud(db) -> int`.

- [ ] **Step 1: Tests (fallan)**

`tests/radar_tests/test_jobs.py`:

```python
"""
Cola en Postgres (§6.2): sin contenido, reclamo entre tenants con SKIP LOCKED
por una función SECURITY DEFINER, reintentos con backoff y un solo job vivo
por (link, tipo).
"""
import uuid

from app.radar import jobs as cola

from .helpers import (como_superusuario, crear_consentimiento_directo, crear_link_directo, crear_linea_directa,
                      crear_tenant_directo, crear_usuario, crear_worker_directo)


async def _link(db, worker_id, estado="vinculado", nombre="A"):
    t = await crear_tenant_directo(db, nombre)
    u = await crear_usuario(db, t, "dueno@cliente.com", "dueno")
    li = await crear_linea_directa(db, t)
    c = await crear_consentimiento_directo(db, t, li, u)
    return t, await crear_link_directo(db, t, li, worker_id, c, estado=estado)


async def _job(db, t, job_id):
    async with db.tenant_tx(t) as con:
        return await con.fetchrow("SELECT *, ejecutar_desde - now() AS falta FROM jobs WHERE id = $1", job_id)


async def test_encolar_es_idempotente_por_link_y_tipo(radar_db):
    t, k = await _link(radar_db, await crear_worker_directo(radar_db))
    async with radar_db.tenant_tx(t) as con:
        primero = await cola.encolar(con, tipo="fin_vinculo", link_id=k, causa="pedido_kis")
        segundo = await cola.encolar(con, tipo="fin_vinculo", link_id=k, causa="pedido_kis")
    assert primero is not None and segundo is None


async def test_reclamar_y_completar(radar_db):
    t, k = await _link(radar_db, await crear_worker_directo(radar_db))
    async with radar_db.tenant_tx(t) as con:
        jid = await cola.encolar(con, tipo="fin_vinculo", link_id=k, causa="pedido_kis")
    [job] = await cola.reclamar(radar_db)
    assert (job.id, job.tenant_id, job.tipo, job.link_id, job.causa, job.intentos) == \
           (jid, t, "fin_vinculo", k, "pedido_kis", 1)
    await cola.completar(radar_db, job)
    assert (await _job(radar_db, t, jid))["estado"] == "hecho"
    assert await cola.reclamar(radar_db) == []


async def test_no_se_reclama_dos_veces_mientras_corre(radar_db):
    t, k = await _link(radar_db, await crear_worker_directo(radar_db))
    async with radar_db.tenant_tx(t) as con:
        await cola.encolar(con, tipo="fin_vinculo", link_id=k)
    assert len(await cola.reclamar(radar_db)) == 1
    assert await cola.reclamar(radar_db) == []


async def test_lease_vencido_se_vuelve_a_reclamar(radar_db):
    t, k = await _link(radar_db, await crear_worker_directo(radar_db))
    async with radar_db.tenant_tx(t) as con:
        jid = await cola.encolar(con, tipo="fin_vinculo", link_id=k)
    await cola.reclamar(radar_db, lease_s=0)
    [otra_vez] = await cola.reclamar(radar_db)
    assert otra_vez.id == jid and otra_vez.intentos == 2


async def test_fallar_con_backoff_y_fallido_al_tope(radar_db, radar_urls):
    t, k = await _link(radar_db, await crear_worker_directo(radar_db))
    async with radar_db.tenant_tx(t) as con:
        jid = await cola.encolar(con, tipo="fin_vinculo", link_id=k)
    [job] = await cola.reclamar(radar_db)
    assert await cola.fallar(radar_db, job, RuntimeError("x")) == "pendiente"
    assert (await _job(radar_db, t, jid))["falta"].total_seconds() > 25
    # max_intentos no es actualizable por radar_app (GRANT por columna de r0003).
    await como_superusuario(radar_urls, "UPDATE jobs SET ejecutar_desde = now(), max_intentos = 2 WHERE id = $1",
                            jid)
    [job] = await cola.reclamar(radar_db)
    assert await cola.fallar(radar_db, job, RuntimeError("x")) == "fallido"


async def test_fallar_guarda_solo_el_tipo_de_error(radar_db):
    t, k = await _link(radar_db, await crear_worker_directo(radar_db))
    async with radar_db.tenant_tx(t) as con:
        jid = await cola.encolar(con, tipo="fin_vinculo", link_id=k)
    [job] = await cola.reclamar(radar_db)
    await cola.fallar(radar_db, job, ValueError("el 5493411234567 no respondió"))
    assert (await _job(radar_db, t, jid))["ultimo_error"] == "ValueError"
    async with radar_db.tenant_tx(t) as con:
        await con.execute("UPDATE jobs SET ejecutar_desde = now() WHERE id = $1", jid)
    [job] = await cola.reclamar(radar_db)
    await cola.fallar(radar_db, job, type("Error404x", (Exception,), {})())
    assert (await _job(radar_db, t, jid))["ultimo_error"] == "Errorx"


async def test_reprogramar_vuelve_a_pendiente_con_intentos_en_cero(radar_db):
    t, k = await _link(radar_db, await crear_worker_directo(radar_db))
    async with radar_db.tenant_tx(t) as con:
        jid = await cola.encolar(con, tipo="chequeo_salud", link_id=k)
    [job] = await cola.reclamar(radar_db)
    await cola.reprogramar(radar_db, job, 300)
    fila = await _job(radar_db, t, jid)
    assert (fila["estado"], fila["intentos"]) == ("pendiente", 0) and fila["falta"].total_seconds() > 250


async def test_programar_salud_solo_vinculos_vivos(radar_db):
    w = await crear_worker_directo(radar_db)
    t = await crear_tenant_directo(radar_db)
    u = await crear_usuario(radar_db, t, "dueno@cliente.com", "dueno")
    # 'creando' entra: un vínculo huérfano en 'creando' lo destraba el chequeo de salud.
    for i, estado in enumerate(("creando", "esperando_qr", "vinculado", "caido", "cerrado")):
        li = await crear_linea_directa(radar_db, t, f"Línea {i}")
        c = await crear_consentimiento_directo(radar_db, t, li, u)
        await crear_link_directo(radar_db, t, li, w, c, estado=estado)
    assert await cola.programar_salud(radar_db) == 4
    assert await cola.programar_salud(radar_db) == 0


async def test_reclamo_concurrente_no_duplica(radar_db):
    w = await crear_worker_directo(radar_db)
    t1, k1 = await _link(radar_db, w, nombre="A")
    t2, k2 = await _link(radar_db, w, nombre="B")
    for t, k in ((t1, k1), (t2, k2)):
        async with radar_db.tenant_tx(t) as con:
            await cola.encolar(con, tipo="fin_vinculo", link_id=k)
    async with radar_db.sin_tenant() as a:
        primero = await a.fetch("SELECT * FROM radar_jobs_reclamar(1, 300)")
        async with radar_db.sin_tenant() as b:          # otra conexión: la fila de `a` está bloqueada
            segundo = await b.fetch("SELECT * FROM radar_jobs_reclamar(10, 300)")
    assert len(primero) == 1 and len(segundo) == 1
    assert primero[0]["id"] != segundo[0]["id"]
    assert {primero[0]["tenant_id"], segundo[0]["tenant_id"]} == {t1, t2}
```

- [ ] **Step 2: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_jobs.py -q
```
Esperado: `ImportError: cannot import name 'jobs' from 'app.radar'`.

- [ ] **Step 3: Implementar**

`app/radar/jobs.py`:

```python
"""
Cola de jobs de Radar en Postgres (§6.2): tabla `jobs` sin contenido.

- encolar() corre dentro de tenant_tx (el job hereda el tenant por DEFAULT).
- reclamar() usa radar_jobs_reclamar (SECURITY DEFINER, FOR UPDATE SKIP
  LOCKED): reclama entre tenants desde una tabla sin contenido; recién después
  el worker entra a tenant_tx(job.tenant_id).
- Un job reclamado queda 'corriendo' con un lease; si el proceso muere, al
  vencer el lease otro worker lo retoma.
- fallar() guarda solo el TIPO de error (nunca el mensaje) y reintenta con
  backoff exponencial hasta max_intentos.
"""

import re
import uuid
from dataclasses import dataclass
from typing import Optional

import asyncpg

from app.radar.db import RadarDB

LEASE_S = 300


@dataclass(frozen=True)
class Job:
    id: uuid.UUID
    tenant_id: uuid.UUID
    tipo: str
    link_id: uuid.UUID
    causa: Optional[str]
    intentos: int


async def encolar(con: asyncpg.Connection, *, tipo: str, link_id: uuid.UUID, causa: Optional[str] = None,
                  en_segundos: float = 0) -> Optional[uuid.UUID]:
    return await con.fetchval(
        "INSERT INTO jobs (tipo, link_id, causa, ejecutar_desde) "
        "VALUES ($1, $2, $3, now() + make_interval(secs => $4)) ON CONFLICT DO NOTHING RETURNING id",
        tipo, link_id, causa, float(en_segundos))


async def reclamar(db: RadarDB, *, lote: int = 10, lease_s: int = LEASE_S) -> list[Job]:
    async with db.sin_tenant() as con:
        filas = await con.fetch("SELECT * FROM radar_jobs_reclamar($1, $2)", lote, lease_s)
    return [Job(id=f["id"], tenant_id=f["tenant_id"], tipo=f["tipo"], link_id=f["link_id"], causa=f["causa"],
                intentos=f["intentos"]) for f in filas]


async def completar(db: RadarDB, job: Job) -> None:
    async with db.tenant_tx(job.tenant_id) as con:
        await con.execute("UPDATE jobs SET estado = 'hecho', bloqueado_hasta = NULL, updated_at = now() "
                          "WHERE id = $1", job.id)


async def reprogramar(db: RadarDB, job: Job, en_segundos: float) -> None:
    async with db.tenant_tx(job.tenant_id) as con:
        await con.execute(
            "UPDATE jobs SET estado = 'pendiente', intentos = 0, bloqueado_hasta = NULL, ultimo_error = NULL, "
            "ejecutar_desde = now() + make_interval(secs => $2), updated_at = now() WHERE id = $1",
            job.id, float(en_segundos))


async def fallar(db: RadarDB, job: Job, error: BaseException) -> str:
    nombre = re.sub(r"[^A-Za-z_]", "", type(error).__name__)[:60] or "Error"
    async with db.tenant_tx(job.tenant_id) as con:
        return await con.fetchval(
            """
            UPDATE jobs SET ultimo_error = $2, bloqueado_hasta = NULL, updated_at = now(),
                   estado = CASE WHEN intentos >= max_intentos THEN 'fallido' ELSE 'pendiente' END,
                   ejecutar_desde = now() + make_interval(
                       secs => least(30 * power(2, greatest(intentos - 1, 0)), 3600))
             WHERE id = $1 RETURNING estado
            """, job.id, nombre)


async def programar_salud(db: RadarDB) -> int:
    async with db.sin_tenant() as con:
        return await con.fetchval("SELECT radar_jobs_programar_salud()")
```

- [ ] **Step 4: Correr**

```bash
python -m pytest tests/radar_tests/test_jobs.py -q
```
Esperado: `9 passed`.

- [ ] **Step 5: Commit**

```bash
git add app/radar/jobs.py tests/radar_tests/test_jobs.py
git commit -m "Radar tramo 2: cola de jobs en Postgres con SKIP LOCKED, lease y backoff" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Servicio de vínculos (vincular, estado, QR, código, reinicios, fin pedido, restricción)

**Files:**
- Create: `app/radar/vinculos.py`
- Modify: `tests/radar_tests/helpers.py` (`escenario_vinculable`, `vincular_de_prueba`)
- Test: `tests/radar_tests/test_vinculos.py`

**Interfaces:**
- Consumes: `auditoria.registrar`, `eventos_producto.registrar_evento`, `jobs.encolar`, `normalizar_e164`, `TelefonoNoSoportado`, `transicion`, `Transicion`, `sufijo_de`, `restriccion_activa`, `ACTIVOS`, `VIVOS`, `MAX_REINICIOS_QR`, `SIN_EVENTOS_POLLING`, `PATRON_STATUS`, `cuerpo_sesion`, `nombre_sesion`, `crear_sesion_verificada`, `crear_clave_lectura`, `terminar_sesion`, `ConfigNoCoincide`, `WahaError`, `elegir_worker`, `leer_worker`, `cliente_de`, `SinCapacidad`, `ClaveAdminAusente`, `nombre_clave_lectura`.
- Produces: `VinculoRechazado(Exception)` (`.status: int`, `.codigo: str`); `COLUMNAS_LINK`; `async ultimo_link(con, line_id) -> dict | None`; `estado_json(link, *, linea_nombre, tiene_consentimiento, ahora) -> dict` (claves `linea_nombre, tiene_consentimiento, link_id, estado, waha_status, qr_disponible, qr_vencido, passkey, reinicios_restantes, numero, restriccion_activa, restriccion_hasta, caido_desde, conectado_at, fin_causa, desvinculo_confirmado`); `async aplicar_status(con, *, tenant_id, link_id, waha_status: str, origen: str, me_id: str | None = None) -> dict` (`{aplicado, estado_anterior, estado, efectos}`); `async iniciar_vinculo(ctx, *, tenant_id, line_id, actor_user_id, actor_rol, ip, full_sync=False) -> dict`; `async estado_de_linea(ctx, *, tenant_id, line_id) -> dict`; `async qr_png(ctx, *, tenant_id, line_id) -> bytes`; `async reiniciar_qr(ctx, *, tenant_id, line_id, actor_user_id, actor_rol, ip) -> dict`; `async pedir_codigo(ctx, *, tenant_id, line_id, telefono, actor_user_id, actor_rol, ip) -> str`; `async pedir_fin(ctx, *, tenant_id, line_id, causa: Literal["pedido_kis","pedido_dueno"], borrar: bool, actor_user_id, actor_rol, ip) -> dict`; `async marcar_restriccion(ctx, *, tenant_id, line_id, hasta: datetime | None, actor_user_id, actor_rol, ip) -> dict`; `async levantar_restriccion(ctx, *, tenant_id, line_id, actor_user_id, actor_rol, ip) -> dict`. Códigos de `VinculoRechazado`: `404 linea_inexistente`, `409 linea_de_baja|sin_consentimiento|vinculo_activo|restriccion_activa|sin_qr|sin_reinicios|sin_vinculo`, `422 telefono_invalido`, `502 config_no_coincide|waha_error|codigo_no_disponible`, `404 qr_no_disponible`, `503 waha_sin_configurar|sin_capacidad` (`sin_vinculo` es 409 tanto en `pedir_fin` como en `marcar_restriccion`; `reiniciar_qr` responde `409 restriccion_activa` sin llamar a WAHA). `aplicar_status` encola un job `aviso_caida` cuando el efecto `caida` sale de un vínculo `vinculado`. Helpers de test `escenario_vinculable(ctx, nombre="Farmacia A") -> dict` (`tenant_id, dueno_id, dueno_email, line_id, consent_id`) y `vincular_de_prueba(ctx, waha, esc, *, working=True) -> dict` (`link_id, session_name`).

- [ ] **Step 1: Helpers de test**

Agregar al final de `tests/radar_tests/helpers.py`:

```python
async def escenario_vinculable(ctx, nombre: str = "Farmacia A") -> dict:
    """Tenant con dueño, una línea y el consentimiento de esa línea (sin vínculo todavía)."""
    t = await crear_tenant_directo(ctx.db, nombre)
    email = f"dueno-{uuid.uuid4().hex[:8]}@cliente.com"
    d = await crear_usuario(ctx.db, t, email, "dueno")
    li = await crear_linea_directa(ctx.db, t, "Local centro")
    c = await crear_consentimiento_directo(ctx.db, t, li, d)
    return {"tenant_id": t, "dueno_id": d, "dueno_email": email, "line_id": li, "consent_id": c}


async def vincular_de_prueba(ctx, waha, esc: dict, *, working: bool = True) -> dict:
    """Vincula la línea del escenario contra el WAHA falso y, si `working`, simula el escaneo."""
    from app.radar.vinculos import aplicar_status, iniciar_vinculo
    from app.radar.waha.sesion import nombre_sesion

    estado = await iniciar_vinculo(ctx, tenant_id=esc["tenant_id"], line_id=esc["line_id"],
                                   actor_user_id=esc["dueno_id"], actor_rol="dueno", ip=None)
    link_id = uuid.UUID(estado["link_id"])
    nombre = nombre_sesion(link_id)
    if working:
        waha.sesiones[nombre]["status"] = "WORKING"
        async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
            await aplicar_status(con, tenant_id=esc["tenant_id"], link_id=link_id, waha_status="WORKING",
                                 origen="webhook", me_id=waha.me_id)
    return {"link_id": link_id, "session_name": nombre}
```

- [ ] **Step 2: Tests (fallan)**

`tests/radar_tests/test_vinculos.py`:

```python
"""
Servicio de vínculos: una sola lógica para la Consola KIS y para el dueño.
Consentimiento obligatorio, admisión, verificación, estados, QR, código,
reinicios, fin pedido y restricción de cuenta.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.radar.constantes import TENANT_KIS
from app.radar.vinculos import (VinculoRechazado, aplicar_status, estado_de_linea, iniciar_vinculo,
                                levantar_restriccion, marcar_restriccion, pedir_codigo, pedir_fin, qr_png,
                                reiniciar_qr)
from app.radar.waha.sesion import nombre_sesion
from app.radar.workers import listar_workers, nombre_clave_lectura

from .helpers import crear_link_directo, crear_linea_directa, escenario_vinculable, vincular_de_prueba
from .waha_falso import PNG


async def _iniciar(ctx, esc, **kw):
    return await iniciar_vinculo(ctx, tenant_id=esc["tenant_id"], line_id=esc["line_id"],
                                 actor_user_id=esc["dueno_id"], actor_rol="dueno", ip="10.0.0.1", **kw)


async def _fila(ctx, esc, sql, *args):
    async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
        return await con.fetchrow(sql, *args)


async def _rechazo(coro) -> VinculoRechazado:
    with pytest.raises(VinculoRechazado) as e:
        await coro
    return e.value


def _actor(esc):
    return dict(tenant_id=esc["tenant_id"], line_id=esc["line_id"], actor_user_id=esc["dueno_id"],
                actor_rol="dueno", ip=None)


async def test_iniciar_crea_la_sesion_de_p3_y_una_clave_de_lectura(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    estado = await _iniciar(ctx_waha, esc)
    link_id = uuid.UUID(estado["link_id"])
    assert estado["estado"] == "esperando_qr" and estado["qr_disponible"] is True
    [cuerpo] = waha.cuerpos
    assert cuerpo["name"] == nombre_sesion(link_id)
    assert cuerpo["config"]["metadata"] == {"tenant_id": str(esc["tenant_id"]), "line_id": str(esc["line_id"]),
                                            "link_id": str(link_id)}
    assert ctx_waha.secretos.get(nombre_clave_lectura(link_id)) == b"valor-secreto-1"
    fila = await _fila(ctx_waha, esc, "SELECT key_id, consent_id, engine FROM links WHERE id = $1", link_id)
    assert (fila["key_id"], fila["consent_id"], fila["engine"]) == ("k1", esc["consent_id"], "NOWEB")
    auditado = await _fila(ctx_waha, esc, "SELECT count(*) AS n FROM access_audit_log "
                                          "WHERE accion = 'vinculo_iniciado' AND objeto_id = $1", link_id)
    evento = await _fila(ctx_waha, esc, "SELECT count(*) AS n FROM product_events WHERE evento = 'vinculo_iniciado'")
    assert auditado["n"] == 1 and evento["n"] == 1


async def test_sin_consentimiento_no_se_crea_ninguna_sesion(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    otra = await crear_linea_directa(ctx_waha.db, esc["tenant_id"], "Sin consentimiento")
    e = await _rechazo(iniciar_vinculo(ctx_waha, tenant_id=esc["tenant_id"], line_id=otra,
                                       actor_user_id=esc["dueno_id"], actor_rol="dueno", ip=None))
    assert (e.status, e.codigo) == (409, "sin_consentimiento")
    assert waha.llamadas == []


async def test_un_vinculo_activo_por_linea(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    await _iniciar(ctx_waha, esc)
    e = await _rechazo(_iniciar(ctx_waha, esc))
    assert (e.status, e.codigo) == (409, "vinculo_activo")


async def test_config_distinta_aborta_sin_qr(ctx_waha, waha):
    waha.mutar_eco = lambda c: c["noweb"].__setitem__("markOnline", True)
    esc = await escenario_vinculable(ctx_waha)
    e = await _rechazo(_iniciar(ctx_waha, esc))
    assert (e.status, e.codigo) == (502, "config_no_coincide")
    assert waha.sesiones == {}
    fila = await _fila(ctx_waha, esc, "SELECT estado FROM links WHERE line_id = $1", esc["line_id"])
    audit = await _fila(ctx_waha, esc, "SELECT detalle::text AS d FROM access_audit_log WHERE accion = 'vinculo_abortado'")
    assert fila["estado"] == "abortado" and "config_no_coincide" in audit["d"]


async def test_sin_capacidad_no_crea_sesion_ni_muestra_qr(ctx_waha, waha):
    async with ctx_waha.db.tenant_tx(TENANT_KIS) as con:
        await con.execute("UPDATE waha_workers SET max_sesiones = 1")
    esc = await escenario_vinculable(ctx_waha)
    e = await _rechazo(_iniciar(ctx_waha, esc))
    assert (e.status, e.codigo) == (503, "sin_capacidad")
    assert waha.llamadas == []
    audit = await _fila(ctx_waha, esc, "SELECT count(*) AS n FROM access_audit_log WHERE accion = 'admision_rechazada'")
    assert audit["n"] == 1


async def test_restriccion_activa_bloquea_volver_a_vincular(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    [w] = await listar_workers(ctx_waha)
    await crear_link_directo(ctx_waha.db, esc["tenant_id"], esc["line_id"], w.id, esc["consent_id"], estado="caido",
                             restriccion_hasta=datetime.now(timezone.utc) + timedelta(days=2))
    e = await _rechazo(_iniciar(ctx_waha, esc))
    assert (e.status, e.codigo) == (409, "restriccion_activa")
    assert waha.llamadas == []


async def test_sin_webhook_configurado_no_vincula(ctx_waha, waha):
    ctx_waha.settings = ctx_waha.settings.model_copy(update={"waha_webhook_url": ""})
    esc = await escenario_vinculable(ctx_waha)
    e = await _rechazo(_iniciar(ctx_waha, esc))
    assert (e.status, e.codigo) == (503, "waha_sin_configurar")


async def test_working_vincula_la_linea_y_guarda_solo_el_sufijo(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    fila = await _fila(ctx_waha, esc, "SELECT estado, numero_sufijo, conectado_at FROM links WHERE id = $1",
                       v["link_id"])
    assert (fila["estado"], fila["numero_sufijo"]) == ("vinculado", "4567") and fila["conectado_at"] is not None
    assert (await _fila(ctx_waha, esc, "SELECT estado FROM lines WHERE id = $1", esc["line_id"]))["estado"] == "vinculada"
    ev = await _fila(ctx_waha, esc, "SELECT waha_status, origen FROM link_status_events WHERE link_id = $1 "
                                    "ORDER BY id DESC LIMIT 1", v["link_id"])
    assert (ev["waha_status"], ev["origen"]) == ("WORKING", "webhook")
    todo = await _fila(ctx_waha, esc, "SELECT row_to_json(l)::text AS t FROM links l WHERE id = $1", v["link_id"])
    assert "5493411234567" not in todo["t"]
    estado = await estado_de_linea(ctx_waha, tenant_id=esc["tenant_id"], line_id=esc["line_id"])
    assert estado["numero"] == "…4567" and estado["estado"] == "vinculado"


async def test_reconectar_reemplaza_al_vinculo_caido(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v1 = await vincular_de_prueba(ctx_waha, waha, esc)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        r = await aplicar_status(con, tenant_id=esc["tenant_id"], link_id=v1["link_id"], waha_status="FAILED",
                                 origen="webhook")
    assert r["estado"] == "caido"
    await vincular_de_prueba(ctx_waha, waha, esc)
    job = await _fila(ctx_waha, esc, "SELECT tipo, causa FROM jobs WHERE link_id = $1", v1["link_id"])
    assert (job["tipo"], job["causa"]) == ("fin_vinculo", "reemplazado")


async def test_vinculo_caido_que_revive_con_otro_activo_se_cierra(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v1 = await vincular_de_prueba(ctx_waha, waha, esc)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await aplicar_status(con, tenant_id=esc["tenant_id"], link_id=v1["link_id"], waha_status="FAILED",
                             origen="webhook")
    await vincular_de_prueba(ctx_waha, waha, esc, working=False)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        r = await aplicar_status(con, tenant_id=esc["tenant_id"], link_id=v1["link_id"], waha_status="WORKING",
                                 origen="webhook", me_id=waha.me_id)
    assert r["estado"] == "caido"
    job = await _fila(ctx_waha, esc, "SELECT causa FROM jobs WHERE link_id = $1", v1["link_id"])
    assert job["causa"] == "reemplazado"


async def test_estado_hace_polling_de_respaldo_sin_eventos(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    estado = await _iniciar(ctx_waha, esc)
    waha.estados[nombre_sesion(uuid.UUID(estado["link_id"]))] = ["SCAN_QR_CODE"]
    estado = await estado_de_linea(ctx_waha, tenant_id=esc["tenant_id"], line_id=esc["line_id"])
    assert estado["waha_status"] == "SCAN_QR_CODE"
    ev = await _fila(ctx_waha, esc, "SELECT origen FROM link_status_events ORDER BY id DESC LIMIT 1")
    assert ev["origen"] == "polling"


async def test_qr_y_hasta_tres_reinicios(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    await _iniciar(ctx_waha, esc)
    assert await qr_png(ctx_waha, tenant_id=esc["tenant_id"], line_id=esc["line_id"]) == PNG
    for i in range(3):
        estado = await reiniciar_qr(ctx_waha, **_actor(esc))
        assert estado["reinicios_restantes"] == 2 - i
    e = await _rechazo(reiniciar_qr(ctx_waha, **_actor(esc)))
    assert (e.status, e.codigo) == (409, "sin_reinicios")


async def test_codigo_de_vinculacion_no_se_guarda(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    await _iniciar(ctx_waha, esc)
    assert await pedir_codigo(ctx_waha, telefono="+5493411234567", **_actor(esc)) == "ABCD-EFGH"
    todo = await _fila(ctx_waha, esc, "SELECT string_agg(detalle::text, '') AS t FROM access_audit_log")
    assert "ABCD" not in todo["t"] and "3411234567" not in todo["t"]
    e = await _rechazo(pedir_codigo(ctx_waha, telefono="+1 555 123 4567", **_actor(esc)))
    assert (e.status, e.codigo) == (422, "telefono_invalido")
    waha.falla_codigo = True
    e = await _rechazo(pedir_codigo(ctx_waha, telefono="+5493411234567", **_actor(esc)))
    assert (e.status, e.codigo) == (502, "codigo_no_disponible")


async def test_pedir_fin_encola_el_job_y_marca_el_borrado(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    r = await pedir_fin(ctx_waha, causa="pedido_dueno", borrar=True, **_actor(esc))
    assert r == {"vinculos": [str(v["link_id"])], "borrado_solicitado": True}
    job = await _fila(ctx_waha, esc, "SELECT tipo, causa FROM jobs WHERE link_id = $1", v["link_id"])
    assert (job["tipo"], job["causa"]) == ("fin_vinculo", "pedido_dueno")
    linea = await _fila(ctx_waha, esc, "SELECT borrado_solicitado_at FROM lines WHERE id = $1", esc["line_id"])
    assert linea["borrado_solicitado_at"] is not None
    otro = await escenario_vinculable(ctx_waha, "Farmacia B")
    e = await _rechazo(pedir_fin(ctx_waha, causa="pedido_dueno", borrar=False, **_actor(otro)))
    assert (e.status, e.codigo) == (409, "sin_vinculo")


async def test_marcar_y_levantar_restriccion(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    await vincular_de_prueba(ctx_waha, waha, esc)
    estado = await marcar_restriccion(ctx_waha, hasta=None, **_actor(esc))
    assert estado["restriccion_activa"] is True
    estado = await levantar_restriccion(ctx_waha, **_actor(esc))
    assert estado["restriccion_activa"] is False
    audit = await _fila(ctx_waha, esc, "SELECT count(*) AS n FROM access_audit_log "
                                       "WHERE accion IN ('restriccion_marcada', 'restriccion_levantada')")
    assert audit["n"] == 2
    otro = await escenario_vinculable(ctx_waha, "Farmacia B")
    e = await _rechazo(marcar_restriccion(ctx_waha, hasta=None, **_actor(otro)))
    assert (e.status, e.codigo) == (409, "sin_vinculo")


async def test_con_restriccion_activa_no_se_reinicia_el_qr(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    await _iniciar(ctx_waha, esc)
    await marcar_restriccion(ctx_waha, hasta=datetime.now(timezone.utc) + timedelta(days=1), **_actor(esc))
    waha.llamadas.clear()
    e = await _rechazo(reiniciar_qr(ctx_waha, **_actor(esc)))
    assert (e.status, e.codigo) == (409, "restriccion_activa")
    assert not any(x.endswith("/restart") for x in waha.llamadas)
    fila = await _fila(ctx_waha, esc, "SELECT qr_reinicios FROM links WHERE line_id = $1", esc["line_id"])
    assert fila["qr_reinicios"] == 0


async def test_caida_de_un_vinculado_encola_el_aviso_y_la_de_uno_que_cierra_no(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        r = await aplicar_status(con, tenant_id=esc["tenant_id"], link_id=v["link_id"], waha_status="FAILED",
                                 origen="webhook")
        await aplicar_status(con, tenant_id=esc["tenant_id"], link_id=v["link_id"], waha_status="FAILED",
                             origen="webhook")                       # caído → caído: no repite el aviso
    assert (r["estado_anterior"], r["estado"]) == ("vinculado", "caido")
    avisos = await _fila(ctx_waha, esc, "SELECT count(*) AS n FROM jobs WHERE tipo = 'aviso_caida' "
                                        "AND link_id = $1", v["link_id"])
    assert avisos["n"] == 1
    otro = await escenario_vinculable(ctx_waha, "Farmacia B")
    w = await vincular_de_prueba(ctx_waha, waha, otro)
    async with ctx_waha.db.tenant_tx(otro["tenant_id"]) as con:
        await con.execute("UPDATE links SET estado = 'cerrando' WHERE id = $1", w["link_id"])
        await aplicar_status(con, tenant_id=otro["tenant_id"], link_id=w["link_id"], waha_status="FAILED",
                             origen="webhook")
        n = await con.fetchval("SELECT count(*) FROM jobs WHERE tipo = 'aviso_caida' AND link_id = $1", w["link_id"])
    assert n == 0
```

- [ ] **Step 3: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_vinculos.py -q
```
Esperado: `ModuleNotFoundError: No module named 'app.radar.vinculos'`.

- [ ] **Step 4: Implementar**

`app/radar/vinculos.py`:

```python
"""
Servicio de vínculos (§3 P3, §3.1 C2/C4, Estados especiales, §6.6). Lo usan
igual la Consola KIS y la pantalla del dueño: una sola máquina de estados.

- Sin una fila en `consents` para la línea no se crea sesión.
- Un vínculo activo por línea; reconectar = vínculo nuevo de la misma línea.
- Con restricción de cuenta activa no se vincula ni se reconecta; el fin sí.
- Sin worker con capacidad no se crea sesión ni se muestra QR.
- Las llamadas a WAHA van fuera de las transacciones.
- QR, códigos, teléfonos y me.id no se guardan ni se loguean.
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Literal, Optional

import asyncpg

from app.radar import auditoria, eventos_producto
from app.radar import jobs as cola
from app.radar.contexto import RadarContexto
from app.radar.telefonos import TelefonoNoSoportado, normalizar_e164
from app.radar.vinculo_estados import (ACTIVOS, MAX_REINICIOS_QR, PATRON_STATUS, SIN_EVENTOS_POLLING, VIVOS,
                                       Transicion, restriccion_activa, sufijo_de, transicion)
from app.radar.waha.cliente import WahaError
from app.radar.waha.gestor import (ConfigNoCoincide, crear_clave_lectura, crear_sesion_verificada,
                                   terminar_sesion)
from app.radar.waha.sesion import cuerpo_sesion, nombre_sesion
from app.radar.workers import (ClaveAdminAusente, SinCapacidad, cliente_de, elegir_worker, leer_worker,
                               nombre_clave_lectura)

logger = logging.getLogger("app.radar.vinculos")

COLUMNAS_LINK = ("id", "line_id", "worker_id", "session_name", "engine", "full_sync", "estado", "waha_status",
                 "qr_reinicios", "numero_sufijo", "conectado_at", "caido_desde", "ultimo_status_at",
                 "restriccion_hasta", "restriccion_sin_fecha", "fin_causa", "desvinculo_confirmado",
                 "observado_hasta", "created_at", "cerrado_at")
_SELECT_LINK = "SELECT " + ", ".join(COLUMNAS_LINK) + " FROM links"


class VinculoRechazado(Exception):
    def __init__(self, status: int, codigo: str) -> None:
        super().__init__(codigo)
        self.status = status
        self.codigo = codigo


async def ultimo_link(con: asyncpg.Connection, line_id: uuid.UUID) -> Optional[dict]:
    fila = await con.fetchrow(_SELECT_LINK + " WHERE line_id = $1 ORDER BY created_at DESC LIMIT 1", line_id)
    return dict(fila) if fila else None


def _iso(v: Optional[datetime]) -> Optional[str]:
    return v.isoformat() if v else None


def estado_json(link: Optional[dict], *, linea_nombre: str, tiene_consentimiento: bool, ahora: datetime) -> dict:
    base: dict[str, Any] = {"linea_nombre": linea_nombre, "tiene_consentimiento": tiene_consentimiento}
    if link is None:
        return {**base, "link_id": None, "estado": "sin_vinculo", "waha_status": None, "qr_disponible": False,
                "qr_vencido": False, "passkey": False, "reinicios_restantes": MAX_REINICIOS_QR, "numero": None,
                "restriccion_activa": False, "restriccion_hasta": None, "caido_desde": None, "conectado_at": None,
                "fin_causa": None, "desvinculo_confirmado": None}
    ws = link["waha_status"]
    en_qr = link["estado"] == "esperando_qr"
    return {
        **base,
        "link_id": str(link["id"]),
        "estado": link["estado"],
        "waha_status": ws,
        "qr_disponible": en_qr and ws in (None, "STARTING", "SCAN_QR_CODE"),
        "qr_vencido": en_qr and ws in ("FAILED", "STOPPED"),
        "passkey": ws == "PASSKEY_REQUIRED",
        "reinicios_restantes": MAX_REINICIOS_QR - link["qr_reinicios"],
        "numero": ("…" + link["numero_sufijo"]) if link["numero_sufijo"] else None,
        "restriccion_activa": restriccion_activa(link["restriccion_hasta"], link["restriccion_sin_fecha"], ahora),
        "restriccion_hasta": _iso(link["restriccion_hasta"]),
        "caido_desde": _iso(link["caido_desde"]),
        "conectado_at": _iso(link["conectado_at"]),
        "fin_causa": link["fin_causa"],
        "desvinculo_confirmado": link["desvinculo_confirmado"],
    }


async def _leer_estado(con: asyncpg.Connection, line_id: uuid.UUID):
    linea = await con.fetchrow("SELECT nombre, estado FROM lines WHERE id = $1", line_id)
    if linea is None:
        raise VinculoRechazado(404, "linea_inexistente")
    consentida = await con.fetchval("SELECT EXISTS (SELECT 1 FROM consents WHERE line_id = $1)", line_id)
    link = await ultimo_link(con, line_id)
    ahora = await con.fetchval("SELECT now()")
    return linea, consentida, link, ahora


async def aplicar_status(con: asyncpg.Connection, *, tenant_id: uuid.UUID, link_id: uuid.UUID, waha_status: str,
                         origen: str, me_id: Optional[str] = None) -> dict:
    """Aplica un estado de WAHA a un vínculo, dentro de la transacción del llamador."""
    if not PATRON_STATUS.match(waha_status or ""):
        raise ValueError("status de WAHA inválido")
    link = await con.fetchrow("SELECT id, line_id, estado, created_at FROM links WHERE id = $1 FOR UPDATE", link_id)
    if link is None:
        return {"aplicado": False, "estado_anterior": None, "estado": None, "efectos": []}
    t = transicion(link["estado"], waha_status)
    if "ignorar" in t.efectos:
        return {"aplicado": False, "estado_anterior": link["estado"], "estado": link["estado"], "efectos": ["ignorar"]}
    if link["estado"] == "caido" and t.estado == "vinculado":
        otro = await con.fetchval("SELECT id FROM links WHERE line_id = $1 AND id <> $2 AND estado = ANY($3::text[])",
                                  link["line_id"], link_id, list(ACTIVOS))
        if otro is not None:
            # La línea ya tiene un vínculo nuevo: el viejo que revive se cierra (§6.6).
            await con.execute("UPDATE links SET fin_causa = COALESCE(fin_causa, 'reemplazado') WHERE id = $1", link_id)
            await cola.encolar(con, tipo="fin_vinculo", link_id=link_id, causa="reemplazado")
            t = Transicion("caido", frozenset())
    efectos = t.efectos
    conectado = "conectado" in efectos
    await con.execute(
        """
        UPDATE links SET estado = $2, waha_status = $3, ultimo_status_at = now(), updated_at = now(),
               conectado_at = CASE WHEN $4 THEN COALESCE(conectado_at, now()) ELSE conectado_at END,
               caido_desde = CASE WHEN $4 THEN NULL WHEN $5 THEN COALESCE(caido_desde, now()) ELSE caido_desde END,
               numero_sufijo = COALESCE($6, numero_sufijo)
         WHERE id = $1
        """,
        link_id, t.estado, waha_status, conectado, "caida" in efectos,
        sufijo_de(me_id) if waha_status == "WORKING" else None)
    await con.execute("INSERT INTO link_status_events (link_id, waha_status, origen, estado_link) "
                      "VALUES ($1, $2, $3, $4)", link_id, waha_status, origen, t.estado)
    if conectado:
        await con.execute("UPDATE lines SET estado = 'vinculada', updated_at = now() "
                          "WHERE id = $1 AND estado <> 'de_baja'", link["line_id"])
        segundos = await con.fetchval("SELECT extract(epoch FROM now() - $1::timestamptz)::float8", link["created_at"])
        await eventos_producto.registrar_evento(con, tenant_id=tenant_id, evento="vinculo_working",
                                                line_id=link["line_id"], objeto_id=link_id,
                                                valores={"segundos": round(segundos, 1)})
    if "reemplazar_anteriores" in efectos:
        for anterior in await con.fetch("SELECT id FROM links WHERE line_id = $1 AND id <> $2 AND estado = 'caido'",
                                        link["line_id"], link_id):
            await con.execute("UPDATE links SET fin_causa = COALESCE(fin_causa, 'reemplazado') WHERE id = $1",
                              anterior["id"])
            await cola.encolar(con, tipo="fin_vinculo", link_id=anterior["id"], causa="reemplazado")
    if "abandonar" in efectos:
        await con.execute("UPDATE links SET fin_causa = COALESCE(fin_causa, 'qr_abandonado') WHERE id = $1", link_id)
        await cola.encolar(con, tipo="fin_vinculo", link_id=link_id, causa="qr_abandonado")
    if "caida" in efectos and link["estado"] == "vinculado":
        # Estados especiales, "Vínculo caído": banner con fecha y email al dueño.
        # El mail sale fuera de esta transacción, desde el job aviso_caida
        # (fin_vinculo.avisar_caida). Un vínculo en `cerrando` nunca llega acá:
        # la máquina de estados lo ignora.
        await cola.encolar(con, tipo="aviso_caida", link_id=link_id)
    return {"aplicado": True, "estado_anterior": link["estado"], "estado": t.estado, "efectos": sorted(efectos)}


async def iniciar_vinculo(ctx: RadarContexto, *, tenant_id: uuid.UUID, line_id: uuid.UUID,
                          actor_user_id: Optional[uuid.UUID], actor_rol: str, ip: Optional[str],
                          full_sync: bool = False) -> dict:
    rs = ctx.settings
    if not rs.waha_webhook_url or len(rs.waha_webhook_hmac_key) < 32:
        raise VinculoRechazado(503, "waha_sin_configurar")
    async with ctx.db.tenant_tx(tenant_id) as con:
        linea = await con.fetchrow("SELECT estado FROM lines WHERE id = $1", line_id)
        if linea is None:
            raise VinculoRechazado(404, "linea_inexistente")
        if linea["estado"] == "de_baja":
            raise VinculoRechazado(409, "linea_de_baja")
        consent_id = await con.fetchval(
            "SELECT id FROM consents WHERE line_id = $1 ORDER BY created_at DESC LIMIT 1", line_id)
        if consent_id is None:
            raise VinculoRechazado(409, "sin_consentimiento")
        if await con.fetchval("SELECT EXISTS (SELECT 1 FROM links WHERE line_id = $1 AND estado = ANY($2::text[]))",
                              line_id, list(ACTIVOS)):
            raise VinculoRechazado(409, "vinculo_activo")
        if await con.fetchval("SELECT EXISTS (SELECT 1 FROM links WHERE line_id = $1 "
                              "AND (restriccion_sin_fecha OR restriccion_hasta > now()))", line_id):
            raise VinculoRechazado(409, "restriccion_activa")
    try:
        worker = await elegir_worker(ctx)
    except SinCapacidad:
        async with ctx.db.tenant_tx(tenant_id) as con:
            await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=actor_user_id, actor_rol=actor_rol,
                                      accion="admision_rechazada", tipo_objeto="line", objeto_id=line_id, ip=ip,
                                      detalle={"motivo": "sin_capacidad"})
        raise VinculoRechazado(503, "sin_capacidad")

    full_sync = bool(full_sync) and worker.engine == "NOWEB"
    link_id = uuid.uuid4()
    nombre = nombre_sesion(link_id)
    try:
        async with ctx.db.tenant_tx(tenant_id) as con:
            await con.execute(
                "INSERT INTO links (id, line_id, worker_id, consent_id, session_name, engine, full_sync, creado_por) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                link_id, line_id, worker.id, consent_id, nombre, worker.engine, full_sync, actor_user_id)
    except asyncpg.UniqueViolationError:
        raise VinculoRechazado(409, "vinculo_activo")

    cuerpo = cuerpo_sesion(link_id=link_id, tenant_id=tenant_id, line_id=line_id, engine=worker.engine,
                           webhook_url=rs.waha_webhook_url, hmac_key=rs.waha_webhook_hmac_key, full_sync=full_sync)
    motivo: Optional[str] = None
    key_id = valor = ""
    try:
        async with cliente_de(ctx, worker) as cli:
            try:
                await crear_sesion_verificada(cli, cuerpo)
                key_id, valor = await crear_clave_lectura(cli, nombre)
            except ConfigNoCoincide as e:
                motivo = "config_no_coincide"
                logger.warning("ALERTA vínculo %s abortado: WAHA no guardó %s", link_id, ", ".join(e.problemas))
            except WahaError as e:
                motivo = "waha_error"
                logger.warning("vínculo %s abortado: WAHA falló al crear (%s)", link_id, type(e).__name__)
                try:
                    await terminar_sesion(cli, nombre, intentar_start=False)
                except WahaError:
                    pass
    except ClaveAdminAusente:
        motivo = "waha_error"
        logger.warning("vínculo %s abortado: falta la clave admin del worker", link_id)
    if motivo:
        async with ctx.db.tenant_tx(tenant_id) as con:
            await con.execute("UPDATE links SET estado = 'abortado', cerrado_at = now(), updated_at = now() "
                              "WHERE id = $1", link_id)
            await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=actor_user_id, actor_rol=actor_rol,
                                      accion="vinculo_abortado", tipo_objeto="link", objeto_id=link_id, ip=ip,
                                      detalle={"motivo": motivo})
        raise VinculoRechazado(502, motivo)

    ctx.secretos.set(nombre_clave_lectura(link_id), valor.encode())
    async with ctx.db.tenant_tx(tenant_id) as con:
        await con.execute("UPDATE links SET key_id = $2, updated_at = now() WHERE id = $1", link_id, key_id)
        await con.execute("UPDATE links SET estado = 'esperando_qr' WHERE id = $1 AND estado = 'creando'", link_id)
        await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=actor_user_id, actor_rol=actor_rol,
                                  accion="vinculo_iniciado", tipo_objeto="link", objeto_id=link_id, ip=ip)
        await eventos_producto.registrar_evento(con, tenant_id=tenant_id, evento="vinculo_iniciado", line_id=line_id,
                                                user_id=actor_user_id, objeto_id=link_id)
        linea, consentida, link, ahora = await _leer_estado(con, line_id)
    return estado_json(link, linea_nombre=linea["nombre"], tiene_consentimiento=consentida, ahora=ahora)


async def estado_de_linea(ctx: RadarContexto, *, tenant_id: uuid.UUID, line_id: uuid.UUID) -> dict:
    async with ctx.db.tenant_tx(tenant_id) as con:
        linea, consentida, link, ahora = await _leer_estado(con, line_id)
    sin_eventos = link is not None and (link["ultimo_status_at"] is None
                                        or ahora - link["ultimo_status_at"] > SIN_EVENTOS_POLLING)
    if link is not None and link["estado"] == "esperando_qr" and sin_eventos:
        # P3: "sin eventos por 20 s → polling de respaldo", hecho por el servidor.
        try:
            worker = await leer_worker(ctx, link["worker_id"])
            async with cliente_de(ctx, worker) as cli:
                sesion = await cli.leer_sesion(link["session_name"])
            status = sesion["status"] if sesion else "AUSENTE"
            if isinstance(status, str) and PATRON_STATUS.match(status):
                async with ctx.db.tenant_tx(tenant_id) as con:
                    await aplicar_status(con, tenant_id=tenant_id, link_id=link["id"], waha_status=status,
                                         origen="polling", me_id=sesion["me_id"] if sesion else None)
                    linea, consentida, link, ahora = await _leer_estado(con, line_id)
        except (WahaError, ClaveAdminAusente, LookupError) as e:
            logger.warning("vínculo %s: polling de respaldo falló (%s)", link["id"], type(e).__name__)
    return estado_json(link, linea_nombre=linea["nombre"], tiene_consentimiento=consentida, ahora=ahora)


async def _link_en_qr(ctx: RadarContexto, tenant_id: uuid.UUID, line_id: uuid.UUID) -> dict:
    async with ctx.db.tenant_tx(tenant_id) as con:
        _, _, link, _ = await _leer_estado(con, line_id)
    if link is None or link["estado"] != "esperando_qr":
        raise VinculoRechazado(409, "sin_qr")
    return link


async def qr_png(ctx: RadarContexto, *, tenant_id: uuid.UUID, line_id: uuid.UUID) -> bytes:
    link = await _link_en_qr(ctx, tenant_id, line_id)
    try:
        worker = await leer_worker(ctx, link["worker_id"])
        async with cliente_de(ctx, worker) as cli:
            png = await cli.qr_png(link["session_name"])
    except (WahaError, ClaveAdminAusente, LookupError):
        png = None
    if png is None:
        raise VinculoRechazado(404, "qr_no_disponible")
    return png


async def reiniciar_qr(ctx: RadarContexto, *, tenant_id: uuid.UUID, line_id: uuid.UUID,
                       actor_user_id: Optional[uuid.UUID], actor_rol: str, ip: Optional[str]) -> dict:
    link = await _link_en_qr(ctx, tenant_id, line_id)
    if link["qr_reinicios"] >= MAX_REINICIOS_QR:
        raise VinculoRechazado(409, "sin_reinicios")
    # Estados especiales, Restricción: mientras esté activa el sistema no
    # reinicia la sesión (se puede marcar con el vínculo en esperando_qr).
    async with ctx.db.tenant_tx(tenant_id) as con:
        ahora = await con.fetchval("SELECT now()")
    if restriccion_activa(link["restriccion_hasta"], link["restriccion_sin_fecha"], ahora):
        raise VinculoRechazado(409, "restriccion_activa")
    try:
        worker = await leer_worker(ctx, link["worker_id"])
        async with cliente_de(ctx, worker) as cli:
            await cli.reiniciar_sesion(link["session_name"])
    except (WahaError, ClaveAdminAusente, LookupError):
        raise VinculoRechazado(502, "waha_error")
    async with ctx.db.tenant_tx(tenant_id) as con:
        await con.execute("UPDATE links SET qr_reinicios = qr_reinicios + 1, waha_status = NULL, "
                          "ultimo_status_at = now(), updated_at = now() WHERE id = $1", link["id"])
        await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=actor_user_id, actor_rol=actor_rol,
                                  accion="qr_reiniciado", tipo_objeto="link", objeto_id=link["id"], ip=ip)
    return await estado_de_linea(ctx, tenant_id=tenant_id, line_id=line_id)


async def pedir_codigo(ctx: RadarContexto, *, tenant_id: uuid.UUID, line_id: uuid.UUID, telefono: str,
                       actor_user_id: Optional[uuid.UUID], actor_rol: str, ip: Optional[str]) -> str:
    """Código de vinculación (P3). Ni el teléfono ni el código se guardan o se loguean."""
    try:
        e164 = normalizar_e164(telefono)
    except TelefonoNoSoportado:
        raise VinculoRechazado(422, "telefono_invalido")
    link = await _link_en_qr(ctx, tenant_id, line_id)
    try:
        worker = await leer_worker(ctx, link["worker_id"])
        async with cliente_de(ctx, worker) as cli:
            codigo = await cli.pedir_codigo(link["session_name"], e164[1:])
    except (WahaError, ClaveAdminAusente, LookupError):
        codigo = None
    if codigo is None:
        raise VinculoRechazado(502, "codigo_no_disponible")
    async with ctx.db.tenant_tx(tenant_id) as con:
        await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=actor_user_id, actor_rol=actor_rol,
                                  accion="codigo_solicitado", tipo_objeto="link", objeto_id=link["id"], ip=ip)
    return codigo


async def pedir_fin(ctx: RadarContexto, *, tenant_id: uuid.UUID, line_id: uuid.UUID,
                    causa: Literal["pedido_kis", "pedido_dueno"], borrar: bool, actor_user_id: Optional[uuid.UUID],
                    actor_rol: str, ip: Optional[str]) -> dict:
    """Desconectar (o desconectar y borrar). La parte de WAHA la hace el job de
    fin; el borrado de nuestra base lo toma el tramo 6 desde borrado_solicitado_at."""
    if causa not in ("pedido_kis", "pedido_dueno"):
        raise ValueError("causa de fin pedido inválida")
    async with ctx.db.tenant_tx(tenant_id) as con:
        if not await con.fetchval("SELECT EXISTS (SELECT 1 FROM lines WHERE id = $1)", line_id):
            raise VinculoRechazado(404, "linea_inexistente")
        vivos = await con.fetch("SELECT id FROM links WHERE line_id = $1 AND estado = ANY($2::text[]) "
                                "ORDER BY created_at", line_id, list(VIVOS))
        if not vivos and not borrar:
            raise VinculoRechazado(409, "sin_vinculo")
        for fila in vivos:
            await con.execute("UPDATE links SET fin_causa = COALESCE(fin_causa, $2), updated_at = now() WHERE id = $1",
                              fila["id"], causa)
            await cola.encolar(con, tipo="fin_vinculo", link_id=fila["id"], causa=causa)
        if borrar:
            await con.execute("UPDATE lines SET borrado_solicitado_at = COALESCE(borrado_solicitado_at, now()), "
                              "updated_at = now() WHERE id = $1", line_id)
        await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=actor_user_id, actor_rol=actor_rol,
                                  accion="borrado_pedido" if borrar else "desconexion_pedida", tipo_objeto="line",
                                  objeto_id=line_id, ip=ip, detalle={"causa": causa})
    return {"vinculos": [str(f["id"]) for f in vivos], "borrado_solicitado": borrar}


async def marcar_restriccion(ctx: RadarContexto, *, tenant_id: uuid.UUID, line_id: uuid.UUID,
                             hasta: Optional[datetime], actor_user_id: Optional[uuid.UUID], actor_rol: str,
                             ip: Optional[str]) -> dict:
    """Restricción de cuenta (Estados especiales). Sin fecha: la levanta un admin de KIS."""
    async with ctx.db.tenant_tx(tenant_id) as con:
        _, _, link, _ = await _leer_estado(con, line_id)
        if link is None:
            raise VinculoRechazado(409, "sin_vinculo")   # 409 como en pedir_fin: la línea existe
        await con.execute("UPDATE links SET restriccion_hasta = $2, restriccion_sin_fecha = ($2::timestamptz IS NULL), "
                          "updated_at = now() WHERE id = $1", link["id"], hasta)
        await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=actor_user_id, actor_rol=actor_rol,
                                  accion="restriccion_marcada", tipo_objeto="link", objeto_id=link["id"], ip=ip)
    return await estado_de_linea(ctx, tenant_id=tenant_id, line_id=line_id)


async def levantar_restriccion(ctx: RadarContexto, *, tenant_id: uuid.UUID, line_id: uuid.UUID,
                               actor_user_id: Optional[uuid.UUID], actor_rol: str, ip: Optional[str]) -> dict:
    async with ctx.db.tenant_tx(tenant_id) as con:
        await _leer_estado(con, line_id)
        await con.execute("UPDATE links SET restriccion_hasta = NULL, restriccion_sin_fecha = FALSE, "
                          "updated_at = now() WHERE line_id = $1", line_id)
        await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=actor_user_id, actor_rol=actor_rol,
                                  accion="restriccion_levantada", tipo_objeto="line", objeto_id=line_id, ip=ip)
    return await estado_de_linea(ctx, tenant_id=tenant_id, line_id=line_id)
```

- [ ] **Step 5: Correr**

```bash
python -m pytest tests/radar_tests/test_vinculos.py -q
```
Esperado: `17 passed`.

- [ ] **Step 6: Commit**

```bash
git add app/radar/vinculos.py tests/radar_tests/helpers.py tests/radar_tests/test_vinculos.py
git commit -m "Radar tramo 2: servicio de vinculos con consentimiento, admision, QR, codigo y restriccion" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Job de fin de vínculo

**Files:**
- Create: `app/radar/fin_vinculo.py`
- Test: `tests/radar_tests/test_fin_vinculo.py`

**Interfaces:**
- Consumes: `Job`, `jobs.reclamar`, `jobs.encolar`, `terminar_sesion`, `leer_worker`, `cliente_de`, `nombre_clave_lectura`, `restriccion_activa`, `ACTIVOS`, `auditoria.registrar`, `eventos_producto.registrar_evento`, `Email`, `RadarContexto.mailer`, `pedir_fin`, `marcar_restriccion`, `aplicar_status`.
- Produces: `FinIncompleto(RuntimeError)`; `ESPERA_START_S = 180.0`; `INTERVALO_START_S = 5.0`; `SIN_AVISO = ("reemplazado", "qr_abandonado")`; `ASUNTO_AVISO`, `TEXTO_AVISO`; `async ejecutar(ctx, job: Job, *, espera_start_s: float = ESPERA_START_S, intervalo_s: float = INTERVALO_START_S) -> Literal["hecho"]` (lanza `FinIncompleto` si la sesión no quedó borrada o quedan claves; un mail que falla no lo hace fallar y el evento `vinculo_cerrado` lleva `aviso_enviado` 0/1); `HORA_ARGENTINA`, `ASUNTO_CAIDA`, `TEXTO_CAIDA`; `async avisar_caida(ctx, job: Job) -> Literal["hecho"]` (handler del job `aviso_caida`: email al dueño con la fecha DD/MM de `caido_desde`, solo si el vínculo sigue `caido`).

- [ ] **Step 1: Tests (fallan)**

`tests/radar_tests/test_fin_vinculo.py`:

```python
"""
Fin de vínculo (§6.3 punto 6): cerrando → start si hace falta y no hay
restricción → un único DELETE → claves de esa sesión → verificación 404 →
clave local borrada → registro → aviso al dueño. Nunca logout.
"""
import json

import pytest

from app.radar import fin_vinculo
from app.radar import jobs as cola
from app.radar.fin_vinculo import FinIncompleto
from app.radar.vinculos import aplicar_status, marcar_restriccion, pedir_fin
from app.radar.workers import nombre_clave_lectura

from .helpers import MailerQueFalla, escenario_vinculable, vincular_de_prueba


async def _job_fin(ctx):
    return next(j for j in await cola.reclamar(ctx.db) if j.tipo == "fin_vinculo")


async def _fila(ctx, esc, sql, *args):
    async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
        return await con.fetchrow(sql, *args)


async def _pedir(ctx, esc, causa="pedido_dueno"):
    await pedir_fin(ctx, tenant_id=esc["tenant_id"], line_id=esc["line_id"], causa=causa, borrar=False,
                    actor_user_id=esc["dueno_id"], actor_rol="dueno", ip=None)


async def test_fin_completo(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    await _pedir(ctx_waha, esc)
    waha.llamadas.clear()
    assert await fin_vinculo.ejecutar(ctx_waha, await _job_fin(ctx_waha)) == "hecho"
    n = v["session_name"]
    assert waha.llamadas.count(f"DELETE /api/sessions/{n}") == 1
    assert not any("logout" in x for x in waha.llamadas)
    assert waha.sesiones == {} and waha.claves == []
    assert ctx_waha.secretos.get(nombre_clave_lectura(v["link_id"])) is None
    link = await _fila(ctx_waha, esc, "SELECT estado, fin_causa, desvinculo_confirmado, fin_resultado, cerrado_at "
                                      "FROM links WHERE id = $1", v["link_id"])
    assert (link["estado"], link["fin_causa"], link["desvinculo_confirmado"]) == ("cerrado", "pedido_dueno", True)
    assert json.loads(link["fin_resultado"])["ok"] is True and link["cerrado_at"] is not None
    linea = await _fila(ctx_waha, esc, "SELECT estado FROM lines WHERE id = $1", esc["line_id"])
    assert linea["estado"] == "sin_vinculo"
    mail = ctx_waha.mailer.enviados[-1]
    assert mail.para == esc["dueno_email"] and "Dispositivos vinculados" in mail.texto
    audit = await _fila(ctx_waha, esc, "SELECT detalle::text AS d FROM access_audit_log WHERE accion = 'vinculo_cerrado'")
    assert "pedido_dueno" in audit["d"]


async def test_sesion_detenida_intenta_start_antes_del_delete(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    waha.sesiones[v["session_name"]]["status"] = "STOPPED"
    await _pedir(ctx_waha, esc)
    await fin_vinculo.ejecutar(ctx_waha, await _job_fin(ctx_waha), espera_start_s=1, intervalo_s=0)
    assert f"POST /api/sessions/{v['session_name']}/start" in waha.llamadas
    link = await _fila(ctx_waha, esc, "SELECT desvinculo_confirmado FROM links WHERE id = $1", v["link_id"])
    assert link["desvinculo_confirmado"] is True


async def test_con_restriccion_no_hay_start_pero_se_borra(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    await marcar_restriccion(ctx_waha, tenant_id=esc["tenant_id"], line_id=esc["line_id"], hasta=None,
                             actor_user_id=None, actor_rol="admin", ip=None)
    waha.sesiones[v["session_name"]]["status"] = "FAILED"
    await _pedir(ctx_waha, esc, causa="pedido_kis")
    await fin_vinculo.ejecutar(ctx_waha, await _job_fin(ctx_waha))
    assert not any(x.endswith("/start") for x in waha.llamadas)
    link = await _fila(ctx_waha, esc, "SELECT estado, desvinculo_confirmado FROM links WHERE id = $1", v["link_id"])
    assert (link["estado"], link["desvinculo_confirmado"]) == ("cerrado", False)


async def test_fallo_en_claves_deja_cerrando_y_el_reintento_completa(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    await _pedir(ctx_waha, esc)
    job = await _job_fin(ctx_waha)
    waha.falla_claves = True
    with pytest.raises(FinIncompleto):
        await fin_vinculo.ejecutar(ctx_waha, job)
    assert waha.llamadas.count(f"DELETE /api/sessions/{v['session_name']}") == 1
    link = await _fila(ctx_waha, esc, "SELECT estado, fin_resultado FROM links WHERE id = $1", v["link_id"])
    assert link["estado"] == "cerrando"
    assert json.loads(link["fin_resultado"])["error_claves"] == "WahaHttpError"
    waha.falla_claves = False
    assert await fin_vinculo.ejecutar(ctx_waha, job) == "hecho"
    link = await _fila(ctx_waha, esc, "SELECT estado FROM links WHERE id = $1", v["link_id"])
    assert link["estado"] == "cerrado" and waha.claves == []


async def test_es_idempotente(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    await vincular_de_prueba(ctx_waha, waha, esc)
    await _pedir(ctx_waha, esc)
    job = await _job_fin(ctx_waha)
    await fin_vinculo.ejecutar(ctx_waha, job)
    waha.llamadas.clear()
    assert await fin_vinculo.ejecutar(ctx_waha, job) == "hecho"
    assert waha.llamadas == []


async def test_reemplazo_no_manda_email(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await cola.encolar(con, tipo="fin_vinculo", link_id=v["link_id"], causa="reemplazado")
    antes = len(ctx_waha.mailer.enviados)
    await fin_vinculo.ejecutar(ctx_waha, await _job_fin(ctx_waha))
    assert len(ctx_waha.mailer.enviados) == antes
    link = await _fila(ctx_waha, esc, "SELECT estado, fin_causa FROM links WHERE id = $1", v["link_id"])
    assert (link["estado"], link["fin_causa"]) == ("cerrado", "reemplazado")


async def test_status_de_un_vinculo_que_cierra_se_ignora(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await con.execute("UPDATE links SET estado = 'cerrando' WHERE id = $1", v["link_id"])
        r = await aplicar_status(con, tenant_id=esc["tenant_id"], link_id=v["link_id"], waha_status="SCAN_QR_CODE",
                                 origen="webhook")
    assert r["aplicado"] is False
    link = await _fila(ctx_waha, esc, "SELECT estado FROM links WHERE id = $1", v["link_id"])
    assert link["estado"] == "cerrando"


async def test_mail_que_falla_no_rompe_el_fin(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    await _pedir(ctx_waha, esc)
    ctx_waha.mailer = MailerQueFalla()
    assert await fin_vinculo.ejecutar(ctx_waha, await _job_fin(ctx_waha)) == "hecho"
    link = await _fila(ctx_waha, esc, "SELECT estado FROM links WHERE id = $1", v["link_id"])
    assert link["estado"] == "cerrado"
    ev = await _fila(ctx_waha, esc, "SELECT valores::text AS v FROM product_events WHERE evento = 'vinculo_cerrado'")
    assert json.loads(ev["v"])["aviso_enviado"] == 0


async def _caer(ctx, esc, v):
    async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
        await aplicar_status(con, tenant_id=esc["tenant_id"], link_id=v["link_id"], waha_status="FAILED",
                             origen="webhook")
        await con.execute("UPDATE links SET caido_desde = '2026-09-20 15:00:00+00' WHERE id = $1", v["link_id"])
    return next(j for j in await cola.reclamar(ctx.db) if j.tipo == "aviso_caida")


async def test_aviso_de_caida_con_la_fecha(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    job = await _caer(ctx_waha, esc, v)
    antes = len(ctx_waha.mailer.enviados)
    assert await fin_vinculo.avisar_caida(ctx_waha, job) == "hecho"
    [mail] = ctx_waha.mailer.enviados[antes:]
    assert mail.para == esc["dueno_email"] and "el 20/09" in mail.texto and "Reconectar" in mail.texto
    assert "4567" not in mail.texto


async def test_aviso_de_caida_no_sale_si_ya_reconecto_ni_rompe_si_falla_el_mail(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    job = await _caer(ctx_waha, esc, v)
    ctx_waha.mailer = MailerQueFalla()
    assert await fin_vinculo.avisar_caida(ctx_waha, job) == "hecho"      # falla el proveedor: no relanza
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await aplicar_status(con, tenant_id=esc["tenant_id"], link_id=v["link_id"], waha_status="WORKING",
                             origen="webhook", me_id=waha.me_id)
    ctx_waha.mailer = MailerQueFalla(falla_si="nunca")
    assert await fin_vinculo.avisar_caida(ctx_waha, job) == "hecho"
    assert ctx_waha.mailer.enviados == []
```

- [ ] **Step 2: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_fin_vinculo.py -q
```
Esperado: `ImportError: cannot import name 'fin_vinculo' from 'app.radar'`.

- [ ] **Step 3: Implementar**

`app/radar/fin_vinculo.py`:

```python
"""
Job de fin de vínculo (§6.3 punto 6). Idempotente y auditado; lo disparan
las cinco causas de §2.2 (duración, pedido, caída > 72 h, migración, baja),
más el reemplazo por un vínculo nuevo y el QR abandonado.

1. Marca el vínculo `cerrando`: desde ahí el receptor ignora sus session.status.
2. Si la sesión está STOPPED/FAILED y NO hay restricción activa, intenta start
   y espera hasta 3 min (el desvínculo del lado de WhatsApp solo ocurre con la
   sesión WORKING).
3. Un único DELETE /api/sessions/{name}. Nunca logout.
4. Borra las claves de esa sesión y verifica 404 y cero claves.
5. Borra la clave de lectura local, registra el resultado y avisa al dueño.

Un paso fallido no bloquea los siguientes: si al final la sesión no quedó
borrada o quedan claves, el vínculo sigue `cerrando` y el job se reintenta.
Nuestra base no se toca (tramo 6).
"""

import json
import logging
from datetime import timedelta, timezone
from typing import Literal

from app.radar import auditoria, eventos_producto
from app.radar.contexto import RadarContexto
from app.radar.jobs import Job
from app.radar.mailer import Email
from app.radar.vinculo_estados import ACTIVOS, restriccion_activa
from app.radar.waha.gestor import terminar_sesion
from app.radar.workers import cliente_de, leer_worker, nombre_clave_lectura

logger = logging.getLogger("app.radar.fin_vinculo")

ESPERA_START_S = 180.0
INTERVALO_START_S = 5.0
SIN_AVISO = ("reemplazado", "qr_abandonado")
ASUNTO_AVISO = "Tu WhatsApp se desconectó de Radar"
TEXTO_AVISO = (
    "Desconectamos una línea de tu cuenta de Radar y borramos la sesión de conexión y sus credenciales.\n\n"
    "Revisá WhatsApp → Dispositivos vinculados. Si todavía ves este dispositivo, quitalo desde ahí. "
    "Las credenciales ya fueron destruidas y no se puede volver a usar.\n\n"
    "Conservamos el tablero y las conversaciones ya importadas según la configuración de la línea. "
    "Para borrarlas, usá «Desconectar y borrar todo».\n\n{url}\n"
)
# Aviso de caída (Estados especiales): solo la fecha, ningún dato de conversación.
HORA_ARGENTINA = timezone(timedelta(hours=-3))   # sin horario de verano; no depende de tzdata
ASUNTO_CAIDA = "Tu WhatsApp se desconectó de Radar"
TEXTO_CAIDA = (
    "Tu WhatsApp se desconectó de Radar el {fecha}.\n\n"
    "Entrá a Radar y tocá «Reconectar» para volver a vincularlo. Si no se reconecta en 72 horas, "
    "cerramos el vínculo y borramos la sesión de conexión.\n\n{url}\n"
)


class FinIncompleto(RuntimeError):
    pass


async def ejecutar(ctx: RadarContexto, job: Job, *, espera_start_s: float = ESPERA_START_S,
                   intervalo_s: float = INTERVALO_START_S) -> Literal["hecho"]:
    async with ctx.db.tenant_tx(job.tenant_id) as con:
        link = await con.fetchrow(
            "SELECT id, line_id, worker_id, session_name, estado, restriccion_hasta, restriccion_sin_fecha, fin_causa "
            "FROM links WHERE id = $1 FOR UPDATE", job.link_id)
        if link is None or link["estado"] in ("cerrado", "abortado"):
            return "hecho"
        causa = link["fin_causa"] or job.causa or "pedido_kis"
        await con.execute("UPDATE links SET estado = 'cerrando', fin_causa = $2, updated_at = now() WHERE id = $1",
                          link["id"], causa)
        ahora = await con.fetchval("SELECT now()")

    restringida = restriccion_activa(link["restriccion_hasta"], link["restriccion_sin_fecha"], ahora)
    worker = await leer_worker(ctx, link["worker_id"])
    async with cliente_de(ctx, worker) as cli:
        res = await terminar_sesion(cli, link["session_name"], intentar_start=not restringida,
                                    espera_start_s=espera_start_s, intervalo_s=intervalo_s)
    ctx.secretos.delete(nombre_clave_lectura(link["id"]))
    res["causa"] = causa
    ok = bool(res["ok"])

    duenos: list[str] = []
    async with ctx.db.tenant_tx(job.tenant_id) as con:
        await con.execute(
            """
            UPDATE links SET fin_resultado = $2::jsonb,
                   desvinculo_confirmado = COALESCE(desvinculo_confirmado, FALSE) OR $3,
                   estado = CASE WHEN $4 THEN 'cerrado' ELSE estado END,
                   cerrado_at = CASE WHEN $4 THEN now() ELSE cerrado_at END,
                   updated_at = now()
             WHERE id = $1
            """, link["id"], json.dumps(res), bool(res["desvinculo_confirmado"]), ok)
        if ok:
            activos = await con.fetchval("SELECT count(*) FROM links WHERE line_id = $1 AND estado = ANY($2::text[])",
                                         link["line_id"], list(ACTIVOS))
            if activos == 0:
                await con.execute("UPDATE lines SET estado = 'sin_vinculo', updated_at = now() "
                                  "WHERE id = $1 AND estado = 'vinculada'", link["line_id"])
            await auditoria.registrar(con, tenant_id=job.tenant_id, actor_user_id=None, actor_rol="sistema",
                                      accion="vinculo_cerrado", tipo_objeto="link", objeto_id=link["id"],
                                      detalle={"causa": causa, "ok": True})
            if causa not in SIN_AVISO:
                duenos = await _emails_duenos(con)
    if not ok:
        logger.warning("ALERTA fin de vínculo %s incompleto: sesión borrada=%s, error de claves=%s",
                       link["id"], res["sesion_borrada"], res["error_claves"])
        raise FinIncompleto("fin de vínculo incompleto; se reintenta")
    # El vínculo ya está cerrado: un mail que falla no convierte el job en falla
    # (el reintento no lo reenviaría, porque encuentra el vínculo cerrado).
    url = ctx.settings.public_base_url.rstrip("/") + "/radar/"
    enviados = await _avisar(ctx, duenos, ASUNTO_AVISO, TEXTO_AVISO.format(url=url), link["id"])
    async with ctx.db.tenant_tx(job.tenant_id) as con:
        await eventos_producto.registrar_evento(
            con, tenant_id=job.tenant_id, evento="vinculo_cerrado", line_id=link["line_id"], objeto_id=link["id"],
            valores={"desvinculo_confirmado": 1 if res["desvinculo_confirmado"] else 0,
                     "aviso_enviado": 1 if enviados else 0})
    return "hecho"


async def _emails_duenos(con) -> list[str]:
    return [f["email"] for f in await con.fetch(
        "SELECT u.email FROM memberships m JOIN users u ON u.id = m.user_id WHERE m.rol = 'dueno'")]


async def _avisar(ctx: RadarContexto, destinatarios: list[str], asunto: str, texto: str, link_id) -> int:
    """Manda el aviso a cada destinatario sin relanzar (como enviar_link_seguro
    del tramo 1). Devuelve cuántos salieron. Solo se loguea el tipo de error."""
    enviados = 0
    for email in destinatarios:
        try:
            await ctx.mailer.enviar(Email(para=email, asunto=asunto, texto=texto, huella="sin-token"))
            enviados += 1
        except Exception as e:
            logger.warning("aviso del vínculo %s no enviado: %s", link_id, type(e).__name__)
    return enviados


async def avisar_caida(ctx: RadarContexto, job: Job) -> Literal["hecho"]:
    """Job aviso_caida (Estados especiales, "Vínculo caído"): email al dueño con
    la fecha de la caída. Si el vínculo ya se recuperó o está cerrando, no avisa."""
    async with ctx.db.tenant_tx(job.tenant_id) as con:
        link = await con.fetchrow("SELECT id, estado, caido_desde FROM links WHERE id = $1", job.link_id)
        if link is None or link["estado"] != "caido" or link["caido_desde"] is None:
            return "hecho"
        duenos = await _emails_duenos(con)
    fecha = link["caido_desde"].astimezone(HORA_ARGENTINA).strftime("%d/%m")
    url = ctx.settings.public_base_url.rstrip("/") + "/radar/"
    await _avisar(ctx, duenos, ASUNTO_CAIDA, TEXTO_CAIDA.format(fecha=fecha, url=url), link["id"])
    return "hecho"
```

- [ ] **Step 4: Correr**

```bash
python -m pytest tests/radar_tests/test_fin_vinculo.py -q
```
Esperado: `10 passed`.

- [ ] **Step 5: Commit**

```bash
git add app/radar/fin_vinculo.py tests/radar_tests/test_fin_vinculo.py
git commit -m "Radar tramo 2: job de fin de vinculo con un unico DELETE, claves y verificacion 404" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Chequeo de salud cada 5 minutos

**Files:**
- Create: `app/radar/salud.py`
- Test: `tests/radar_tests/test_salud.py`

**Interfaces:**
- Consumes: `Job`, `jobs.encolar`, `auditoria.registrar`, `aplicar_status`, `leer_worker`, `cliente_de`, `WahaError`, `ClaveAdminAusente`, `VIVOS`, `CAIDA_MAXIMA`, `QR_ABANDONADO`, `PATRON_STATUS`.
- Produces: `INTERVALO_S = 300`; `CREANDO_HUERFANO = timedelta(minutes=15)`; `async ejecutar(ctx, job: Job) -> Literal["reprogramar", "hecho"]`. Efectos: aplica el estado real de WAHA (origen `salud`; 404 → `AUSENTE`); encola `fin_vinculo` con causa `caida_72h` (caído hace más de 72 h), `qr_abandonado` (en `esperando_qr` hace más de 30 min) o `duracion` (`duracion_vinculo_dias > 0` cumplido desde `conectado_at`). Un vínculo en `creando` con más de 15 min está huérfano: sin sesión en WAHA pasa a `abortado` (auditado `vinculo_abortado`, motivo `waha_error`) y libera la línea; con sesión se encola su fin con causa `qr_abandonado`. Uno en `creando` más reciente no se toca. Un error de red no cambia el estado.

- [ ] **Step 1: Tests (fallan)**

`tests/radar_tests/test_salud.py`:

```python
"""Chequeo de salud (Estados especiales): estado real, caída > 72 h, QR abandonado y duración."""
import uuid

from app.radar import salud
from app.radar.jobs import Job
from app.radar.waha.sesion import nombre_sesion
from app.radar.workers import listar_workers

from .helpers import como_superusuario, crear_link_directo, escenario_vinculable, vincular_de_prueba


def _job(esc, link_id):
    return Job(id=uuid.uuid4(), tenant_id=esc["tenant_id"], tipo="chequeo_salud", link_id=link_id, causa=None,
               intentos=1)


async def _sql(ctx, esc, sql, *args):
    async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
        return await con.fetchrow(sql, *args)


async def _fin_encolado(ctx, esc, link_id):
    fila = await _sql(ctx, esc, "SELECT causa FROM jobs WHERE link_id = $1 AND tipo = 'fin_vinculo'", link_id)
    return fila["causa"] if fila else None


async def test_vinculo_sano_se_reprograma(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    assert await salud.ejecutar(ctx_waha, _job(esc, v["link_id"])) == "reprogramar"
    link = await _sql(ctx_waha, esc, "SELECT estado, ultimo_chequeo_at FROM links WHERE id = $1", v["link_id"])
    assert link["estado"] == "vinculado" and link["ultimo_chequeo_at"] is not None
    ev = await _sql(ctx_waha, esc, "SELECT origen FROM link_status_events ORDER BY id DESC LIMIT 1")
    assert ev["origen"] == "salud"


async def test_sesion_ausente_marca_caida(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    del waha.sesiones[v["session_name"]]
    await salud.ejecutar(ctx_waha, _job(esc, v["link_id"]))
    link = await _sql(ctx_waha, esc, "SELECT estado, caido_desde, waha_status FROM links WHERE id = $1", v["link_id"])
    assert (link["estado"], link["waha_status"]) == ("caido", "AUSENTE") and link["caido_desde"] is not None


async def test_caida_de_mas_de_72_horas_encola_el_fin(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    waha.sesiones[v["session_name"]]["status"] = "FAILED"
    await _sql(ctx_waha, esc, "UPDATE links SET estado = 'caido', caido_desde = now() - interval '73 hours' "
                              "WHERE id = $1", v["link_id"])
    assert await salud.ejecutar(ctx_waha, _job(esc, v["link_id"])) == "reprogramar"
    assert await _fin_encolado(ctx_waha, esc, v["link_id"]) == "caida_72h"


async def test_caida_reciente_no_encola(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    waha.sesiones[v["session_name"]]["status"] = "FAILED"
    await _sql(ctx_waha, esc, "UPDATE links SET estado = 'caido', caido_desde = now() - interval '1 hour' "
                              "WHERE id = $1", v["link_id"])
    await salud.ejecutar(ctx_waha, _job(esc, v["link_id"]))
    assert await _fin_encolado(ctx_waha, esc, v["link_id"]) is None


async def test_qr_abandonado_encola_el_fin(ctx_waha, waha, radar_urls):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc, working=False)
    await como_superusuario(radar_urls, "UPDATE links SET created_at = now() - interval '31 minutes' WHERE id = $1",
                            v["link_id"])
    await salud.ejecutar(ctx_waha, _job(esc, v["link_id"]))
    assert await _fin_encolado(ctx_waha, esc, v["link_id"]) == "qr_abandonado"


async def test_duracion_del_vinculo_cumplida_encola_el_fin(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    await _sql(ctx_waha, esc, "UPDATE lines SET duracion_vinculo_dias = 1 WHERE id = $1", esc["line_id"])
    await _sql(ctx_waha, esc, "UPDATE links SET conectado_at = now() - interval '2 days' WHERE id = $1", v["link_id"])
    await salud.ejecutar(ctx_waha, _job(esc, v["link_id"]))
    assert await _fin_encolado(ctx_waha, esc, v["link_id"]) == "duracion"


async def test_error_de_red_no_cambia_el_estado(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    waha.falla_leer = True
    assert await salud.ejecutar(ctx_waha, _job(esc, v["link_id"])) == "reprogramar"
    link = await _sql(ctx_waha, esc, "SELECT estado FROM links WHERE id = $1", v["link_id"])
    assert link["estado"] == "vinculado"


async def test_vinculo_cerrado_termina_el_chequeo(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    await _sql(ctx_waha, esc, "UPDATE links SET estado = 'cerrado' WHERE id = $1", v["link_id"])
    waha.llamadas.clear()
    assert await salud.ejecutar(ctx_waha, _job(esc, v["link_id"])) == "hecho"
    assert waha.llamadas == []


async def _creando(ctx, esc):
    """Vínculo que quedó en 'creando' (el proceso murió entre el INSERT y WAHA)."""
    [w] = await listar_workers(ctx)
    return await crear_link_directo(ctx.db, esc["tenant_id"], esc["line_id"], w.id, esc["consent_id"],
                                    estado="creando")


async def test_creando_huerfano_sin_sesion_se_aborta_y_libera_la_linea(ctx_waha, waha, radar_urls):
    esc = await escenario_vinculable(ctx_waha)
    k = await _creando(ctx_waha, esc)
    waha.llamadas.clear()
    assert await salud.ejecutar(ctx_waha, _job(esc, k)) == "reprogramar"      # reciente: puede estar creándose
    assert waha.llamadas == []
    await como_superusuario(radar_urls, "UPDATE links SET created_at = now() - interval '16 minutes' WHERE id = $1", k)
    assert await salud.ejecutar(ctx_waha, _job(esc, k)) == "hecho"
    link = await _sql(ctx_waha, esc, "SELECT estado, cerrado_at FROM links WHERE id = $1", k)
    assert link["estado"] == "abortado" and link["cerrado_at"] is not None
    audit = await _sql(ctx_waha, esc, "SELECT detalle::text AS d FROM access_audit_log WHERE accion = 'vinculo_abortado'")
    assert "waha_error" in audit["d"]
    v = await vincular_de_prueba(ctx_waha, waha, esc)                          # la línea ya no está bloqueada
    assert v["link_id"] != k


async def test_creando_huerfano_con_sesion_encola_el_fin(ctx_waha, waha, radar_urls):
    esc = await escenario_vinculable(ctx_waha)
    k = await _creando(ctx_waha, esc)
    n = nombre_sesion(k)
    waha.sesiones[n] = {"name": n, "status": "SCAN_QR_CODE", "engine": "NOWEB", "config": {}, "me_id": None}
    await como_superusuario(radar_urls, "UPDATE links SET created_at = now() - interval '16 minutes' WHERE id = $1", k)
    assert await salud.ejecutar(ctx_waha, _job(esc, k)) == "hecho"
    assert await _fin_encolado(ctx_waha, esc, k) == "qr_abandonado"
```

- [ ] **Step 2: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_salud.py -q
```
Esperado: `ImportError: cannot import name 'salud' from 'app.radar'`.

- [ ] **Step 3: Implementar**

`app/radar/salud.py`:

```python
"""
Chequeo de salud de cada vínculo vivo, cada 5 min (Estados especiales:
"session.status + chequeo de salud cada 5 min").

Lee el estado real en WAHA y lo aplica con la misma máquina de estados que el
webhook. Después decide si corresponde encolar el fin del vínculo:
- caído hace más de 72 h (dispositivo quitado desde el teléfono, teléfono
  inactivo): el fin corre a las 72 h si no hubo reconexión antes;
- en esperando_qr hace más de 30 min (QR abandonado);
- duracion_vinculo_dias > 0 cumplida desde conectado_at.
Un vínculo que quedó en 'creando' más de 15 min (el proceso murió entre el
INSERT y WAHA) se aborta si la sesión no existe o se cierra si existe.
Un error de red con WAHA no cambia nada: se reintenta en el próximo chequeo.
La "sospecha de silencio" necesita tráfico y llega en el tramo 3.
"""

import logging
from datetime import timedelta
from typing import Literal

from app.radar import auditoria
from app.radar import jobs as cola
from app.radar.contexto import RadarContexto
from app.radar.jobs import Job
from app.radar.vinculo_estados import CAIDA_MAXIMA, PATRON_STATUS, QR_ABANDONADO, VIVOS
from app.radar.vinculos import aplicar_status
from app.radar.waha.cliente import WahaError
from app.radar.workers import ClaveAdminAusente, cliente_de, leer_worker

logger = logging.getLogger("app.radar.salud")

INTERVALO_S = 300
# Un vínculo que sigue en 'creando' después de esto quedó huérfano (la creación
# real tarda segundos: INSERT, POST /api/sessions, verificación y clave).
CREANDO_HUERFANO = timedelta(minutes=15)


async def ejecutar(ctx: RadarContexto, job: Job) -> Literal["reprogramar", "hecho"]:
    async with ctx.db.tenant_tx(job.tenant_id) as con:
        link = await con.fetchrow("SELECT id, worker_id, session_name, estado, now() - created_at AS edad "
                                  "FROM links WHERE id = $1", job.link_id)
    if link is None or link["estado"] not in VIVOS:
        return "hecho"
    if link["estado"] == "creando" and link["edad"] <= CREANDO_HUERFANO:
        return "reprogramar"          # iniciar_vinculo puede estar hablando con WAHA: no se toca
    try:
        worker = await leer_worker(ctx, link["worker_id"])
        async with cliente_de(ctx, worker) as cli:
            sesion = await cli.leer_sesion(link["session_name"])
    except (WahaError, ClaveAdminAusente, LookupError) as e:
        logger.warning("chequeo de salud del vínculo %s: WAHA no respondió (%s)", link["id"], type(e).__name__)
        return "reprogramar"
    if link["estado"] == "creando":
        return await _cerrar_creando_huerfano(ctx, job, link["id"], sesion_existe=sesion is not None)
    status = sesion["status"] if sesion else "AUSENTE"
    if not isinstance(status, str) or not PATRON_STATUS.match(status):
        status = "DESCONOCIDO"
    async with ctx.db.tenant_tx(job.tenant_id) as con:
        await con.execute("UPDATE links SET ultimo_chequeo_at = now() WHERE id = $1", link["id"])
        await aplicar_status(con, tenant_id=job.tenant_id, link_id=link["id"], waha_status=status, origen="salud",
                             me_id=sesion["me_id"] if sesion else None)
        f = await con.fetchrow(
            "SELECT l.estado, l.caido_desde, l.conectado_at, l.created_at, li.duracion_vinculo_dias, now() AS ahora "
            "FROM links l JOIN lines li ON li.id = l.line_id WHERE l.id = $1", link["id"])
        causa = None
        if f["estado"] == "caido" and f["caido_desde"] and f["ahora"] - f["caido_desde"] > CAIDA_MAXIMA:
            causa = "caida_72h"
        elif f["estado"] == "esperando_qr" and f["ahora"] - f["created_at"] > QR_ABANDONADO:
            causa = "qr_abandonado"
        elif (f["estado"] == "vinculado" and f["duracion_vinculo_dias"] > 0 and f["conectado_at"]
              and f["ahora"] - f["conectado_at"] > timedelta(days=f["duracion_vinculo_dias"])):
            causa = "duracion"
        if causa:
            await con.execute("UPDATE links SET fin_causa = COALESCE(fin_causa, $2) WHERE id = $1", link["id"], causa)
            await cola.encolar(con, tipo="fin_vinculo", link_id=link["id"], causa=causa)
    return "reprogramar" if f["estado"] in VIVOS else "hecho"


async def _cerrar_creando_huerfano(ctx: RadarContexto, job: Job, link_id, *, sesion_existe: bool) -> Literal["hecho"]:
    """Un vínculo en 'creando' con más de CREANDO_HUERFANO: el proceso murió
    entre el INSERT y WAHA. Sin sesión en WAHA → abortado (libera la línea: el
    índice de un activo por línea ya no lo cuenta). Con sesión → fin de vínculo
    con causa qr_abandonado, que la borra con un único DELETE."""
    async with ctx.db.tenant_tx(job.tenant_id) as con:
        if sesion_existe:
            await con.execute("UPDATE links SET fin_causa = COALESCE(fin_causa, 'qr_abandonado'), updated_at = now() "
                              "WHERE id = $1 AND estado = 'creando'", link_id)
            await cola.encolar(con, tipo="fin_vinculo", link_id=link_id, causa="qr_abandonado")
        else:
            cambiado = await con.execute("UPDATE links SET estado = 'abortado', cerrado_at = now(), updated_at = now() "
                                         "WHERE id = $1 AND estado = 'creando'", link_id)
            if cambiado == "UPDATE 1":
                await auditoria.registrar(con, tenant_id=job.tenant_id, actor_user_id=None, actor_rol="sistema",
                                          accion="vinculo_abortado", tipo_objeto="link", objeto_id=link_id,
                                          detalle={"motivo": "waha_error"})
        logger.warning("vínculo %s huérfano en 'creando': %s", link_id,
                       "fin encolado" if sesion_existe else "abortado")
    return "hecho"
```

- [ ] **Step 4: Correr**

```bash
python -m pytest tests/radar_tests/test_salud.py -q
```
Esperado: `10 passed`.

- [ ] **Step 5: Commit**

```bash
git add app/radar/salud.py tests/radar_tests/test_salud.py
git commit -m "Radar tramo 2: chequeo de salud con caida de 72 h, QR abandonado y duracion del vinculo" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: Worker de la cola: proceso propio y modo embebido en el servicio web

**Files:**
- Create: `app/radar/worker.py`
- Modify: `app/radar/settings.py` (`worker_embebido`), `app/radar/app.py` (`_lifespan_radar`)
- Test: `tests/radar_tests/test_worker.py`

**Interfaces:**
- Consumes: `jobs.reclamar/completar/reprogramar/fallar/programar_salud`, `fin_vinculo.ejecutar`, `fin_vinculo.avisar_caida`, `salud.ejecutar`, `salud.INTERVALO_S`, `construir_contexto`, `validar_settings`, `get_radar_settings`.
- Produces: `HANDLERS: dict[str, Callable[[RadarContexto, Job], Awaitable[str]]]`; `async correr_una_vez(ctx, *, lote: int = 10) -> int`; `async bucle(ctx, *, parar: asyncio.Event, pausa_s: float = 2.0, salud_cada_s: float = 300) -> None`; entrada `python -m app.radar.worker`; `RadarSettings.worker_embebido: bool = True`; `app.state.radar_worker: asyncio.Task | None`.

- [ ] **Step 1: Tests (fallan)**

`tests/radar_tests/test_worker.py`:

```python
"""Worker de la cola: despacha, completa, reprograma el chequeo, reintenta y para limpio."""
import asyncio

from app.radar import jobs as cola
from app.radar import worker
from app.radar.app import crear_app_radar
from app.radar.settings import RadarSettings
from app.radar.vinculos import aplicar_status, pedir_fin

from .helpers import escenario_vinculable, vincular_de_prueba


async def _sql(ctx, esc, sql, *args):
    async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
        return await con.fetchrow(sql, *args)


async def _pedir(ctx, esc):
    await pedir_fin(ctx, tenant_id=esc["tenant_id"], line_id=esc["line_id"], causa="pedido_kis", borrar=False,
                    actor_user_id=None, actor_rol="admin", ip=None)


async def test_correr_una_vez_despacha_el_fin_y_lo_completa(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    await _pedir(ctx_waha, esc)
    assert await worker.correr_una_vez(ctx_waha) == 1
    job = await _sql(ctx_waha, esc, "SELECT estado FROM jobs WHERE link_id = $1", v["link_id"])
    link = await _sql(ctx_waha, esc, "SELECT estado FROM links WHERE id = $1", v["link_id"])
    assert (job["estado"], link["estado"]) == ("hecho", "cerrado")


async def test_chequeo_de_salud_se_reprograma_a_5_minutos(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    assert await cola.programar_salud(ctx_waha.db) == 1
    await worker.correr_una_vez(ctx_waha)
    job = await _sql(ctx_waha, esc, "SELECT estado, intentos, ejecutar_desde - now() AS falta FROM jobs "
                                    "WHERE link_id = $1 AND tipo = 'chequeo_salud'", v["link_id"])
    assert (job["estado"], job["intentos"]) == ("pendiente", 0) and job["falta"].total_seconds() > 250


async def test_excepcion_en_el_handler_reintenta_con_backoff(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    await _pedir(ctx_waha, esc)
    waha.falla_claves = True
    await worker.correr_una_vez(ctx_waha)
    job = await _sql(ctx_waha, esc, "SELECT estado, intentos, ultimo_error FROM jobs WHERE link_id = $1",
                     v["link_id"])
    assert (job["estado"], job["intentos"], job["ultimo_error"]) == ("pendiente", 1, "FinIncompleto")


async def test_tipo_sin_handler_falla_sin_romper_el_ciclo(ctx_waha, waha, monkeypatch):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    await _pedir(ctx_waha, esc)
    monkeypatch.delitem(worker.HANDLERS, "fin_vinculo")
    assert await worker.correr_una_vez(ctx_waha) == 1
    job = await _sql(ctx_waha, esc, "SELECT estado, ultimo_error FROM jobs WHERE link_id = $1", v["link_id"])
    assert (job["estado"], job["ultimo_error"]) == ("pendiente", "LookupError")


async def test_aviso_de_caida_se_despacha_y_completa(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await aplicar_status(con, tenant_id=esc["tenant_id"], link_id=v["link_id"], waha_status="FAILED",
                             origen="webhook")
    antes = len(ctx_waha.mailer.enviados)
    assert await worker.correr_una_vez(ctx_waha) == 1
    job = await _sql(ctx_waha, esc, "SELECT estado FROM jobs WHERE link_id = $1 AND tipo = 'aviso_caida'",
                     v["link_id"])
    assert job["estado"] == "hecho" and len(ctx_waha.mailer.enviados) == antes + 1


async def test_bucle_programa_salud_y_para(ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    parar = asyncio.Event()

    async def cortar():
        await asyncio.sleep(0.3)
        parar.set()

    await asyncio.gather(worker.bucle(ctx_waha, parar=parar, pausa_s=0.05, salud_cada_s=3600), cortar())
    job = await _sql(ctx_waha, esc, "SELECT count(*) AS n, min(estado) AS estado FROM jobs "
                                    "WHERE link_id = $1 AND tipo = 'chequeo_salud'", v["link_id"])
    assert (job["n"], job["estado"]) == (1, "pendiente")


async def test_lifespan_arranca_y_detiene_el_worker_embebido(radar_urls, tmp_path):
    rs = RadarSettings(_env_file=None, database_url=radar_urls["app"], migrator_database_url=radar_urls["migrator"],
                       fuente_database_url=radar_urls["fuente"], cookie_secret="secreto-de-test-de-32-caracteres!",
                       secrets_dir=str(tmp_path / "secretos"), worker_embebido=True)
    app = crear_app_radar(rs)
    async with app.router.lifespan_context(app):
        tarea = app.state.radar_worker
        await asyncio.sleep(0.05)
        assert tarea is not None and not tarea.done()
    assert tarea.done()
```

- [ ] **Step 2: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_worker.py -q
```
Esperado: `ImportError: cannot import name 'worker' from 'app.radar'`.

- [ ] **Step 3: Implementar el worker**

`app/radar/worker.py`:

```python
"""
Worker de la cola de Radar (§6.2).

- Como proceso aparte: `python -m app.radar.worker` (misma imagen, mismas
  variables RADAR_). No corre migraciones: las corre el servicio web.
- Embebido (por defecto, RADAR_WORKER_EMBEBIDO=true): el lifespan del servicio
  web corre `bucle` como tarea. Motivo: las claves de WAHA viven en el
  FileSecretStore de un volumen de Railway, y un volumen se monta en un solo
  servicio. Con un gestor de secretos externo se pasa a proceso aparte.

Cada job se reclama sin tenant (función SECURITY DEFINER sobre una tabla sin
contenido) y el handler entra a tenant_tx(job.tenant_id). Un error en un job no
corta el ciclo: se registra el tipo de error y se reintenta con backoff.
"""

import asyncio
import logging
import signal
import time
from typing import Awaitable, Callable, Optional

from app.radar import fin_vinculo, salud
from app.radar import jobs as cola
from app.radar.contexto import RadarContexto
from app.radar.jobs import Job

logger = logging.getLogger("app.radar.worker")

HANDLERS: dict[str, Callable[[RadarContexto, Job], Awaitable[str]]] = {
    "fin_vinculo": fin_vinculo.ejecutar,
    "chequeo_salud": salud.ejecutar,
    "aviso_caida": fin_vinculo.avisar_caida,
}


async def correr_una_vez(ctx: RadarContexto, *, lote: int = 10) -> int:
    trabajos = await cola.reclamar(ctx.db, lote=lote)
    for job in trabajos:
        try:
            handler = HANDLERS.get(job.tipo)
            if handler is None:
                raise LookupError("tipo de job sin handler")
            resultado = await handler(ctx, job)
            if resultado == "reprogramar":
                await cola.reprogramar(ctx.db, job, salud.INTERVALO_S)
            else:
                await cola.completar(ctx.db, job)
        except Exception as e:  # un job roto no corta el ciclo
            logger.warning("job %s (%s) falló: %s", job.id, job.tipo, type(e).__name__)
            await cola.fallar(ctx.db, job, e)
    return len(trabajos)


async def bucle(ctx: RadarContexto, *, parar: asyncio.Event, pausa_s: float = 2.0,
                salud_cada_s: float = float(salud.INTERVALO_S)) -> None:
    ultima: Optional[float] = None
    while not parar.is_set():
        if ultima is None or time.monotonic() - ultima >= salud_cada_s:
            try:
                await cola.programar_salud(ctx.db)
            except Exception as e:
                logger.warning("no se pudieron programar los chequeos de salud: %s", type(e).__name__)
            ultima = time.monotonic()
        try:
            hechos = await correr_una_vez(ctx)
        except Exception as e:
            logger.warning("ciclo del worker falló: %s", type(e).__name__)
            hechos = 0
        if hechos == 0:
            try:
                await asyncio.wait_for(parar.wait(), timeout=pausa_s)
            except asyncio.TimeoutError:
                pass


async def _main() -> None:
    from app.radar.app import construir_contexto, validar_settings
    from app.radar.settings import get_radar_settings

    logging.basicConfig(level="INFO")
    rs = get_radar_settings()
    validar_settings(rs)
    ctx = await construir_contexto(rs)
    parar = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, parar.set)
        except (NotImplementedError, RuntimeError):   # Windows
            pass
    try:
        await bucle(ctx, parar=parar)
    finally:
        await ctx.cerrar()


if __name__ == "__main__":
    asyncio.run(_main())
```

- [ ] **Step 4: Setting y lifespan**

En `app/radar/settings.py`, dentro de `RadarSettings`, después de `waha_timeout_s`:

```python
    # Worker de la cola dentro del servicio web (ver app/radar/worker.py).
    worker_embebido: bool = True
```

En `app/radar/app.py`, reemplazar `_lifespan_radar` por:

```python
@asynccontextmanager
async def _lifespan_radar(app: FastAPI):
    rs: RadarSettings = app.state.radar_settings
    logging.basicConfig(level="INFO")
    propio = app.state.radar is None
    if propio:
        validar_settings(rs)
        await asyncio.wait_for(asyncio.to_thread(migrar_resultados, rs.migrator_database_url), timeout=60)
        await asyncio.wait_for(asyncio.to_thread(migrar_fuente, rs.fuente_database_url), timeout=60)
        app.state.radar = await construir_contexto(rs)
    logger.info("Radar arrancó: almacén de fuente %s", app.state.radar.fuente.almacen)
    parar = asyncio.Event()
    tarea = None
    if propio and rs.worker_embebido:
        from app.radar.worker import bucle   # import local: app.radar.worker importa este módulo en _main
        tarea = asyncio.create_task(bucle(app.state.radar, parar=parar))
    app.state.radar_worker = tarea
    yield
    if tarea is not None:
        parar.set()
        try:
            await asyncio.wait_for(tarea, timeout=15)
        except asyncio.TimeoutError:
            tarea.cancel()
    if propio:
        await app.state.radar.cerrar()
```

- [ ] **Step 5: Correr**

```bash
python -m pytest tests/radar_tests -q
```
Esperado: todo verde; `test_worker.py` suma 7 (y `test_lifespan_migra_y_arma_el_contexto` del tramo 1 sigue pasando con el worker embebido).

- [ ] **Step 6: Commit**

```bash
git add app/radar/worker.py app/radar/settings.py app/radar/app.py tests/radar_tests/test_worker.py
git commit -m "Radar tramo 2: worker de la cola, como proceso propio o embebido en el servicio web" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: Receptor `POST /webhook/waha` (solo `session.status` en este tramo)

**Files:**
- Create: `app/radar/routers/webhook_waha.py`
- Modify: `app/radar/app.py` (router)
- Test: `tests/radar_tests/test_webhook_waha.py`

**Interfaces:**
- Consumes: `contexto`, `aplicar_status`, `PATRON_STATUS`, `RadarSettings.waha_webhook_hmac_key`; fixtures `cliente`, `ctx_waha`, `waha`; helpers `HMAC_TEST`, `escenario_vinculable`, `vincular_de_prueba`.
- Produces: `verificar_hmac(crudo: bytes, cabecera: str | None, clave: str) -> bool`; `POST /webhook/waha` → `401 {"ok": false}` (firma ausente/inválida o sin clave), `400` (cuerpo no JSON o sin `event`), `200 {"ok": true, "descartado": true}` (`message.*`), `200 {"ok": true, "ignorado": true}` (otro evento, metadata inválida o que no coincide, status con forma inválida), `200 {"ok": true, "aplicado": bool}` (`session.status` aplicado).

- [ ] **Step 1: Tests (fallan)**

`tests/radar_tests/test_webhook_waha.py`:

```python
"""
Receptor de webhooks de WAHA (§6.3 punto 1): HMAC sha512 fail-closed,
resolución por metadata + nombre de sesión, session.status aplicado con la
máquina de estados, y message.* descartado SIN persistir nada (tramo 3).
"""
import hashlib
import hmac
import json
import logging
import uuid

from .helpers import HMAC_TEST, escenario_vinculable, vincular_de_prueba


def _firmar(crudo: bytes, clave: str = HMAC_TEST) -> str:
    return hmac.new(clave.encode(), crudo, hashlib.sha512).hexdigest()


async def _post(cliente, sobre=None, *, firma=None, crudo=None):
    crudo = crudo if crudo is not None else json.dumps(sobre).encode()
    headers = {"content-type": "application/json"}
    if firma is not False:
        headers["x-webhook-hmac"] = firma or _firmar(crudo)
    return await cliente.post("/webhook/waha", content=crudo, headers=headers)


def _status(esc, v, status, me=None):
    sobre = {"event": "session.status", "session": v["session_name"],
             "metadata": {"tenant_id": str(esc["tenant_id"]), "line_id": str(esc["line_id"]),
                          "link_id": str(v["link_id"])},
             "payload": {"status": status}}
    if me:
        sobre["me"] = {"id": me, "pushName": "Negocio"}
    return sobre


async def _estado(ctx, esc, link_id):
    async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
        return await con.fetchrow("SELECT estado, numero_sufijo FROM links WHERE id = $1", link_id)


async def test_sin_clave_configurada_rechaza_todo(cliente, radar_ctx):
    r = await _post(cliente, {"event": "session.status"})
    assert r.status_code == 401


async def test_firma_invalida_o_ausente(cliente, ctx_waha):
    assert (await _post(cliente, {"event": "session.status"}, firma="00" * 64)).status_code == 401
    assert (await _post(cliente, {"event": "session.status"}, firma=False)).status_code == 401


async def test_mensajes_se_descartan_sin_persistir_nada(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)

    async def conteos():
        async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
            return tuple(await con.fetchrow(
                "SELECT (SELECT count(*) FROM link_status_events), (SELECT count(*) FROM access_audit_log), "
                "(SELECT count(*) FROM product_events), (SELECT count(*) FROM jobs)"))

    antes = await conteos()
    sobre = {**_status(esc, v, "WORKING"), "event": "message.any",
             "payload": {"id": "false_5493411111111@c.us_AAA", "from": "5493411111111@c.us",
                         "body": "hola, mi DNI es 30111222"}}
    r = await _post(cliente, sobre)
    assert r.status_code == 200 and r.json() == {"ok": True, "descartado": True}
    assert await conteos() == antes


async def test_session_status_aplica_la_transicion(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc, working=False)
    r = await _post(cliente, _status(esc, v, "WORKING", me=waha.me_id))
    assert r.status_code == 200 and r.json() == {"ok": True, "aplicado": True}
    fila = await _estado(ctx_waha, esc, v["link_id"])
    assert (fila["estado"], fila["numero_sufijo"]) == ("vinculado", "4567")


async def test_metadata_de_otro_tenant_o_de_otra_sesion_se_ignora(cliente, ctx_waha, waha):
    a = await escenario_vinculable(ctx_waha, "Farmacia A")
    b = await escenario_vinculable(ctx_waha, "Farmacia B")
    v = await vincular_de_prueba(ctx_waha, waha, a, working=False)
    cruzado = _status(a, v, "WORKING")
    cruzado["metadata"]["tenant_id"] = str(b["tenant_id"])
    assert (await _post(cliente, cruzado)).json() == {"ok": True, "ignorado": True}
    otra_sesion = {**_status(a, v, "WORKING"), "session": "v_ffffffffffff"}
    assert (await _post(cliente, otra_sesion)).json() == {"ok": True, "ignorado": True}
    assert (await _estado(ctx_waha, a, v["link_id"]))["estado"] == "esperando_qr"


async def test_metadata_invalida_se_ignora(cliente, ctx_waha):
    sobre = {"event": "session.status", "session": "v_0123456789ab",
             "metadata": {"tenant_id": "x", "line_id": str(uuid.uuid4()), "link_id": None},
             "payload": {"status": "WORKING"}}
    r = await _post(cliente, sobre)
    assert r.status_code == 200 and r.json() == {"ok": True, "ignorado": True}


async def test_cuerpo_que_no_es_json(cliente, ctx_waha):
    assert (await _post(cliente, crudo=b"no-es-json")).status_code == 400
    assert (await _post(cliente, crudo=b"[1, 2]")).status_code == 400


async def test_no_loguea_el_cuerpo(cliente, ctx_waha, waha, caplog):
    caplog.set_level(logging.DEBUG)
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc, working=False)
    sobre = {**_status(esc, v, "WORKING"), "event": "message",
             "payload": {"body": "hola secreto", "from": "5493411111111@c.us"}}
    await _post(cliente, sobre)
    await _post(cliente, _status(esc, v, "WORKING", me=waha.me_id))
    assert "hola secreto" not in caplog.text
    assert "5493411111111" not in caplog.text and "5493411234567" not in caplog.text


async def test_status_con_forma_invalida_se_ignora(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc, working=False)
    r = await _post(cliente, _status(esc, v, "working; DROP TABLE links"))
    assert r.json() == {"ok": True, "ignorado": True}
    assert (await _estado(ctx_waha, esc, v["link_id"]))["estado"] == "esperando_qr"
```

- [ ] **Step 2: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_webhook_waha.py -q
```
Esperado: fallas con `404 Not Found` en `/webhook/waha` (y `assert 404 == 401` en el primero).

- [ ] **Step 3: Implementar el router**

`app/radar/routers/webhook_waha.py`:

```python
"""
POST /webhook/waha (§6.3 punto 1).

- HMAC sha512 del cuerpo CRUDO en X-Webhook-Hmac, verificado antes de parsear.
  Fail-closed: sin RADAR_WAHA_WEBHOOK_HMAC_KEY (32+) responde 401 a todo.
- En este tramo solo aplica session.status. Los message.* se aceptan con 200
  (para que WAHA no reintente) y se descartan SIN persistir nada: la ingesta,
  el filtro de exclusión y webhook_inbox son del tramo 3.
- Resuelve tenant, línea y vínculo por metadata y exige que el nombre de
  sesión coincida con el del vínculo: un evento cruzado se ignora.
- Responde rápido: una transacción corta y ninguna llamada a WAHA.
- Nunca loguea el cuerpo, ni en errores.
"""

import hashlib
import hmac
import json
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.radar.contexto import contexto
from app.radar.vinculo_estados import PATRON_STATUS
from app.radar.vinculos import aplicar_status

router = APIRouter(tags=["radar-webhook"])

_MIN_CLAVE = 32
_IGNORADO = {"ok": True, "ignorado": True}


def verificar_hmac(crudo: bytes, cabecera: Optional[str], clave: str) -> bool:
    if not cabecera or not clave or len(clave) < _MIN_CLAVE:
        return False
    esperado = hmac.new(clave.encode(), crudo, hashlib.sha512).hexdigest()
    return hmac.compare_digest(esperado.encode(), cabecera.strip().lower().encode())


def _uuid(valor: Any) -> Optional[uuid.UUID]:
    if not isinstance(valor, str):
        return None
    try:
        return uuid.UUID(valor)
    except ValueError:
        return None


@router.post("/webhook/waha")
async def recibir(request: Request):
    ctx = contexto(request)
    crudo = await request.body()
    if not verificar_hmac(crudo, request.headers.get("x-webhook-hmac"), ctx.settings.waha_webhook_hmac_key):
        return JSONResponse({"ok": False}, status_code=401)
    try:
        sobre = json.loads(crudo)
    except ValueError:
        return JSONResponse({"ok": False}, status_code=400)
    if not isinstance(sobre, dict) or not isinstance(sobre.get("event"), str):
        return JSONResponse({"ok": False}, status_code=400)
    evento = sobre["event"]
    if evento.startswith("message"):
        return {"ok": True, "descartado": True}
    if evento != "session.status":
        return _IGNORADO
    meta = sobre.get("metadata") if isinstance(sobre.get("metadata"), dict) else {}
    tenant_id, line_id, link_id = _uuid(meta.get("tenant_id")), _uuid(meta.get("line_id")), _uuid(meta.get("link_id"))
    payload = sobre.get("payload") if isinstance(sobre.get("payload"), dict) else {}
    status = payload.get("status")
    if not (tenant_id and line_id and link_id) or not isinstance(status, str) or not PATRON_STATUS.match(status):
        return _IGNORADO
    me = sobre.get("me")
    me_id = me.get("id") if isinstance(me, dict) else None
    async with ctx.db.tenant_tx(tenant_id) as con:
        fila = await con.fetchrow("SELECT line_id, session_name FROM links WHERE id = $1", link_id)
        if fila is None or fila["line_id"] != line_id or fila["session_name"] != sobre.get("session"):
            return _IGNORADO
        r = await aplicar_status(con, tenant_id=tenant_id, link_id=link_id, waha_status=status, origen="webhook",
                                 me_id=me_id)
    return {"ok": True, "aplicado": r["aplicado"]}
```

En `app/radar/app.py` (`crear_app_radar` registra cada router con una línea `app.include_router`, sin tupla): el import de routers queda

```python
from app.radar.routers import admin, cuenta, health, login, parametros, soporte, webhook_waha
```

y se agrega, después de `app.include_router(soporte.router)`:

```python
    app.include_router(webhook_waha.router)
```

- [ ] **Step 4: Correr**

```bash
python -m pytest tests/radar_tests -q
```
Esperado: todo verde; `test_webhook_waha.py` suma 9 (y `test_modo_radar_no_monta_bot` sigue pasando: `/webhook/waha` no es `/webhook`).

- [ ] **Step 5: Commit**

```bash
git add app/radar/routers/webhook_waha.py app/radar/app.py tests/radar_tests/test_webhook_waha.py
git commit -m "Radar tramo 2: receptor de webhooks de WAHA con HMAC fail-closed, solo session.status" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 14: Consentimiento asistido y API de la Consola KIS (C1, C2, C4)

**Files:**
- Create: `app/radar/consentimiento_asistido.py`, `app/radar/consola.py`, `app/radar/routers/vinculo_comun.py`, `app/radar/routers/consola.py`
- Modify: `app/radar/app.py` (router), `app/radar/parametros_service.py` (`tenant_ya_consentido`), `app/radar/routers/parametros.py` (usa `tenant_ya_consentido`), `tests/radar_tests/test_parametros_api.py` (docstring)
- Test: `tests/radar_tests/test_consola_api.py`

**Interfaces:**
- Consumes: `VERSIONES`, `hash_texto` (`app.radar.consentimiento`), `leer_linea` (`app.radar.lineas`), `DE_LINEA` (`app.radar.parametros`), `a_json`, `leer_parametros_tenant`, `leer_propuesta_tenant`, `lineas_vivas_sin_consentir` (`app.radar.parametros_service`), `_tenant_ya_consentido` (`app.radar.routers.parametros`, se mueve), `MailerQueFalla`, `Email`, `requiere_rol`, `ip_de`, `Sesion`, `contexto`, `auditoria.registrar`, `eventos_producto.registrar_evento`, `radar_admin_consola_lineas()`, `semaforo`, `restriccion_activa`, servicios de `app.radar.vinculos`; helpers `entrar`, `crear_usuario`, `crear_linea_directa`, `crear_tenant_directo`, `escenario_vinculable`, `vincular_de_prueba`.
- Produces: `async tenant_ya_consentido(con, line_id, propuesta_t) -> dict` en `app.radar.parametros_service` (reemplaza a `routers/parametros._tenant_ya_consentido`); dependencia `_tenant_cliente(tenant_id, request, admin) -> Sesion` en `routers/consola.py` (404 `tenant_inexistente` si el tenant no existe o es KIS; la usan todas las rutas `_LINEA`); `copia_enviada` refleja si el mail salió; `VERSION_VIGENTE: str`; `async texto_para_linea(con, tenant_id, line_id) -> dict | None` (`version, texto, hash, linea_nombre, linea_estado, parametros_linea, parametros_tenant`); `async registrar_consentimiento_asistido(con, *, tenant_id, line_id, dueno_user_id, admin_user_id, version, opciones, ip, modo_asistencia, aceptado_por_nombre) -> uuid.UUID`; `email_copia_consentimiento(para: str, *, version: str) -> Email`; `PENDIENTES: dict[str, str]`; `fila_consola(f: dict, ahora) -> dict`; `async listar_lineas_consola(ctx, *, estado: str | None = None, tenant_id: uuid.UUID | None = None) -> list[dict]`; en `vinculo_comun`: `VincularIn(full_sync: bool = False)`, `CodigoIn(telefono)`, `DesconectarIn(confirmar: bool = False)`, `BorrarIn(confirmar: bool = False, nombre_linea: str = "")`, `RestriccionIn(hasta: datetime | None = None)`, `SIN_CACHE`, `http_de(e: VinculoRechazado) -> HTTPException`, `respuesta_png(contenido: bytes) -> Response`, `respuesta_codigo(codigo: str) -> JSONResponse`, `exigir_confirmacion(body: DesconectarIn) -> None`, `async exigir_nombre(ctx, tenant_id, line_id, body: BorrarIn) -> None`. Endpoints (todos `requiere_rol("admin")`): `GET /radar/admin/consola/lineas?estado=&tenant_id=`; bajo `/radar/admin/tenants/{tenant_id}/lineas/{line_id}`: `GET /consentimiento-asistido/texto`, `POST /consentimiento-asistido` (201 `{consent_id, copia_enviada}`), `POST /vinculo` (201, estado; también es "Reconectar"), `GET /vinculo`, `GET /vinculo/qr` (PNG, `no-store`), `POST /vinculo/codigo` (`{codigo}`, `no-store`), `POST /vinculo/reiniciar-qr`, `POST /vinculo/desconectar` (202), `POST /vinculo/desconectar-y-borrar` (202), `PUT /vinculo/restriccion`, `DELETE /vinculo/restriccion`. Errores: `{"detail": {"error": "<codigo>"}}`.

- [ ] **Step 1: Tests (fallan)**

`tests/radar_tests/test_consola_api.py`:

```python
"""
API de la Consola KIS (§3.1): C1 lista con semáforo, C2 consentimiento
asistido + vínculo con QR, C4 acciones con doble confirmación. Solo el rol
admin del tenant KIS; todo auditado.
"""
import json
import uuid

from app.radar.constantes import TENANT_KIS
from app.radar.parametros_service import lineas_vivas_sin_consentir
from app.radar.vinculos import aplicar_status

from .helpers import (MailerQueFalla, como_superusuario, crear_linea_directa, crear_tenant_directo, crear_usuario,
                      entrar, escenario_vinculable, vincular_de_prueba)
from .waha_falso import PNG


def _url(esc, sufijo="", line_id=None):
    return f"/radar/admin/tenants/{esc['tenant_id']}/lineas/{line_id or esc['line_id']}{sufijo}"


async def _admin(cliente, ctx):
    uid = await crear_usuario(ctx.db, TENANT_KIS, "admin@keepitsimple.com.ar", "admin")
    await entrar(cliente, ctx, TENANT_KIS, uid, "admin")
    return uid


async def _sql(ctx, esc, sql, *args):
    async with ctx.db.tenant_tx(esc["tenant_id"]) as con:
        return await con.fetchrow(sql, *args)


async def test_solo_admins_de_kis(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    await entrar(cliente, ctx_waha, esc["tenant_id"], esc["dueno_id"], "dueno")
    assert (await cliente.get("/radar/admin/consola/lineas")).status_code == 403
    assert (await cliente.post(_url(esc, "/vinculo"), json={})).status_code == 403
    cliente.cookies.clear()
    assert (await cliente.get("/radar/admin/consola/lineas")).status_code == 401
    assert waha.llamadas == []


async def test_lista_todas_las_lineas_con_semaforo_y_pendientes(cliente, ctx_waha, waha):
    a = await escenario_vinculable(ctx_waha, "Farmacia A")
    await escenario_vinculable(ctx_waha, "Farmacia B")
    await vincular_de_prueba(ctx_waha, waha, a)
    await _admin(cliente, ctx_waha)
    r = await cliente.get("/radar/admin/consola/lineas")
    assert r.status_code == 200
    por = {f["tenant_nombre"]: f for f in r.json()}
    assert set(por) == {"Farmacia A", "Farmacia B"}
    assert (por["Farmacia A"]["semaforo"], por["Farmacia A"]["numero"]) == ("verde", "…4567")
    assert (por["Farmacia B"]["semaforo"], por["Farmacia B"]["link_estado"]) == ("gris", None)
    assert por["Farmacia A"]["pendiente"] == {"ultimo_mensaje": "tramo 3", "sincronizacion": "tramo 3",
                                              "huecos": "tramo 3", "gasto_ia_mes": "tramo 5"}
    assert por["Farmacia A"]["ultimo_mensaje"] is None and por["Farmacia A"]["worker"] == "w1 (1/50)"
    assert "5493411234567" not in r.text


async def test_filtros_por_estado_y_cliente(cliente, ctx_waha, waha):
    a = await escenario_vinculable(ctx_waha, "Farmacia A")
    b = await escenario_vinculable(ctx_waha, "Farmacia B")
    await vincular_de_prueba(ctx_waha, waha, a)
    await _admin(cliente, ctx_waha)
    vinculadas = (await cliente.get("/radar/admin/consola/lineas?estado=vinculada")).json()
    assert [f["tenant_nombre"] for f in vinculadas] == ["Farmacia A"]
    de_b = (await cliente.get(f"/radar/admin/consola/lineas?tenant_id={b['tenant_id']}")).json()
    assert [f["tenant_nombre"] for f in de_b] == ["Farmacia B"]
    assert (await cliente.get("/radar/admin/consola/lineas?estado=otro")).status_code == 422


async def test_texto_de_consentimiento_con_los_parametros_de_la_linea(cliente, ctx_waha):
    esc = await escenario_vinculable(ctx_waha)
    await _admin(cliente, ctx_waha)
    t = (await cliente.get(_url(esc, "/consentimiento-asistido/texto"))).json()
    assert t["version"] == "v1" and "Qué hacemos" in t["texto"] and len(t["hash"]) == 64
    assert t["parametros_linea"]["duracion_vinculo_dias"] == 0 and t["linea_nombre"] == "Local centro"


async def test_consentimiento_asistido_registra_quien_y_como_y_manda_copia(cliente, ctx_waha):
    esc = await escenario_vinculable(ctx_waha)
    nueva = await crear_linea_directa(ctx_waha.db, esc["tenant_id"], "Sucursal")
    admin = await _admin(cliente, ctx_waha)
    r = await cliente.post(_url(esc, "/consentimiento-asistido", line_id=nueva),
                           json={"version_texto": "v1", "titular_leyo_y_acepto": True, "modo": "videollamada",
                                 "nombre": "Ana Pérez"})
    assert r.status_code == 201 and r.json()["copia_enviada"] is True
    c = await _sql(ctx_waha, esc, "SELECT user_id, modo, cargado_por, modo_asistencia, aceptado_por_nombre, opciones "
                                  "FROM consents WHERE line_id = $1", nueva)
    assert (c["user_id"], c["modo"], c["cargado_por"], c["modo_asistencia"], c["aceptado_por_nombre"]) == \
           (esc["dueno_id"], "asistido", admin, "videollamada", "Ana Pérez")
    assert json.loads(c["opciones"])["parametros_linea"]["duracion_vinculo_dias"] == 0
    mail = ctx_waha.mailer.enviados[-1]
    assert mail.para == esc["dueno_email"] and "versión v1" in mail.texto and "Qué hacemos" in mail.texto
    audit = await _sql(ctx_waha, esc, "SELECT actor_rol, detalle::text AS d FROM access_audit_log "
                                      "WHERE accion = 'consentimiento_asistido'")
    assert audit["actor_rol"] == "admin" and "videollamada" in audit["d"]


async def test_consentimiento_asistido_exige_la_aceptacion_y_un_dueno(cliente, ctx_waha):
    esc = await escenario_vinculable(ctx_waha)
    await _admin(cliente, ctx_waha)
    cuerpo = {"version_texto": "v1", "titular_leyo_y_acepto": False, "modo": "presencial", "nombre": "Ana"}
    r = await cliente.post(_url(esc, "/consentimiento-asistido"), json=cuerpo)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "consentimiento_no_aceptado"
    sin_dueno = await crear_tenant_directo(ctx_waha.db, "Sin dueño")
    li = await crear_linea_directa(ctx_waha.db, sin_dueno)
    r = await cliente.post(f"/radar/admin/tenants/{sin_dueno}/lineas/{li}/consentimiento-asistido",
                           json={**cuerpo, "titular_leyo_y_acepto": True})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "sin_dueno"


async def test_vincular_muestra_qr_solo_con_consentimiento(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    sin = await crear_linea_directa(ctx_waha.db, esc["tenant_id"], "Sin consentimiento")
    await _admin(cliente, ctx_waha)
    r = await cliente.post(_url(esc, "/vinculo", line_id=sin), json={})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "sin_consentimiento"
    r = await cliente.post(_url(esc, "/vinculo"), json={"full_sync": False})
    assert r.status_code == 201 and r.json()["estado"] == "esperando_qr"
    assert (await cliente.get(_url(esc, "/vinculo"))).json()["qr_disponible"] is True
    qr = await cliente.get(_url(esc, "/vinculo/qr"))
    assert qr.status_code == 200 and qr.content == PNG
    assert qr.headers["content-type"] == "image/png" and qr.headers["cache-control"] == "no-store"


async def test_codigo_y_reinicio_del_qr(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    await _admin(cliente, ctx_waha)
    await cliente.post(_url(esc, "/vinculo"), json={})
    r = await cliente.post(_url(esc, "/vinculo/codigo"), json={"telefono": "+5493411234567"})
    assert r.json() == {"codigo": "ABCD-EFGH"} and r.headers["cache-control"] == "no-store"
    r = await cliente.post(_url(esc, "/vinculo/reiniciar-qr"))
    assert r.status_code == 200 and r.json()["reinicios_restantes"] == 2


async def test_desconectar_exige_confirmar(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    await _admin(cliente, ctx_waha)
    r = await cliente.post(_url(esc, "/vinculo/desconectar"), json={})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "falta_confirmacion"
    assert (await cliente.post(_url(esc, "/vinculo/desconectar"), json={"confirmar": True})).status_code == 202
    job = await _sql(ctx_waha, esc, "SELECT causa FROM jobs WHERE link_id = $1", v["link_id"])
    assert job["causa"] == "pedido_kis"


async def test_borrar_todo_exige_el_nombre_exacto_de_la_linea(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    await vincular_de_prueba(ctx_waha, waha, esc)
    await _admin(cliente, ctx_waha)
    r = await cliente.post(_url(esc, "/vinculo/desconectar-y-borrar"), json={"confirmar": True, "nombre_linea": "Otra"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "confirmacion_incorrecta"
    r = await cliente.post(_url(esc, "/vinculo/desconectar-y-borrar"),
                           json={"confirmar": True, "nombre_linea": "Local centro"})
    assert r.status_code == 202 and r.json()["borrado_solicitado"] is True
    linea = await _sql(ctx_waha, esc, "SELECT borrado_solicitado_at FROM lines WHERE id = $1", esc["line_id"])
    assert linea["borrado_solicitado_at"] is not None


async def test_restriccion_bloquea_reconectar_pero_no_desconectar(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await aplicar_status(con, tenant_id=esc["tenant_id"], link_id=v["link_id"], waha_status="FAILED",
                             origen="webhook")
    await _admin(cliente, ctx_waha)
    r = await cliente.put(_url(esc, "/vinculo/restriccion"), json={"hasta": None})
    assert r.status_code == 200 and r.json()["restriccion_activa"] is True
    r = await cliente.post(_url(esc, "/vinculo"), json={})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "restriccion_activa"
    assert (await cliente.post(_url(esc, "/vinculo/desconectar"), json={"confirmar": True})).status_code == 202
    assert (await cliente.delete(_url(esc, "/vinculo/restriccion"))).json()["restriccion_activa"] is False


async def test_las_acciones_quedan_auditadas_con_el_admin(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    admin = await _admin(cliente, ctx_waha)
    await cliente.post(_url(esc, "/vinculo"), json={})
    await cliente.post(_url(esc, "/vinculo/codigo"), json={"telefono": "+5493411234567"})
    await cliente.post(_url(esc, "/vinculo/desconectar"), json={"confirmar": True})
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        filas = await con.fetch("SELECT accion, actor_rol, actor_user_id FROM access_audit_log ORDER BY id")
    assert [f["accion"] for f in filas] == ["vinculo_iniciado", "codigo_solicitado", "desconexion_pedida"]
    assert all(f["actor_rol"] == "admin" and f["actor_user_id"] == admin for f in filas)


async def test_rutas_de_linea_solo_sobre_un_tenant_cliente(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    await _admin(cliente, ctx_waha)
    kis = f"/radar/admin/tenants/{TENANT_KIS}/lineas/{esc['line_id']}"
    otro = f"/radar/admin/tenants/{uuid.uuid4()}/lineas/{esc['line_id']}"
    for r in (await cliente.get(kis + "/consentimiento-asistido/texto"),
              await cliente.post(kis + "/vinculo", json={}),
              await cliente.post(otro + "/vinculo/desconectar", json={"confirmar": True}),
              await cliente.put(otro + "/vinculo/restriccion", json={"hasta": None})):
        assert r.status_code == 404 and r.json()["detail"]["error"] == "tenant_inexistente"
    assert waha.llamadas == []


async def test_copia_que_no_sale_no_rompe_el_consentimiento(cliente, ctx_waha):
    esc = await escenario_vinculable(ctx_waha)
    nueva = await crear_linea_directa(ctx_waha.db, esc["tenant_id"], "Sucursal")
    await _admin(cliente, ctx_waha)
    ctx_waha.mailer = MailerQueFalla()
    r = await cliente.post(_url(esc, "/consentimiento-asistido", line_id=nueva),
                           json={"version_texto": "v1", "titular_leyo_y_acepto": True, "modo": "presencial",
                                 "nombre": "Ana"})
    assert r.status_code == 201 and r.json()["copia_enviada"] is False
    assert (await _sql(ctx_waha, esc, "SELECT count(*) AS n FROM consents WHERE line_id = $1", nueva))["n"] == 1


async def test_asistido_arrastra_la_propuesta_de_tenant_ya_aceptada(cliente, ctx_waha, radar_urls):
    esc = await escenario_vinculable(ctx_waha)
    t, li = esc["tenant_id"], esc["line_id"]
    await como_superusuario(radar_urls, "UPDATE tenants SET parametros_propuestos = '{\"ia_habilitada\": true}'::jsonb, "
                                        "parametros_propuestos_at = now() - interval '1 hour' WHERE id = $1", t)
    async with ctx_waha.db.tenant_tx(t) as con:        # el dueño ya había aceptado la propuesta
        await con.execute("UPDATE lines SET estado = 'vinculada' WHERE id = $1", li)
        await con.execute("INSERT INTO consents (line_id, user_id, version_texto, hash_texto, opciones) "
                          "VALUES ($1, $2, 'v1', repeat('a', 64), $3::jsonb)", li, esc["dueno_id"],
                          json.dumps({"parametros_linea": {}, "parametros_tenant": {"ia_habilitada": True}}))
        assert await lineas_vivas_sin_consentir(con, {"ia_habilitada": True}) == []
    await _admin(cliente, ctx_waha)
    r = await cliente.post(_url(esc, "/consentimiento-asistido"),
                           json={"version_texto": "v1", "titular_leyo_y_acepto": True, "modo": "presencial",
                                 "nombre": "Ana"})
    assert r.status_code == 201
    c = await _sql(ctx_waha, esc, "SELECT opciones FROM consents WHERE modo = 'asistido'")
    assert json.loads(c["opciones"])["parametros_tenant"]["ia_habilitada"] is True
    async with ctx_waha.db.tenant_tx(t) as con:        # la línea no vuelve a quedar pendiente
        assert await lineas_vivas_sin_consentir(con, {"ia_habilitada": True}) == []
```

- [ ] **Step 2: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_consola_api.py -q
```
Esperado: fallas por `404 Not Found` en `/radar/admin/consola/lineas` y en las rutas de vínculo.

- [ ] **Step 3: Consentimiento asistido y listado de la Consola**

Primero, mover `_tenant_ya_consentido` de `app/radar/routers/parametros.py` a `app/radar/parametros_service.py` como función pública, para que el consentimiento asistido use exactamente la misma regla que el del dueño. Agregar al final de `app/radar/parametros_service.py` (ya importa `json`, `uuid`, `asyncpg`, `validar` y define `coincide_con_propuesta`):

```python
async def tenant_ya_consentido(con: asyncpg.Connection, line_id: uuid.UUID, propuesta_t: dict) -> dict:
    """Valores de la propuesta de tenant vigente que el último consentimiento
    de esta línea ya aceptó, siempre que sea posterior a esa propuesta (un
    consentimiento viejo no vale para una propuesta nueva). Lo usan el
    consentimiento del dueño (routers/parametros.py) y el asistido de la Consola."""
    crudo = await con.fetchval(
        "SELECT c.opciones->'parametros_tenant' FROM consents c "
        "WHERE c.id = (SELECT c2.id FROM consents c2 WHERE c2.line_id = $1 ORDER BY c2.created_at DESC LIMIT 1) "
        "AND c.created_at >= (SELECT t.parametros_propuestos_at FROM tenants t WHERE t.id = c.tenant_id)",
        line_id)
    previos = json.loads(crudo) if crudo else {}
    return {n: validar(n, v) for n, v in propuesta_t.items()
            if n in previos and coincide_con_propuesta(propuesta_t, n, validar(n, previos[n]))}
```

En `app/radar/routers/parametros.py`: borrar la función `_tenant_ya_consentido` completa; sumar `tenant_ya_consentido` al `from app.radar.parametros_service import (...)`; en `consentir`, cambiar `ya_consentidos = await _tenant_ya_consentido(con, line_id, propuesta_t)` por `ya_consentidos = await tenant_ya_consentido(con, line_id, propuesta_t)`; y borrar `import json`, que queda sin uso (`validar` y `coincide_con_propuesta` se siguen usando en `_validar_parciales` y en `consentir`). En `tests/radar_tests/test_parametros_api.py`, el docstring de `test_consentimiento_de_linea_no_arrastra_tenant_de_una_propuesta_anterior` pasa a nombrar `tenant_ya_consentido`. Los tests del tramo 1 no cambian de comportamiento.

`app/radar/consentimiento_asistido.py`:

```python
"""
Consentimiento asistido (§3.1 C2) y texto de P2 para una línea.

El admin de KIS muestra el texto de P2 con los parámetros de la línea, el
titular lo acepta en la misma sesión (presencial o videollamada) y queda en
`consents` con modo='asistido', cargado_por = el admin y user_id = el dueño de
la cuenta. Se manda copia al dueño por email. Sin esto no se crea la sesión.
"""

import json
import uuid
from typing import Optional

import asyncpg

from app.radar.consentimiento import VERSIONES, hash_texto
from app.radar.lineas import leer_linea
from app.radar.mailer import Email
from app.radar.parametros import DE_LINEA
from app.radar.parametros_service import a_json, leer_parametros_tenant

VERSION_VIGENTE = max(VERSIONES, key=lambda v: int(v[1:]))


async def texto_para_linea(con: asyncpg.Connection, tenant_id: uuid.UUID, line_id: uuid.UUID) -> Optional[dict]:
    fila = await leer_linea(con, line_id)
    if fila is None:
        return None
    return {
        "version": VERSION_VIGENTE,
        "texto": VERSIONES[VERSION_VIGENTE],
        "hash": hash_texto(VERSION_VIGENTE),
        "linea_nombre": fila["nombre"],
        "linea_estado": fila["estado"],
        "parametros_linea": a_json({n: fila[n] for n in DE_LINEA}),
        "parametros_tenant": a_json(await leer_parametros_tenant(con, tenant_id)),
    }


async def registrar_consentimiento_asistido(con: asyncpg.Connection, *, tenant_id: uuid.UUID, line_id: uuid.UUID,
                                            dueno_user_id: uuid.UUID, admin_user_id: uuid.UUID, version: str,
                                            opciones: dict, ip: Optional[str], modo_asistencia: str,
                                            aceptado_por_nombre: str) -> uuid.UUID:
    return await con.fetchval(
        "INSERT INTO consents (tenant_id, line_id, user_id, version_texto, hash_texto, opciones, ip, modo, "
        "cargado_por, modo_asistencia, aceptado_por_nombre) "
        "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, 'asistido', $8, $9, $10) RETURNING id",
        tenant_id, line_id, dueno_user_id, version, hash_texto(version), json.dumps(opciones, default=str), ip,
        admin_user_id, modo_asistencia, aceptado_por_nombre)


def email_copia_consentimiento(para: str, *, version: str) -> Email:
    """Copia al dueño: el texto aceptado, su versión y la huella del hash. Sin datos de conversación."""
    h = hash_texto(version)
    return Email(
        para=para,
        asunto="Copia del consentimiento de tu línea en Radar",
        texto=(f"Un admin de Keep IT Simple registró que aceptaste este texto (versión {version}, huella {h[:16]}). "
               "Si no lo aceptaste, respondé este correo.\n\n" + VERSIONES[version]),
        huella=h[:8],
    )
```

`app/radar/consola.py`:

```python
"""
C1 de la Consola KIS (§3.1): todas las líneas de todos los clientes, con el
último vínculo de cada una, por la función SECURITY DEFINER
radar_admin_consola_lineas(). Sin conversaciones ni teléfonos: del número solo
el sufijo. Los campos de tramos futuros van en null con su tramo en `pendiente`.
"""

import uuid
from datetime import datetime
from typing import Optional

from app.radar.contexto import RadarContexto
from app.radar.vinculo_estados import restriccion_activa, semaforo

PENDIENTES = {"ultimo_mensaje": "tramo 3", "sincronizacion": "tramo 3", "huecos": "tramo 3",
              "gasto_ia_mes": "tramo 5"}


def _iso(v: Optional[datetime]) -> Optional[str]:
    return v.isoformat() if v else None


def fila_consola(f: dict, ahora: datetime) -> dict:
    restringida = restriccion_activa(f["restriccion_hasta"], f["restriccion_sin_fecha"], ahora)
    return {
        "tenant_id": str(f["tenant_id"]),
        "tenant_nombre": f["tenant_nombre"],
        "line_id": str(f["line_id"]),
        "line_nombre": f["line_nombre"],
        "line_estado": f["line_estado"],
        "link_id": str(f["link_id"]) if f["link_id"] else None,
        "link_estado": f["link_estado"],
        "waha_status": f["waha_status"],
        "numero": ("…" + f["numero_sufijo"]) if f["numero_sufijo"] else None,
        "observado_hasta": _iso(f["observado_hasta"]),
        "caido_desde": _iso(f["caido_desde"]),
        "ultimo_status_at": _iso(f["ultimo_status_at"]),
        "engine": f["engine"],
        "salud": ("restricción activa" if restringida else
                  ("sin restricción" if f["link_id"] else None)),
        "worker": (f"{f['worker_nombre']} ({f['worker_sesiones']}/{f['worker_max_sesiones']})"
                   if f["worker_nombre"] else None),
        "semaforo": semaforo(f, ahora),
        **{campo: None for campo in PENDIENTES},
        "pendiente": dict(PENDIENTES),
    }


async def listar_lineas_consola(ctx: RadarContexto, *, estado: Optional[str] = None,
                                tenant_id: Optional[uuid.UUID] = None) -> list[dict]:
    async with ctx.db.sin_tenant() as con:
        filas = await con.fetch("SELECT * FROM radar_admin_consola_lineas()")
        ahora = await con.fetchval("SELECT now()")
    return [fila_consola(dict(f), ahora) for f in filas
            if (estado is None or f["line_estado"] == estado) and (tenant_id is None or f["tenant_id"] == tenant_id)]
```

- [ ] **Step 4: Piezas comunes de los routers de vínculo**

`app/radar/routers/vinculo_comun.py`:

```python
"""Modelos y respuestas que comparten la Consola KIS y la pantalla del dueño."""

import uuid
from datetime import datetime
from typing import Optional

from fastapi import HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app.radar.contexto import RadarContexto
from app.radar.vinculos import VinculoRechazado

# El QR y el código son credenciales efímeras: nunca en caché.
SIN_CACHE = {"Cache-Control": "no-store"}


class VincularIn(BaseModel):
    full_sync: bool = False


class CodigoIn(BaseModel):
    telefono: str = Field(min_length=6, max_length=32)


class DesconectarIn(BaseModel):
    confirmar: bool = False


class BorrarIn(BaseModel):
    confirmar: bool = False
    nombre_linea: str = Field(default="", max_length=80)


class RestriccionIn(BaseModel):
    hasta: Optional[datetime] = None


def http_de(e: VinculoRechazado) -> HTTPException:
    return HTTPException(status_code=e.status, detail={"error": e.codigo})


def respuesta_png(contenido: bytes) -> Response:
    return Response(content=contenido, media_type="image/png", headers=SIN_CACHE)


def respuesta_codigo(codigo: str) -> JSONResponse:
    return JSONResponse({"codigo": codigo}, headers=SIN_CACHE)


def exigir_confirmacion(body: Optional[DesconectarIn]) -> None:
    if body is None or not body.confirmar:
        raise HTTPException(status_code=422, detail={"error": "falta_confirmacion"})


async def exigir_nombre(ctx: RadarContexto, tenant_id: uuid.UUID, line_id: uuid.UUID, body: Optional[BorrarIn]) -> None:
    """Segunda confirmación de "Desconectar y borrar todo": el nombre exacto de la línea."""
    if body is None or not body.confirmar:
        raise HTTPException(status_code=422, detail={"error": "falta_confirmacion"})
    async with ctx.db.tenant_tx(tenant_id) as con:
        nombre = await con.fetchval("SELECT nombre FROM lines WHERE id = $1", line_id)
    if nombre is None:
        raise HTTPException(status_code=404, detail={"error": "linea_inexistente"})
    if body.nombre_linea.strip() != nombre:
        raise HTTPException(status_code=422, detail={"error": "confirmacion_incorrecta"})
```

- [ ] **Step 5: Router de la Consola**

`app/radar/routers/consola.py`:

```python
"""
API de la Consola KIS (§3.1). Solo el rol `admin` (tenant KIS). Todo lo que
toca WAHA lo hace el backend; ninguna clave de WAHA llega al navegador.
Cada acción queda auditada en el tenant del cliente, con el admin como actor.

GET  /radar/admin/consola/lineas                                   C1
GET  /radar/admin/tenants/{t}/lineas/{l}/consentimiento-asistido/texto
POST /radar/admin/tenants/{t}/lineas/{l}/consentimiento-asistido   C2 paso 2
POST /radar/admin/tenants/{t}/lineas/{l}/vinculo                   C2 paso 3-4 (y "Reconectar")
GET  /radar/admin/tenants/{t}/lineas/{l}/vinculo                   estado en vivo (polling)
GET  …/vinculo/qr · POST …/vinculo/codigo · POST …/vinculo/reiniciar-qr
POST …/vinculo/desconectar · POST …/vinculo/desconectar-y-borrar   C4
PUT|DELETE …/vinculo/restriccion                                   C4 (restricción de cuenta)
"""

import logging
import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.radar import auditoria, eventos_producto
from app.radar.auth import Sesion, ip_de, requiere_rol
from app.radar.consentimiento import VERSIONES
from app.radar.consentimiento_asistido import (email_copia_consentimiento, registrar_consentimiento_asistido,
                                               texto_para_linea)
from app.radar.consola import listar_lineas_consola
from app.radar.contexto import contexto
from app.radar.parametros_service import (a_json, leer_parametros_tenant, leer_propuesta_tenant,
                                          tenant_ya_consentido)
from app.radar.routers.vinculo_comun import (BorrarIn, CodigoIn, DesconectarIn, RestriccionIn, VincularIn,
                                             exigir_confirmacion, exigir_nombre, http_de, respuesta_codigo,
                                             respuesta_png)
from app.radar.vinculos import (VinculoRechazado, estado_de_linea, iniciar_vinculo, levantar_restriccion,
                                marcar_restriccion, pedir_codigo, pedir_fin, qr_png, reiniciar_qr)

logger = logging.getLogger("app.radar.consola")

router = APIRouter(prefix="/radar/admin", tags=["radar-consola"])
_LINEA = "/tenants/{tenant_id}/lineas/{line_id}"
_ESTADO_LINEA = r"^(vinculada|sin_vinculo|de_baja)$"


async def _tenant_cliente(tenant_id: uuid.UUID, request: Request,
                          admin: Sesion = Depends(requiere_rol("admin"))) -> Sesion:
    """Como admin.py del tramo 1: las rutas de línea solo operan sobre un tenant
    cliente que existe (nunca sobre el tenant KIS). Depende de requiere_rol, así
    que primero responde 401/403 y recién después mira el tenant."""
    async with contexto(request).db.tenant_tx(tenant_id) as con:
        existe = await con.fetchval("SELECT count(*) FROM tenants WHERE id = $1 AND NOT es_kis", tenant_id)
    if existe == 0:
        raise HTTPException(status_code=404, detail={"error": "tenant_inexistente"})
    return admin


class ConsentimientoAsistidoIn(BaseModel):
    version_texto: str = Field(pattern=r"^v[0-9]+$")
    titular_leyo_y_acepto: bool
    modo: Literal["presencial", "videollamada"]
    nombre: str = Field(min_length=1, max_length=120)


def _actor(admin: Sesion, request: Request) -> dict:
    return {"actor_user_id": admin.user_id, "actor_rol": admin.rol, "ip": ip_de(request)}


@router.get("/consola/lineas")
async def lineas(request: Request, estado: Optional[str] = Query(default=None, pattern=_ESTADO_LINEA),
                 tenant_id: Optional[uuid.UUID] = None, admin: Sesion = Depends(requiere_rol("admin"))):
    return await listar_lineas_consola(contexto(request), estado=estado, tenant_id=tenant_id)


@router.get(_LINEA + "/consentimiento-asistido/texto")
async def texto(tenant_id: uuid.UUID, line_id: uuid.UUID, request: Request,
                admin: Sesion = Depends(_tenant_cliente)):
    async with contexto(request).db.tenant_tx(tenant_id) as con:
        t = await texto_para_linea(con, tenant_id, line_id)
    if t is None:
        raise HTTPException(status_code=404, detail={"error": "linea_inexistente"})
    return t


@router.post(_LINEA + "/consentimiento-asistido", status_code=201)
async def consentimiento_asistido(tenant_id: uuid.UUID, line_id: uuid.UUID, body: ConsentimientoAsistidoIn,
                                  request: Request, admin: Sesion = Depends(_tenant_cliente)):
    if not body.titular_leyo_y_acepto:
        raise HTTPException(status_code=422, detail={"error": "consentimiento_no_aceptado"})
    if body.version_texto not in VERSIONES:
        raise HTTPException(status_code=422, detail={"error": "version_desconocida"})
    ctx = contexto(request)
    ip = ip_de(request)
    async with ctx.db.tenant_tx(tenant_id) as con:
        t = await texto_para_linea(con, tenant_id, line_id)
        if t is None:
            raise HTTPException(status_code=404, detail={"error": "linea_inexistente"})
        if t["linea_estado"] == "de_baja":
            raise HTTPException(status_code=409, detail={"error": "linea_de_baja"})
        dueno = await con.fetchrow("SELECT u.id, u.email FROM memberships m JOIN users u ON u.id = m.user_id "
                                   "WHERE m.rol = 'dueno' ORDER BY m.created_at LIMIT 1")
        if dueno is None:
            raise HTTPException(status_code=409, detail={"error": "sin_dueno"})
        # Como el consentimiento propio del tramo 1 (routers/parametros.py): este
        # pasa a ser "el último" de la línea, así que arrastra los valores de la
        # propuesta de tenant vigente que el anterior ya aceptaba; si no, una
        # línea vinculada que ya había aceptado volvería a quedar pendiente.
        ya_consentidos = await tenant_ya_consentido(con, line_id, await leer_propuesta_tenant(con, tenant_id))
        parametros_tenant = a_json({**(await leer_parametros_tenant(con, tenant_id)), **ya_consentidos})
        opciones = {"parametros_linea": t["parametros_linea"], "parametros_tenant": parametros_tenant}
        cid = await registrar_consentimiento_asistido(
            con, tenant_id=tenant_id, line_id=line_id, dueno_user_id=dueno["id"], admin_user_id=admin.user_id,
            version=body.version_texto, opciones=opciones, ip=ip, modo_asistencia=body.modo,
            aceptado_por_nombre=body.nombre.strip())
        await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=admin.user_id, actor_rol=admin.rol,
                                  accion="consentimiento_asistido", tipo_objeto="consent", objeto_id=cid, ip=ip,
                                  detalle={"modo": body.modo})
        await eventos_producto.registrar_evento(con, tenant_id=tenant_id, evento="consentimiento_registrado",
                                                line_id=line_id, user_id=dueno["id"], objeto_id=cid)
    # El consentimiento ya quedó guardado: un proveedor de mail caído no lo
    # convierte en 500 (mismo criterio que enviar_link_seguro del tramo 1).
    try:
        await ctx.mailer.enviar(email_copia_consentimiento(dueno["email"], version=body.version_texto))
        copia = True
    except Exception as e:
        logger.warning("copia del consentimiento %s no enviada: %s", cid, type(e).__name__)
        copia = False
    return {"consent_id": str(cid), "copia_enviada": copia}


@router.post(_LINEA + "/vinculo", status_code=201)
async def vincular(tenant_id: uuid.UUID, line_id: uuid.UUID, request: Request, body: Optional[VincularIn] = None,
                   admin: Sesion = Depends(_tenant_cliente)):
    try:
        return await iniciar_vinculo(contexto(request), tenant_id=tenant_id, line_id=line_id,
                                     full_sync=body.full_sync if body else False, **_actor(admin, request))
    except VinculoRechazado as e:
        raise http_de(e)


@router.get(_LINEA + "/vinculo")
async def estado(tenant_id: uuid.UUID, line_id: uuid.UUID, request: Request,
                 admin: Sesion = Depends(_tenant_cliente)):
    try:
        return await estado_de_linea(contexto(request), tenant_id=tenant_id, line_id=line_id)
    except VinculoRechazado as e:
        raise http_de(e)


@router.get(_LINEA + "/vinculo/qr")
async def qr(tenant_id: uuid.UUID, line_id: uuid.UUID, request: Request,
             admin: Sesion = Depends(_tenant_cliente)):
    try:
        return respuesta_png(await qr_png(contexto(request), tenant_id=tenant_id, line_id=line_id))
    except VinculoRechazado as e:
        raise http_de(e)


@router.post(_LINEA + "/vinculo/codigo")
async def codigo(tenant_id: uuid.UUID, line_id: uuid.UUID, body: CodigoIn, request: Request,
                 admin: Sesion = Depends(_tenant_cliente)):
    try:
        return respuesta_codigo(await pedir_codigo(contexto(request), tenant_id=tenant_id, line_id=line_id,
                                                   telefono=body.telefono, **_actor(admin, request)))
    except VinculoRechazado as e:
        raise http_de(e)


@router.post(_LINEA + "/vinculo/reiniciar-qr")
async def reiniciar(tenant_id: uuid.UUID, line_id: uuid.UUID, request: Request,
                    admin: Sesion = Depends(_tenant_cliente)):
    try:
        return await reiniciar_qr(contexto(request), tenant_id=tenant_id, line_id=line_id, **_actor(admin, request))
    except VinculoRechazado as e:
        raise http_de(e)


@router.post(_LINEA + "/vinculo/desconectar", status_code=202)
async def desconectar(tenant_id: uuid.UUID, line_id: uuid.UUID, request: Request,
                      body: Optional[DesconectarIn] = None, admin: Sesion = Depends(_tenant_cliente)):
    exigir_confirmacion(body)
    try:
        return await pedir_fin(contexto(request), tenant_id=tenant_id, line_id=line_id, causa="pedido_kis",
                               borrar=False, **_actor(admin, request))
    except VinculoRechazado as e:
        raise http_de(e)


@router.post(_LINEA + "/vinculo/desconectar-y-borrar", status_code=202)
async def desconectar_y_borrar(tenant_id: uuid.UUID, line_id: uuid.UUID, request: Request,
                               body: Optional[BorrarIn] = None, admin: Sesion = Depends(_tenant_cliente)):
    ctx = contexto(request)
    await exigir_nombre(ctx, tenant_id, line_id, body)
    try:
        return await pedir_fin(ctx, tenant_id=tenant_id, line_id=line_id, causa="pedido_kis", borrar=True,
                               **_actor(admin, request))
    except VinculoRechazado as e:
        raise http_de(e)


@router.put(_LINEA + "/vinculo/restriccion")
async def marcar(tenant_id: uuid.UUID, line_id: uuid.UUID, body: RestriccionIn, request: Request,
                 admin: Sesion = Depends(_tenant_cliente)):
    try:
        return await marcar_restriccion(contexto(request), tenant_id=tenant_id, line_id=line_id, hasta=body.hasta,
                                        **_actor(admin, request))
    except VinculoRechazado as e:
        raise http_de(e)


@router.delete(_LINEA + "/vinculo/restriccion")
async def levantar(tenant_id: uuid.UUID, line_id: uuid.UUID, request: Request,
                   admin: Sesion = Depends(_tenant_cliente)):
    try:
        return await levantar_restriccion(contexto(request), tenant_id=tenant_id, line_id=line_id,
                                          **_actor(admin, request))
    except VinculoRechazado as e:
        raise http_de(e)
```

En `app/radar/app.py` (`crear_app_radar` registra cada router con una línea `app.include_router`, sin tupla): el import de routers queda

```python
from app.radar.routers import admin, consola, cuenta, health, login, parametros, soporte, webhook_waha
```

y se agrega, después de `app.include_router(soporte.router)` y de los routers de las tareas anteriores:

```python
    app.include_router(consola.router)
```

- [ ] **Step 6: Correr**

```bash
python -m pytest tests/radar_tests -q
```
Esperado: todo verde; `test_consola_api.py` suma 15 (y `test_parametros_api.py` del tramo 1 sigue verde con `tenant_ya_consentido` movido).

- [ ] **Step 7: Commit**

```bash
git add app/radar tests/radar_tests/test_consola_api.py tests/radar_tests/test_parametros_api.py
git commit -m "Radar tramo 2: API de la Consola KIS con consentimiento asistido, vinculo y acciones auditadas" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 15: Pantallas de la Consola y de P3 del dueño (HTML estático, JS mínimo, CSP estricta)

**Files:**
- Create: `app/radar/static/consola.html`, `app/radar/static/conectar.html`, `app/radar/static/radar.js`, `app/radar/static/radar.css`, `app/radar/routers/paginas.py`
- Modify: `app/radar/app.py` (router)
- Test: `tests/radar_tests/test_paginas.py`

**Interfaces:**
- Consumes: `requiere_rol`, `ip_de`, `Sesion`, `contexto`, `auditoria.registrar`; los endpoints de las Tasks 14 y 16 y `POST /radar/api/lineas/{id}/consentimientos` del tramo 1.
- Produces: `CSP: str`, `CABECERAS_HTML: dict`; `GET /radar/consola` (admin; audita `consola_abierta`), `GET /radar/conectar?linea=<uuid>` (dueño), `GET /radar/estaticos/{radar.js|radar.css}` (sin sesión, `nosniff`; cualquier otro nombre → 404).

- [ ] **Step 1: Tests (fallan)**

`tests/radar_tests/test_paginas.py`:

```python
"""
Pantallas (decisión 12): HTML estático sin scripts ni estilos inline, un único
JS que escribe solo con textContent, y CSP estricta (§7: escapar todo y CSP).
"""
import pathlib
import re

from app.radar.constantes import TENANT_KIS
from app.radar.routers.paginas import CSP

from .helpers import crear_usuario, entrar, escenario_vinculable

ESTATICOS = pathlib.Path(__file__).resolve().parents[2] / "app" / "radar" / "static"
IDS_CLIENTE = {"error", "panel", "texto-consentimiento", "parametros", "version", "acepto", "btn-consentir",
               "paso-consentimiento", "paso-vincular", "full-sync", "btn-generar", "estado-texto", "qr", "cuenta",
               "reinicios", "btn-reiniciar", "telefono", "btn-codigo", "codigo", "aviso", "btn-desconectar",
               "btn-borrar"}


def _ids_html(nombre: str) -> set[str]:
    return set(re.findall(r'\bid="([a-z0-9-]+)"', (ESTATICOS / nombre).read_text(encoding="utf-8")))


def _sin_inline(html: str) -> None:
    assert re.search(r"<script(?![^>]*\bsrc=)", html) is None
    assert all(c.strip() == "" for c in re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S))
    assert '<script src="/radar/estaticos/radar.js" defer></script>' in html
    assert " style=" not in html and "<style" not in html
    assert re.search(r"\son[a-z]+\s*=", html) is None


async def _admin(cliente, ctx):
    uid = await crear_usuario(ctx.db, TENANT_KIS, "admin@keepitsimple.com.ar", "admin")
    await entrar(cliente, ctx, TENANT_KIS, uid, "admin")
    return uid


async def test_consola_solo_para_admins(cliente, radar_ctx):
    assert (await cliente.get("/radar/consola")).status_code == 401
    esc = await escenario_vinculable(radar_ctx)
    await entrar(cliente, radar_ctx, esc["tenant_id"], esc["dueno_id"], "dueno")
    assert (await cliente.get("/radar/consola")).status_code == 403
    await _admin(cliente, radar_ctx)
    assert (await cliente.get("/radar/consola")).status_code == 200


async def test_consola_con_csp_estricta_y_sin_inline(cliente, radar_ctx):
    await _admin(cliente, radar_ctx)
    r = await cliente.get("/radar/consola")
    assert r.headers["content-security-policy"] == CSP
    assert "unsafe-inline" not in CSP and "default-src 'none'" in CSP
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    _sin_inline(r.text)


async def test_abrir_la_consola_queda_auditado(cliente, radar_ctx):
    uid = await _admin(cliente, radar_ctx)
    await cliente.get("/radar/consola")
    async with radar_ctx.db.tenant_tx(TENANT_KIS) as con:
        n = await con.fetchval("SELECT count(*) FROM access_audit_log WHERE accion = 'consola_abierta' "
                               "AND actor_user_id = $1", uid)
    assert n == 1


async def test_conectar_solo_para_el_dueno(cliente, radar_ctx):
    esc = await escenario_vinculable(radar_ctx)
    gestor = await crear_usuario(radar_ctx.db, esc["tenant_id"], "gestor@cliente.com", "gestor")
    await entrar(cliente, radar_ctx, esc["tenant_id"], gestor, "gestor")
    assert (await cliente.get("/radar/conectar")).status_code == 403
    await entrar(cliente, radar_ctx, esc["tenant_id"], esc["dueno_id"], "dueno")
    r = await cliente.get(f"/radar/conectar?linea={esc['line_id']}")
    assert r.status_code == 200 and r.headers["content-security-policy"] == CSP
    _sin_inline(r.text)


def test_el_js_no_arma_html():
    js = (ESTATICOS / "radar.js").read_text(encoding="utf-8")
    for prohibido in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert prohibido not in js, prohibido
    assert "textContent" in js


async def test_estaticos_con_tipo_y_nosniff(cliente):
    js = await cliente.get("/radar/estaticos/radar.js")
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
    assert js.headers["x-content-type-options"] == "nosniff"
    css = await cliente.get("/radar/estaticos/radar.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert (await cliente.get("/radar/estaticos/consola.html")).status_code == 404
    assert (await cliente.get("/radar/estaticos/..%2Fapp.py")).status_code == 404


def test_los_ids_que_usa_el_js_existen_en_la_consola():
    js = (ESTATICOS / "radar.js").read_text(encoding="utf-8")
    usados = set(re.findall(r'(?:\$|enlazar)\("([a-z0-9-]+)"', js))
    assert usados and usados <= _ids_html("consola.html"), usados - _ids_html("consola.html")


def test_la_pantalla_del_dueno_tiene_lo_que_usa_el_modo_cliente():
    js = (ESTATICOS / "radar.js").read_text(encoding="utf-8")
    assert IDS_CLIENTE <= set(re.findall(r'(?:\$|enlazar)\("([a-z0-9-]+)"', js))
    assert IDS_CLIENTE <= _ids_html("conectar.html"), IDS_CLIENTE - _ids_html("conectar.html")
    assert {"filtro-estado", "lineas", "restriccion-hasta"}.isdisjoint(_ids_html("conectar.html"))


def test_textos_del_js_no_prometen_lo_que_el_codigo_no_hace():
    js = (ESTATICOS / "radar.js").read_text(encoding="utf-8")
    assert "te avisamos por email" not in js          # sin_capacidad: nada avisa cuando se libera lugar
    assert "e.caido_desde" in js and "se desconectó el " in js       # caída con fecha DD/MM
    assert "r.copia_enviada" in js                    # la copia del consentimiento puede no haber salido
```

- [ ] **Step 2: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_paginas.py -q
```
Esperado: `ModuleNotFoundError: No module named 'app.radar.routers.paginas'`.

- [ ] **Step 3: HTML y CSS**

`app/radar/static/consola.html`:

```html
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Radar — Consola KIS</title>
<link rel="stylesheet" href="/radar/estaticos/radar.css">
<script src="/radar/estaticos/radar.js" defer></script>
</head>
<body data-modo="consola">
<header>
  <h1>Consola KIS</h1>
  <p class="nota">Solo admins de Keep IT Simple. Cada acción queda auditada. Nunca se muestran conversaciones.</p>
</header>
<p id="error" class="error" role="alert"></p>

<section>
  <h2>Líneas</h2>
  <div class="filtros">
    <label>Estado
      <select id="filtro-estado">
        <option value="">Todos</option>
        <option value="vinculada">Vinculada</option>
        <option value="sin_vinculo">Sin vínculo</option>
        <option value="de_baja">De baja</option>
      </select>
    </label>
    <label>Cliente
      <select id="filtro-cliente"><option value="">Todos</option></select>
    </label>
    <button type="button" id="btn-refrescar">Refrescar</button>
  </div>
  <div class="tabla">
    <table>
      <thead>
        <tr>
          <th>Semáforo</th><th>Cliente</th><th>Línea</th><th>Número</th><th>Estado</th><th>Sesión WAHA</th>
          <th>Observado hasta</th><th>Último mensaje</th><th>Sincronización</th><th>Huecos</th>
          <th>Salud de la cuenta</th><th>Worker</th><th>IA del mes</th><th></th>
        </tr>
      </thead>
      <tbody id="lineas"></tbody>
    </table>
  </div>
  <p class="nota">"—" = dato que llega con la ingesta (tramo 3) o con la IA (tramo 5).</p>
</section>

<section id="panel" hidden>
  <h2 id="panel-titulo"></h2>
  <button type="button" id="btn-cerrar-panel">Cerrar</button>

  <div id="paso-consentimiento" hidden>
    <h3>Consentimiento asistido</h3>
    <pre id="texto-consentimiento"></pre>
    <p id="parametros" class="nota"></p>
    <input type="hidden" id="version" value="">
    <label><input type="checkbox" id="acepto"> El titular leyó y aceptó este texto en esta sesión</label>
    <div id="fila-asistido">
      <label>Modo
        <select id="modo">
          <option value="presencial">Presencial</option>
          <option value="videollamada">Videollamada</option>
        </select>
      </label>
      <label>Quién aceptó <input type="text" id="nombre" maxlength="120" autocomplete="off"></label>
    </div>
    <button type="button" id="btn-consentir">Registrar consentimiento y enviar copia al dueño</button>
  </div>

  <div id="paso-vincular" hidden>
    <h3>Conectar WhatsApp</h3>
    <p id="fila-profundidad"><label><input type="checkbox" id="full-sync"> Hasta 12 meses de historial (solo servidores NOWEB; no se puede cambiar después de escanear)</label></p>
    <button type="button" id="btn-generar">Generar QR</button>
  </div>

  <div id="paso-estado">
    <p id="estado-texto" role="status"></p>
    <img id="qr" alt="Código QR para vincular WhatsApp" width="264" height="264" hidden>
    <p id="cuenta" class="nota" hidden></p>
    <p id="reinicios" class="nota"></p>
    <button type="button" id="btn-reiniciar" hidden>Generar un código nuevo</button>
    <p class="nota">Abrí WhatsApp → Dispositivos vinculados → Vincular un dispositivo → Escaneá. Si falla del lado del teléfono, revisá que no tenga ya 4 dispositivos vinculados.</p>
    <details>
      <summary>Estoy en el mismo teléfono: vincular con código</summary>
      <label>Número de la línea, en formato internacional <input type="tel" id="telefono" autocomplete="off"></label>
      <button type="button" id="btn-codigo">Pedir código</button>
      <p id="codigo"></p>
    </details>
    <p id="aviso" class="nota"></p>
  </div>

  <div id="acciones">
    <h3>Acciones</h3>
    <button type="button" id="btn-desconectar">Desconectar</button>
    <button type="button" id="btn-borrar" class="peligro">Desconectar y borrar todo</button>
    <div id="fila-restriccion">
      <label>Restricción de cuenta hasta <input type="datetime-local" id="restriccion-hasta"></label>
      <button type="button" id="btn-restriccion">Marcar restricción (vacío = sin fecha)</button>
      <button type="button" id="btn-levantar">Levantar restricción</button>
    </div>
  </div>
</section>
</body>
</html>
```

`app/radar/static/conectar.html`:

```html
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Radar — Conectar WhatsApp</title>
<link rel="stylesheet" href="/radar/estaticos/radar.css">
<script src="/radar/estaticos/radar.js" defer></script>
</head>
<body data-modo="cliente">
<header>
  <h1>Conectar tu WhatsApp</h1>
  <p class="nota">No enviamos ni modificamos nada: solo leemos.</p>
</header>
<p id="error" class="error" role="alert"></p>

<section id="panel">
  <div id="paso-consentimiento" hidden>
    <h2>Qué vamos a analizar y qué no</h2>
    <pre id="texto-consentimiento"></pre>
    <p id="parametros" class="nota"></p>
    <input type="hidden" id="version" value="">
    <label><input type="checkbox" id="acepto"> Soy titular o responsable de esta línea y confirmo el acuerdo de tratamiento de datos de mi contrato</label>
    <button type="button" id="btn-consentir">Aceptar y continuar</button>
  </div>

  <div id="paso-vincular" hidden>
    <h2>Conectar WhatsApp</h2>
    <p><label><input type="checkbox" id="full-sync"> Hasta 12 meses de historial (no se puede cambiar después de escanear; para ampliarlo hay que desvincular y escanear de nuevo)</label></p>
    <button type="button" id="btn-generar">Generar QR</button>
  </div>

  <div id="paso-estado">
    <p id="estado-texto" role="status"></p>
    <img id="qr" alt="Código QR para vincular WhatsApp" width="264" height="264" hidden>
    <p id="cuenta" class="nota" hidden></p>
    <p id="reinicios" class="nota"></p>
    <button type="button" id="btn-reiniciar" hidden>Generar un código nuevo</button>
    <p class="nota">Abrí WhatsApp → Dispositivos vinculados → Vincular un dispositivo → Escaneá. Si falla, revisá que el teléfono no tenga ya 4 dispositivos vinculados.</p>
    <details>
      <summary>Estoy en el mismo teléfono: vincular con código</summary>
      <label>Número de la línea, en formato internacional <input type="tel" id="telefono" autocomplete="off"></label>
      <button type="button" id="btn-codigo">Pedir código</button>
      <p id="codigo"></p>
    </details>
    <p id="aviso" class="nota"></p>
  </div>

  <div>
    <button type="button" id="btn-desconectar">Desconectar</button>
    <button type="button" id="btn-borrar" class="peligro">Desconectar y borrar todo</button>
  </div>
</section>
</body>
</html>
```

`app/radar/static/radar.css`:

```css
:root { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; color: #1d1d1f; background: #fafafa; }
body { margin: 0 auto; max-width: 1200px; padding: 16px; }
[hidden] { display: none !important; }
h1 { font-size: 22px; } h2 { font-size: 18px; } h3 { font-size: 16px; }
.tabla { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td { border-bottom: 1px solid #ddd; padding: 6px 8px; text-align: left; white-space: nowrap; }
tr.semaforo-verde td:first-child { color: #137333; font-weight: 600; }
tr.semaforo-amarillo td:first-child { color: #b06000; font-weight: 600; }
tr.semaforo-rojo td:first-child { color: #c5221f; font-weight: 600; }
tr.semaforo-gris td:first-child { color: #5f6368; }
pre { white-space: pre-wrap; background: #fff; border: 1px solid #ddd; padding: 12px; max-height: 320px; overflow: auto; }
.error { color: #c5221f; min-height: 1em; }
.nota { color: #5f6368; font-size: 13px; }
.peligro { color: #c5221f; }
.filtros label { margin-right: 12px; }
button { margin: 4px 4px 4px 0; }
#panel { margin-top: 24px; padding: 16px; background: #fff; border: 1px solid #ddd; }
```

- [ ] **Step 4: JS**

`app/radar/static/radar.js`:

```javascript
"use strict";
// Radar: Consola KIS (C1, C2, C4) y P2/P3 del dueño. Sin dependencias.
// Todo dato del servidor se escribe con textContent o setAttribute; nunca se arma HTML.
(function () {
  const modo = document.body.dataset.modo;            // "consola" | "cliente"
  const $ = (id) => document.getElementById(id);
  const REFRESCO_ESTADO_MS = 3000;
  const REFRESCO_LINEAS_MS = 15000;
  const RENUEVA_QR_S = 15;
  let actual = null;                                  // {tenant_id, line_id, nombre}
  let sondeo = null;
  let qrDesde = 0;

  const MENSAJES = {
    // Sin promesa de aviso: en este tramo nada avisa cuando se libera lugar.
    sin_capacidad: "En este momento no hay lugar para una conexión nueva. Probá de nuevo más tarde o escribinos.",
    tenant_inexistente: "El cliente no existe.",
    sin_vinculo: "Esta línea no tiene una conexión.",
    sin_consentimiento: "Falta el consentimiento de esta línea.",
    consentimiento_no_aceptado: "Hay que marcar la aceptación del texto.",
    restriccion_activa: "La cuenta tiene una restricción activa: no se puede volver a vincular.",
    sin_reinicios: "Se agotaron los reintentos. Escribinos y te ayudamos.",
    codigo_no_disponible: "No se pudo generar el código. Seguí con el QR.",
    telefono_invalido: "Revisá el número: formato internacional, por ejemplo +54 9 341 123 4567.",
    vinculo_activo: "Esta línea ya tiene una conexión en curso.",
    config_no_coincide: "No pudimos preparar la conexión segura. Ya avisamos al equipo.",
    waha_error: "El servidor de conexión no respondió. Probá de nuevo en unos minutos.",
    waha_sin_configurar: "El servidor de conexión no está configurado.",
    falta_confirmacion: "Falta confirmar la acción.",
    confirmacion_incorrecta: "El nombre no coincide con el de la línea.",
    sin_dueno: "La cuenta no tiene un dueño cargado.",
    linea_invalida: "El link no indica una línea válida.",
  };
  const TEXTOS_ESTADO = {
    sin_vinculo: "Sin conexión.",
    creando: "Preparando conexión segura…",
    esperando_qr: "Escaneá el código con el teléfono de la línea.",
    vinculado: "Conectado.",
    caido: "Tu WhatsApp se desconectó.",
    cerrando: "Desconectando…",
    cerrado: "Desconectado. El tablero y lo ya importado se conservan según la configuración de la línea.",
    abortado: "No se pudo preparar la conexión.",
  };

  function mostrarError(e) {
    $("error").textContent = e ? (MENSAJES[e.message] || ("Error: " + e.message)) : "";
  }

  function base() {
    if (modo === "consola") {
      return "/radar/admin/tenants/" + encodeURIComponent(actual.tenant_id) + "/lineas/" + encodeURIComponent(actual.line_id);
    }
    return "/radar/api/lineas/" + encodeURIComponent(actual.line_id);
  }

  async function pedir(metodo, url, cuerpo) {
    const opciones = { method: metodo, credentials: "same-origin", headers: {} };
    if (cuerpo !== undefined) {
      opciones.headers["Content-Type"] = "application/json";
      opciones.body = JSON.stringify(cuerpo);
    }
    const r = await fetch(url, opciones);
    let datos = null;
    try { datos = await r.json(); } catch (e) { datos = null; }
    if (!r.ok) {
      const d = datos && datos.detail;
      throw new Error((d && d.error) || (typeof d === "string" ? d : "error_" + r.status));
    }
    return datos;
  }

  async function accion(fn) {
    try { await fn(); mostrarError(null); } catch (e) { mostrarError(e); }
  }

  function enlazar(id, fn) {
    const el = $(id);
    if (el) el.addEventListener("click", () => accion(fn));
  }

  // ---- P2: consentimiento (asistido en la Consola, propio en la pantalla del dueño)
  async function cargarConsentimiento() {
    const url = modo === "consola" ? base() + "/consentimiento-asistido/texto" : base() + "/vinculo/texto-consentimiento";
    const t = await pedir("GET", url);
    $("texto-consentimiento").textContent = t.texto;
    $("parametros").textContent = "Parámetros de esta línea: " + JSON.stringify(t.parametros_linea);
    $("version").value = t.version;
  }

  async function consentir() {
    if (!$("acepto").checked) throw new Error("consentimiento_no_aceptado");
    if (modo === "consola") {
      const r = await pedir("POST", base() + "/consentimiento-asistido", {
        version_texto: $("version").value, titular_leyo_y_acepto: true,
        modo: $("modo").value, nombre: $("nombre").value.trim(),
      });
      $("aviso").textContent = r.copia_enviada
        ? "Consentimiento registrado. Se envió una copia al dueño."
        : "Consentimiento registrado. La copia por email no salió: reenviala a mano.";
    } else {
      await pedir("POST", base() + "/consentimientos", { version_texto: $("version").value, acepta: true, titular: true });
    }
    await refrescarEstado();
  }

  // ---- P3: estado del vínculo, QR, código
  function diaMes(iso) {
    // "DD/MM" en hora de Argentina (Estados especiales: "se desconectó el DD/MM").
    return new Date(iso).toLocaleDateString("es-AR", {
      day: "2-digit", month: "2-digit", timeZone: "America/Argentina/Buenos_Aires",
    });
  }

  function mostrarEstado(e) {
    actual.nombre = e.linea_nombre;
    let texto = TEXTOS_ESTADO[e.estado] || e.estado;
    if (e.estado === "caido" && e.caido_desde) {
      texto = "Tu WhatsApp se desconectó el " + diaMes(e.caido_desde) + ". Tocá «Reconectar».";
    }
    if (e.estado === "esperando_qr") texto = "Abrí WhatsApp → Dispositivos vinculados → Vincular un dispositivo → Escaneá.";
    if (e.estado === "vinculado" && e.numero) {
      texto = "Conectado: " + e.numero + ". ¿Es esta la línea del negocio? Si no lo es, usá «Desconectar y borrar todo».";
    }
    if (e.qr_vencido) texto = e.reinicios_restantes > 0 ? "El código venció. Generá uno nuevo." : MENSAJES.sin_reinicios;
    if (e.passkey) texto = "WhatsApp pide una verificación adicional que todavía no soportamos desde acá. Escribinos y te ayudamos.";
    if (e.restriccion_activa) texto += " " + MENSAJES.restriccion_activa;
    $("estado-texto").textContent = texto;

    const puedeVincular = ["sin_vinculo", "cerrado", "abortado", "caido"].includes(e.estado) && !e.restriccion_activa;
    $("paso-consentimiento").hidden = !(puedeVincular && !e.tiene_consentimiento);
    $("paso-vincular").hidden = !(puedeVincular && e.tiene_consentimiento);
    $("btn-generar").textContent = e.estado === "caido" ? "Reconectar" : "Generar QR";

    const conQr = e.qr_disponible === true;
    $("qr").hidden = !conQr;
    $("cuenta").hidden = !conQr;
    if (conQr && (!$("qr").getAttribute("src") || Date.now() - qrDesde > RENUEVA_QR_S * 1000)) {
      $("qr").setAttribute("src", base() + "/vinculo/qr?r=" + Date.now());
      qrDesde = Date.now();
    }
    if (!conQr) $("qr").removeAttribute("src");
    $("btn-reiniciar").hidden = !(e.qr_vencido && e.reinicios_restantes > 0);
    $("reinicios").textContent = e.estado === "esperando_qr" ? "Reintentos disponibles: " + e.reinicios_restantes : "";
    $("btn-desconectar").hidden = !["creando", "esperando_qr", "vinculado", "caido"].includes(e.estado);
  }

  async function refrescarEstado() {
    const e = await pedir("GET", base() + "/vinculo");
    mostrarEstado(e);
    if (["sin_vinculo", "cerrado", "abortado"].includes(e.estado) && sondeo) {
      clearInterval(sondeo);
      sondeo = null;
    }
    return e;
  }

  function iniciarSondeo() {
    if (sondeo) clearInterval(sondeo);
    sondeo = setInterval(() => { refrescarEstado().catch(mostrarError); }, REFRESCO_ESTADO_MS);
  }

  setInterval(() => {
    const cuenta = $("cuenta");
    if (cuenta && !cuenta.hidden) {
      const resta = Math.max(0, RENUEVA_QR_S - Math.floor((Date.now() - qrDesde) / 1000));
      cuenta.textContent = "El código se renueva solo en " + resta + " s.";
    }
  }, 1000);

  async function generar() {
    mostrarEstado(await pedir("POST", base() + "/vinculo", { full_sync: $("full-sync").checked }));
    iniciarSondeo();
  }

  async function reiniciar() {
    mostrarEstado(await pedir("POST", base() + "/vinculo/reiniciar-qr", {}));
  }

  async function pedirCodigo() {
    const r = await pedir("POST", base() + "/vinculo/codigo", { telefono: $("telefono").value.trim() });
    $("codigo").textContent = "Código: " + r.codigo + ". En el teléfono: Dispositivos vinculados → Vincular con número de teléfono.";
  }

  async function desconectar() {
    if (!window.confirm("¿Desconectar esta línea de WhatsApp? El tablero y lo ya importado se conservan.")) return;
    await pedir("POST", base() + "/vinculo/desconectar", { confirmar: true });
    $("aviso").textContent = "Desconexión pedida: la sesión se borra en unos minutos.";
    await refrescarEstado();
    iniciarSondeo();
  }

  async function borrarTodo() {
    if (!window.confirm("Desconectar y borrar todo: se borra la conexión y se pide borrar los datos de esta línea. ¿Seguir?")) return;
    const nombre = window.prompt("Para confirmar, escribí el nombre de la línea: " + actual.nombre);
    if (nombre === null) return;
    await pedir("POST", base() + "/vinculo/desconectar-y-borrar", { confirmar: true, nombre_linea: nombre });
    $("aviso").textContent = "Pedido registrado: la conexión se borra en minutos; el borrado de los datos de la línea queda en curso.";
    await refrescarEstado();
    iniciarSondeo();
  }

  // ---- C1 y C4 (solo Consola)
  const COLUMNAS = ["semaforo", "tenant_nombre", "line_nombre", "numero", "line_estado", "waha_status", "observado_hasta",
                    "ultimo_mensaje", "sincronizacion", "huecos", "salud", "worker", "gasto_ia_mes"];

  function celda(valor) {
    const td = document.createElement("td");
    td.textContent = (valor === null || valor === undefined || valor === "") ? "—" : String(valor);
    return td;
  }

  function pintarTabla(filas) {
    const cuerpo = $("lineas");
    cuerpo.replaceChildren();
    for (const f of filas) {
      const tr = document.createElement("tr");
      tr.className = "semaforo-" + f.semaforo;
      for (const c of COLUMNAS) tr.appendChild(celda(f[c]));
      const td = document.createElement("td");
      const boton = document.createElement("button");
      boton.type = "button";
      boton.textContent = "Operar";
      boton.addEventListener("click", () => abrirPanel(f));
      td.appendChild(boton);
      tr.appendChild(td);
      cuerpo.appendChild(tr);
    }
  }

  function llenarClientes(filas) {
    const sel = $("filtro-cliente");
    if (sel.options.length > 1) return;
    const vistos = new Map();
    for (const f of filas) vistos.set(f.tenant_id, f.tenant_nombre);
    for (const [id, nombre] of vistos) {
      const op = document.createElement("option");
      op.value = id;
      op.textContent = nombre;
      sel.appendChild(op);
    }
  }

  async function cargarLineas() {
    const p = new URLSearchParams();
    if ($("filtro-estado").value) p.set("estado", $("filtro-estado").value);
    if ($("filtro-cliente").value) p.set("tenant_id", $("filtro-cliente").value);
    const filas = await pedir("GET", "/radar/admin/consola/lineas" + (p.toString() ? "?" + p.toString() : ""));
    pintarTabla(filas);
    llenarClientes(filas);
  }

  function abrirPanel(f) {
    actual = { tenant_id: f.tenant_id, line_id: f.line_id, nombre: f.line_nombre };
    $("panel").hidden = false;
    $("panel-titulo").textContent = f.tenant_nombre + " — " + f.line_nombre;
    $("codigo").textContent = "";
    $("aviso").textContent = "";
    $("qr").removeAttribute("src");
    qrDesde = 0;
    cargarConsentimiento().catch(mostrarError);
    refrescarEstado().then(iniciarSondeo).catch(mostrarError);
  }

  async function marcarRestriccion() {
    const valor = $("restriccion-hasta").value;
    mostrarEstado(await pedir("PUT", base() + "/vinculo/restriccion", { hasta: valor ? new Date(valor).toISOString() : null }));
  }

  async function levantarRestriccion() {
    mostrarEstado(await pedir("DELETE", base() + "/vinculo/restriccion"));
  }

  async function cerrarPanel() {
    $("panel").hidden = true;
    if (sondeo) clearInterval(sondeo);
    sondeo = null;
    actual = null;
  }

  // ---- arranque
  enlazar("btn-consentir", consentir);
  enlazar("btn-generar", generar);
  enlazar("btn-reiniciar", reiniciar);
  enlazar("btn-codigo", pedirCodigo);
  enlazar("btn-desconectar", desconectar);
  enlazar("btn-borrar", borrarTodo);

  if (modo === "consola") {
    enlazar("btn-restriccion", marcarRestriccion);
    enlazar("btn-levantar", levantarRestriccion);
    enlazar("btn-refrescar", cargarLineas);
    enlazar("btn-cerrar-panel", cerrarPanel);
    $("filtro-estado").addEventListener("change", () => accion(cargarLineas));
    $("filtro-cliente").addEventListener("change", () => accion(cargarLineas));
    accion(cargarLineas);
    setInterval(() => { cargarLineas().catch(mostrarError); }, REFRESCO_LINEAS_MS);
  } else {
    const linea = new URLSearchParams(window.location.search).get("linea") || "";
    if (!/^[0-9a-f-]{36}$/.test(linea)) {
      mostrarError(new Error("linea_invalida"));
      return;
    }
    actual = { line_id: linea, nombre: "" };
    cargarConsentimiento().catch(mostrarError);
    refrescarEstado().then(iniciarSondeo).catch(mostrarError);
  }
})();
```

- [ ] **Step 5: Router de páginas**

`app/radar/routers/paginas.py`:

```python
"""
Pantallas de Radar (decisión 12 del plan del tramo 2).

HTML estático que no interpola nada; todo dato llega por JSON y el JS lo
escribe con textContent. Por eso la CSP no necesita 'unsafe-inline' ni hashes:
solo el JS y el CSS propios, imágenes propias (el QR) y fetch al mismo origen.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app.radar import auditoria
from app.radar.auth import Sesion, ip_de, requiere_rol
from app.radar.contexto import contexto

router = APIRouter(tags=["radar-paginas"])

ESTATICOS = Path(__file__).resolve().parent.parent / "static"
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
       "form-action 'none'; base-uri 'none'; frame-ancestors 'none'")
CABECERAS_HTML = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                  "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}
_TIPOS = {"radar.js": "text/javascript; charset=utf-8", "radar.css": "text/css; charset=utf-8"}


def _html(nombre: str) -> HTMLResponse:
    return HTMLResponse((ESTATICOS / nombre).read_text(encoding="utf-8"), headers=CABECERAS_HTML)


@router.get("/radar/consola", response_class=HTMLResponse)
async def consola(request: Request, admin: Sesion = Depends(requiere_rol("admin"))):
    ctx = contexto(request)
    async with ctx.db.tenant_tx(admin.tenant_id) as con:
        await auditoria.registrar(con, tenant_id=admin.tenant_id, actor_user_id=admin.user_id, actor_rol=admin.rol,
                                  accion="consola_abierta", tipo_objeto="user", objeto_id=admin.user_id,
                                  ip=ip_de(request))
    return _html("consola.html")


@router.get("/radar/conectar", response_class=HTMLResponse)
async def conectar(dueno: Sesion = Depends(requiere_rol("dueno"))):
    return _html("conectar.html")


@router.get("/radar/estaticos/{nombre}")
async def estatico(nombre: str):
    tipo = _TIPOS.get(nombre)          # lista cerrada: ningún otro archivo se sirve
    if tipo is None:
        raise HTTPException(status_code=404)
    return Response((ESTATICOS / nombre).read_bytes(), media_type=tipo,
                    headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"})
```

En `app/radar/app.py` (`crear_app_radar` registra cada router con una línea `app.include_router`, sin tupla): el import de routers queda

```python
from app.radar.routers import admin, consola, cuenta, health, login, paginas, parametros, soporte, webhook_waha
```

y se agrega, después de `app.include_router(soporte.router)` y de los routers de las tareas anteriores:

```python
    app.include_router(paginas.router)
```

- [ ] **Step 6: Correr**

```bash
python -m pytest tests/radar_tests -q
```
Esperado: todo verde; `test_paginas.py` suma 9.

- [ ] **Step 7: Commit**

```bash
git add app/radar tests/radar_tests/test_paginas.py
git commit -m "Radar tramo 2: pantallas de la Consola KIS y de conexion del duenio con CSP estricta" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 16: P3 del dueño sobre el mismo backend

**Files:**
- Create: `app/radar/routers/vinculo.py`
- Modify: `app/radar/app.py` (router)
- Test: `tests/radar_tests/test_vinculo_cliente.py`

**Interfaces:**
- Consumes: servicios de `app.radar.vinculos`, `texto_para_linea`, `leer_linea`, `sesion_actual`, `requiere_rol`, `ip_de`, piezas de `vinculo_comun`; `POST /radar/api/lineas/{id}/consentimientos` (tramo 1) en los tests.
- Produces: bajo `/radar/api/lineas/{line_id}/vinculo`: `GET ""` (cualquier rol que vea la línea), `GET /texto-consentimiento`, `POST ""` (201), `GET /qr`, `POST /codigo`, `POST /reiniciar-qr`, `POST /desconectar` (202, causa `pedido_dueno`), `POST /desconectar-y-borrar` (202) — todo salvo el `GET ""` exige `requiere_rol("dueno")`. Una línea de otro tenant o fuera de `lineas_permitidas` responde 404.

- [ ] **Step 1: Tests (fallan)**

`tests/radar_tests/test_vinculo_cliente.py`:

```python
"""P3 del dueño (§3 P3): el mismo backend y la misma máquina de estados que la Consola."""
import hashlib
import hmac
import json

from app.radar.vinculos import aplicar_status, marcar_restriccion

from .helpers import HMAC_TEST, crear_linea_directa, crear_usuario, entrar, escenario_vinculable, vincular_de_prueba
from .waha_falso import PNG


def _url(line_id, sufijo=""):
    return f"/radar/api/lineas/{line_id}/vinculo{sufijo}"


async def test_el_dueno_consiente_y_vincula_su_linea(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    nueva = await crear_linea_directa(ctx_waha.db, esc["tenant_id"], "Sucursal")
    await entrar(cliente, ctx_waha, esc["tenant_id"], esc["dueno_id"], "dueno")
    r = await cliente.post(_url(nueva), json={})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "sin_consentimiento"
    r = await cliente.post(f"/radar/api/lineas/{nueva}/consentimientos",
                           json={"version_texto": "v1", "acepta": True, "titular": True})
    assert r.status_code == 201
    r = await cliente.post(_url(nueva), json={"full_sync": False})
    assert r.status_code == 201 and r.json()["estado"] == "esperando_qr"
    qr = await cliente.get(_url(nueva, "/qr"))
    assert qr.content == PNG and qr.headers["cache-control"] == "no-store"


async def test_gestor_ve_el_estado_pero_no_opera(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    gestor = await crear_usuario(ctx_waha.db, esc["tenant_id"], "gestor@cliente.com", "gestor")
    await entrar(cliente, ctx_waha, esc["tenant_id"], gestor, "gestor")
    assert (await cliente.get(_url(esc["line_id"]))).status_code == 200
    assert (await cliente.post(_url(esc["line_id"]), json={})).status_code == 403
    assert (await cliente.post(_url(esc["line_id"], "/desconectar"), json={"confirmar": True})).status_code == 403
    assert waha.llamadas == []


async def test_no_cruza_tenants(cliente, ctx_waha, waha):
    a = await escenario_vinculable(ctx_waha, "Farmacia A")
    b = await escenario_vinculable(ctx_waha, "Farmacia B")
    await entrar(cliente, ctx_waha, b["tenant_id"], b["dueno_id"], "dueno")
    assert (await cliente.get(_url(a["line_id"]))).status_code == 404
    assert (await cliente.post(_url(a["line_id"]), json={})).status_code == 404
    assert waha.llamadas == []


async def test_la_misma_maquina_de_estados_por_webhook(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc, working=False)
    sobre = {"event": "session.status", "session": v["session_name"],
             "metadata": {"tenant_id": str(esc["tenant_id"]), "line_id": str(esc["line_id"]),
                          "link_id": str(v["link_id"])},
             "me": {"id": waha.me_id}, "payload": {"status": "WORKING"}}
    crudo = json.dumps(sobre).encode()
    firma = hmac.new(HMAC_TEST.encode(), crudo, hashlib.sha512).hexdigest()
    await cliente.post("/webhook/waha", content=crudo, headers={"content-type": "application/json",
                                                                  "x-webhook-hmac": firma})
    await entrar(cliente, ctx_waha, esc["tenant_id"], esc["dueno_id"], "dueno")
    estado = (await cliente.get(_url(esc["line_id"]))).json()
    assert (estado["estado"], estado["numero"]) == ("vinculado", "…4567")


async def test_desconectar_pedido_por_el_dueno(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    await entrar(cliente, ctx_waha, esc["tenant_id"], esc["dueno_id"], "dueno")
    assert (await cliente.post(_url(esc["line_id"], "/desconectar"), json={"confirmar": True})).status_code == 202
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        causa = await con.fetchval("SELECT causa FROM jobs WHERE link_id = $1", v["link_id"])
    assert causa == "pedido_dueno"


async def test_restriccion_bloquea_reconectar_desde_el_cliente(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    v = await vincular_de_prueba(ctx_waha, waha, esc)
    async with ctx_waha.db.tenant_tx(esc["tenant_id"]) as con:
        await aplicar_status(con, tenant_id=esc["tenant_id"], link_id=v["link_id"], waha_status="FAILED",
                             origen="webhook")
    await marcar_restriccion(ctx_waha, tenant_id=esc["tenant_id"], line_id=esc["line_id"], hasta=None,
                             actor_user_id=None, actor_rol="admin", ip=None)
    await entrar(cliente, ctx_waha, esc["tenant_id"], esc["dueno_id"], "dueno")
    r = await cliente.post(_url(esc["line_id"]), json={})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "restriccion_activa"


async def test_texto_de_consentimiento_solo_para_el_dueno(cliente, ctx_waha):
    esc = await escenario_vinculable(ctx_waha)
    await entrar(cliente, ctx_waha, esc["tenant_id"], esc["dueno_id"], "dueno")
    t = (await cliente.get(_url(esc["line_id"], "/texto-consentimiento"))).json()
    assert t["version"] == "v1" and "Qué no hacemos" in t["texto"]
    lector = await crear_usuario(ctx_waha.db, esc["tenant_id"], "lector@cliente.com", "lector")
    await entrar(cliente, ctx_waha, esc["tenant_id"], lector, "lector")
    assert (await cliente.get(_url(esc["line_id"], "/texto-consentimiento"))).status_code == 403
```

- [ ] **Step 2: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_vinculo_cliente.py -q
```
Esperado: fallas por `404 Not Found` en `/radar/api/lineas/{id}/vinculo`.

- [ ] **Step 3: Implementar el router**

`app/radar/routers/vinculo.py`:

```python
"""
P3 del dueño (§3 P3) sobre el mismo backend que la Consola KIS: mismos
servicios de app.radar.vinculos y misma máquina de estados. El tenant sale de
la sesión, nunca de la URL. Ver el estado puede cualquier rol que vea la línea;
operar, solo el dueño.
"""

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from app.radar.auth import Sesion, ip_de, requiere_rol, sesion_actual
from app.radar.consentimiento_asistido import texto_para_linea
from app.radar.contexto import RadarContexto, contexto
from app.radar.lineas import leer_linea
from app.radar.routers.vinculo_comun import (BorrarIn, CodigoIn, DesconectarIn, VincularIn, exigir_confirmacion,
                                             exigir_nombre, http_de, respuesta_codigo, respuesta_png)
from app.radar.vinculos import (VinculoRechazado, estado_de_linea, iniciar_vinculo, pedir_codigo, pedir_fin, qr_png,
                                reiniciar_qr)

router = APIRouter(prefix="/radar/api/lineas/{line_id}/vinculo", tags=["radar-vinculo"])


async def _linea_visible(ctx: RadarContexto, sesion: Sesion, line_id: uuid.UUID) -> None:
    if not sesion.puede_ver_linea(line_id):
        raise HTTPException(status_code=404, detail={"error": "linea_inexistente"})
    async with ctx.db.tenant_tx(sesion.tenant_id) as con:
        if await leer_linea(con, line_id) is None:
            raise HTTPException(status_code=404, detail={"error": "linea_inexistente"})


def _actor(sesion: Sesion, request: Request) -> dict:
    return {"actor_user_id": sesion.user_id, "actor_rol": sesion.rol, "ip": ip_de(request)}


@router.get("")
async def estado(line_id: uuid.UUID, request: Request, sesion: Sesion = Depends(sesion_actual)):
    ctx = contexto(request)
    await _linea_visible(ctx, sesion, line_id)
    try:
        return await estado_de_linea(ctx, tenant_id=sesion.tenant_id, line_id=line_id)
    except VinculoRechazado as e:
        raise http_de(e)


@router.get("/texto-consentimiento")
async def texto(line_id: uuid.UUID, request: Request, sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    await _linea_visible(ctx, sesion, line_id)
    async with ctx.db.tenant_tx(sesion.tenant_id) as con:
        return await texto_para_linea(con, sesion.tenant_id, line_id)


@router.post("", status_code=201)
async def vincular(line_id: uuid.UUID, request: Request, body: Optional[VincularIn] = None,
                   sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    await _linea_visible(ctx, sesion, line_id)
    try:
        return await iniciar_vinculo(ctx, tenant_id=sesion.tenant_id, line_id=line_id,
                                     full_sync=body.full_sync if body else False, **_actor(sesion, request))
    except VinculoRechazado as e:
        raise http_de(e)


@router.get("/qr")
async def qr(line_id: uuid.UUID, request: Request, sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    await _linea_visible(ctx, sesion, line_id)
    try:
        return respuesta_png(await qr_png(ctx, tenant_id=sesion.tenant_id, line_id=line_id))
    except VinculoRechazado as e:
        raise http_de(e)


@router.post("/codigo")
async def codigo(line_id: uuid.UUID, body: CodigoIn, request: Request, sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    await _linea_visible(ctx, sesion, line_id)
    try:
        return respuesta_codigo(await pedir_codigo(ctx, tenant_id=sesion.tenant_id, line_id=line_id,
                                                   telefono=body.telefono, **_actor(sesion, request)))
    except VinculoRechazado as e:
        raise http_de(e)


@router.post("/reiniciar-qr")
async def reiniciar(line_id: uuid.UUID, request: Request, sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    await _linea_visible(ctx, sesion, line_id)
    try:
        return await reiniciar_qr(ctx, tenant_id=sesion.tenant_id, line_id=line_id, **_actor(sesion, request))
    except VinculoRechazado as e:
        raise http_de(e)


@router.post("/desconectar", status_code=202)
async def desconectar(line_id: uuid.UUID, request: Request, body: Optional[DesconectarIn] = None,
                      sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    await _linea_visible(ctx, sesion, line_id)
    exigir_confirmacion(body)
    try:
        return await pedir_fin(ctx, tenant_id=sesion.tenant_id, line_id=line_id, causa="pedido_dueno", borrar=False,
                               **_actor(sesion, request))
    except VinculoRechazado as e:
        raise http_de(e)


@router.post("/desconectar-y-borrar", status_code=202)
async def desconectar_y_borrar(line_id: uuid.UUID, request: Request, body: Optional[BorrarIn] = None,
                               sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    await _linea_visible(ctx, sesion, line_id)
    await exigir_nombre(ctx, sesion.tenant_id, line_id, body)
    try:
        return await pedir_fin(ctx, tenant_id=sesion.tenant_id, line_id=line_id, causa="pedido_dueno", borrar=True,
                               **_actor(sesion, request))
    except VinculoRechazado as e:
        raise http_de(e)
```

En `app/radar/app.py` (`crear_app_radar` registra cada router con una línea `app.include_router`, sin tupla): el import de routers queda

```python
from app.radar.routers import admin, consola, cuenta, health, login, paginas, parametros, soporte, vinculo, webhook_waha
```

y se agrega, después de `app.include_router(soporte.router)` y de los routers de las tareas anteriores:

```python
    app.include_router(vinculo.router)
```

- [ ] **Step 4: Correr**

```bash
python -m pytest tests/radar_tests -q
```
Esperado: todo verde; `test_vinculo_cliente.py` suma 7.

- [ ] **Step 5: Commit**

```bash
git add app/radar/routers/vinculo.py app/radar/app.py tests/radar_tests/test_vinculo_cliente.py
git commit -m "Radar tramo 2: P3 del duenio sobre el mismo backend y la misma maquina de estados" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 17: Despliegue, runbook manual contra el GOWS de staging y corrida completa

**Files:**
- Create: `docs/radar-waha-runbook-tramo2.md`
- Modify: `docs/radar-despliegue.md`, `.env.example`
- Test: la suite completa (`python -m pytest -q`).

**Interfaces:**
- Consumes: todo lo anterior y `docs/superpowers/plans/2026-09-21-radar-gows-staging-runbook.md` (contenedor GOWS endurecido).
- Produces: documentación de variables y operación del tramo 2 y un runbook manual para validar contra WAHA real lo que los tests no pueden (nombres de `actions`, eco de `metadata`, `me` en `session.status`, borrado verificable).

- [ ] **Step 1: Sección del tramo 2 en `docs/radar-despliegue.md`**

Agregar al final de `docs/radar-despliegue.md` (y quitar "WAHA y vínculos (tramo 2)" de "Qué NO existe todavía"):

```markdown
## Tramo 2: WAHA, vínculos y Consola KIS

### Variables nuevas

| Variable | Qué es |
|---|---|
| `RADAR_WAHA_WEBHOOK_URL` | URL de `POST /webhook/waha` que alcanza WAHA. En Railway, por red privada: `http://<servicio-radar>.railway.internal:<puerto>/webhook/waha`. Vacía = no se puede vincular (503). |
| `RADAR_WAHA_WEBHOOK_HMAC_KEY` | 32+ caracteres aleatorios (`python -c "import secrets; print(secrets.token_urlsafe(48))"`). Firma los webhooks (sha512). Vacía = el receptor rechaza todo; corta = la app no arranca. |
| `RADAR_WAHA_TIMEOUT_S` | Timeout de cada llamada a WAHA (default 20). |
| `RADAR_WORKER_EMBEBIDO` | `true` (default): la cola de jobs corre dentro del servicio web. Ver "Worker". |

### Servidor WAHA de clientes

- Un servicio aparte por worker, **sin dominio público** (solo red privada de Railway), con el endurecimiento del runbook GOWS (`WAHA_PRINT_QR=False`, `WAHA_PRESENCE_AUTO_ONLINE=False`, `WAHA_SESSION_CONFIG_IGNORE_*=true`, medios apagados, `WAHA_APPS_ENABLED=false`, dashboard y Swagger deshabilitados, clave hasheada) y **sin backups del volumen**.
- Sin `WHATSAPP_HOOK_URL` global: Radar configura el webhook en cada sesión.

### Registrar un worker

En la shell del servicio de Radar, con la clave admin **en claro** del WAHA (la del gestor de contraseñas, nunca en un archivo del repo):

    WAHA_ADMIN_KEY='...' python scripts/radar_workers.py registrar --nombre w1 \
        --base-url http://waha-w1.railway.internal:3000 --engine GOWS --max-sesiones 50 --disco-max-gb 20
    python scripts/radar_workers.py listar
    python scripts/radar_workers.py disco --nombre w1 --usado-gb 3.5

La clave queda en `RADAR_SECRETS_DIR` como `waha_admin__<worker_id>.b64` (0600), junto a las `k_tenant` y a las claves de lectura por vínculo (`waha_lectura__<link_id>.b64`). Nunca en Postgres.

### Worker de la cola

- Por defecto corre dentro del servicio web (`RADAR_WORKER_EMBEBIDO=true`): las claves de WAHA viven en el volumen de `RADAR_SECRETS_DIR` y en Railway un volumen se monta en un solo servicio.
- Si los secretos pasan a un gestor externo: `RADAR_WORKER_EMBEBIDO=false` en el web y un segundo servicio con la misma imagen y el comando `python -m app.radar.worker`.
- Programa el chequeo de salud de cada vínculo vivo cada 5 min y corre los fines de vínculo.

### Pantallas

- Consola KIS: `https://<radar>/radar/consola` (rol `admin`).
- Pantalla del dueño: `https://<radar>/radar/conectar?linea=<line_id>` (rol `dueno`).
```

Agregar al final de `.env.example`:

```ini
# ── Radar tramo 2 (WAHA) ─────────────────────────────────────────────────────
RADAR_WAHA_WEBHOOK_URL=
RADAR_WAHA_WEBHOOK_HMAC_KEY=
RADAR_WAHA_TIMEOUT_S=20
RADAR_WORKER_EMBEBIDO=true
```

- [ ] **Step 2: Runbook manual**

`docs/radar-waha-runbook-tramo2.md`:

````markdown
# Radar tramo 2 — prueba manual contra el WAHA GOWS de staging

**Para:** Mariano · **Cuándo:** después de que `python -m pytest tests/radar_tests -q` esté en verde.
**Con qué:** el contenedor GOWS del runbook `docs/superpowers/plans/2026-09-21-radar-gows-staging-runbook.md` y **una línea de prueba con consentimiento** (nunca la línea personal ni la de un cliente).
**Reglas:** no uses herramientas MCP de WAHA; no pegues claves, QR, códigos ni números en ningún chat ni archivo del repo; anotá solo estados y sí/no.

## 0. Preparación

1. Radar local contra un Postgres local (ver `docs/radar-despliegue.md`), con `APP_MODE=radar`, `RADAR_COOKIE_SECURE=false`, `RADAR_PUBLIC_BASE_URL=http://localhost:8000` y `RADAR_WAHA_WEBHOOK_HMAC_KEY` generada al momento.
2. Túnel para que WAHA llegue al receptor: `cloudflared tunnel --url http://localhost:8000`. Poné `RADAR_WAHA_WEBHOOK_URL=https://<túnel>/webhook/waha` y reiniciá Radar.
3. Registrá el worker (la clave en claro solo en la variable de la shell):

       export WAHA_ADMIN_KEY='...'
       python scripts/radar_workers.py registrar --nombre gows-staging --base-url https://TU-DOMINIO --engine GOWS --max-sesiones 5 --disco-max-gb 5

4. Creá el primer admin (`scripts/radar_admin.py crear-admin`), entrá con el link, y creá un cliente de prueba con su línea (`POST /radar/admin/tenants`).

## 1. Consola: consentimiento asistido y QR

1. Abrí `http://localhost:8000/radar/consola`, tocá **Operar** en la línea.
2. Marcá la aceptación, elegí el modo, escribí un nombre y registrá. Esperado: aviso de copia enviada (con `RADAR_MAILER=log`, en el log aparece solo `*@dominio` y la huella).
3. **Generar QR.** Esperado: el QR aparece y se renueva solo.
4. Verificá la sesión con la clave admin (sin copiar la salida a ningún lado):

       curl -s -H "X-Api-Key: $WAHA_ADMIN_KEY" https://TU-DOMINIO/api/sessions?all=true | python -c "import json,sys; [print(s['name'], s['status']) for s in json.load(sys.stdin)]"
       curl -s -H "X-Api-Key: $WAHA_ADMIN_KEY" https://TU-DOMINIO/api/sessions/v_XXXXXXXXXXXX | python -c "import json,sys; c=json.load(sys.stdin)['config']; print(sorted(c)); print(c.get('ignore')); print(c.get('gows')); print(sorted(c.get('metadata', {}))); print([ (w['url'][:30], sorted(w['events'])) for w in c['webhooks']])"

   Esperado: `ignore` con los cuatro en `true`, `gows.storage` como lo pidió Radar, `metadata` con `link_id`, `line_id`, `tenant_id`, y un único webhook a tu túnel con los cinco eventos.
5. En los logs del contenedor (Railway → Deployments → Logs) **no** tiene que aparecer el QR (`WAHA_PRINT_QR=False`).

| Punto a validar | Resultado (sí/no) |
|---|---|
| La sesión se llama `v_` + 12 hex y no hay otras sesiones de Radar vivas | |
| `ignore`, `storage`, `metadata` y webhook coinciden | |
| El QR no aparece en los logs de WAHA | |

## 2. Clave de solo lectura

    curl -s -H "X-Api-Key: $WAHA_ADMIN_KEY" https://TU-DOMINIO/api/keys | python -c "import json,sys; [print(k.get('id'), k.get('session'), k.get('isAdmin'), k.get('actions')) for k in json.load(sys.stdin)]"

| Punto a validar | Resultado |
|---|---|
| Hay exactamente una clave para la sesión, con `isAdmin: false` | |
| `actions` quedó como lo mandó Radar (`read: true`, el resto `false`) y **no** en `null` | |
| `POST /api/keys` devolvió el valor en el campo `key` (si Radar abortó con `waha_error`, este es el primer sospechoso) | |

Si los nombres de `actions` no son los que usa WAHA, corregí `ACCIONES_CLAVE_LECTURA` en `app/radar/waha/sesion.py` y su test **antes** del piloto.

## 3. Escaneo y estados

1. Escaneá con el teléfono de prueba. Esperado: "Conectado: …NNNN. ¿Es esta la línea del negocio?".
2. En C1 la línea aparece en verde con el sufijo y el worker `gows-staging (1/5)`.
3. En la base: `SELECT waha_status, origen FROM link_status_events ORDER BY id` (como dueño de las tablas) muestra `WORKING` con origen `webhook`.

| Punto a validar | Resultado |
|---|---|
| Los `session.status` llegan por webhook (origen `webhook`, no solo `polling`) | |
| El `session.status` trae `metadata` (si no, el receptor lo ignora y el estado avanza solo por polling y chequeo) | |
| `WORKING` trae `me.id` (si no, no se muestra el sufijo) | |

## 4. Código de vinculación y QR vencido

1. Desconectá (paso 6), vinculá de nuevo y usá "vincular con código" con el número de la línea de prueba. Esperado: código `ABCD-EFGH`; el QR sigue disponible si falla.
2. Vinculá de nuevo y **no escanees** durante ~3 min. Esperado: "El código venció. Generá uno nuevo."; hasta 3 reintentos; al cuarto, el mensaje de ayuda.

| Punto a validar | Resultado |
|---|---|
| El código funciona y convive con la rotación del QR | |
| Tras el vencimiento, WAHA emite `FAILED` (o `STOPPED`) y el reinicio muestra un QR nuevo | |

## 5. Desvínculo desde el teléfono

1. Con la línea vinculada, quitá el dispositivo desde el teléfono (Dispositivos vinculados).
2. Esperado en ≤ 5 min: la línea en rojo, estado "Tu WhatsApp se desconectó el DD/MM. Tocá «Reconectar»." y, en el log del mailer, un aviso de caída al dueño (solo `*@dominio`).
3. **Reconectar** desde la Consola, escanear, y verificar que el vínculo anterior pasa a `cerrado` con causa `reemplazado`.

| Punto a validar | Resultado |
|---|---|
| Qué estado emite GOWS al quitar el dispositivo (`FAILED`, `STOPPED`, `SCAN_QR_CODE`) | |
| El vínculo viejo se cierra al llegar el nuevo a `WORKING` | |

## 6. Desconectar y verificar el borrado

1. **Desconectar** desde la Consola. Esperado en ≤ 1 min: estado "Desconectado.", email de aviso (en el log).
2. Verificá:

       curl -s -o /dev/null -w "%{http_code}\n" -H "X-Api-Key: $WAHA_ADMIN_KEY" https://TU-DOMINIO/api/sessions/v_XXXXXXXXXXXX   # 404
       curl -s -H "X-Api-Key: $WAHA_ADMIN_KEY" https://TU-DOMINIO/api/keys | python -c "import json,sys; print(sum(1 for k in json.load(sys.stdin) if k.get('session','').startswith('v_')))"   # 0

3. En la shell del contenedor WAHA: `ls /app/.sessions` no tiene el directorio de esa sesión.
4. En el teléfono, el dispositivo ya no figura.
5. En los logs de WAHA no hay ningún `POST .../logout`.

| Punto a validar | Resultado |
|---|---|
| 404 de la sesión y cero claves de sesiones `v_` | |
| Directorio de la sesión borrado del volumen | |
| El dispositivo desapareció del teléfono (`desvinculo_confirmado = true` en `links`) | |

## 7. Restricción y admisión

1. En la Consola, **Marcar restricción** sin fecha; **Reconectar** tiene que dar "La cuenta tiene una restricción activa"; **Desconectar** tiene que funcionar. Levantala.
2. `python scripts/radar_workers.py disco --nombre gows-staging --usado-gb 4` (80 % de 5 GB) y generá QR en otra línea. Esperado: "En este momento no hay lugar para una conexión nueva. Probá de nuevo más tarde o escribinos." y ninguna sesión nueva en WAHA. Volvé el disco a 0.

## 8. Cierre

- Borrá todas las sesiones `v_` que hayan quedado (Desconectar desde la Consola) y verificá el paso 6.
- Anotá los resultados de las tablas en el plan del spike (sin identificadores). Cualquier "no" en los pasos 2 y 3 bloquea el piloto hasta corregir el código.
````

- [ ] **Step 3: Corrida completa**

```bash
python -m pytest -q
```
Esperado: toda la suite en verde, la del bot y la de Radar. `tests/radar_tests` suma 200 tests nuevos (9 + 12 + 23 + 17 + 10 + 7 + 29 + 9 + 17 + 10 + 10 + 7 + 9 + 15 + 9 + 7) sobre los 206 que el tramo 1 real tiene en `tests/radar_tests` (`pytest --collect-only`): 406, con el mismo `skipped` de Windows del tramo 1.

- [ ] **Step 4: Commit**

```bash
git add docs/radar-despliegue.md docs/radar-waha-runbook-tramo2.md .env.example
git commit -m "Radar tramo 2: documentacion de despliegue y runbook manual contra el GOWS de staging" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Self-Review

### (a) Cobertura del spec (tramo 2 de §8 y §3.1)

| Requisito | Dónde queda |
|---|---|
| Gestor de sesiones WAHA; clave admin solo en ese módulo y nunca en el navegador (§3 P3, §6.1) | Task 3 (`WahaCliente`), Task 5 (`gestor`), Task 6 (`cliente_de` único lector; test `test_la_clave_admin_solo_se_lee_en_workers`) |
| Lista blanca de rutas; sin send, read, presence, logout; test que lo verifica (§6.1) | Task 3 (`RUTAS_PERMITIDAS`; `test_rutas_fuera_de_la_lista_blanca_no_salen`, 12 rutas prohibidas, incluido `logout`, `sendSeen`, `presence`, `chats/overview`, lecturas de mensajes) |
| Cuerpo de P3: `v_<id>`, metadata tenant/line/link, `ignore`, `markOnline:false`, webhooks con HMAC y reintentos; bloque noweb o gows según `waha_workers.engine`; sin `deviceName` | Task 4 (`cuerpo_sesion`, test de igualdad exacta), Task 9 (`iniciar_vinculo` usa `worker.engine`) |
| Verificación posterior que aborta antes del QR | Task 4 (`verificar_config`, 10 diferencias), Task 5 (`crear_sesion_verificada` borra y lanza), Task 9 (`test_config_distinta_aborta_sin_qr`) |
| Clave de solo lectura por sesión con `actions` explícito | Task 3 (`crear_clave` rechaza `actions` vacío), Task 4 (`ACCIONES_CLAVE_LECTURA`), Task 9 (valor al `SecretStore`, `key_id` en la base) |
| QR por endpoint autenticado, sin loguear ni guardar; código de vinculación | Task 9 (`qr_png`, `pedir_codigo`), Task 14/16 (`no-store`), Task 3 (`test_no_loguea_codigo_ni_numero`), Task 9 (`test_codigo_de_vinculacion_no_se_guarda`) |
| Fin de vínculo: un único DELETE + borrado de claves + verificación 404, nunca logout; start previo si STOPPED/FAILED sin restricción; paso fallido no bloquea | Task 5 (`terminar_sesion`), Task 10 (job, reintento, `desvinculo_confirmado`, aviso con "Dispositivos vinculados") |
| Admisión por capacidad (80 % sesiones / 70 % disco) con `waha_workers` | Task 2 (tabla + `radar_admin_ocupacion_workers`), Task 6 (`admite`, `elegir`, `elegir_worker`), Task 9 (`test_sin_capacidad_no_crea_sesion_ni_muestra_qr`) |
| `links` (`observado_hasta`, `restriccion_hasta`, estado, `desvinculo_confirmado`…), `link_status_events`, `waha_workers`; estados de línea `vinculada|sin_vinculo|de_baja`; `tenant_id`, RLS, `tenant_tx`; entre tenants por `SECURITY DEFINER` | Task 2 (r0003; 4 funciones `SECURITY DEFINER`), Task 9/10 (transiciones de `lines.estado`) |
| Receptor `POST /webhook/waha` solo para `session.status`, HMAC sha512 fail-closed, resolución por metadata, respuesta rápida; `message.*` descartados sin persistir | Task 13 (9 tests, incluido `test_mensajes_se_descartan_sin_persistir_nada` y `test_no_loguea_el_cuerpo`) |
| Máquina de estados: STARTING, SCAN_QR_CODE con rotación, WORKING, FAILED con hasta 3 reinicios, PASSKEY_REQUIRED defensivo; polling de respaldo a 20 s | Task 7 (`transicion`, 19 casos), Task 9 (`reiniciar_qr`, `estado_de_linea`), Task 15 (rotación y cuenta regresiva en la UI) |
| Chequeo de salud cada 5 min; caída > 72 h → fin; restricción bloquea reconectar pero no el fin | Task 11, Task 12 (`bucle` programa cada 300 s), Task 9 (`restriccion_activa`), Task 10 (`test_con_restriccion_no_hay_start_pero_se_borra`), Task 14 (`test_restriccion_bloquea_reconectar_pero_no_desconectar`) |
| Worker de jobs en Postgres con `FOR UPDATE SKIP LOCKED` | Task 2 (`radar_jobs_reclamar`), Task 8, Task 12 |
| Consola C1 (semáforo, filtros, polling 15 s; campos de tramos 3/5 como "—") | Task 2 (`radar_admin_consola_lineas`), Task 14 (`fila_consola`, `PENDIENTES`), Task 15 |
| Consola C2: consentimiento asistido (`cargado_por`, `modo='asistido'`, copia por email) + QR + estado en vivo | Task 2 (columnas + `CHECK`), Task 14, Task 15 |
| Consola C4: reconectar, desconectar, desconectar y borrar (parte WAHA, doble confirmación), todo auditado | Task 9 (`pedir_fin`), Task 14 (`exigir_confirmacion`, `exigir_nombre`, `test_las_acciones_quedan_auditadas_con_el_admin`), Task 15 |
| UI escapando todo y con CSP estricta | Task 15 (HTML estático, `textContent`, CSP sin `unsafe-inline`, tests de sumideros y de inline) |
| P3 del dueño sobre el mismo backend y la misma máquina de estados | Task 16 (`test_la_misma_maquina_de_estados_por_webhook`) |
| §9 "QR mostrado → WORKING" y "mediana P0 → WORKING" | Task 9 (`vinculo_iniciado`, `vinculo_working` con `segundos`) |
| §9 "Sesiones de WAHA vivas de vínculos ya terminados = 0" | Task 10 (verificación 404 y cero claves; si no, reintento), Task 11 (QR abandonado y caída > 72 h encolan el fin) |
| Tests con WAHA falso; runbook manual contra GOWS | Todas las tasks (`WahaFalso`), Task 17 |
| Vínculo caído: banner con fecha y email al dueño; un vínculo que cierra no avisa | Task 9 (`aplicar_status` encola `aviso_caida`; `test_caida_de_un_vinculado_encola_el_aviso_y_la_de_uno_que_cierra_no`), Task 10 (`avisar_caida`, `test_aviso_de_caida_con_la_fecha`), Task 12 (`HANDLERS["aviso_caida"]`), Task 15 (`diaMes(e.caido_desde)`) |
| Restricción: el sistema no reinicia la sesión mientras está activa | Task 9 (`reiniciar_qr` → `409 restriccion_activa`; `test_con_restriccion_activa_no_se_reinicia_el_qr`) |
| Admisión: no mostrar promesas que el código no cumple | Task 15 (`MENSAJES.sin_capacidad` sin "te avisamos por email"; `test_textos_del_js_no_prometen_lo_que_el_codigo_no_hace`) |
| Ningún vínculo queda trabado en `creando` | Task 2 (`radar_jobs_programar_salud` incluye `creando`), Task 8 (`test_programar_salud_solo_vinculos_vivos`), Task 11 (`CREANDO_HUERFANO`, dos tests) |
| Privilegios mínimos de `radar_app` (criterio del tramo 1) | Task 2 (`UPDATES_POR_COLUMNA`, `borrado_solicitado_at`, `test_grant_update_no_alcanza_columnas_de_identidad`) |
| Semántica de consentimientos del tramo 1 (`parametros_propuestos_at`) | Task 14 (`tenant_ya_consentido`, `test_asistido_arrastra_la_propuesta_de_tenant_ya_aceptada`) |
| Un mail que falla no rompe una operación confirmada | Task 10 (`_avisar`, `test_mail_que_falla_no_rompe_el_fin`), Task 14 (`test_copia_que_no_sale_no_rompe_el_consentimiento`) |
| La Consola no opera sobre el tenant KIS ni sobre tenants inexistentes | Task 14 (`_tenant_cliente`, `test_rutas_de_linea_solo_sobre_un_tenant_cliente`) |

**Fuera de alcance (a propósito):** pasada de conteo, P4 (selección de chats), backfill, reconciliación, `observado_hasta` en movimiento, "sospecha de silencio" y su reinicio automático, ingesta de `message.*` y `webhook_inbox` (tramo 3); KPI y C3 (tramo 4); IA y gasto de IA del mes (tramo 5); purgas, "borrar todo" sobre nuestra base con constancia, recordatorio semestral y baja de líneas (tramo 6). También quedan fuera P1 del cliente (decisión 14), "reintentar sincronización", "re-analizar" y "mover de worker" de C4 (dependen de los tramos 3 y 5), la detección automática de la restricción de cuenta (decisión 8) y el modo sin copia en WAHA (segunda etapa).

### (b) Placeholders

Revisado: no hay `TBD`, `TODO`, "similar a la tarea N" ni pasos sin código. Las únicas modificaciones descritas en prosa sobre archivos del tramo 1 son inserciones puntuales con el bloque exacto (constantes, campos de `RadarSettings`, imports, el import de routers y la línea `app.include_router` de cada router nuevo en `app.py`) o reemplazos completos de una función o constante (`validar_settings`, `_lifespan_radar`, `ACCIONES`…, `_validar_valor`, `EVENTOS`, `TABLAS`, `TABLAS_TENANT`, `RadarContexto`). La mudanza de `_tenant_ya_consentido` (Task 14) trae la función completa en su lugar nuevo y lista cada línea que cambia en `routers/parametros.py`. La enmienda del 2026-09-24 no deja placeholders: cada corrección tiene su código y, si cambia comportamiento, su test. Los `[VALIDAR]` del spec que el código no puede cerrar están en el runbook de la Task 17 como puntos a verificar, con qué corregir si fallan.

### (c) Consistencia de nombres con el tramo 1

Se usan tal cual: `RadarDB.tenant_tx`/`sin_tenant`, `politica_por_tenant`, `grants_app`, `definir_funcion_admin`, `radar_tenant_actual()`, `TENANT_KIS`/`TENANT_KIS_STR`, `RadarContexto` (se agrega un campo con default, sin romper a quien lo construye), `RadarSettings` (prefijo `RADAR_`), `validar_settings`, `construir_contexto`, `crear_app_radar`, `contexto(request)`, `auditoria.registrar` (con `ACCIONES`, `TIPOS_OBJETO`, `CLAVES_DETALLE`, `DetalleProhibido`, `validar_detalle`), `eventos_producto.registrar_evento` (`EVENTOS`), `Email`/`MemoryMailer`/`ctx.mailer.enviar`, `SecretStore` (`get/set/delete`, nombres `^[a-z0-9_:-]{1,120}$`: `waha_admin:<uuid>` y `waha_lectura:<uuid>` cumplen), `Sesion` (`user_id`, `tenant_id`, `rol`, `puede_ver_linea`), `sesion_actual`, `requiere_rol`, `ip_de`, `normalizar_e164`/`TelefonoNoSoportado`, `VERSIONES`/`hash_texto` (`app.radar.consentimiento`), `leer_linea` (`app.radar.lineas`), `DE_LINEA` (`app.radar.parametros`), `a_json`/`leer_parametros_tenant` (`app.radar.parametros_service`), el endpoint `POST /radar/api/lineas/{id}/consentimientos` con `ConsentimientoIn(version_texto, acepta, titular, …)`, las fixtures `radar_urls`, `radar_db`, `radar_ctx`, `cliente` y los helpers `crear_tenant_directo`, `crear_usuario`, `crear_linea_directa`, `entrar`. La cookie `radar_sesion` tiene `Path=/radar`, por eso todas las pantallas y APIs nuevas con sesión cuelgan de `/radar/…`; `/webhook/waha` no usa cookie. Los tests del tramo 1 que este plan toca (`test_esquema.py::TABLAS_TENANT`, `conftest.py::TABLAS`) se modifican explícitamente en la Task 2; `test_modo_radar_no_monta_bot` sigue pasando porque `/webhook/waha` ≠ `/webhook`.

Enmienda 2026-09-24, contra el tramo 1 ya implementado: los nombres de arriba se verificaron en `app/radar/`, `migrations_radar/versions/r0001|r0002` y `tests/radar_tests/`. Además se usan `UPDATES_POR_COLUMNA` (patrón de r0002), `radar_urls["super"]` (`conftest.py`), `MailerQueFalla` (`helpers.py`), `leer_propuesta_tenant`, `lineas_vivas_sin_consentir` y `coincide_con_propuesta` (`parametros_service.py`), y el chequeo `SELECT … FROM tenants WHERE id = $1 AND NOT es_kis` de `routers/admin.py`. `app.py` registra los routers con una línea `app.include_router` por router (sin tupla), `test_esquema.py` tiene 14 tests y `tests/radar_tests` recoge 206.

### (d) Verificación de los bloques

Los bloques se extrajeron a un árbol temporal fuera del repo. Resultado: los tests que no dependen de la base ni del tramo 1 (Tasks 3, 4, 5 y 7) corren contra el código del plan con `79 passed` (23 + 17 + 10 + 29, los mismos conteos de sus "Esperado"); los 56 bloques Python parsean; `radar.js` pasa `node --check`; y los chequeos estáticos de la Task 15 (sin sumideros de HTML, sin inline, ids del JS presentes en `consola.html` y los del modo cliente en `conectar.html`) se verificaron sobre los archivos extraídos. Los tests con base (pgserver + roles) y los que importan módulos del tramo 1 no se pudieron correr porque el tramo 1 todavía no está implementado en ninguna rama: sus conteos salen de contar funciones y casos parametrizados, y se confirman al ejecutar el plan.

Enmienda 2026-09-24: los 61 bloques Python del plan enmendado parsean y `radar.js` pasa `node --check`. Las Tasks 3, 4, 5 y 7 no cambiaron. Los tests nuevos o modificados de la enmienda usan base y el tramo 1, así que se confirman al ejecutar el plan (el tramo 1 ya existe en esta rama; el total de 206 de `tests/radar_tests` sale de `pytest --collect-only`).

### (e) Decisiones abiertas para el dueño

1. **Gestor de secretos externo** (heredada del tramo 1): con él, el worker pasa a proceso propio (`RADAR_WORKER_EMBEBIDO=false`); mientras tanto, web y worker comparten el volumen de secretos en un solo servicio.
2. **Detección automática de la restricción de cuenta** (`reachoutTimelock`/`messageCapping`): sumar esas rutas a la lista blanca cuando el spike confirme su semántica; hoy es manual desde C4.
3. **Medición del disco de cada worker** (punto 13 del spike): hoy se carga a mano; la admisión por disco es tan buena como ese dato.
4. **Variantes del texto de consentimiento** para parámetros distintos de los iniciales ("la conexión dura N días", purga): hoy hay una sola versión (`v1`) y la pantalla muestra los parámetros al lado.
5. **Plazo del QR abandonado** (30 min) y **cantidad de reinicios** (3): valores de este plan, a calibrar con el punto 8 del spike.
6. **Proveedor de email transaccional** (heredada del tramo 1): la copia del consentimiento asistido y los avisos de fin y de caída del vínculo usan el `Mailer`; con `log` no le llegan al dueño.
7. **Aviso cuando se libera capacidad** (§6.2, Admisión: "te avisamos por email"): en este tramo no hay mecanismo que registre el pedido rechazado y avise después, así que el texto no lo promete (enmienda, punto 11). Implementarlo (pedido pendiente + aviso desde el worker) o dejar el texto actual.
8. **Worker embebido por defecto** (decisión 15): §6.2 pide un proceso aparte; se mantiene embebido por el volumen de secretos, reversible con `RADAR_WORKER_EMBEBIDO=false`. Necesita el OK explícito del dueño o un cambio en §6.2 antes de ejecutar.
9. **Plazo de un vínculo huérfano en `creando`** (15 min, `salud.CREANDO_HUERFANO`): valor de este plan.
