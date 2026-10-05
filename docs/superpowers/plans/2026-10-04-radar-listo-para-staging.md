# Radar — listo para staging (tramo 2.5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** que un admin de KIS pueda desplegar Radar en Railway y usarlo de punta a punta **desde el navegador**, sin shell en el servidor: entrar con link mágico, dar de alta un cliente con su línea, cargar el servidor WAHA, registrar el consentimiento, generar el QR y operar el vínculo.

**Por qué:** los tramos 1 y 2 dejaron la API completa, pero para usarla hoy hace falta `curl` y una shell en el servicio:

- no hay página de login (solo `POST /radar/login`), y al canjear el link el navegador termina en un JSON (`/radar/api/yo`);
- las pantallas sin sesión responden `401` JSON en lugar de llevar al login, y no hay botón de salir;
- el alta de cliente, línea e invitación existe solo como API;
- el servidor WAHA (worker) se registra solo con `scripts/radar_workers.py` en la shell del servicio, porque la clave admin va al `SecretStore` del volumen;
- los roles de Postgres (`radar_app`, `radar_admin`) se crean con `psql`, y el primer admin con `scripts/radar_admin.py` en la shell;
- no hay proveedor de email: con `RADAR_MAILER=log` ningún link sale del servidor.

**Rama:** `feature/radar-tramo2` (worktree `D:\Dev\WhatsappBOTy-radar-tramo2`). Se sigue sobre la misma rama: es la que se va a desplegar.

**Tech stack:** el del repo. Python 3.12, FastAPI, asyncpg, pydantic-settings, pytest + pytest-asyncio (`asyncio_mode=auto`), pgserver. Sin dependencias nuevas (SMTP con `smtplib` de la stdlib).

## Global Constraints

- Tests: `python -m pytest tests/radar_tests -q` y la suite completa `python -m pytest -q` en verde al final de cada tarea (base: 967 passed, 1 skipped, más lo que sume cada tarea).
- **Nada de red real ni WAHA real:** los tests usan `WahaFalso` (`tests/radar_tests/waha_falso.py`) vía `ctx.waha_transport`, y un SMTP falso por monkeypatch. Prohibido llamar herramientas MCP de WAHA.
- Pantallas: siguen la decisión 12 del tramo 2 (`app/radar/routers/paginas.py`): HTML estático sin inline (`_sin_inline` de `tests/radar_tests/test_paginas.py`), un único `radar.js` que escribe solo con `textContent`/`setAttribute`, CSP estricta `CSP` sin cambios. Nada de `innerHTML`.
- Secretos: la clave admin de WAHA, la contraseña SMTP y la de `radar_app` **nunca** van a un log, a la base, a una respuesta HTTP ni a un mensaje de error. Hay test para cada uno.
- Auditoría: toda acción nueva de un admin sobre un cliente o un worker queda en `access_audit_log` (`app.radar.auditoria.registrar`), como las existentes.
- Estilo: el del código vecino (español, comentarios densos solo donde explican un porqué, nombres en castellano).
- Commits: uno o más por tarea, mensaje `Radar staging: ...`, terminando con la línea `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Sin push.
- Documentación: cada tarea que agrega una variable de entorno la suma a la tabla de `docs/radar-despliegue.md`.

## Decisiones

1. **Login por página propia.** `GET /radar/login` sirve `login.html` (email + botón; el JS hace `POST /radar/login` y muestra siempre el mismo aviso, sin revelar si el email existe).
2. **Aterrizaje por rol.** El canje redirige a `GET /radar/inicio`, que decide: `admin` → `/radar/consola`; `dueno` → `/radar/conectar?linea=<id>` de su primera línea no dada de baja (por `created_at`), o `/radar/conectar` sin parámetro si no tiene; `gestor`/`lector` → `/radar/api/yo` (su pantalla llega con el tablero, tramo 4). `GET /` y `GET /radar` redirigen a `/radar/inicio`.
3. **Pantallas sin sesión van al login.** `/radar/consola`, `/radar/conectar` y `/radar/inicio` responden `303 → /radar/login` sin sesión válida. Con rol equivocado siguen en `403`. Las rutas de API (`/radar/api/*`, `/radar/admin/*`) siguen respondiendo `401` JSON.
4. **Alta desde la Consola** con los endpoints que ya existen (`POST /radar/admin/tenants`, `POST /radar/admin/tenants/{t}/lineas`, `POST /radar/admin/tenants/{t}/invitaciones`, `GET /radar/admin/propuesta`). Al crear un cliente se abre el panel "Operar" de su línea nueva.
5. **Servidores WAHA desde la Consola.** API nueva `/radar/admin/workers` (listar, registrar, reemplazar clave, actualizar disco). Antes de guardar una clave se verifica contra WAHA con `GET /api/server/version`: tiene que responder y el motor tiene que coincidir. La clave viaja una sola vez del navegador al servidor por HTTPS y va directo al `SecretStore`. `scripts/radar_workers.py` sigue existiendo.
6. **SMTP genérico** (`RADAR_MAILER=smtp`): sirve para Google Workspace (contraseña de aplicación), Resend, SES o Brevo sin atarse a un proveedor. `log` sigue siendo el default.
7. **Roles de Postgres al arrancar, opt-in** (`RADAR_BOOTSTRAP_ROLES=true`): si el rol de migración puede crear roles (en Railway, `postgres` es superusuario), el arranque crea `radar_admin` y `radar_app` si faltan y le pone a `radar_app` la contraseña que trae `RADAR_DATABASE_URL`. Así el despliegue no necesita `psql`. Sin la variable, todo sigue como antes (`scripts/radar_bootstrap_roles.sql`).
8. **Admins iniciales por variable** (`RADAR_ADMINS_INICIALES=email1,email2`): el arranque crea esos admins de KIS si no existen, **sin mandar nada**. Después cada uno entra por `/radar/login` (necesita SMTP). `scripts/radar_admin.py crear-admin` sigue existiendo para el caso sin SMTP.
9. **Host configurable en el Dockerfile** (`UVICORN_HOST`, default `0.0.0.0`): la red privada de Railway puede requerir escuchar en IPv6 (`::`). El bot no cambia.

## File Structure

```
app/radar/static/login.html                 nuevo: pantalla de login
app/radar/static/consola.html               + secciones "Alta" y "Servidores WAHA", + botón Salir
app/radar/static/conectar.html              + botón Salir
app/radar/static/radar.js                   + modo "login", alta, workers, salir, filtro de clientes desde /tenants
app/radar/routers/paginas.py                + /radar/login (GET), /radar/inicio, /, /radar; redirección al login
app/radar/routers/login.py                  canje → /radar/inicio
app/radar/routers/workers_admin.py          nuevo: /radar/admin/workers
app/radar/workers.py                        + verificar_clave_worker, reemplazar_clave
app/radar/mailer.py                         + SmtpMailer; construir_mailer recibe los settings
app/radar/settings.py                       + smtp_*, bootstrap_roles, admins_iniciales
app/radar/bootstrap.py                      nuevo: asegurar_roles, asegurar_admins_iniciales
app/radar/app.py                            lifespan: roles → migraciones → contexto → admins; middleware no-store
Dockerfile                                  --host ${UVICORN_HOST:-0.0.0.0}
docs/radar-despliegue.md                    variables nuevas
tests/radar_tests/test_paginas.py           + login, inicio, redirecciones, ids nuevos
tests/radar_tests/test_workers_admin.py     nuevo
tests/radar_tests/test_mailer_smtp.py       nuevo
tests/radar_tests/test_bootstrap.py         nuevo
```

---

### Task 1: Login, aterrizaje por rol, redirección y salir

**Files:** `app/radar/static/login.html` (nuevo), `app/radar/static/consola.html`, `app/radar/static/conectar.html`, `app/radar/static/radar.js`, `app/radar/routers/paginas.py`, `app/radar/routers/login.py`, `tests/radar_tests/test_paginas.py`, `tests/radar_tests/test_login.py`.

- [ ] **Tests primero** (en `test_paginas.py` salvo que se indique):
  - `test_login_es_una_pagina_sin_inline`: `GET /radar/login` → 200, `content-security-policy == CSP`, `cache-control: no-store`, `_sin_inline`, contiene `id="email"`, `id="btn-entrar"`, `id="aviso"` y `data-modo="login"`.
  - `test_pantallas_sin_sesion_van_al_login`: sin cookie, `GET /radar/consola`, `/radar/conectar` y `/radar/inicio` → `303` con `location: /radar/login` (usar `follow_redirects=False`). Reemplaza la primera aserción de `test_consola_solo_para_admins` (hoy espera 401); el resto de ese test (403 para dueño, 200 para admin) se mantiene.
  - `test_api_sin_sesion_sigue_en_401`: `GET /radar/api/yo` y `GET /radar/admin/consola/lineas` sin cookie → 401.
  - `test_inicio_admin_va_a_la_consola`, `test_inicio_dueno_va_a_conectar_su_linea` (la `location` lleva `?linea=<line_id>` de `escenario_vinculable`), `test_inicio_dueno_sin_lineas_va_a_conectar_sin_linea` (la única línea con `de_baja_at` seteado), `test_inicio_lector_va_a_yo`.
  - `test_raiz_redirige_a_inicio`: `GET /` y `GET /radar` → 303 a `/radar/inicio`.
  - `test_login.py::test_canje_redirige_a_inicio`: el `POST /radar/login/canjear` exitoso responde 303 con `location: /radar/inicio` (ajustar el test existente que espera `/radar/api/yo`).
  - `test_consola_y_conectar_tienen_salir`: los dos HTML tienen `id="btn-salir"`.
- [ ] **Implementación:**
  - `paginas.py`: una dependencia de página que resuelve la sesión con `sesion_actual` y, si lanza `HTTPException(401)`, hace que la ruta devuelva `RedirectResponse("/radar/login", 303)`; con rol equivocado sigue el 403. Rutas nuevas `GET /radar/login` (sirve `login.html` con `CABECERAS_HTML`), `GET /radar/inicio`, `GET /`, `GET /radar`. `/radar/inicio` audita igual que la consola solo cuando va a la consola (la consola ya audita al abrirse: no duplicar).
  - La primera línea del dueño: `SELECT id FROM lines WHERE de_baja_at IS NULL ORDER BY created_at LIMIT 1` dentro de `tenant_tx(sesion.tenant_id)` (respeta `lineas_permitidas` si el dueño las tuviera: usar `listar_lineas` de `app/radar/lineas.py` si ya filtra, si no, la consulta).
  - `login.py`: el canje redirige a `/radar/inicio`.
  - `login.html`: título "Radar — Entrar", campo email (`type="email"`, `autocomplete="email"`), botón `btn-entrar`, `<p id="aviso" role="status">`, `<p id="error" class="error" role="alert">`. `body data-modo="login"`.
  - `radar.js`: modo `login`: al enviar, `preventDefault`, `POST /radar/login` `{email}` y muestra siempre "Si el email está registrado, te mandamos un link para entrar. Revisá tu correo." Botón `btn-salir` (consola y cliente): `POST /radar/logout` y `location.assign("/radar/login")` (también si el logout falla). Para el modo login no se enlaza nada de consola ni cliente.
- [ ] Commit.

### Task 2: Alta de cliente, línea e invitación en la Consola

**Files:** `app/radar/static/consola.html`, `app/radar/static/radar.js`, `tests/radar_tests/test_paginas.py`.

- [ ] **Tests primero:**
  - `test_consola_tiene_alta`: `consola.html` tiene los ids `alta-nombre`, `alta-rubro`, `alta-dueno-email`, `alta-dueno-nombre`, `alta-linea-nombre`, `alta-propuesta`, `btn-alta`, `linea-cliente`, `linea-nombre`, `btn-agregar-linea`, `inv-cliente`, `inv-email`, `btn-invitar`, `alta-aviso`; sigue pasando `_sin_inline`.
  - Un test de API de punta a punta del flujo que usa la pantalla (si no existe ya en `test_admin.py`): admin crea cliente → la línea aparece en `GET /radar/admin/consola/lineas` → agrega una segunda línea → reenvía invitación (202, `enviada` true con `MemoryMailer`).
- [ ] **Implementación (solo HTML y JS, sin endpoints nuevos):**
  - Sección `<section id="alta">` arriba de "Líneas", con tres formularios:
    1. **Cliente nuevo:** nombre, rubro (texto, `pattern="[a-z_]{1,40}"`, ayuda "minúsculas y guion bajo, ej.: farmacia"), email y nombre del dueño, nombre de la primera línea. Al salir del campo rubro (evento `change`) llama `GET /radar/admin/propuesta?rubro=` y escribe en `alta-propuesta` "Perfil de datos: X · Parámetros propuestos: …" (texto plano). `btn-alta` → `POST /radar/admin/tenants` con `{nombre, rubro, dueno: {email, nombre}, linea: {nombre}}`. Mensaje en `alta-aviso`: "Cliente creado. Invitación al dueño: enviada." o "… no salió: reenviala desde «Reenviar invitación»." Después: refresca la tabla y los selects de clientes, limpia el formulario y abre el panel "Operar" de la línea nueva (`abrirPanel` con `tenant_nombre` y `line_nombre` del formulario).
    2. **Agregar línea:** select `linea-cliente` + nombre → `POST /radar/admin/tenants/{t}/lineas` `{nombre}`.
    3. **Reenviar invitación:** select `inv-cliente` + email → `POST /radar/admin/tenants/{t}/invitaciones` `{email}`; "Invitación enviada." / "No salió: revisá el email o el correo saliente."
  - Los tres selects de cliente (`filtro-cliente`, `linea-cliente`, `inv-cliente`) se llenan desde `GET /radar/admin/tenants` (todos los clientes, no solo los que tienen líneas), se reconstruyen en cada carga y conservan la selección. Arregla el bug actual de `llenarClientes` (solo llena una vez y un cliente nuevo no aparece hasta recargar).
  - Mensajes de error nuevos en `MENSAJES` para lo que devuelvan estos endpoints (422 con texto: mostrarlo tal cual, ya lo hace `pedir`).
- [ ] Commit.

### Task 3: Servidores WAHA desde la Consola

**Files:** `app/radar/routers/workers_admin.py` (nuevo), `app/radar/workers.py`, `app/radar/app.py` (incluir el router), `app/radar/static/consola.html`, `app/radar/static/radar.js`, `tests/radar_tests/test_workers_admin.py` (nuevo), `tests/radar_tests/waha_falso.py` (si hace falta simular 401 o red caída en `/api/server/version`).

- [ ] **Tests primero** (`test_workers_admin.py`):
  - Solo admin: sin sesión 401, dueño 403.
  - `GET /radar/admin/workers` lista `{id, nombre, base_url, engine, max_sesiones, sesiones, disco_max_gb, disco_usado_gb, activo, clave_cargada}`; nunca un campo con la clave.
  - `POST /radar/admin/workers` con WAHA falso que responde la versión con el motor pedido → 201 `{id, nombre, version, engine}`; la clave queda en `ctx.secretos` bajo `nombre_clave_admin(id)`; auditado `worker_registrado` con el admin como actor.
  - Verificación: WAHA responde 401 → 422 `{"error": "clave_incorrecta"}`; error de red → 422 `waha_no_responde`; motor distinto → 422 `motor_distinto`. En los tres casos **no** se inserta fila ni se guarda la clave.
  - Nombre repetido → 409 `worker_duplicado` (y no se pisa la clave del existente).
  - Validación: nombre fuera de `^[a-z0-9_-]{1,40}$`, `base_url` sin `http://`/`https://`, `max_sesiones` fuera de 1..500, disco ≤ 0, clave de menos de 16 caracteres → 422.
  - `PUT /radar/admin/workers/{id}/clave` verifica contra el `base_url` y el motor del worker y reemplaza la clave; auditado `worker_clave_reemplazada`; con clave incorrecta no reemplaza.
  - `PUT /radar/admin/workers/{id}/disco` `{usado_gb}` actualiza; worker inexistente → 404.
  - **La clave no se filtra:** con `caplog` en DEBUG durante el registro, ni el texto de la clave ni la clave aparecen en logs, respuestas ni `access_audit_log.detalle`.
  - `consola.html` tiene `workers`, `worker-nombre`, `worker-url`, `worker-motor`, `worker-max`, `worker-disco`, `worker-clave` (`type="password"`, `autocomplete="off"`), `btn-worker`, `worker-reemplazo`, `worker-clave-nueva`, `btn-worker-clave`; `_sin_inline`.
- [ ] **Implementación:**
  - `workers.py`: `async def verificar_clave_worker(ctx, *, base_url, engine, admin_key) -> dict` que arma un `WahaCliente(base_url, admin_key, transport=ctx.waha_transport, timeout=ctx.settings.waha_timeout_s)`, llama `version_servidor()` y traduce: `WahaHttpError` 401/403 → `ClaveIncorrecta`; otro `WahaError` → `WahaNoResponde`; `engine` distinto → `MotorDistinto`. Devuelve `{version, engine, tier}`. `reemplazar_clave(ctx, worker_id, admin_key, actor_user_id)` (verifica, guarda, audita). `registrar_worker` no cambia de firma.
  - `workers_admin.py`: router `prefix="/radar/admin/workers"`, `requiere_rol("admin")`, modelos pydantic con las validaciones de arriba; `UniqueViolationError` de asyncpg → 409. Ningún `detail` incluye la clave ni la URL completa con credenciales.
  - Consola: sección "Servidores WAHA" con la tabla (nombre, motor, URL, sesiones `x/y`, disco `usado/max GB`, activo, clave cargada sí/no), el formulario de alta, un formulario "Reemplazar clave" (select `worker-reemplazo` + `worker-clave-nueva` + `btn-worker-clave`) y, por fila, un botón "Disco" que pide los GB con `window.prompt` y hace el `PUT`. Al terminar un alta o reemplazo, el campo de la clave se vacía. Mensajes: `clave_incorrecta` "WAHA rechazó la clave.", `waha_no_responde` "No se pudo conectar con ese servidor WAHA.", `motor_distinto` "El servidor WAHA usa otro motor.", `worker_duplicado` "Ya hay un servidor con ese nombre."
  - La nota del pie de la tabla de líneas y la de la sección dicen que la Consola nunca muestra la clave.
- [ ] Commit.

### Task 4: Email por SMTP

**Files:** `app/radar/mailer.py`, `app/radar/settings.py`, `app/radar/app.py` (`construir_contexto`, `validar_settings`), `scripts/radar_admin.py` y cualquier otro llamador de `construir_mailer`, `tests/radar_tests/test_mailer_smtp.py` (nuevo), `docs/radar-despliegue.md`.

- [ ] **Tests primero** (monkeypatch de `smtplib.SMTP` y `smtplib.SMTP_SSL` con una clase falsa que registra llamadas; nada de red):
  - `starttls`: conecta a host/puerto, llama `starttls()`, `login(usuario, password)` y `send_message` con `From` = `RADAR_REMITENTE`, `To` = destinatario, `Subject` = asunto, cuerpo = texto (UTF-8, tildes intactas).
  - `ssl` usa `SMTP_SSL` y no llama `starttls`; `ninguna` no llama ni `starttls` ni `SMTP_SSL`; sin usuario no llama `login`.
  - Un error de SMTP se propaga como excepción (los llamadores ya lo manejan: `enviar_link_seguro` devuelve False) y el log **no** contiene la contraseña ni el cuerpo; en éxito loguea solo `*@dominio` y la huella, como `LogMailer`.
  - `validar_settings`: `RADAR_MAILER=smtp` sin `RADAR_SMTP_HOST` → `RuntimeError`; con usuario y sin contraseña → `RuntimeError`; `RADAR_SMTP_SEGURIDAD` fuera de `starttls|ssl|ninguna` → `RuntimeError`. `construir_mailer` sigue rechazando nombres desconocidos (mensaje con `log|memoria|smtp`).
  - El envío no bloquea el loop (`asyncio.to_thread`): basta con verificar que `enviar` es corrutina y que el falso se llamó desde otro hilo.
- [ ] **Implementación:** settings `smtp_host: str = ""`, `smtp_port: int = 587`, `smtp_usuario: str = ""`, `smtp_password: str = ""`, `smtp_seguridad: str = "starttls"`, `smtp_timeout_s: float = 20.0`. `SmtpMailer` con `email.message.EmailMessage`, `smtplib` en `asyncio.to_thread`, timeout. `construir_mailer(nombre, rs=None)` o `construir_mailer(rs)`: elegir la forma que menos toque a los llamadores y actualizarlos todos. La contraseña nunca entra en un `repr` (pydantic `SecretStr` o excluirla del repr).
- [ ] Docs: variables en la tabla, y una nota: con Google Workspace, `smtp.gmail.com:587`, usuario = la cuenta, contraseña de aplicación, y `RADAR_REMITENTE` tiene que ser esa cuenta o un alias suyo.
- [ ] Commit.

### Task 5: Roles de Postgres al arrancar (opt-in)

**Files:** `app/radar/bootstrap.py` (nuevo), `app/radar/settings.py`, `app/radar/app.py`, `tests/radar_tests/test_bootstrap.py` (nuevo), `docs/radar-despliegue.md`.

- [ ] **Tests primero.** Los roles son de todo el cluster y `conftest.py` ya los crea en el pgserver de la sesión: estos tests levantan **su propio** pgserver en un directorio temporal (fixture de módulo), así parten sin roles.
  - `asegurar_roles(migrator_url, app_url)` en un cluster vacío crea `radar_admin` (`NOLOGIN`) y `radar_app` (`LOGIN`, `NOSUPERUSER`, `NOBYPASSRLS`, `NOINHERIT`) y le asigna contraseña (`pg_authid.rolpassword IS NOT NULL`); le da `radar_admin` al rol de migración.
  - Idempotente: correrlo dos veces no falla ni cambia los atributos.
  - Con `RADAR_DATABASE_URL` cuyo usuario no es `radar_app` → `RuntimeError` (las migraciones otorgan permisos a ese nombre). Sin contraseña o con menos de 16 caracteres → `RuntimeError`.
  - Rol de migración sin `CREATEROLE` ni superusuario → no hace nada y loguea una advertencia.
  - La contraseña se fija **solo si hace falta**: `radar_app` recién creado, o `radar_app` existente que no puede entrar con la URL (la decisión se testea con un monkeypatch de la función que prueba la conexión, porque pgserver autentica por `trust`). Con el rol existente y la conexión OK, no se ejecuta ningún `ALTER ROLE`.
  - Ni la contraseña ni la URL completa aparecen en el log (`caplog`).
  - Integración: `crear_app_radar` con `bootstrap_roles=True` sobre el cluster vacío arranca (lifespan completo con `LifespanManager` o `TestClient`) y `GET /health` da `ok`.
- [ ] **Implementación:** `settings.bootstrap_roles: bool = False`. En `_lifespan_radar`, si está activo, `asegurar_roles` corre **antes** de las migraciones (en un hilo, con timeout como las migraciones). El `ALTER ROLE ... PASSWORD` se arma en el servidor con `SELECT format('ALTER ROLE radar_app PASSWORD %L', $1)` y se ejecuta el resultado: nunca se interpola la contraseña en Python. Los nombres de rol son constantes, no vienen de la URL.
- [ ] Docs: `RADAR_BOOTSTRAP_ROLES`, y que con él `RADAR_DATABASE_URL` puede escribirse con las referencias de Railway, por ejemplo `postgresql://radar_app:<contraseña>@${{Postgres.PGHOST}}:${{Postgres.PGPORT}}/${{Postgres.PGDATABASE}}`.
- [ ] Commit.

### Task 6: Admins iniciales por variable

**Files:** `app/radar/bootstrap.py`, `app/radar/settings.py`, `app/radar/app.py`, `app/radar/admin_kis.py` (si conviene reusar), `tests/radar_tests/test_bootstrap.py`, `docs/radar-despliegue.md`.

- [ ] **Tests primero:**
  - `asegurar_admins_iniciales(ctx, "a@kis.com, B@KIS.com")` crea dos usuarios del tenant KIS con membresía `admin` (emails normalizados), **sin mandar mails** (`MemoryMailer.enviados` vacío) y audita `admin_inicial_creado` con actor `sistema`.
  - Idempotente: la segunda corrida no crea nada, no audita y no cambia el nombre de un admin existente.
  - Un usuario de KIS que ya existe con otro rol queda en `admin`.
  - Email inválido en la variable → `validar_settings` lanza `RuntimeError` sin repetir el email completo en el mensaje (solo la posición en la lista).
  - El log dice cuántos se crearon y cuántos ya existían, sin emails.
  - Con la variable vacía no hace nada.
- [ ] **Implementación:** `settings.admins_iniciales: str = ""`. En el lifespan, después de `construir_contexto` y antes de arrancar el worker embebido.
- [ ] Docs: la variable y el flujo "poné tu email, desplegá, entrá por `/radar/login`" (necesita SMTP; si no hay, `scripts/radar_admin.py crear-admin` en la shell del servicio).
- [ ] Commit.

### Task 7: Detalles de despliegue

**Files:** `Dockerfile`, `app/radar/app.py`, `tests/radar_tests/test_paginas.py` o un test nuevo, `docs/radar-despliegue.md`.

- [ ] `Dockerfile`: `CMD ["sh", "-c", "uvicorn app.main:app --host ${UVICORN_HOST:-0.0.0.0} --port ${PORT:-8000}"]`. Test de texto sobre el Dockerfile (que el default siga siendo `0.0.0.0` y el puerto `${PORT:-8000}`).
- [ ] Middleware de la app de Radar: toda respuesta de `/radar/api/*` y `/radar/admin/*` lleva `Cache-Control: no-store` si no trae otra (el estado del vínculo, el número de la línea y las listas no deben quedar en cachés). Test con `GET /radar/admin/consola/lineas` y `GET .../vinculo`; el PNG del QR (`.../vinculo/qr`) también `no-store`.
- [ ] Docs: `UVICORN_HOST` (`::` para escuchar en IPv6 si la red privada de Railway lo pide).
- [ ] Commit.

### Task 8: Verificación final

- [ ] Suite de Radar y suite completa en verde; reportar los conteos.
- [ ] Arranque real con `python scripts/radar_local.py serve --webhook-url https://example.invalid/webhook/waha` y comprobar con `curl` (sin navegador): `GET /` → 303 `/radar/inicio`; `GET /radar/inicio` sin cookie → 303 `/radar/login`; `GET /radar/login` → 200; `GET /health` → `ok`. Cortar el servidor al terminar (y verificar que el puerto 8000 quedó libre).
- [ ] Revisión final de toda la rama desde el commit `b52790a` con foco en: secretos que se filtran (clave WAHA, SMTP, contraseña de `radar_app`), redirecciones abiertas, CSP, auditoría de las acciones nuevas.

## Fuera de este plan

- Métricas de la Consola (C3): llegan con los tramos 3 y 4.
- El proveedor de email concreto y sus registros DNS: los configura el dueño (la guía de staging lo explica).
- Crear los servicios de Railway: lo hace el dueño con la guía de staging.
