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
| `RADAR_FUENTE_DATABASE_URL` | Postgres del almacén de fuente permanente (servidor distinto del de resultados, con backups). En este tramo solo tiene la tabla `fuente_meta`. |
| `RADAR_SECRETS_DIR` | Directorio de `FileSecretStore` para las `k_tenant`: un volumen de Railway montado ahí, fuera de los backups de Postgres. Default `/data/radar-secrets`. |
| `RADAR_COOKIE_SECRET` | 32+ caracteres aleatorios para firmar la cookie de sesión (`python -c "import secrets; print(secrets.token_urlsafe(48))"`). Con menos de 32 la app no arranca (`validar_settings`, `app/radar/app.py`). |
| `RADAR_COOKIE_SECURE` | `true` en producción (default). `false` solo para http local. |
| `RADAR_PUBLIC_BASE_URL` | Base de los links mágicos, ej. `https://radar.keepitsimple.com.ar`. Default `http://localhost:8000`. |
| `RADAR_MAILER` | `log` (default: loguea solo dominio y huella del token) o `memoria` (tests). El proveedor de producción está por definir. |
| `RADAR_REMITENTE` | Remitente de los emails. Default `radar@keepitsimple.com.ar`. |

Todas estas variables las lee `RadarSettings` (`app/radar/settings.py`, prefijo
`RADAR_`); `RADAR_MIGRATOR_DATABASE_URL` la lee `app.radar.migrate` a través del
mismo objeto. `validar_settings()` exige `database_url`, `migrator_database_url`,
`fuente_database_url` y un `cookie_secret` de 32+ caracteres antes de arrancar el
lifespan.

## Primera vez

1. Crear los roles en el Postgres de resultados, como superusuario:
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
  Railway) y le cede `radar_admin` con `GRANT radar_admin TO :migrator`.
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

WAHA y vínculos (tramo 2), ingesta y tablas de conversación en el almacén de
fuente (tramo 3), KPI (4), IA (5), purga, supresiones, "borrar todo", emails
de hallazgos y el almacén purgable (6). Ver el plan
`docs/superpowers/plans/2026-09-21-radar-tramo1-acceso-y-datos.md`.
