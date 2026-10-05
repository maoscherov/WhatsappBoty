# Radar — despliegue propio (tramo 1)

Radar corre con la misma imagen que el bot, en un servicio de Railway aparte,
con `APP_MODE=radar`, Postgres y Redis propios. Nunca comparte la base de un
cliente de Remedia (spec §6.2, S7). En este tramo, Redis no se usa todavía.

## Variables

| Variable | Qué es |
|---|---|
| `APP_MODE=radar` | Monta los routers de Radar y ninguno del bot. Por defecto `bot` (`app/config.py`). |
| `RADAR_MIGRATOR_DATABASE_URL` | Rol dueño de las tablas (en Railway, el usuario por defecto). Solo lo usa Alembic en el arranque. |
| `RADAR_DATABASE_URL` | Rol `radar_app`: `NOSUPERUSER`, `NOBYPASSRLS`, no dueño. La app aborta si conecta con un superusuario o con `BYPASSRLS`. |
| `RADAR_BOOTSTRAP_ROLES` | `true`: el arranque crea `radar_admin` y `radar_app` si faltan y le pone a `radar_app` la contraseña de `RADAR_DATABASE_URL`, sin `psql` (ver "Roles al arrancar"). Con ella, `RADAR_DATABASE_URL` tiene que ser del usuario `radar_app`, con una contraseña de 16+ caracteres, y puede escribirse con las referencias de Railway: `postgresql://radar_app:<contraseña>@${{Postgres.PGHOST}}:${{Postgres.PGPORT}}/${{Postgres.PGDATABASE}}`. Default `false`: los roles se crean a mano con `scripts/radar_bootstrap_roles.sql`, como antes (ver "Primera vez"). |
| `RADAR_FUENTE_DATABASE_URL` | Postgres del almacén de fuente permanente (servidor distinto del de resultados, con backups). En este tramo solo tiene la tabla `fuente_meta`. |
| `RADAR_SECRETS_DIR` | Directorio de `FileSecretStore` para las `k_tenant`: un volumen de Railway montado ahí, fuera de los backups de Postgres. Default `/data/radar-secrets`. |
| `RADAR_COOKIE_SECRET` | 32+ caracteres aleatorios para firmar la cookie de sesión (`python -c "import secrets; print(secrets.token_urlsafe(48))"`). Con menos de 32 la app no arranca (`validar_settings`, `app/radar/app.py`). |
| `RADAR_COOKIE_SECURE` | `true` en producción (default). `false` solo para http local. |
| `RADAR_PUBLIC_BASE_URL` | Base de los links mágicos, ej. `https://radar.keepitsimple.com.ar`. Default `http://localhost:8000`. |
| `RADAR_MAILER` | `log` (default: no manda nada, loguea solo dominio y huella del token), `memoria` (tests) o `smtp` (manda de verdad; ver "Email por SMTP"). Otro valor: la app no arranca. |
| `RADAR_REMITENTE` | Remitente (`From`) de los emails; puede ser `Nombre <dir@dominio>`. Default `radar@keepitsimple.com.ar`. Con `smtp`, el proveedor tiene que dejar usar esa dirección al usuario de `RADAR_SMTP_USUARIO`. |
| `RADAR_SMTP_HOST` | Servidor SMTP: solo el nombre, sin puerto ni `smtp://`. Obligatoria con `RADAR_MAILER=smtp`. |
| `RADAR_SMTP_PORT` | Puerto del servidor SMTP. Default `587` (con `RADAR_SMTP_SEGURIDAD=ssl`, en general `465`). |
| `RADAR_SMTP_USUARIO` | Usuario con el que se autentica. Vacío = sin autenticar (relay de una red privada). |
| `RADAR_SMTP_PASSWORD` | Contraseña de ese usuario (de aplicación o clave SMTP del proveedor). Obligatoria si hay usuario; solo ASCII. Nunca va a un log, a la base, a una respuesta HTTP ni a un mensaje de error, y no sale en el `repr` de los settings. |
| `RADAR_SMTP_SEGURIDAD` | `starttls` (default: conecta y sube a TLS antes de autenticar), `ssl` (TLS desde el primer byte) o `ninguna` (sin cifrar: solo para un relay sin autenticar dentro de una red privada; junto con `RADAR_SMTP_USUARIO` la app no arranca). Con TLS se verifica el certificado y el nombre del servidor. Otro valor: la app no arranca. |
| `RADAR_SMTP_TIMEOUT_S` | Timeout de cada operación del socket (conectar y cada comando). Default `20.0`. |

Todas estas variables las lee `RadarSettings` (`app/radar/settings.py`, prefijo
`RADAR_`); `RADAR_MIGRATOR_DATABASE_URL` la lee `app.radar.migrate` a través del
mismo objeto. `validar_settings()` exige `database_url`, `migrator_database_url`,
`fuente_database_url` y un `cookie_secret` de 32+ caracteres antes de arrancar el
lifespan.

## Email por SMTP

Con `RADAR_MAILER=smtp`, Radar manda los mails (links de acceso, invitaciones, copia
del consentimiento y avisos del vínculo) por SMTP con `smtplib` (`SmtpMailer`,
`app/radar/mailer.py`): sirve para Google Workspace, Resend, Amazon SES o Brevo sin
atarse a ninguno. Es una conexión por mail, en un hilo aparte para no frenar el servicio.

`validar_settings()` frena el arranque, antes de las migraciones, si falta
`RADAR_SMTP_HOST`, si hay usuario sin contraseña, si `RADAR_SMTP_SEGURIDAD` no es
`starttls`, `ssl` o `ninguna`, si hay `ninguna` con usuario (la contraseña viajaría
sin cifrar) o si el usuario o la contraseña tienen caracteres que no son ASCII
(`smtplib` no autentica con otros). Los mensajes nombran la variable, nunca el valor.
Radar no prueba la conexión al arrancar: la primera señal es el primer mail (pedí un
link en `/radar/login` con un admin que ya exista y mirá el log). `scripts/radar_admin.py
crear-admin` no usa SMTP aunque `RADAR_MAILER=smtp` esté en el entorno: sigue imprimiendo
el link en la terminal.

Los avisos del vínculo (caída y fin) los manda la cola de jobs: si corre como proceso
aparte (`python -m app.radar.worker`, ver "Worker de la cola"), ese servicio necesita las
mismas `RADAR_MAILER`, `RADAR_REMITENTE` y `RADAR_SMTP_*` que el web; sin ellas usa `log`
y esos avisos no salen.

**Google Workspace.** `RADAR_SMTP_HOST=smtp.gmail.com`, `RADAR_SMTP_PORT=587`,
`RADAR_SMTP_SEGURIDAD=starttls`, `RADAR_SMTP_USUARIO` = la cuenta que manda (por
ejemplo `radar@keepitsimple.com.ar`) y `RADAR_SMTP_PASSWORD` = una **contraseña de
aplicación** de esa cuenta (hace falta la verificación en dos pasos; la contraseña
normal de la cuenta no sirve). `RADAR_REMITENTE` tiene que ser esa cuenta o un alias
suyo: con otra dirección, Google cambia el remitente o rechaza el mail.

**Qué mirar en el log.** Si salió: `email enviado a *@dominio asunto=... huella=...`
(el mismo formato que con `log`). Si no salió, quien mandaba (`link no enviado (...)`,
`copia del consentimiento ... no enviada`, `aviso del vínculo ... no enviado`)
escribe solo el nombre del tipo de error, nunca su texto (puede traer el email
completo o la respuesta del servidor), la contraseña ni el cuerpo:

- `SMTPAuthenticationError`: usuario o contraseña.
- `SMTPSenderRefused`: `RADAR_REMITENTE` no está permitido para ese usuario.
- `SMTPRecipientsRefused`: el servidor rechazó al destinatario.
- `SSLError`, `SMTPServerDisconnected`: la seguridad no corresponde al puerto (587 con
  `starttls`, 465 con `ssl`).
- `SMTPNotSupportedError`: el servidor no ofrece STARTTLS o no ofrece autenticación;
  revisá `RADAR_SMTP_SEGURIDAD` y `RADAR_SMTP_USUARIO`.
- `TimeoutError`, `ConnectionRefusedError`, `gaierror`: host, puerto o red (revisá
  también que el plan del hosting permita SMTP saliente).

## Roles al arrancar

Con `RADAR_BOOTSTRAP_ROLES=true`, el arranque (`asegurar_roles`, `app/radar/bootstrap.py`) hace el paso 1 de
"Primera vez" antes de las migraciones, sin `psql`:

- Crea `radar_admin` y `radar_app` si faltan, con los atributos de `scripts/radar_bootstrap_roles.sql`. A un
  rol que ya existe no le cambia ningún atributo.
- Le cede `radar_admin` al rol de `RADAR_MIGRATOR_DATABASE_URL` (`GRANT radar_admin TO CURRENT_USER`), como
  el script con `:migrator`.
- Le pone a `radar_app` la contraseña de `RADAR_DATABASE_URL` solo si hace falta: si lo acaba de crear, o si
  `radar_app` no entra con esa URL (por ejemplo, porque se cambió la contraseña en la variable).

Necesita que el rol de migración pueda crear roles: superusuario (en Railway, `postgres`) o `CREATEROLE`. Si
no puede, deja un aviso en el log y no crea nada (las migraciones fallan después con "falta el rol
radar_app"). Con `CREATEROLE` sin superusuario, desde Postgres 16 solo puede cambiar la contraseña de un
`radar_app` que creó él o sobre el que tiene `ADMIN` (hasta Postgres 15, `CREATEROLE` alcanza): si no puede, el
`ALTER ROLE` falla y la app no arranca, con un error que lo dice; la contraseña se fija a mano.

En Railway:

    RADAR_BOOTSTRAP_ROLES=true
    RADAR_MIGRATOR_DATABASE_URL=${{Postgres.DATABASE_URL}}
    RADAR_DATABASE_URL=postgresql://radar_app:<contraseña>@${{Postgres.PGHOST}}:${{Postgres.PGPORT}}/${{Postgres.PGDATABASE}}

La contraseña la elige quien despliega: 16+ caracteres, por ejemplo
`python -c "import secrets; print(secrets.token_urlsafe(32))"`, que no hace falta codificar. Si tiene
caracteres reservados (`@`, `:`, `/`, `%`...), van codificados en la URL (`@` es `%40`) y el rol queda con la
contraseña decodificada, la que mandan asyncpg y libpq al conectar. Con otro usuario que `radar_app` (las
migraciones le otorgan los permisos por nombre) o con una contraseña más corta, la app no arranca.

El log dice qué hizo (`RADAR_BOOTSTRAP_ROLES: roles creados: ...; contraseña de radar_app: ...`), nunca la
contraseña ni las URLs; los errores nombran la variable, nunca el valor. La contraseña tampoco llega al
servidor: el arranque calcula su verificador (con el `password_encryption` del servidor, por defecto
SCRAM-SHA-256, y una sal al azar, como `\password` de psql) y el `ALTER ROLE` lleva solo eso. Aunque el servidor
guarde las sentencias (`log_statement`, `log_min_duration_statement`, `pg_stat_statements`, pgaudit) o una
sentencia fallida, ahí queda el verificador, nunca la contraseña. Sin la variable nada cambia: los roles se
crean a mano como en "Primera vez".

## Primera vez

1. Crear los roles en el Postgres de resultados (con `RADAR_BOOTSTRAP_ROLES=true` lo hace el arranque: ver
   "Roles al arrancar"), como superusuario:
   `psql "$URL_SUPERUSUARIO" -v migrator=postgres -f scripts/radar_bootstrap_roles.sql`
   (crea `radar_app LOGIN NOSUPERUSER NOBYPASSRLS` y `radar_admin NOLOGIN`, y le
   cede `radar_admin` al rol `:migrator` para que Alembic pueda `ALTER ... OWNER
   TO radar_admin`) y fijar la contraseña de `radar_app` con
   `ALTER ROLE radar_app PASSWORD '...'` (no lo hace el script, a propósito: no
   se commitea ninguna contraseña).
2. Arrancar el servicio: el lifespan (`app/radar/app.py::_lifespan_radar`) corre
   `alembic upgrade head` sobre `migrations_radar/` (tabla `alembic_version_radar`)
   y `migrations_fuente/` (`alembic_version_fuente`) antes de aceptar tráfico. Si
   una migración falla, el servicio no arranca.
3. Crear el primer admin de KIS desde la shell del servicio:
   `python scripts/radar_admin.py crear-admin --email mariano@keepitsimple.com.ar --nombre "Mariano"`
   Imprime una sola vez el link de invitación (7 días, un solo uso); es
   idempotente (repetirlo actualiza el nombre y reenvía la invitación, hasta 3
   veces cada 15 minutos: el mismo límite de pedidos que `/radar/login`).
4. `GET /health` responde `{"status": "ok", "modo": "radar", "resultados": {...},
   "fuente": {...}, "commit": "<sha corto o null>"}` con 200, o `{"status":
   "arrancando", ...}` con 503 si todavía no hay contexto (`app.state.radar`
   sigue en `None`), o `{"status": "degradado", ...}` con 503 si falla la
   consulta de salud a resultados o a fuente.

## Secretos

`k_tenant` (32 bytes por tenant) vive en `RADAR_SECRETS_DIR`, un archivo por
tenant, permisos 0600 (`FileSecretStore`, `app/radar/secrets.py`). No está en
Postgres ni en sus backups. Se destruye solo en la baja del cliente (tramo 6).
Si se pierde el volumen, los `contact_hmac`/`lid_hmac` existentes quedan sin
correspondencia: hacer backup del volumen por separado y cifrado.

## Roles de Postgres y permisos

Dos roles en el Postgres de resultados (§6.4):

- `radar_admin` (`NOLOGIN`): dueño de las tablas y de las funciones
  `SECURITY DEFINER` (`radar_admin_crear_tenant`, `radar_admin_listar_tenants`).
  Alembic corre como el `:migrator` (por defecto el usuario `postgres` de
  Railway) y le cede `radar_admin` con `GRANT radar_admin TO :migrator` (o el
  arranque, con `RADAR_BOOTSTRAP_ROLES=true`).
- `radar_app` (`LOGIN NOSUPERUSER NOBYPASSRLS`): el rol de la aplicación en
  tiempo de ejecución. `RadarDB.connect()` (`app/radar/db.py`) aborta la
  conexión si detecta que el rol conectado es superusuario o tiene
  `BYPASSRLS`.

`radar_app` no tiene `UPDATE` genérico sobre las tablas: cada migración otorga
`GRANT UPDATE (columnas...)` explícito, excluyendo siempre `id`, `tenant_id` y
las columnas de identidad o creación. Tal como quedaron las migraciones:

- `tenants` (r0001): `UPDATE (nombre, rubro, perfil_de_datos,
  retencion_fichas_meses, retener_fragmentos, ia_habilitada, via_llm,
  parametros_propuestos, updated_at)`. Nunca `id`, `es_kis` ni `created_at`: un
  tenant no puede volverse KIS actualizando una fila.
- `users` (r0002): `UPDATE (nombre)`. Nunca `email` (identidad del usuario).
- `memberships` (r0002): `UPDATE (rol, lineas_permitidas)`. Nunca `user_id`;
  el `CHECK (rol <> 'admin' OR tenant_id = TENANT_KIS)` de la tabla impide
  además que un `UPDATE` deje `rol = 'admin'` fuera del tenant KIS.
- `lines` (r0002): `UPDATE (nombre, estado, almacen_fuente,
  duracion_vinculo_dias, retencion_fuente_dias, retencion_tras_desvinculo_dias,
  tope_ia_mensual_usd, parametros_propuestos, fuente_purgada_hasta,
  de_baja_at, updated_at)`.
- `login_tokens` (r0002): `UPDATE (used_at)` únicamente (el canje marca el
  token usado; nunca se reescribe `token_hash`).
- `sessions` (r0002): `UPDATE (revoked_at, last_seen_at)`.
- `support_grants` (r0002): `UPDATE (revocado_at)`.
- `consents`, `access_audit_log`, `product_events`: sin `UPDATE` en absoluto
  (solo `SELECT, INSERT`; son evidencia de solo inserción).

`ENABLE` + `FORCE ROW LEVEL SECURITY` está activa en todas las tablas con
`tenant_id`; las políticas usan `current_setting('app.tenant_id', true)` vía
`radar_tenant_actual()` y devuelven cero filas si no hay tenant seteado. Todo
acceso pasa por `RadarDB.tenant_tx(tenant_id)` (transacción +
`set_config('app.tenant_id', ...)`); `app.services.db.execute()/fetch()` están
prohibidos dentro de `app/radar/` (verificado por un test de la suite).

## Migraciones

`migrations_radar/` (tabla de versión `alembic_version_radar`) y
`migrations_fuente/` (`alembic_version_fuente`) son dos árboles de Alembic
independientes, cada uno con su propio `env.py` (ambos usan
`app.radar.alembic_env.correr()`) y su propio `script.py.mako` (copiado de
`migrations/script.py.mako`, el del bot) para que `alembic revision
--autogenerate` funcione igual que en el árbol del bot. Ninguno de los dos
corre en el arranque del bot ni toca `DATABASE_URL`.

- `alembic_radar.ini` → `migrations_radar/`.
- `alembic_fuente.ini` → `migrations_fuente/`.

Comando manual (si hace falta migrar sin levantar el servicio):

```bash
alembic -c alembic_radar.ini -x url=postgresql://... upgrade head
alembic -c alembic_fuente.ini -x url=postgresql://... upgrade head
```

## Proxy, IP y logs

- Radar asume **exactamente un proxy confiable** adelante (el de Railway).
  La IP que se guarda como evidencia (`consents.ip`, `access_audit_log.ip`,
  `login_tokens.ip_solicitud`) es el **último** valor de `X-Forwarded-For`, el
  que agrega ese proxy; el primero lo puede escribir el cliente. Si algún día
  hay dos proxies encadenados, hay que ajustar `app/radar/auth.py::ip_de`.
- Los links mágicos llevan el token en el fragmento (`…/radar/login/canjear?t=<tenant>#k=<token>`):
  el navegador nunca lo manda al servidor, así no aparece en el access log de
  uvicorn (que escribe `path?query`) ni en los logs HTTP de Railway. No hace
  falta apagar el access log; el `CMD` del Dockerfile y el `Procfile` quedan
  como están.

## Consentimiento y arrastre entre líneas

Cuando el dueño afloja un parámetro de **tenant** con líneas vivas, cada línea
`vinculada` necesita su propio consentimiento antes de que el valor se aplique
(`lineas_vivas_sin_consentir`, `app/radar/parametros_service.py`). Al consentir
una línea (`POST /radar/api/lineas/{line_id}/consentimientos`), el valor de
tenant se guarda en la fila de `consents` de esa línea junto con el valor de
línea. Si otra línea del mismo tenant consiente después, el endpoint arrastra
("carry-over") lo que esa línea ya tenía consentido para el tenant: lee el
último consentimiento de la línea, se queda con los valores que **siguen
coincidiendo con la propuesta de tenant vigente** (`coincide_con_propuesta`) y
los fusiona antes de decidir si falta algún parámetro de tenant por consentir.
Esto evita que un consentimiento de línea posterior "olvide" el consentimiento
de tenant que otra línea ya dio, sin abrirle al dueño una vía para aflojar por
su cuenta: solo se arrastra lo que un admin de KIS propuso y sigue vigente.

## Qué NO existe todavía

Ingesta y tablas de conversación en el almacén de fuente (tramo 3), KPI (4),
IA (5), purga, supresiones, "borrar todo", emails de hallazgos y el almacén
purgable (6). Ver el plan
`docs/superpowers/plans/2026-09-21-radar-tramo1-acceso-y-datos.md`.

## Tramo 2: WAHA, vínculos y Consola KIS

### Variables nuevas

| Variable | Qué es |
|---|---|
| `RADAR_WAHA_WEBHOOK_URL` | URL de `POST /webhook/waha` que alcanza WAHA. En Railway, por red privada: `http://<servicio-radar>.railway.internal:<puerto>/webhook/waha`. Vacía = no se puede vincular (503). |
| `RADAR_WAHA_WEBHOOK_HMAC_KEY` | 32+ caracteres aleatorios (`python -c "import secrets; print(secrets.token_urlsafe(48))"`). Firma los webhooks (sha512). Vacía = el receptor rechaza todo; corta = la app no arranca (`validar_settings`, `app/radar/app.py`). |
| `RADAR_WAHA_TIMEOUT_S` | Timeout de cada llamada a WAHA (default `20.0`). |
| `RADAR_WORKER_EMBEBIDO` | `true` (default): la cola de jobs corre dentro del servicio web. Ver "Worker de la cola". |

Las lee `RadarSettings` (`app/radar/settings.py`), igual que las del tramo 1.

### Servidor WAHA de clientes

- Un servicio aparte por worker, **sin dominio público** (solo red privada de Railway), con el endurecimiento del runbook GOWS
  (`docs/superpowers/plans/2026-09-21-radar-gows-staging-runbook.md`: `WAHA_PRINT_QR=False`, `WAHA_PRESENCE_AUTO_ONLINE=False`,
  `WAHA_SESSION_CONFIG_IGNORE_*=true`, medios apagados, `WAHA_APPS_ENABLED=false`, dashboard y Swagger deshabilitados, clave
  hasheada) y **sin backups del volumen**.
- Sin `WHATSAPP_HOOK_URL` global: Radar configura el webhook en cada sesión (`app/radar/waha/sesion.py::cuerpo_sesion`).

### Registrar un worker

En la shell del servicio de Radar, con la clave admin **en claro** del WAHA (la del gestor de contraseñas, nunca en un archivo
del repo):

    WAHA_ADMIN_KEY='...' python scripts/radar_workers.py registrar --nombre w1 \
        --base-url http://waha-w1.railway.internal:3000 --engine GOWS --max-sesiones 50 --disco-max-gb 20
    python scripts/radar_workers.py listar
    python scripts/radar_workers.py disco --nombre w1 --usado-gb 3.5

La clave admin va al `SecretStore` como `waha_admin:<worker_id>` (`RADAR_SECRETS_DIR`, 0600), junto a las `k_tenant` del tramo 1
y a las claves de lectura por vínculo (`waha_lectura:<link_id>`). Nunca en Postgres; solo la lee `app/radar/workers.py::cliente_de`.

### Worker de la cola

- Por defecto corre dentro del servicio web (`RADAR_WORKER_EMBEBIDO=true`, `app/radar/worker.py`): el lifespan del servicio web
  corre `bucle` como tarea. Motivo: las claves de WAHA viven en el volumen de `RADAR_SECRETS_DIR` y en Railway un volumen se
  monta en un solo servicio. **Pendiente de OK del dueño**: §6.2 del spec pide un proceso aparte; se mantiene embebido por
  ahora, reversible con `RADAR_WORKER_EMBEBIDO=false` (ver "Decisiones abiertas" del plan del tramo 2).
- Si los secretos pasan a un gestor externo: `RADAR_WORKER_EMBEBIDO=false` en el web y un segundo servicio con la misma imagen
  y el comando `python -m app.radar.worker` (no corre migraciones: las corre el servicio web).
- La cola vive en Postgres (`radar_jobs_reclamar`, `FOR UPDATE SKIP LOCKED`). Programa el chequeo de salud de cada vínculo vivo
  cada 5 min y corre los fines de vínculo. Un job que agota sus reintentos (`intentos >= max_intentos`) pasa a `fallido` en vez
  de quedar reintentando para siempre; si era un fin de vínculo, el vínculo sigue en `cerrando` y la programación de salud
  (cada 5 min) le vuelve a encolar el fin, sin techo, hasta que WAHA confirme el borrado.
- **Nunca corren a la vez el worker embebido y el proceso aparte** (ni dos réplicas del servicio web con el embebido): con
  `RADAR_WORKER_EMBEBIDO=true` no se levanta `python -m app.radar.worker`, y al pasar a proceso aparte se pone `false` en el web
  antes de arrancarlo. La cola tolera un segundo worker (reclama de a un job, lease de 600 s, y completar/reprogramar/fallar solo
  tocan un job que sigue siendo suyo), pero dos `fin_vinculo` del mismo vínculo en paralelo romperían "un único DELETE por intento".

### Pantallas

- Consola KIS: `https://<radar>/radar/consola` (rol `admin`).
- Pantalla del dueño: `https://<radar>/radar/conectar?linea=<line_id>` (rol `dueno`).

### Desvíos del plan frente al código real

- **`UPDATE` por columna en r0003**: igual que en r0002 (tramo 1), las tablas nuevas del tramo 2 (`links`, `link_status_events`,
  `waha_workers`, `radar_jobs`) usan `GRANT UPDATE (columnas...)` explícito por tabla (`UPDATES_POR_COLUMNA` en
  `migrations_radar/versions/r0003_*.py`), no `UPDATE` genérico. `radar_app` nunca puede tocar `id`, `tenant_id` ni las
  columnas de identidad/creación.
- **`CHECK` de `jobs.causa`**: la columna `causa` de `radar_jobs` tiene `CHECK (causa ~ '^[a-z0-9_]{1,40}$')` en vez de una
  lista fija de valores, para no tener que migrar el esquema cada vez que se agrega una causa nueva de fin de vínculo.
- **Jobs agotados pasan a `fallido`**: `radar_jobs_reclamar` (r0003) hace
  `estado = CASE WHEN intentos >= max_intentos THEN 'fallido' ELSE 'pendiente' END`: un job sin reintentos disponibles no queda
  dando vueltas, queda visible como fallido para intervención manual.
- **`terminar_sesion` no corta ante fallas**: `app/radar/waha/gestor.py::terminar_sesion` sigue cada paso (lectura, start
  opcional, `DELETE`, borrado de claves, verificación) aunque un paso anterior haya fallado, y devuelve un resumen con el error
  de cada paso en vez de abortar en el primero. Así un fallo de lectura, por ejemplo, no impide intentar igual el `DELETE` y el
  borrado de claves; el job de fin de vínculo decide con ese resumen si reintentar.
- **Worker embebido por defecto**: ver "Worker de la cola" arriba — decisión pendiente del dueño frente a §6.2 del spec.
