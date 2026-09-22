# Radar tramo 1 — Despliegue y acceso Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dejar corriendo el despliegue propio de Radar con tenants, líneas y sus parámetros, usuarios con roles, login por link mágico, invitación por un admin de KIS, consentimientos por línea, auditoría sin identificadores, RLS efectiva sobre dos roles de Postgres, almacén de fuente separado y secretos `k_tenant` fuera de la base.

**Architecture:** La misma imagen del repo arranca en modo `bot` (todo igual que hoy) o en modo `radar` (`APP_MODE=radar`): en radar no se monta ningún router del bot y viceversa. Radar tiene su propio árbol de migraciones (`migrations_radar/`, tabla de versión `alembic_version_radar`) que corre con un rol dueño (`RADAR_MIGRATOR_DATABASE_URL`) mientras la app conecta con `radar_app` (`RADAR_DATABASE_URL`, sin superusuario ni `BYPASSRLS`) y accede a todo a través de `tenant_tx(tenant_id)`, que fija `app.tenant_id` por transacción. Las operaciones entre tenants de los admins de KIS pasan por funciones `SECURITY DEFINER` propiedad de un rol `radar_admin` sin login; el texto de conversaciones vivirá en un segundo Postgres (`RADAR_FUENTE_DATABASE_URL`) que en este tramo solo tiene su marcador de esquema y un health check.

**Tech Stack:** Python 3.12, FastAPI, asyncpg (SQL crudo, sin ORM), Alembic con SQL crudo (dos entornos nuevos), PostgreSQL ≥ 15 con RLS, pytest + pytest-asyncio (`asyncio_mode=auto`), pgserver (Postgres embebido) para tests, httpx `ASGITransport`, HMAC-SHA256 de la stdlib.

**Spec:** `docs/superpowers/specs/2026-09-21-onboarding-radar-whatsapp-design.md` (v5.1), secciones 2.1, 4.4, 6.2, 6.4, 7 y 8 (tramo 1).

## Global Constraints

- Radar corre como **despliegue propio** con Postgres y Redis propios; nunca usa la base de un cliente de Remedia (§6.2, S7). En modo radar el router del bot no se monta.
- `APP_MODE` por defecto es `bot`: ningún despliegue existente cambia de comportamiento.
- Las tablas de Radar **no se crean en la base de un cliente del bot**: viven en `migrations_radar/` con tabla de versión `alembic_version_radar`, y solo corren en modo radar.
- Dos roles de Postgres (§6.4): el dueño de las tablas solo corre Alembic (`RADAR_MIGRATOR_DATABASE_URL`); la app conecta como `radar_app`, `NOSUPERUSER`, `NOBYPASSRLS`, no dueño (`RADAR_DATABASE_URL`). `RadarDB.connect()` aborta si el rol es superusuario o `BYPASSRLS`.
- `ENABLE` + `FORCE ROW LEVEL SECURITY` en **cada** tabla con tenant. Las políticas usan `current_setting('app.tenant_id', true)` y devuelven cero filas si está vacío.
- Todo acceso a tablas de Radar pasa por `tenant_tx(tenant_id)`: transacción + `SELECT set_config('app.tenant_id', $1, true)`. `app.services.db.execute()/fetch()` están **prohibidos** en `app/radar/` y un test lo verifica.
- Todas las tablas llevan `tenant_id NOT NULL` (`tenants` usa `id`). Los ids son UUID opacos.
- `access_audit_log` guarda actor, rol, acción, tipo de objeto, UUID interno, fecha e IP. **Nunca** teléfonos, JID, nombres ni texto. Se retiene 24 meses (la purga es del tramo 6).
- `product_events` solo admite tenant, línea, usuario, nombre de evento, UUID internos y valores numéricos: un test de esquema (y un `CHECK` en la tabla) rechaza propiedades de texto libre.
- `k_tenant` son 32 bytes aleatorios por tenant, guardados **fuera de la base y de sus backups**; `contact_hmac = HMAC-SHA256(k_tenant, E.164)` y `lid_hmac = HMAC-SHA256(k_tenant, lid)`. Ningún hash sin clave.
- Login por email con **link mágico de un solo uso y con vencimiento** (15 min; invitación 7 días). Sesión web en cookie `HttpOnly`, `Secure`, `SameSite=Lax`, firmada, **atada al tenant dentro del token**, con tabla `sessions` para revocar. Los tokens se guardan hasheados (sha256) y se comparan por hash o con `hmac.compare_digest`. Nunca viajan tokens en la URL, salvo el link mágico, que lleva el token en el **fragmento** (`#k=…`, nunca llega al servidor ni a los access logs), se canjea por POST y se invalida. `RADAR_COOKIE_SECRET` tiene al menos 32 caracteres o la app no arranca.
- Roles: `admin` (KIS, solo en el tenant KIS), `dueno`, `gestor`, `lector`; `soporte` existe solo como rol de sesión, otorgado por el dueño con `support_grants` de 24–72 h, visible y auditado. El rol se valida en el servidor en cada consulta.
- Parámetros exactamente como §2.1: `duracion_vinculo_dias` (línea, 0), `retencion_fuente_dias` (línea, 0), `retencion_tras_desvinculo_dias` (línea, 0), `retencion_fichas_meses` (tenant, 12), `tope_ia_mensual_usd` (línea, NULL), `perfil_de_datos` (tenant, `estandar|sensible`), `retener_fragmentos` (tenant, false), `ia_habilitada` (tenant, true), `via_llm` (tenant, `lotes|sincronica`).
- "Más estricto" (§2.1): en duración y retención cualquier N > 0 es más estricto que 0 y entre valores > 0 gana el menor; en los demás apagar es más estricto que encender. Pasar un parámetro de una línea `vinculada` a un valor más laxo exige una fila nueva en `consents` antes de aplicarse; endurecer se aplica directo y queda auditado. El cliente solo puede endurecer: el consentimiento acepta **exactamente** lo que un admin de KIS propuso (`lines.parametros_propuestos` / `tenants.parametros_propuestos`), nunca valores que el dueño elija por su cuenta.
- La IP que se guarda como evidencia (`consents.ip`, `access_audit_log.ip`, `login_tokens.ip_solicitud`) es el **último** salto de `X-Forwarded-For`: Radar corre detrás de un único proxy confiable (Railway) y el primer valor lo puede inventar el cliente.
- `lines.estado ∈ {vinculada, sin_vinculo, de_baja}`; `lines.almacen_fuente ∈ {permanente, purgable}`, por defecto `permanente`. En este tramo solo existe el almacén permanente: `retencion_fuente_dias > 0` se rechaza con 422 hasta el tramo 6.
- Los emails llevan solo conteos y un link autenticado; nunca datos de conversación. El mailer loguea solo el dominio del destinatario y una huella (sha256 truncada) del token, jamás el token.
- Nada de f-strings con entrada de usuario en SQL; todo parámetro va con `$n`. Los nombres de columna que se interpolan salen de listas fijas del código.
- Prohibido loguear `payload`, `body`, prompts o respuestas de modelos (§6.3 punto 8). En este tramo no hay cuerpos de mensajes; los logs de Radar llevan solo UUID, tipo de error y método + ruta.
- Tests: `python -m pytest tests/radar_tests -q` (con `python -m` para que `app` esté en `sys.path`). Todos los endpoints se prueban con httpx `ASGITransport` contra pgserver, conectando como `radar_app`.

---

## Decisiones de este plan

El spec deja abiertos estos puntos; se eligió en cada caso lo más simple que lo cumple.

1. **Aislamiento de migraciones.** `app/main.py` corre `alembic upgrade head` en cada arranque, y hoy hay un despliegue por cliente del bot. Radar tiene su propio entorno: `alembic_radar.ini` + `migrations_radar/` con `version_table = alembic_version_radar`, y un segundo entorno `alembic_fuente.ini` + `migrations_fuente/` (`alembic_version_fuente`) para el almacén de fuente. Ambos comparten `app/radar/alembic_env.py`, que **nunca lee `DATABASE_URL`**: la URL la fija `app/radar/migrate.py`. Corren solo en el lifespan de modo radar y, a diferencia del bot, **una migración fallida impide el arranque** (RLS depende del esquema). En tests, la fixture `radar_urls` los aplica sobre pgserver.
2. **Modo de la app.** `Settings.app_mode: str = "bot"` (env `APP_MODE`). `app/main.py` pasa a tener `crear_app(settings)`: en `bot` monta lo de siempre; en `radar` delega en `app/radar/app.py::crear_app_radar()`. `app = crear_app()` sigue existiendo para `uvicorn app.main:app`. Un test crea las dos apps y verifica qué rutas monta cada una.
3. **Roles de base.** Tres roles: el **migrator** (dueño de las tablas; en Railway puede ser el usuario por defecto), `radar_app` (login, `NOSUPERUSER NOBYPASSRLS NOINHERIT`, con `SELECT/INSERT/UPDATE` selectivos, **sin** `INSERT` en `tenants` ni `DELETE` en tablas de auditoría) y `radar_admin` (`NOLOGIN`, dueño de tres funciones `SECURITY DEFINER`: `radar_admin_crear_tenant`, `radar_admin_listar_tenants`, `radar_auth_usuarios_por_email`). Los roles los crea una vez `scripts/radar_bootstrap_roles.sql` (necesita `CREATEROLE`), y la migración r0001 falla con un mensaje claro si faltan. Se eligió la función `SECURITY DEFINER` sobre una segunda URL de admin porque no agrega un pool ni una credencial más, y porque `radar_app` no puede hacer `SET ROLE radar_admin` (`NOINHERIT`, sin membresía). Cada llamada a esas funciones queda auditada por la app. El usuario por defecto de pgserver es superusuario, así que la fixture **crea los roles y conecta como `radar_app`**; sin eso los tests de RLS no probarían nada, y `RadarDB.connect()` lo rechaza de todas formas.
4. **Tenant KIS.** Como toda tabla lleva `tenant_id NOT NULL`, los admins de KIS son usuarios del tenant fijo `TENANT_KIS = 00000000-0000-0000-0000-000000000001` (`tenants.es_kis = true`), sembrado por r0001 antes de activar RLS. El `CHECK` de `memberships` solo admite `rol = 'admin'` en ese tenant. Un admin actúa sobre clientes por `/radar/admin/*` (tenant en la ruta), nunca desde su propia sesión de tenant.
5. **Almacén de fuente.** Solo se cablea `fuente_permanente`: `RADAR_FUENTE_DATABASE_URL`, migración `f0001` con la tabla `fuente_meta` (`almacen = 'permanente'`, versión de esquema), `FuenteStore.salud()` y `lines.almacen_fuente DEFAULT 'permanente'`. El almacén purgable no se construye (tramo 6); la columna conserva los valores `permanente|purgable` y el `CHECK (almacen_fuente = 'purgable') = (retencion_fuente_dias > 0)` deja la coherencia en la base. En este tramo el acceso al almacén de fuente usa una sola URL (no hay tablas con datos); los roles del almacén de fuente se definen en el tramo 3 junto con sus tablas. Consecuencia para el perfil `sensible`: su propuesta de `retencion_fuente_dias = 7` (§2.1, "una recomendación, no una regla") **no se aplica** en el alta, porque exigiría el almacén purgable; queda visible en `GET /radar/admin/propuesta` para que el admin la tenga presente cuando exista.
6. **Secretos.** `SecretStore` (`get/set/delete` de bytes) con `FileSecretStore` (un archivo por secreto, base64, permisos 0600, en `RADAR_SECRETS_DIR`) para dev, tests y producción sobre un volumen de Railway fuera de los backups de Postgres, y `MemorySecretStore` para tests unitarios. Elegir un gestor de secretos externo queda como decisión del dueño (ver Self-Review). Normalización E.164 solo para Argentina: `+54…`, `54…` (como llegan en los ids de WhatsApp) y nacional con `0` inicial y `15` opcional; cualquier otro país lanza `TelefonoNoSoportado`.
7. **Email.** Interfaz `Mailer.enviar(Email)` con `MemoryMailer` (tests) y `LogMailer` (dev/staging: loguea `*@dominio`, asunto y huella del token). El proveedor transaccional de producción queda como decisión pendiente del dueño; `RADAR_MAILER` solo acepta `log|memoria` y falla al arrancar con otro valor.
8. **Sesión.** Cookie `radar_sesion = "<tenant_id>.<token>.<firma>"`, firma HMAC-SHA256 con `RADAR_COOKIE_SECRET` (mínimo 32 caracteres, lo exige `validar_settings`), `Path=/radar`, 30 días; la fila de `sessions` guarda `token_hash` y permite revocar. El link mágico es `…/radar/login/canjear?t=<tenant>#k=<token>`: el token va en el **fragmento**, que el navegador nunca manda al servidor, así no queda en el access log de uvicorn (que escribe `path?query`) ni en los logs HTTP de Railway. La página de canje es un formulario POST con un único script inline que copia `#k` al campo oculto y **no auto-envía**: un escáner de links del correo (aunque ejecute JS) no consume el token, y la página lleva `Content-Security-Policy` con el hash de ese script (`default-src 'none'`, §7 "aplicar CSP"). La fila de `login_tokens` se marca usada con un único `UPDATE … WHERE used_at IS NULL RETURNING`. Límite de 3 pedidos de link por usuario cada 15 minutos, contado en `login_tokens`; la respuesta de `POST /radar/login` es siempre `202` para no revelar si el email existe.
9. **Cambios laxos con consentimiento: se guarda la propuesta.** §2.1 dice que los parámetros "los fija un admin de KIS" y que el cliente "solo puede endurecer"; el consentimiento es la aceptación del dueño de un valor más laxo que KIS propuso, no un canal para aflojar por su cuenta. Por eso, cuando un admin intenta un valor más laxo sobre una línea `vinculada` (o de tenant con líneas vivas) recibe `409 requiere_consentimiento` **y la app guarda esa propuesta** en `lines.parametros_propuestos` / `tenants.parametros_propuestos` (JSONB, se fusiona con la anterior) auditándola como `parametro_propuesto`. El dueño la ve en `GET /radar/api/lineas/{id}/parametros` (`propuesta`) y la acepta con `POST /radar/api/lineas/{id}/consentimientos`, que **solo admite valores que coincidan con la propuesta** (`422 sin_propuesta` si falta alguno); al aplicarse, esos nombres se quitan de la propuesta. Para un parámetro de **tenant** más laxo con líneas vivas se exige que **cada** línea `vinculada` tenga un consentimiento cuyas `opciones.parametros_tenant` incluya esos valores; el valor se aplica (y la propuesta se consume) cuando la última lo cumple.
10. **`tope_ia_mensual_usd`.** El spec lo deja "a definir en el piloto": queda `NULL` (sin tope). Para el orden de estrictez, `NULL` juega el papel del `0` de las retenciones (lo más laxo) y entre montos gana el menor.
11. **Sesión de soporte.** `support_grants` es por tenant (el dueño no conoce a los usuarios de KIS). Con un grant vigente, un admin de KIS pide `POST /radar/admin/tenants/{id}/sesion-soporte` y recibe una cookie de sesión **de ese tenant** con `rol = soporte`, cuyo vencimiento es el del grant. El `/yo` de una sesión de soporte devuelve `email = null`, porque el usuario vive en el tenant KIS y RLS lo oculta desde el tenant del cliente (la FK sí se valida: las comprobaciones de integridad referencial no pasan por RLS).
12. **Sin CORS en modo radar.** La cookie es same-origin; no se agrega `CORSMiddleware` (el bot conserva el suyo).

---

## File Structure

```
app/
├── config.py                          # MOD: Settings.app_mode ("bot" | "radar")
├── middleware.py                      # NUEVO: log_errores compartido por los dos modos
├── main.py                            # MOD: crear_app(settings) — modo bot igual que hoy, modo radar delega
└── radar/
    ├── __init__.py                    # paquete
    ├── constantes.py                  # TENANT_KIS y su forma texto
    ├── settings.py                    # RadarSettings (prefijo RADAR_)
    ├── app.py                         # crear_app_radar, lifespan radar (migra, arma el contexto)
    ├── contexto.py                    # RadarContexto (settings, db, fuente, secretos, mailer) + contexto(request)
    ├── db.py                          # RadarDB: pool radar_app, tenant_tx, sin_tenant; lanza, no traga
    ├── alembic_env.py                 # correr(): entorno Alembic compartido por migrations_radar y migrations_fuente
    ├── migrate.py                     # migrar_resultados(url), migrar_fuente(url)
    ├── rls_sql.py                     # politica_por_tenant(), grants_app(), definir_funcion_admin()
    ├── fuente.py                      # FuenteStore: pool al almacén de fuente + salud()
    ├── secrets.py                     # SecretStore, FileSecretStore, MemorySecretStore, k_tenant, contact_hmac, lid_hmac
    ├── telefonos.py                   # normalizar_e164 (Argentina)
    ├── parametros.py                  # tabla §2.1, comparar/es_mas_estricto, propuesta_para_perfil, perfil_por_rubro
    ├── auditoria.py                   # registrar() en access_audit_log con lista blanca de detalle
    ├── eventos_producto.py            # registrar_evento() en product_events (solo valores numéricos)
    ├── mailer.py                      # Email, Mailer, MemoryMailer, LogMailer, huella_token
    ├── auth.py                        # tokens, cookie firmada, sessions, sesion_actual, requiere_rol
    ├── links.py                       # enviar_link (login/invitación) con límite por usuario
    ├── lineas.py                      # crear_linea, listar_lineas, leer_linea
    ├── parametros_service.py          # cambiar_parametros_linea / _tenant (estrictez, consentimiento) y propuestas de KIS
    ├── consentimiento.py              # textos versionados de P2, hash, registrar_consentimiento
    ├── admin_kis.py                   # crear_admin_kis (primer admin), crear_tenant_con_dueno
    └── routers/
        ├── __init__.py
        ├── health.py                  # GET /health en modo radar
        ├── login.py                   # POST /radar/login, GET+POST /radar/login/canjear, POST /radar/logout
        ├── cuenta.py                  # GET /radar/api/yo, /lineas, /usuarios, POST /usuarios, PUT /usuarios/{id}/rol
        ├── parametros.py              # GET/PUT /radar/api/lineas/{id}/parametros, POST/GET consentimientos
        ├── soporte.py                 # POST/GET/DELETE /radar/api/soporte
        └── admin.py                   # /radar/admin/tenants…, lineas, parametros, sesion-soporte
alembic_radar.ini                      # NUEVO
alembic_fuente.ini                     # NUEVO
migrations_radar/env.py                # NUEVO: from app.radar.alembic_env import correr; correr()
migrations_radar/versions/r0001_tenants_y_rls.py
migrations_radar/versions/r0002_acceso_lineas_y_auditoria.py
migrations_fuente/env.py               # NUEVO
migrations_fuente/versions/f0001_fuente_meta.py
scripts/radar_bootstrap_roles.sql      # NUEVO: roles radar_app / radar_admin (una vez, como superusuario)
scripts/radar_admin.py                 # NUEVO: crear-admin (primer admin de KIS)
docs/radar-despliegue.md               # NUEVO: variables, roles, secretos, arranque
DEVELOPMENT.md                         # MOD: sección Radar
tests/radar_tests/
├── __init__.py
├── conftest.py                        # radar_urls (pgserver + roles), radar_db, radar_ctx, cliente
├── helpers.py                         # crear_tenant_directo, crear_usuario, crear_linea_directa, entrar, DOMINIO_COOKIE
├── test_modo.py                       # Task 1
├── test_rls.py                        # Task 2
├── test_fuente_y_health.py            # Task 3
├── test_secretos.py                   # Task 4
├── test_telefonos.py                  # Task 4
├── test_parametros.py                 # Task 5
├── test_esquema.py                    # Task 6
├── test_auditoria.py                  # Task 7
├── test_auth.py                       # Task 8
├── test_login.py                      # Task 9
├── test_admin.py                      # Task 10
├── test_cuenta.py                     # Task 11
├── test_parametros_api.py             # Task 12
└── test_soporte.py                    # Task 13
```

---

### Task 1: Modo de app y fábrica `crear_app`

**Files:**
- Modify: `app/config.py`, `app/main.py`
- Create: `app/middleware.py`, `app/radar/__init__.py`, `app/radar/settings.py`, `app/radar/app.py`, `app/radar/routers/__init__.py`, `app/radar/routers/health.py`
- Test: `tests/radar_tests/__init__.py`, `tests/radar_tests/test_modo.py`

**Interfaces:**
- Consumes: `app.config.Settings`, routers del bot ya existentes.
- Produces: `Settings.app_mode: str`; `app.main.crear_app(settings: Settings | None = None) -> FastAPI`; `app.middleware.log_errores(request, call_next)`; `app.radar.settings.RadarSettings` y `get_radar_settings() -> RadarSettings`; `app.radar.app.crear_app_radar(rs: RadarSettings | None = None, contexto=None) -> FastAPI`; `GET /health` en modo radar → `{"status": "ok", "modo": "radar", "commit": str | None}`.

- [ ] **Step 1: Test de modos (falla)**

`tests/radar_tests/__init__.py` vacío. `tests/radar_tests/test_modo.py`:

```python
"""
Una imagen, dos modos. En modo bot no se monta nada de Radar; en modo radar
no se monta nada del bot (§6.2: "En ese despliegue el router del bot no se monta").
"""
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import crear_app


def _rutas(app) -> set[str]:
    return {r.path for r in app.routes if hasattr(r, "path")}


def test_modo_por_defecto_es_bot():
    assert Settings(_env_file=None).app_mode == "bot"


def test_modo_bot_no_monta_radar():
    rutas = _rutas(crear_app(Settings(_env_file=None, app_mode="bot")))
    assert {"/webhook", "/bo", "/health"} <= rutas
    assert not any(p.startswith("/radar") for p in rutas)


def test_modo_radar_no_monta_bot():
    rutas = _rutas(crear_app(Settings(_env_file=None, app_mode="radar")))
    assert "/health" in rutas
    assert "/webhook" not in rutas
    assert "/bo" not in rutas
    assert "/simulate" not in rutas


def test_modo_invalido_no_arranca():
    with pytest.raises(RuntimeError):
        crear_app(Settings(_env_file=None, app_mode="otro"))


async def test_health_radar_dice_modo():
    app = crear_app(Settings(_env_file=None, app_mode="radar"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        r = await c.get("/health")
    assert r.status_code == 200
    assert r.json()["modo"] == "radar"
```

- [ ] **Step 2: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_modo.py -q
```
Esperado: `ImportError: cannot import name 'crear_app' from 'app.main'` (5 errores de colección).

- [ ] **Step 3: `Settings.app_mode`**

En `app/config.py`, dentro de `class Settings`, después de `env: str = "development"`:

```python
    # Modo del despliegue: "bot" (Remedia; por defecto, nada cambia para los
    # despliegues existentes) o "radar" (despliegue propio de Radar, §6.2:
    # routers de Radar y ninguno del bot).
    app_mode: str = "bot"
```

- [ ] **Step 4: Middleware compartido**

`app/middleware.py`:

```python
"""Middleware compartido por los dos modos de la app (bot y Radar)."""

import logging

_log = logging.getLogger("app.errors")


async def log_errores(request, call_next):
    """Loguea cualquier 5xx con método + ruta para rastrearlo en Railway.
    Nunca loguea cuerpos ni query strings."""
    try:
        response = await call_next(request)
    except Exception as e:
        _log.exception(f"💥 500 en {request.method} {request.url.path} — {type(e).__name__}: {e}")
        raise
    if response.status_code >= 500:
        _log.error(f"💥 {response.status_code} en {request.method} {request.url.path}")
    return response
```

- [ ] **Step 5: Paquete Radar, settings y app mínima**

`app/radar/__init__.py` y `app/radar/routers/__init__.py` vacíos.

`app/radar/settings.py`:

```python
"""
Configuración propia de Radar. Todas las variables llevan el prefijo RADAR_.

Radar corre como despliegue aparte (APP_MODE=radar) con Postgres y Redis
propios (§6.2, S7). Nunca comparte la base de un cliente del bot, por eso
ninguna de estas URLs es DATABASE_URL.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class RadarSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RADAR_", env_file=".env", extra="ignore")

    # Base de resultados, dos URLs y dos roles (§6.4, RLS efectiva).
    database_url: str = ""            # rol radar_app: NOSUPERUSER, NOBYPASSRLS, no dueño
    migrator_database_url: str = ""   # dueño de las tablas; solo Alembic

    # Almacén de fuente permanente (§6.4). En este tramo no tiene tablas de
    # conversación: solo su marcador de esquema.
    fuente_database_url: str = ""

    # Secretos fuera de la base: directorio del FileSecretStore (volumen,
    # fuera de los backups de Postgres). Ver docs/radar-despliegue.md.
    secrets_dir: str = "/data/radar-secrets"

    # Cookie de sesión firmada. Vacío = la app no arranca en modo radar.
    cookie_secret: str = ""
    cookie_secure: bool = True

    # Base pública para armar links mágicos, ej. https://radar.keepitsimple.com.ar
    public_base_url: str = "http://localhost:8000"

    # Backend de email: "log" (solo dominio y huella del token) o "memoria" (tests).
    mailer: str = "log"
    remitente: str = "radar@keepitsimple.com.ar"


@lru_cache
def get_radar_settings() -> RadarSettings:
    return RadarSettings()
```

`app/radar/routers/health.py`:

```python
"""Health check del despliegue de Radar (Railway lo consulta en /health)."""

import os

from fastapi import APIRouter, Request

router = APIRouter(tags=["radar-health"])


def _commit() -> str | None:
    return (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "")[:9] or None


@router.get("/health")
async def health(request: Request):
    return {"status": "ok", "modo": "radar", "commit": _commit()}
```

`app/radar/app.py`:

```python
"""
App de Radar (APP_MODE=radar). Misma imagen que el bot, routers distintos,
sin CORS (la cookie de sesión es same-origin).
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.middleware import log_errores
from app.radar.routers import health
from app.radar.settings import RadarSettings, get_radar_settings


@asynccontextmanager
async def _lifespan_radar(app: FastAPI):
    yield


def crear_app_radar(rs: RadarSettings | None = None, contexto=None) -> FastAPI:
    rs = rs or get_radar_settings()
    app = FastAPI(title="Radar", version="0.1.0", lifespan=_lifespan_radar)
    app.state.radar_settings = rs
    app.state.radar = contexto
    app.middleware("http")(log_errores)
    app.include_router(health.router)
    return app
```

- [ ] **Step 6: `crear_app` en `app/main.py`**

Reemplazar en `app/main.py` desde la línea `app = FastAPI(` hasta el final del archivo por:

```python
def crear_app(settings=None) -> FastAPI:
    """Una imagen, dos modos (APP_MODE). En modo radar no se monta ningún
    router del bot ni sus páginas; en modo bot no se monta nada de Radar."""
    settings = settings or get_settings()
    if settings.app_mode == "radar":
        from app.radar.app import crear_app_radar
        return crear_app_radar()
    if settings.app_mode != "bot":
        raise RuntimeError(f"APP_MODE inválido: {settings.app_mode!r} (bot|radar)")

    app = FastAPI(title="Remedia Bot", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.middleware("http")(log_errores)

    for modulo in (webhook, simulate, backoffice, mp_webhook, orders_api, media,
                   payway, sync_api, agent_ws, backoffice_branches):
        app.include_router(modulo.router)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def root():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/bo")
    async def backoffice_ui():
        return FileResponse(STATIC_DIR / "backoffice.html")

    @app.get("/backoffice")
    async def orders_ui():
        return FileResponse(STATIC_DIR / "orders.html")

    @app.get("/dashboard")
    async def dashboard_ui():
        return FileResponse(STATIC_DIR / "dashboard.html")

    @app.get("/tablero")
    async def tablero_ui():
        """Tablero CERCA: indicadores por vertical (diseño 'Tableros CERCA')."""
        return FileResponse(STATIC_DIR / "tablero.html")

    @app.get("/health")
    async def health():
        import os
        return {
            "status": "ok",
            "bot": "Remedia",
            # Railway inyecta el SHA del commit deployado — permite verificar qué
            # versión está corriendo sin mirar los logs.
            "commit": (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "")[:9] or None,
        }

    return app


app = crear_app()
```

Y en los imports del tope de `app/main.py` agregar `from app.middleware import log_errores`. Borrar la función `_log_errores` original (quedó en `app/middleware.py`).

- [ ] **Step 7: Correr los tests nuevos y los viejos del bot**

```bash
python -m pytest tests/radar_tests/test_modo.py -q
python -m pytest tests/test_logic.py -q -k "rutas_webhook_con_comercio or health"
```
Esperado: `5 passed` y los del bot en verde (`m.app.routes` sigue existiendo).

- [ ] **Step 8: Commit**

```bash
git checkout -b feature/radar-tramo1
git add app/config.py app/main.py app/middleware.py app/radar tests/radar_tests
git commit -m "Radar: modo de app (APP_MODE) y fabrica crear_app"
```

---

### Task 2: Migraciones de Radar, roles de RLS, `RadarDB.tenant_tx` y tenants

**Files:**
- Create: `alembic_radar.ini`, `migrations_radar/env.py`, `migrations_radar/versions/r0001_tenants_y_rls.py`, `app/radar/constantes.py`, `app/radar/alembic_env.py`, `app/radar/migrate.py`, `app/radar/rls_sql.py`, `app/radar/db.py`, `scripts/radar_bootstrap_roles.sql`
- Test: `tests/radar_tests/conftest.py`, `tests/radar_tests/test_rls.py`

**Interfaces:**
- Consumes: pgserver (`get_server(dir).get_postmaster_info().get_uri(user=, database=)`), asyncpg, Alembic `command.upgrade`.
- Produces: `TENANT_KIS: uuid.UUID`, `TENANT_KIS_STR: str`; `migrar_resultados(migrator_url: str) -> None`; `politica_por_tenant(tabla: str, columna: str = "tenant_id") -> str`, `grants_app(tabla: str, privilegios: str) -> str`, `definir_funcion_admin(firma: str, create_sql: str) -> str`; `RadarDB(dsn: str, max_size: int = 5)` con `connect() -> None` (lanza si el rol es superusuario/BYPASSRLS), `close()`, `tenant_tx(tenant_id: uuid.UUID) -> AsyncIterator[asyncpg.Connection]`, `sin_tenant() -> AsyncIterator[asyncpg.Connection]`; funciones SQL `radar_tenant_actual() RETURNS uuid`, `radar_admin_crear_tenant(p_id uuid, p_nombre text, p_rubro text, p_perfil text, p_retencion_fichas_meses int, p_retener_fragmentos bool, p_ia_habilitada bool, p_via_llm text) RETURNS uuid`, `radar_admin_listar_tenants() RETURNS TABLE (id uuid, nombre text, rubro text, perfil_de_datos text, created_at timestamptz)`; fixtures `radar_urls` (session) y `radar_db` (function).

- [ ] **Step 1: Fixture con pgserver y los roles reales**

`tests/radar_tests/conftest.py`:

```python
"""
Fixtures de Radar.

Postgres embebido (pgserver) con los roles reales de RLS. El usuario por
defecto de pgserver es superusuario, y un superusuario ignora RLS: los tests
de aislamiento solo valen conectando como `radar_app`. Acá se crean, igual
que scripts/radar_bootstrap_roles.sql en producción:

- radar_migrator: dueño de las tablas, corre Alembic (INHERIT, para tener
  CREATE en public como dueño de la base).
- radar_app: la app. NOSUPERUSER, NOBYPASSRLS, NOINHERIT, no dueño.
- radar_admin: NOLOGIN, dueño de las funciones SECURITY DEFINER.
"""

import os
import tempfile

import asyncpg
import psycopg2
import pytest

from app.radar.constantes import TENANT_KIS
from app.radar.db import RadarDB
from app.radar.migrate import migrar_resultados

ROLES_SQL = """
    CREATE ROLE radar_migrator LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    CREATE ROLE radar_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
    CREATE ROLE radar_admin NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
    GRANT radar_admin TO radar_migrator;
"""

# Orden de TRUNCATE: hijas antes que padres. Se amplía en la Task 6.
TABLAS = ["tenants"]


@pytest.fixture(scope="session")
def radar_urls():
    try:
        import pgserver
    except ImportError:
        pytest.skip("pgserver no instalado")

    tmp = tempfile.mkdtemp(prefix="pgtest_radar_")
    srv = pgserver.get_server(tmp)
    info = srv.get_postmaster_info()

    su = psycopg2.connect(info.get_uri())
    su.autocommit = True
    cur = su.cursor()
    cur.execute(ROLES_SQL)
    cur.execute("CREATE DATABASE radar_test OWNER radar_migrator")
    su.close()

    urls = {
        "super": info.get_uri(database="radar_test"),
        "migrator": info.get_uri(user="radar_migrator", database="radar_test"),
        "app": info.get_uri(user="radar_app", database="radar_test"),
    }
    migrar_resultados(urls["migrator"])
    yield urls
    try:
        srv.cleanup()
    except Exception:
        pass


async def _limpiar(url_super: str) -> None:
    """Vacía las tablas y vuelve a sembrar el tenant KIS. Como superusuario:
    con FORCE RLS ni el dueño puede insertar sin política."""
    con = await asyncpg.connect(url_super)
    try:
        await con.execute("TRUNCATE " + ", ".join(TABLAS) + " CASCADE")
        await con.execute(
            "INSERT INTO tenants (id, nombre, rubro, es_kis) VALUES ($1, 'Keep IT Simple', 'kis', TRUE)",
            TENANT_KIS,
        )
    finally:
        await con.close()


@pytest.fixture
async def radar_db(radar_urls):
    await _limpiar(radar_urls["super"])
    db = RadarDB(radar_urls["app"], max_size=2)
    await db.connect()
    yield db
    await db.close()
```

- [ ] **Step 2: Tests de RLS (fallan)**

`tests/radar_tests/test_rls.py`:

```python
"""
RLS efectiva, no declarativa (§6.4). Todos los tests corren como radar_app.
"""
import uuid

import asyncpg
import pytest

from app.radar.constantes import TENANT_KIS
from app.radar.db import RadarDB


async def _crear_tenant(db: RadarDB, nombre: str) -> uuid.UUID:
    tid = uuid.uuid4()
    async with db.tenant_tx(tid) as con:
        await con.fetchval(
            "SELECT radar_admin_crear_tenant($1, $2, 'farmacia', 'estandar', 12, FALSE, TRUE, 'lotes')",
            tid, nombre,
        )
    return tid


async def test_connect_rechaza_superusuario(radar_urls):
    db = RadarDB(radar_urls["super"])
    with pytest.raises(RuntimeError, match="BYPASSRLS|superusuario"):
        await db.connect()


async def test_tenant_tx_solo_acepta_uuid(radar_db):
    with pytest.raises(TypeError):
        async with radar_db.tenant_tx("no-es-uuid"):
            pass


async def test_sin_tenant_cero_filas(radar_db):
    await _crear_tenant(radar_db, "A")
    async with radar_db.sin_tenant() as con:
        assert await con.fetchval("SELECT count(*) FROM tenants") == 0


async def test_conexion_devuelta_al_pool_no_conserva_tenant(radar_urls):
    db = RadarDB(radar_urls["app"], max_size=1)   # una sola conexión: la misma vuelve
    await db.connect()
    try:
        a = await _crear_tenant(db, "A")
        async with db.tenant_tx(a) as con:
            assert await con.fetchval("SELECT count(*) FROM tenants") == 1
        async with db.pool.acquire() as con:
            assert await con.fetchval("SELECT current_setting('app.tenant_id', true)") in ("", None)
            assert await con.fetchval("SELECT count(*) FROM tenants") == 0
    finally:
        await db.close()


async def test_tenant_a_no_lee_ni_toca_b(radar_db):
    a = await _crear_tenant(radar_db, "A")
    b = await _crear_tenant(radar_db, "B")
    async with radar_db.tenant_tx(a) as con:
        nombres = [r["nombre"] for r in await con.fetch("SELECT nombre FROM tenants")]
        assert nombres == ["A"]
        # SQL directo con el id de B: RLS lo vuelve invisible
        assert await con.fetchval("SELECT count(*) FROM tenants WHERE id = $1", b) == 0
        assert await con.execute("UPDATE tenants SET nombre = 'hackeado' WHERE id = $1", b) == "UPDATE 0"
    async with radar_db.tenant_tx(b) as con:
        assert await con.fetchval("SELECT nombre FROM tenants WHERE id = $1", b) == "B"


async def test_radar_app_no_puede_insertar_tenants_directo(radar_db):
    tid = uuid.uuid4()
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with radar_db.tenant_tx(tid) as con:
            await con.execute("INSERT INTO tenants (id, nombre) VALUES ($1, 'x')", tid)


async def test_radar_app_no_puede_asumir_radar_admin(radar_db):
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with radar_db.sin_tenant() as con:
            await con.execute("SET ROLE radar_admin")


async def test_listar_tenants_via_funcion_admin_excluye_kis(radar_db):
    await _crear_tenant(radar_db, "A")
    await _crear_tenant(radar_db, "B")
    async with radar_db.sin_tenant() as con:
        filas = await con.fetch("SELECT * FROM radar_admin_listar_tenants()")
    assert sorted(r["nombre"] for r in filas) == ["A", "B"]
    assert TENANT_KIS not in {r["id"] for r in filas}


async def test_tenant_kis_sembrado(radar_db):
    async with radar_db.tenant_tx(TENANT_KIS) as con:
        assert await con.fetchval("SELECT es_kis FROM tenants WHERE id = $1", TENANT_KIS) is True


def test_radar_no_usa_el_db_del_bot():
    """execute()/fetch() de app/services/db.py tragan errores y toman una
    conexión cualquiera del pool: prohibidos en Radar (§6.4)."""
    import pathlib
    raiz = pathlib.Path(__file__).resolve().parents[2] / "app" / "radar"
    for archivo in raiz.rglob("*.py"):
        assert "app.services.db" not in archivo.read_text(encoding="utf-8"), archivo
```

- [ ] **Step 3: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_rls.py -q
```
Esperado: `ModuleNotFoundError: No module named 'app.radar.constantes'` en la colección.

- [ ] **Step 4: Constantes y SQL de RLS**

`app/radar/constantes.py`:

```python
"""Constantes de Radar compartidas por la app, las migraciones y los tests."""

import uuid

# Tenant fijo de Keep IT Simple: ahí viven los admins de KIS (rol `admin`).
# Lo siembra la migración r0001 antes de activar RLS sobre `tenants`.
TENANT_KIS_STR = "00000000-0000-0000-0000-000000000001"
TENANT_KIS = uuid.UUID(TENANT_KIS_STR)
```

`app/radar/rls_sql.py`:

```python
"""
SQL de RLS efectiva (§6.4), compartido por las migraciones de Radar.

Los nombres de tabla y función vienen de las migraciones (constantes del
repo), nunca de entrada de usuario: por eso acá sí se interpolan.
"""


def politica_por_tenant(tabla: str, columna: str = "tenant_id") -> str:
    """ENABLE + FORCE RLS y la política de radar_app sobre `columna`.

    radar_tenant_actual() es NULL cuando app.tenant_id no está fijada o está
    vacía, y `columna = NULL` nunca es true: sin tenant hay cero filas, tanto
    para leer (USING) como para escribir (WITH CHECK).
    """
    return f"""
        ALTER TABLE {tabla} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {tabla} FORCE ROW LEVEL SECURITY;
        CREATE POLICY {tabla}_por_tenant ON {tabla} FOR ALL TO radar_app
            USING ({columna} = radar_tenant_actual())
            WITH CHECK ({columna} = radar_tenant_actual());
    """


def grants_app(tabla: str, privilegios: str) -> str:
    return f"GRANT {privilegios} ON {tabla} TO radar_app;"


def definir_funcion_admin(firma: str, create_sql: str) -> str:
    """Crea una función SECURITY DEFINER propiedad de radar_admin, ejecutable
    solo por radar_app. Cambiar el dueño exige que radar_admin tenga CREATE en
    el esquema; se le da y se le quita en el mismo paso."""
    return f"""
        GRANT CREATE ON SCHEMA public TO radar_admin;
        {create_sql}
        ALTER FUNCTION {firma} OWNER TO radar_admin;
        REVOKE ALL ON FUNCTION {firma} FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION {firma} TO radar_app;
        REVOKE CREATE ON SCHEMA public FROM radar_admin;
    """
```

- [ ] **Step 5: Entorno Alembic compartido y `migrate.py`**

`app/radar/alembic_env.py`:

```python
"""
Entorno compartido por los dos árboles de migraciones de Radar
(migrations_radar/ y migrations_fuente/). Cada uno tiene su .ini con su
`script_location` y su `version_table`; la URL la fija quien invoca
(app/radar/migrate.py) en `sqlalchemy.url`, o `-x url=...` desde la línea
de comandos. Nunca lee DATABASE_URL: esa es la base de un cliente del bot y
Radar no debe tocarla.
"""

from alembic import context
from sqlalchemy import engine_from_config, pool


def correr() -> None:
    config = context.config
    url = config.get_main_option("sqlalchemy.url") or ""
    if not url:
        url = context.get_x_argument(as_dictionary=True).get("url", "")
    if not url:
        raise RuntimeError("Falta la URL: usar app.radar.migrate o `alembic -c <ini> -x url=... upgrade head`")
    version_table = config.get_main_option("version_table") or "alembic_version_radar"

    if context.is_offline_mode():
        context.configure(url=url, target_metadata=None, literal_binds=True,
                          version_table=version_table)
        with context.begin_transaction():
            context.run_migrations()
        return

    section = dict(config.get_section(config.config_ini_section) or {})
    section["sqlalchemy.url"] = url
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=None,
                          version_table=version_table)
        with context.begin_transaction():
            context.run_migrations()
```

`migrations_radar/env.py`:

```python
from app.radar.alembic_env import correr

correr()
```

`alembic_radar.ini`:

```ini
# Migraciones de la base de RESULTADOS de Radar. Tabla de versión propia para
# que nunca se mezcle con la del bot (alembic_version).
[alembic]
script_location = migrations_radar
version_table = alembic_version_radar
prepend_sys_path = .

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARNING
handlers = console
qualname =

[logger_sqlalchemy]
level = WARNING
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

`app/radar/migrate.py`:

```python
"""
Aplica las migraciones de Radar. Se llama desde el lifespan de modo radar
(fallar acá impide el arranque) y desde la fixture de tests.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config

RAIZ = Path(__file__).resolve().parent.parent.parent


def _url_psycopg(url: str) -> str:
    """Alembic usa psycopg2: normaliza el esquema y escapa % para configparser."""
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql+asyncpg://"):
        url = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    return url.replace("%", "%%")


def _config(ini: str, url: str) -> Config:
    if not url:
        raise RuntimeError(f"URL vacía para {ini}")
    cfg = Config(str(RAIZ / ini))
    cfg.set_main_option("script_location", str(RAIZ / cfg.get_main_option("script_location")))
    cfg.set_main_option("sqlalchemy.url", _url_psycopg(url))
    return cfg


def migrar_resultados(migrator_url: str) -> None:
    command.upgrade(_config("alembic_radar.ini", migrator_url), "head")
```

- [ ] **Step 6: Migración r0001**

`migrations_radar/versions/r0001_tenants_y_rls.py`:

```python
"""Radar r0001: tenants, RLS efectiva y funciones de administración de KIS.

Los roles radar_app y radar_admin los crea scripts/radar_bootstrap_roles.sql
(necesita CREATEROLE). Acá solo se verifica que existan, para fallar con un
mensaje claro y no con un GRANT roto a mitad de camino.

Revision ID: r0001
Revises: None
Create Date: 2026-09-21
"""
from alembic import op

from app.radar.constantes import TENANT_KIS_STR
from app.radar.rls_sql import definir_funcion_admin, politica_por_tenant

revision = "r0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'radar_app') THEN
                RAISE EXCEPTION 'falta el rol radar_app: correr scripts/radar_bootstrap_roles.sql';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'radar_admin') THEN
                RAISE EXCEPTION 'falta el rol radar_admin: correr scripts/radar_bootstrap_roles.sql';
            END IF;
        END $$;
        GRANT USAGE ON SCHEMA public TO radar_app;
        GRANT USAGE ON SCHEMA public TO radar_admin;

        -- Tenant actual de la transacción (lo fija RadarDB.tenant_tx). NULL si no hay.
        CREATE FUNCTION radar_tenant_actual() RETURNS uuid
            LANGUAGE sql STABLE
            AS $$ SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid $$;
        GRANT EXECUTE ON FUNCTION radar_tenant_actual() TO radar_app;
        GRANT EXECUTE ON FUNCTION radar_tenant_actual() TO radar_admin;
    """)

    op.execute("""
        CREATE TABLE tenants (
            id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            nombre                 TEXT NOT NULL CHECK (length(nombre) BETWEEN 1 AND 120),
            rubro                  TEXT NOT NULL DEFAULT 'otro' CHECK (rubro ~ '^[a-z_]{1,40}$'),
            es_kis                 BOOLEAN NOT NULL DEFAULT FALSE,
            -- Parámetros de ámbito Tenant (§2.1)
            perfil_de_datos        TEXT NOT NULL DEFAULT 'estandar'
                                   CHECK (perfil_de_datos IN ('estandar', 'sensible')),
            retencion_fichas_meses INTEGER NOT NULL DEFAULT 12 CHECK (retencion_fichas_meses >= 0),
            retener_fragmentos     BOOLEAN NOT NULL DEFAULT FALSE,
            ia_habilitada          BOOLEAN NOT NULL DEFAULT TRUE,
            via_llm                TEXT NOT NULL DEFAULT 'lotes' CHECK (via_llm IN ('lotes', 'sincronica')),
            -- Propuesta pendiente de un admin de KIS para aflojar parámetros de
            -- tenant con líneas vivas (§2.1); la consume el consentimiento del dueño.
            parametros_propuestos  JSONB NULL
                                   CHECK (parametros_propuestos IS NULL OR jsonb_typeof(parametros_propuestos) = 'object'),
            created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)
    # La semilla va ANTES de activar RLS: con FORCE, el dueño tampoco puede
    # insertar sin una política que lo permita.
    op.execute(f"""
        INSERT INTO tenants (id, nombre, rubro, es_kis)
        VALUES ('{TENANT_KIS_STR}', 'Keep IT Simple', 'kis', TRUE);
    """)
    op.execute(politica_por_tenant("tenants", columna="id"))
    op.execute("""
        -- radar_app solo lee y actualiza SU tenant. Crear y listar es de radar_admin.
        GRANT SELECT, UPDATE ON tenants TO radar_app;
        GRANT SELECT, INSERT ON tenants TO radar_admin;
        CREATE POLICY tenants_admin ON tenants FOR ALL TO radar_admin
            USING (true) WITH CHECK (true);
    """)
    op.execute(definir_funcion_admin(
        "radar_admin_crear_tenant(uuid, text, text, text, integer, boolean, boolean, text)",
        """
        CREATE FUNCTION radar_admin_crear_tenant(
            p_id uuid, p_nombre text, p_rubro text, p_perfil text,
            p_retencion_fichas_meses integer, p_retener_fragmentos boolean,
            p_ia_habilitada boolean, p_via_llm text
        ) RETURNS uuid
            LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp
            AS $f$
                INSERT INTO tenants (id, nombre, rubro, perfil_de_datos, retencion_fichas_meses,
                                     retener_fragmentos, ia_habilitada, via_llm)
                VALUES (p_id, p_nombre, p_rubro, p_perfil, p_retencion_fichas_meses,
                        p_retener_fragmentos, p_ia_habilitada, p_via_llm)
                RETURNING id
            $f$;
        """,
    ))
    op.execute(definir_funcion_admin(
        "radar_admin_listar_tenants()",
        """
        CREATE FUNCTION radar_admin_listar_tenants()
            RETURNS TABLE (id uuid, nombre text, rubro text, perfil_de_datos text, created_at timestamptz)
            LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
            AS $f$
                SELECT t.id, t.nombre, t.rubro, t.perfil_de_datos, t.created_at
                FROM tenants t WHERE NOT t.es_kis ORDER BY t.created_at
            $f$;
        """,
    ))


def downgrade() -> None:
    op.execute("""
        DROP FUNCTION IF EXISTS radar_admin_listar_tenants();
        DROP FUNCTION IF EXISTS radar_admin_crear_tenant(uuid, text, text, text, integer, boolean, boolean, text);
        DROP TABLE IF EXISTS tenants;
        DROP FUNCTION IF EXISTS radar_tenant_actual();
    """)
```

`scripts/radar_bootstrap_roles.sql`:

```sql
-- Roles de Radar. Correr UNA vez, como superusuario del servidor Postgres de
-- Radar (nunca en el Postgres de un cliente del bot), antes de la primera
-- migración:
--   psql "$URL_SUPERUSUARIO" -v migrator=postgres -f scripts/radar_bootstrap_roles.sql
-- :migrator es el rol que corre Alembic (dueño de las tablas). Las
-- contraseñas se fijan aparte y nunca en este archivo:
--   ALTER ROLE radar_app PASSWORD '...';
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'radar_app') THEN
        CREATE ROLE radar_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'radar_admin') THEN
        CREATE ROLE radar_admin NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END $$;
-- El migrator tiene que poder ceder funciones a radar_admin (ALTER ... OWNER TO).
GRANT radar_admin TO :migrator;
```

- [ ] **Step 7: `RadarDB`**

`app/radar/db.py`:

```python
"""
Acceso a la base de resultados de Radar con RLS efectiva (§6.4).

- Un solo pool, con el rol radar_app (NOSUPERUSER, NOBYPASSRLS, no dueño).
  connect() lo verifica y aborta si no es así: con un superusuario RLS no
  protegería nada.
- Todo acceso a tablas con tenant pasa por `tenant_tx(tenant_id)`: abre una
  transacción y fija app.tenant_id con set_config(..., is_local=true). El valor
  muere con la transacción, así que una conexión devuelta al pool no conserva
  el tenant.
- `sin_tenant()` es para las funciones SECURITY DEFINER de administración
  (crear/listar tenants, buscar usuarios por email); sobre cualquier tabla con
  RLS devuelve cero filas.
- Nada acá traga errores. app/services/db.py (execute()/fetch() best-effort)
  no se usa en Radar.
"""

import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import asyncpg


def _normalizar_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
    if dsn.startswith("postgres://"):
        return dsn.replace("postgres://", "postgresql://", 1)
    return dsn


class RadarDB:
    def __init__(self, dsn: str, max_size: int = 5):
        self._dsn = _normalizar_dsn(dsn)
        self._max_size = max_size
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        if not self._dsn:
            raise RuntimeError("RADAR_DATABASE_URL vacía")
        pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=self._max_size, timeout=10)
        async with pool.acquire() as con:
            fila = await con.fetchrow(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            )
        if fila is None or fila["rolsuper"] or fila["rolbypassrls"]:
            await pool.close()
            raise RuntimeError(
                "RADAR_DATABASE_URL conecta con un rol superusuario o BYPASSRLS: RLS no tendría efecto"
            )
        self._pool = pool

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("RadarDB sin conectar")
        return self._pool

    @asynccontextmanager
    async def tenant_tx(self, tenant_id: uuid.UUID) -> AsyncIterator[asyncpg.Connection]:
        if not isinstance(tenant_id, uuid.UUID):
            raise TypeError("tenant_tx requiere un uuid.UUID")
        async with self.pool.acquire() as con:
            async with con.transaction():
                await con.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
                yield con

    @asynccontextmanager
    async def sin_tenant(self) -> AsyncIterator[asyncpg.Connection]:
        async with self.pool.acquire() as con:
            async with con.transaction():
                yield con
```

- [ ] **Step 8: Correr los tests**

```bash
python -m pytest tests/radar_tests/test_rls.py -q
```
Esperado: `10 passed`.

- [ ] **Step 9: Commit**

```bash
git add alembic_radar.ini migrations_radar app/radar scripts/radar_bootstrap_roles.sql tests/radar_tests
git commit -m "Radar: tenants, roles de Postgres y RLS efectiva con tenant_tx"
```

---

### Task 3: Almacén de fuente permanente, contexto y arranque de Radar

**Files:**
- Create: `alembic_fuente.ini`, `migrations_fuente/env.py`, `migrations_fuente/versions/f0001_fuente_meta.py`, `app/radar/fuente.py`, `app/radar/contexto.py`
- Modify: `app/radar/migrate.py`, `app/radar/db.py`, `app/radar/app.py`, `app/radar/routers/health.py`, `tests/radar_tests/conftest.py`, `tests/radar_tests/test_modo.py`
- Test: `tests/radar_tests/test_fuente_y_health.py`

**Interfaces:**
- Consumes: `RadarDB`, `migrar_resultados`, `RadarSettings`.
- Produces: `migrar_fuente(fuente_url: str) -> None`; `ALMACENES_DISPONIBLES: frozenset[str] = {"permanente"}`; `FuenteStore(dsn: str)` con `connect() -> None`, `close() -> None`, `salud() -> dict` (`{"ok": True, "almacen": str}` o `{"ok": False, "error": str}`), propiedad `almacen: str`; `RadarDB.salud() -> dict`; `RadarContexto(settings, db, fuente)` con `cerrar()`; `contexto(request) -> RadarContexto` (503 si la app no terminó de arrancar); `validar_settings(rs: RadarSettings) -> None`; `construir_contexto(rs) -> RadarContexto`; lifespan radar que migra las dos bases y arma el contexto (falla el arranque si algo falla); `GET /health` → 200 `{"status":"ok",...}` o 503 `{"status":"degradado"|"arrancando",...}`; fixtures `radar_urls["fuente"]`, `radar_ctx`.

- [ ] **Step 1: Tests (fallan)**

`tests/radar_tests/test_fuente_y_health.py`:

```python
"""
Almacén de fuente separado (§6.4): en este tramo solo el permanente, sin
tablas de conversación, con marcador de esquema y health check.
"""
import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

from app.radar.app import crear_app_radar, validar_settings
from app.radar.contexto import RadarContexto
from app.radar.fuente import ALMACENES_DISPONIBLES, FuenteStore
from app.radar.settings import RadarSettings


def test_solo_existe_el_almacen_permanente():
    assert ALMACENES_DISPONIBLES == frozenset({"permanente"})


async def test_fuente_conecta_y_reporta_almacen(radar_urls):
    fuente = FuenteStore(radar_urls["fuente"])
    await fuente.connect()
    try:
        assert fuente.almacen == "permanente"
        assert await fuente.salud() == {"ok": True, "almacen": "permanente"}
    finally:
        await fuente.close()


async def test_fuente_sin_tablas_de_conversacion(radar_urls):
    con = await asyncpg.connect(radar_urls["fuente"])
    try:
        tablas = {r["table_name"] for r in await con.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
    finally:
        await con.close()
    assert tablas == {"fuente_meta", "alembic_version_fuente"}


async def test_resultados_no_usa_la_tabla_de_version_del_bot(radar_urls):
    con = await asyncpg.connect(radar_urls["migrator"])
    try:
        tablas = {r["table_name"] for r in await con.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
    finally:
        await con.close()
    assert "alembic_version_radar" in tablas
    assert "alembic_version" not in tablas


def test_validar_settings_exige_las_urls_y_el_secreto():
    with pytest.raises(RuntimeError, match="database_url"):
        validar_settings(RadarSettings(_env_file=None))
    # Un secreto corto firma sesiones de 30 días con HMAC sobre casi nada: no arranca.
    with pytest.raises(RuntimeError, match="32"):
        validar_settings(RadarSettings(_env_file=None, database_url="x", migrator_database_url="x",
                                       fuente_database_url="x", cookie_secret="corto"))
    validar_settings(RadarSettings(_env_file=None, database_url="x", migrator_database_url="x",
                                   fuente_database_url="x", cookie_secret="secreto-de-test-de-32-caracteres!"))


async def test_health_ok_con_contexto(radar_ctx):
    app = crear_app_radar(radar_ctx.settings, contexto=radar_ctx)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        r = await c.get("/health")
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["status"] == "ok"
    assert cuerpo["resultados"] == {"ok": True}
    assert cuerpo["fuente"] == {"ok": True, "almacen": "permanente"}


async def test_health_degradado_si_la_fuente_cae(radar_ctx):
    await radar_ctx.fuente.close()
    app = crear_app_radar(radar_ctx.settings, contexto=radar_ctx)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        r = await c.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "degradado"
    assert r.json()["fuente"]["ok"] is False


async def test_lifespan_migra_y_arma_el_contexto(radar_urls, tmp_path):
    rs = RadarSettings(
        _env_file=None,
        database_url=radar_urls["app"],
        migrator_database_url=radar_urls["migrator"],
        fuente_database_url=radar_urls["fuente"],
        cookie_secret="secreto-de-test-de-32-caracteres!",
        secrets_dir=str(tmp_path / "secretos"),
    )
    app = crear_app_radar(rs)          # sin contexto: lo arma el lifespan
    async with app.router.lifespan_context(app):
        assert isinstance(app.state.radar, RadarContexto)
        assert app.state.radar.fuente.almacen == "permanente"
    with pytest.raises(RuntimeError):   # cerrado al salir
        app.state.radar.db.pool
```

- [ ] **Step 2: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_fuente_y_health.py -q
```
Esperado: `ImportError: cannot import name 'validar_settings' from 'app.radar.app'`.

- [ ] **Step 3: Migración del almacén de fuente**

`alembic_fuente.ini`: copiar `alembic_radar.ini` y cambiar solo la sección `[alembic]`:

```ini
# Migraciones del ALMACÉN DE FUENTE de Radar (Postgres separado, §6.4).
[alembic]
script_location = migrations_fuente
version_table = alembic_version_fuente
prepend_sys_path = .
```

`migrations_fuente/env.py`:

```python
from app.radar.alembic_env import correr

correr()
```

`migrations_fuente/versions/f0001_fuente_meta.py`:

```python
"""Fuente f0001: marcador de esquema del almacén de fuente permanente.

Sin tablas de conversación: llegan en el tramo 3 (wa_message_bodies,
wa_message_provider_ids, wa_contact_identities, webhook_inbox). Acá solo
queda `fuente_meta`, que el health check lee para confirmar que la URL apunta
a un almacén de fuente y de qué tipo es.

Revision ID: f0001
Revises: None
Create Date: 2026-09-21
"""
from alembic import op

revision = "f0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE fuente_meta (
            clave      TEXT PRIMARY KEY,
            valor      TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        INSERT INTO fuente_meta (clave, valor) VALUES ('almacen', 'permanente'), ('esquema', '1');
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS fuente_meta")
```

Agregar al final de `app/radar/migrate.py`:

```python
def migrar_fuente(fuente_url: str) -> None:
    command.upgrade(_config("alembic_fuente.ini", fuente_url), "head")
```

- [ ] **Step 4: `FuenteStore` y `RadarDB.salud()`**

`app/radar/fuente.py`:

```python
"""
Almacén de fuente (§6.4): el Postgres separado donde vivirán el texto, los ids
de proveedor y la identidad de contactos (tramo 3). En este tramo solo se
cablea el almacén PERMANENTE: conexión, marcador de esquema y health check.
El purgable es una política opcional del tramo 6.
"""

from typing import Optional

import asyncpg

from app.radar.db import _normalizar_dsn

ALMACENES_DISPONIBLES = frozenset({"permanente"})


class FuenteStore:
    def __init__(self, dsn: str, max_size: int = 3):
        self._dsn = _normalizar_dsn(dsn)
        self._max_size = max_size
        self._pool: Optional[asyncpg.Pool] = None
        self._almacen: str = ""

    async def connect(self) -> None:
        if not self._dsn:
            raise RuntimeError("RADAR_FUENTE_DATABASE_URL vacía")
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=self._max_size, timeout=10)
        async with self._pool.acquire() as con:
            almacen = await con.fetchval("SELECT valor FROM fuente_meta WHERE clave = 'almacen'")
        if almacen not in ALMACENES_DISPONIBLES:
            await self._pool.close()
            self._pool = None
            raise RuntimeError(f"RADAR_FUENTE_DATABASE_URL no apunta a un almacén conocido: {almacen!r}")
        self._almacen = almacen

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    @property
    def almacen(self) -> str:
        return self._almacen

    async def salud(self) -> dict:
        try:
            if self._pool is None:
                raise RuntimeError("sin pool")
            async with self._pool.acquire() as con:
                almacen = await con.fetchval("SELECT valor FROM fuente_meta WHERE clave = 'almacen'")
            return {"ok": True, "almacen": almacen}
        except Exception as e:
            return {"ok": False, "error": type(e).__name__}
```

Agregar a `RadarDB` (en `app/radar/db.py`, después de `sin_tenant`):

```python
    async def salud(self) -> dict:
        try:
            async with self.sin_tenant() as con:
                await con.fetchval("SELECT 1")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": type(e).__name__}
```

- [ ] **Step 5: Contexto, lifespan y health**

`app/radar/contexto.py`:

```python
"""
Contexto de Radar: lo que los routers necesitan y que en tests se reemplaza
por fakes. Vive en app.state.radar; lo arma el lifespan (producción) o la
fixture (tests).
"""

from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.radar.db import RadarDB
from app.radar.fuente import FuenteStore
from app.radar.settings import RadarSettings


@dataclass
class RadarContexto:
    settings: RadarSettings
    db: RadarDB
    fuente: FuenteStore

    async def cerrar(self) -> None:
        await self.db.close()
        await self.fuente.close()


def contexto(request: Request) -> RadarContexto:
    ctx = request.app.state.radar
    if ctx is None:
        raise HTTPException(status_code=503, detail="Radar arrancando")
    return ctx
```

`app/radar/app.py` completo:

```python
"""
App de Radar (APP_MODE=radar). Misma imagen que el bot, routers distintos,
sin CORS (la cookie de sesión es same-origin).

A diferencia del bot, una migración fallida o una base inaccesible IMPIDEN el
arranque: RLS depende del esquema y no hay modo degradado que valga.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.middleware import log_errores
from app.radar.contexto import RadarContexto
from app.radar.db import RadarDB
from app.radar.fuente import FuenteStore
from app.radar.migrate import migrar_fuente, migrar_resultados
from app.radar.routers import health
from app.radar.settings import RadarSettings, get_radar_settings

logger = logging.getLogger("app.radar")

OBLIGATORIAS = ("database_url", "migrator_database_url", "fuente_database_url", "cookie_secret")
MIN_COOKIE_SECRET = 32


def validar_settings(rs: RadarSettings) -> None:
    faltantes = [n for n in OBLIGATORIAS if not getattr(rs, n)]
    if faltantes:
        raise RuntimeError("Faltan variables RADAR_: " + ", ".join(faltantes))
    if len(rs.cookie_secret) < MIN_COOKIE_SECRET:
        raise RuntimeError(f"RADAR_COOKIE_SECRET: mínimo {MIN_COOKIE_SECRET} caracteres aleatorios")


async def construir_contexto(rs: RadarSettings) -> RadarContexto:
    db = RadarDB(rs.database_url)
    await db.connect()
    fuente = FuenteStore(rs.fuente_database_url)
    await fuente.connect()
    return RadarContexto(settings=rs, db=db, fuente=fuente)


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
    yield
    if propio:
        await app.state.radar.cerrar()


def crear_app_radar(rs: RadarSettings | None = None, contexto: RadarContexto | None = None) -> FastAPI:
    rs = rs or get_radar_settings()
    app = FastAPI(title="Radar", version="0.1.0", lifespan=_lifespan_radar)
    app.state.radar_settings = rs
    app.state.radar = contexto
    app.middleware("http")(log_errores)
    app.include_router(health.router)
    return app
```

`app/radar/routers/health.py` completo:

```python
"""Health check del despliegue de Radar (Railway lo consulta en /health)."""

import os

from fastapi import APIRouter, Request, Response

router = APIRouter(tags=["radar-health"])


def _commit() -> str | None:
    return (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "")[:9] or None


@router.get("/health")
async def health(request: Request, response: Response):
    ctx = request.app.state.radar
    if ctx is None:
        response.status_code = 503
        return {"status": "arrancando", "modo": "radar", "commit": _commit()}
    resultados = await ctx.db.salud()
    fuente = await ctx.fuente.salud()
    ok = resultados["ok"] and fuente["ok"]
    response.status_code = 200 if ok else 503
    return {
        "status": "ok" if ok else "degradado",
        "modo": "radar",
        "resultados": resultados,
        "fuente": fuente,
        "commit": _commit(),
    }
```

Reemplazar `test_health_radar_dice_modo` en `tests/radar_tests/test_modo.py` (sin contexto ahora responde 503):

```python
async def test_health_radar_sin_contexto_dice_arrancando():
    app = crear_app(Settings(_env_file=None, app_mode="radar"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        r = await c.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "arrancando"
    assert r.json()["modo"] == "radar"
```

- [ ] **Step 6: Fixture de fuente y contexto**

En `tests/radar_tests/conftest.py`, imports:

```python
from app.radar.contexto import RadarContexto
from app.radar.fuente import FuenteStore
from app.radar.migrate import migrar_fuente, migrar_resultados
from app.radar.settings import RadarSettings
```

En `radar_urls`, después de `cur.execute("CREATE DATABASE radar_test OWNER radar_migrator")`:

```python
    cur.execute("CREATE DATABASE radar_fuente_test OWNER radar_migrator")
```

En el dict `urls`, la clave `"fuente": info.get_uri(user="radar_migrator", database="radar_fuente_test"),` y después de `migrar_resultados(urls["migrator"])`: `migrar_fuente(urls["fuente"])`.

Nueva fixture al final del archivo:

```python
@pytest.fixture
async def radar_ctx(radar_db, radar_urls, tmp_path):
    fuente = FuenteStore(radar_urls["fuente"])
    await fuente.connect()
    rs = RadarSettings(
        _env_file=None,
        database_url=radar_urls["app"],
        migrator_database_url=radar_urls["migrator"],
        fuente_database_url=radar_urls["fuente"],
        cookie_secret="secreto-de-test-de-32-caracteres!",
        cookie_secure=False,
        public_base_url="http://testserver",
        mailer="memoria",
        secrets_dir=str(tmp_path / "secretos"),
    )
    ctx = RadarContexto(settings=rs, db=radar_db, fuente=fuente)
    yield ctx
    await fuente.close()
```

- [ ] **Step 7: Correr**

```bash
python -m pytest tests/radar_tests -q
```
Esperado: todo verde (`test_modo.py` 5, `test_rls.py` 10, `test_fuente_y_health.py` 8).

- [ ] **Step 8: Commit**

```bash
git add alembic_fuente.ini migrations_fuente app/radar tests/radar_tests
git commit -m "Radar: almacen de fuente permanente, contexto y arranque con migraciones"
```

---

### Task 4: Secretos `k_tenant`, HMAC de contactos y normalización E.164

**Files:**
- Create: `app/radar/secrets.py`, `app/radar/telefonos.py`
- Test: `tests/radar_tests/test_secretos.py`, `tests/radar_tests/test_telefonos.py`

**Interfaces:**
- Consumes: nada del repo.
- Produces: `SecretStore` (Protocol: `get(nombre: str) -> bytes | None`, `set(nombre: str, valor: bytes) -> None`, `delete(nombre: str) -> None`), `MemorySecretStore()`, `FileSecretStore(dir: str | Path)`; `nombre_k_tenant(tenant_id: uuid.UUID) -> str`; `crear_k_tenant(store, tenant_id) -> None` (lanza `KTenantYaExiste`); `obtener_k_tenant(store, tenant_id) -> bytes` (lanza `KTenantAusente`); `destruir_k_tenant(store, tenant_id) -> None`; `contact_hmac(k_tenant: bytes, telefono_e164: str) -> str` (hex de 64); `lid_hmac(k_tenant: bytes, lid: str) -> str`; `Seudonimizador(store)` con `contact_hmac(tenant_id, telefono: str) -> str` (normaliza antes) y `lid_hmac(tenant_id, lid) -> str`; `normalizar_e164(texto: str) -> str`, `TelefonoNoSoportado(ValueError)`.

- [ ] **Step 1: Tests de teléfonos (fallan)**

`tests/radar_tests/test_telefonos.py`:

```python
"""
Normalización a E.164 de números argentinos. Cubre los tres formatos que
Radar recibe: el id de WhatsApp (549..., con o sin @c.us), el internacional
con + y el nacional con 0 y 15 opcional ("Suprimir contacto", tramo 6).
"""
import pytest

from app.radar.telefonos import TelefonoNoSoportado, normalizar_e164


@pytest.mark.parametrize("entrada,esperado", [
    ("+54 9 341 123 4567", "+5493411234567"),
    ("+5493411234567", "+5493411234567"),
    ("5493411234567", "+5493411234567"),
    ("5493411234567@c.us", "+5493411234567"),
    ("54 9 11 1234 5678", "+5491112345678"),
    ("0341 15 123 4567", "+5493411234567"),
    ("0341-15-123-4567", "+5493411234567"),
    ("011 15 1234 5678", "+5491112345678"),
    ("02966 15 123456", "+5492966123456"),
    ("0341 412 3456", "+543414123456"),        # fijo, sin 9
    ("+54 341 412 3456", "+543414123456"),
    ("  +54 9 341 123 4567  ", "+5493411234567"),
])
def test_normaliza(entrada, esperado):
    assert normalizar_e164(entrada) == esperado


@pytest.mark.parametrize("entrada", [
    "",
    "hola",
    "+1 555 123 4567",          # otro país
    "341 123 4567",             # nacional sin el 0
    "0341 15 12",               # demasiado corto
    "+54 9 341 123 4567 89",    # demasiado largo
    "+54 8 341 123 4567",       # 11 dígitos sin el 9 de móvil
])
def test_rechaza(entrada):
    with pytest.raises(TelefonoNoSoportado):
        normalizar_e164(entrada)
```

- [ ] **Step 2: Tests de secretos (fallan)**

`tests/radar_tests/test_secretos.py`:

```python
"""
k_tenant fuera de la base (§6.4) y HMAC con clave para contactos y lids.
"""
import hashlib
import hmac
import os
import stat
import sys
import uuid

import pytest

from app.radar.secrets import (
    FileSecretStore, KTenantAusente, KTenantYaExiste, MemorySecretStore, Seudonimizador,
    contact_hmac, crear_k_tenant, destruir_k_tenant, lid_hmac, nombre_k_tenant, obtener_k_tenant,
)
from app.radar.telefonos import TelefonoNoSoportado

K = bytes(range(32))


def test_contact_hmac_es_hmac_sha256_del_e164():
    esperado = hmac.new(K, b"+5493411234567", hashlib.sha256).hexdigest()
    assert contact_hmac(K, "+5493411234567") == esperado
    assert len(esperado) == 64


def test_contact_hmac_exige_e164():
    with pytest.raises(ValueError):
        contact_hmac(K, "5493411234567")


def test_claves_distintas_dan_hmac_distinto():
    assert contact_hmac(K, "+5493411234567") != contact_hmac(bytes(32), "+5493411234567")


def test_lid_hmac_acepta_con_y_sin_sufijo():
    assert lid_hmac(K, "123456789012345@lid") == lid_hmac(K, "123456789012345")
    assert lid_hmac(K, "123456789012345") == hmac.new(K, b"123456789012345", hashlib.sha256).hexdigest()


def test_lid_hmac_rechaza_lid_no_numerico():
    with pytest.raises(ValueError):
        lid_hmac(K, "5493411234567@c.us")


@pytest.mark.parametrize("store_factory", [
    lambda tmp: MemorySecretStore(),
    lambda tmp: FileSecretStore(tmp / "secretos"),
])
def test_ciclo_de_vida_de_k_tenant(tmp_path, store_factory):
    store = store_factory(tmp_path)
    tid = uuid.uuid4()
    with pytest.raises(KTenantAusente):
        obtener_k_tenant(store, tid)
    crear_k_tenant(store, tid)
    k = obtener_k_tenant(store, tid)
    assert len(k) == 32
    with pytest.raises(KTenantYaExiste):
        crear_k_tenant(store, tid)
    assert obtener_k_tenant(store, tid) == k
    destruir_k_tenant(store, tid)
    with pytest.raises(KTenantAusente):
        obtener_k_tenant(store, tid)


def test_file_store_persiste_entre_instancias(tmp_path):
    tid = uuid.uuid4()
    crear_k_tenant(FileSecretStore(tmp_path / "s"), tid)
    assert len(obtener_k_tenant(FileSecretStore(tmp_path / "s"), tid)) == 32


@pytest.mark.skipif(sys.platform == "win32", reason="permisos POSIX")
def test_file_store_archivo_solo_dueno(tmp_path):
    store = FileSecretStore(tmp_path / "s")
    tid = uuid.uuid4()
    crear_k_tenant(store, tid)
    archivo = next((tmp_path / "s").iterdir())
    assert stat.S_IMODE(os.stat(archivo).st_mode) == 0o600


def test_nombre_de_secreto_invalido(tmp_path):
    store = FileSecretStore(tmp_path / "s")
    with pytest.raises(ValueError):
        store.set("../fuera", b"x")


def test_seudonimizador_normaliza_y_usa_la_clave_del_tenant():
    store = MemorySecretStore()
    tid = uuid.uuid4()
    crear_k_tenant(store, tid)
    s = Seudonimizador(store)
    k = obtener_k_tenant(store, tid)
    assert s.contact_hmac(tid, "0341 15 123 4567") == contact_hmac(k, "+5493411234567")
    assert s.contact_hmac(tid, "5493411234567@c.us") == contact_hmac(k, "+5493411234567")
    assert s.lid_hmac(tid, "123456789012345@lid") == lid_hmac(k, "123456789012345")
    with pytest.raises(TelefonoNoSoportado):
        s.contact_hmac(tid, "+1 555 123 4567")
    assert nombre_k_tenant(tid) == f"k_tenant:{tid}"
```

- [ ] **Step 3: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_telefonos.py tests/radar_tests/test_secretos.py -q
```
Esperado: `ModuleNotFoundError: No module named 'app.radar.telefonos'`.

- [ ] **Step 4: `telefonos.py`**

```python
"""
Normalización de teléfonos argentinos a E.164 para calcular contact_hmac.

Formatos soportados:
  - internacional con +: '+54 9 341 123 4567';
  - dígitos con 54 adelante, como llegan en los ids de WhatsApp:
    '5493411234567' o '5493411234567@c.us';
  - nacional con 0 inicial y 15 opcional: '0341 15 123 4567', '011 15 1234 5678',
    '0341 412 3456' (fijo).

Devuelve '+54' + 10 dígitos (fijo) o '+549' + 10 dígitos (móvil). Cualquier
otro país o forma lanza TelefonoNoSoportado. Es una normalización de formato:
no verifica que la característica exista ni que la línea esté activa.

Resolución del 15: la característica tiene 2, 3 o 4 dígitos, y sin el 15
tienen que quedar 10 dígitos (característica + número). Se prueba en ese orden
y gana la primera que cierra; '11 15 15xxxxxx' es ambiguo y se resuelve como
característica 11, que es lo correcto en la práctica.
"""

import re

_DIGITOS = re.compile(r"\D")


class TelefonoNoSoportado(ValueError):
    pass


def _nacional_sin_cero(d: str) -> str:
    """d = dígitos nacionales sin el 0 inicial. Devuelve los 10 u 11 dígitos
    que siguen al 54 (11 si es móvil, con el 9 adelante)."""
    for largo_area in (2, 3, 4):
        if d[largo_area:largo_area + 2] == "15" and len(d) - 2 == 10:
            return "9" + d[:largo_area] + d[largo_area + 2:]
    if len(d) == 10:
        return d
    raise TelefonoNoSoportado("formato nacional no reconocido")


def normalizar_e164(texto: str) -> str:
    t = (texto or "").strip()
    if "@" in t:
        t = t.split("@", 1)[0]
    con_mas = t.startswith("+")
    d = _DIGITOS.sub("", t)
    if not d:
        raise TelefonoNoSoportado("sin dígitos")

    if con_mas or (d.startswith("54") and len(d) >= 12):
        if not d.startswith("54"):
            raise TelefonoNoSoportado("solo se soportan números de Argentina (+54)")
        nacional = d[2:]
    elif d.startswith("0"):
        nacional = _nacional_sin_cero(d[1:])
    else:
        raise TelefonoNoSoportado("formato no reconocido: usar +54..., 54... o 0...")

    if len(nacional) == 11 and nacional[0] != "9":
        raise TelefonoNoSoportado("11 dígitos nacionales solo con el 9 de móvil")
    if len(nacional) not in (10, 11):
        raise TelefonoNoSoportado("largo inválido")
    return "+54" + nacional
```

- [ ] **Step 5: `secrets.py`**

```python
"""
Secretos por tenant fuera de la base y de sus backups (§6.4).

k_tenant: 32 bytes aleatorios por tenant.
  contact_hmac = HMAC-SHA256(k_tenant, teléfono E.164)
  lid_hmac     = HMAC-SHA256(k_tenant, lid)
Un hash sin clave se revierte por enumeración (~10^10 teléfonos); por eso la
clave es obligatoria y nunca se loguea ni se guarda en Postgres.

k_tenant se destruye SOLO en la baja del cliente (tramo 6): "borrar todo" es
por línea y `suppressions` (por tenant) tiene que sobrevivirla.

Bindings:
- FileSecretStore(dir): un archivo por secreto, base64, permisos 0600. Dev y
  tests usan un directorio temporal; producción, un volumen montado en
  RADAR_SECRETS_DIR, fuera de los backups de Postgres (docs/radar-despliegue.md).
- MemorySecretStore: solo tests unitarios.
"""

import base64
import hashlib
import hmac
import os
import re
import secrets as _secrets
import uuid
from pathlib import Path
from typing import Optional, Protocol

from app.radar.telefonos import normalizar_e164

_NOMBRE = re.compile(r"^[a-z0-9_:-]{1,120}$")
_LID = re.compile(r"^[0-9]{5,20}$")


class KTenantAusente(KeyError):
    pass


class KTenantYaExiste(ValueError):
    pass


class SecretStore(Protocol):
    def get(self, nombre: str) -> Optional[bytes]: ...
    def set(self, nombre: str, valor: bytes) -> None: ...
    def delete(self, nombre: str) -> None: ...


def _validar_nombre(nombre: str) -> str:
    if not _NOMBRE.match(nombre or ""):
        raise ValueError("nombre de secreto inválido")
    return nombre


class MemorySecretStore:
    def __init__(self):
        self._datos: dict[str, bytes] = {}

    def get(self, nombre: str) -> Optional[bytes]:
        return self._datos.get(_validar_nombre(nombre))

    def set(self, nombre: str, valor: bytes) -> None:
        self._datos[_validar_nombre(nombre)] = bytes(valor)

    def delete(self, nombre: str) -> None:
        self._datos.pop(_validar_nombre(nombre), None)


class FileSecretStore:
    def __init__(self, dir: str | Path):
        self._dir = Path(dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _ruta(self, nombre: str) -> Path:
        return self._dir / (_validar_nombre(nombre).replace(":", "__") + ".b64")

    def get(self, nombre: str) -> Optional[bytes]:
        ruta = self._ruta(nombre)
        if not ruta.exists():
            return None
        return base64.b64decode(ruta.read_bytes())

    def set(self, nombre: str, valor: bytes) -> None:
        ruta = self._ruta(nombre)
        fd = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(base64.b64encode(bytes(valor)))

    def delete(self, nombre: str) -> None:
        self._ruta(nombre).unlink(missing_ok=True)


def nombre_k_tenant(tenant_id: uuid.UUID) -> str:
    return f"k_tenant:{tenant_id}"


def crear_k_tenant(store: SecretStore, tenant_id: uuid.UUID) -> None:
    nombre = nombre_k_tenant(tenant_id)
    if store.get(nombre) is not None:
        raise KTenantYaExiste(str(tenant_id))
    store.set(nombre, _secrets.token_bytes(32))


def obtener_k_tenant(store: SecretStore, tenant_id: uuid.UUID) -> bytes:
    valor = store.get(nombre_k_tenant(tenant_id))
    if valor is None:
        raise KTenantAusente(str(tenant_id))
    if len(valor) != 32:
        raise ValueError("k_tenant corrupta: no tiene 32 bytes")
    return valor


def destruir_k_tenant(store: SecretStore, tenant_id: uuid.UUID) -> None:
    store.delete(nombre_k_tenant(tenant_id))


def contact_hmac(k_tenant: bytes, telefono_e164: str) -> str:
    if not telefono_e164.startswith("+"):
        raise ValueError("contact_hmac requiere E.164 ('+549...'): usar normalizar_e164")
    return hmac.new(k_tenant, telefono_e164.encode(), hashlib.sha256).hexdigest()


def lid_hmac(k_tenant: bytes, lid: str) -> str:
    lid = (lid or "").strip()
    if lid.endswith("@lid"):
        lid = lid[:-4]
    if not _LID.match(lid):
        raise ValueError("lid inválido")
    return hmac.new(k_tenant, lid.encode(), hashlib.sha256).hexdigest()


class Seudonimizador:
    """Fachada por tenant: normaliza y firma con la k_tenant del store."""

    def __init__(self, store: SecretStore):
        self._store = store

    def contact_hmac(self, tenant_id: uuid.UUID, telefono: str) -> str:
        return contact_hmac(obtener_k_tenant(self._store, tenant_id), normalizar_e164(telefono))

    def lid_hmac(self, tenant_id: uuid.UUID, lid: str) -> str:
        return lid_hmac(obtener_k_tenant(self._store, tenant_id), lid)
```

- [ ] **Step 6: Correr**

```bash
python -m pytest tests/radar_tests/test_telefonos.py tests/radar_tests/test_secretos.py -q
```
Esperado: `30 passed` (en Windows `29 passed, 1 skipped` por los permisos POSIX).

- [ ] **Step 7: Commit**

```bash
git add app/radar/secrets.py app/radar/telefonos.py tests/radar_tests
git commit -m "Radar: k_tenant fuera de la base, contact_hmac/lid_hmac y E.164 argentino"
```

---

### Task 5: Parámetros de §2.1 y orden de estrictez

**Files:**
- Create: `app/radar/parametros.py`
- Test: `tests/radar_tests/test_parametros.py`

**Interfaces:**
- Consumes: nada del repo.
- Produces: `Parametro(nombre, ambito, tipo, inicial, orden)`; `PARAMETROS: dict[str, Parametro]`; `DE_LINEA: list[str]`; `DE_TENANT: list[str]`; `RUBROS_SENSIBLES: frozenset[str]`; `ValorInvalido(ValueError)`; `validar(nombre: str, valor) -> Any` (coaccionado y validado); `comparar(nombre: str, actual, nuevo) -> Literal["igual","mas_estricto","mas_laxo"]`; `es_mas_estricto(nombre: str, candidato, referencia) -> bool`; `iniciales(ambito: str) -> dict`; `propuesta_para_perfil(perfil: str) -> dict`; `perfil_por_rubro(rubro: str) -> str`; `clasificar_cambios(actuales: dict, nuevos: dict) -> dict[str, str]` (solo los que cambian).

- [ ] **Step 1: Tests (fallan)**

`tests/radar_tests/test_parametros.py`:

```python
"""
Tabla de parámetros de §2.1 y la regla "más estricto".
"""
from decimal import Decimal

import pytest

from app.radar.parametros import (
    DE_LINEA, DE_TENANT, PARAMETROS, ValorInvalido, clasificar_cambios, comparar,
    es_mas_estricto, iniciales, perfil_por_rubro, propuesta_para_perfil, validar,
)


def test_tabla_exacta_del_spec():
    assert {n: (p.ambito, p.inicial) for n, p in PARAMETROS.items()} == {
        "duracion_vinculo_dias": ("linea", 0),
        "retencion_fuente_dias": ("linea", 0),
        "retencion_tras_desvinculo_dias": ("linea", 0),
        "retencion_fichas_meses": ("tenant", 12),
        "tope_ia_mensual_usd": ("linea", None),
        "perfil_de_datos": ("tenant", "estandar"),
        "retener_fragmentos": ("tenant", False),
        "ia_habilitada": ("tenant", True),
        "via_llm": ("tenant", "lotes"),
    }
    assert DE_LINEA == ["duracion_vinculo_dias", "retencion_fuente_dias",
                        "retencion_tras_desvinculo_dias", "tope_ia_mensual_usd"]
    assert DE_TENANT == ["retencion_fichas_meses", "perfil_de_datos", "retener_fragmentos",
                         "ia_habilitada", "via_llm"]


def test_iniciales_por_ambito():
    assert iniciales("linea") == {"duracion_vinculo_dias": 0, "retencion_fuente_dias": 0,
                                  "retencion_tras_desvinculo_dias": 0, "tope_ia_mensual_usd": None}
    assert iniciales("tenant")["retencion_fichas_meses"] == 12


@pytest.mark.parametrize("nombre,actual,nuevo,esperado", [
    # plazos: N > 0 es más estricto que 0; entre > 0 gana el menor
    ("duracion_vinculo_dias", 0, 30, "mas_estricto"),
    ("duracion_vinculo_dias", 30, 0, "mas_laxo"),
    ("duracion_vinculo_dias", 30, 7, "mas_estricto"),
    ("duracion_vinculo_dias", 7, 30, "mas_laxo"),
    ("duracion_vinculo_dias", 7, 7, "igual"),
    ("retencion_fuente_dias", 0, 7, "mas_estricto"),
    ("retencion_fichas_meses", 12, 0, "mas_laxo"),
    ("retencion_fichas_meses", 12, 6, "mas_estricto"),
    # monto: NULL = sin tope (lo más laxo); entre montos gana el menor
    ("tope_ia_mensual_usd", None, Decimal("50"), "mas_estricto"),
    ("tope_ia_mensual_usd", Decimal("50"), None, "mas_laxo"),
    ("tope_ia_mensual_usd", Decimal("50"), Decimal("20"), "mas_estricto"),
    # booleanos: apagar es más estricto
    ("ia_habilitada", True, False, "mas_estricto"),
    ("ia_habilitada", False, True, "mas_laxo"),
    ("retener_fragmentos", False, True, "mas_laxo"),
    # enums
    ("perfil_de_datos", "estandar", "sensible", "mas_estricto"),
    ("perfil_de_datos", "sensible", "estandar", "mas_laxo"),
    ("via_llm", "lotes", "sincronica", "mas_estricto"),
    ("via_llm", "sincronica", "lotes", "mas_laxo"),
])
def test_comparar(nombre, actual, nuevo, esperado):
    assert comparar(nombre, actual, nuevo) == esperado


def test_es_mas_estricto_es_estricto_no_reflexivo():
    assert es_mas_estricto("duracion_vinculo_dias", 7, 30)
    assert not es_mas_estricto("duracion_vinculo_dias", 30, 7)
    assert not es_mas_estricto("duracion_vinculo_dias", 7, 7)


@pytest.mark.parametrize("nombre,valor,esperado", [
    ("duracion_vinculo_dias", "30", 30),
    ("tope_ia_mensual_usd", "12.50", Decimal("12.50")),
    ("tope_ia_mensual_usd", None, None),
    ("ia_habilitada", False, False),
    ("via_llm", "lotes", "lotes"),
])
def test_validar_coacciona(nombre, valor, esperado):
    assert validar(nombre, valor) == esperado


@pytest.mark.parametrize("nombre,valor", [
    ("duracion_vinculo_dias", -1),
    ("duracion_vinculo_dias", None),
    ("duracion_vinculo_dias", "treinta"),
    ("duracion_vinculo_dias", True),
    ("tope_ia_mensual_usd", Decimal("-1")),
    ("ia_habilitada", "si"),
    ("via_llm", "streaming"),
    ("perfil_de_datos", "farmacia"),
    ("inexistente", 1),
])
def test_validar_rechaza(nombre, valor):
    with pytest.raises(ValorInvalido):
        validar(nombre, valor)


def test_perfil_sensible_propone_no_fuerza():
    assert propuesta_para_perfil("sensible") == {
        "retencion_fuente_dias": 7, "ia_habilitada": False,
        "via_llm": "sincronica", "retener_fragmentos": False,
    }
    assert propuesta_para_perfil("estandar") == {}
    with pytest.raises(ValorInvalido):
        propuesta_para_perfil("otro")


@pytest.mark.parametrize("rubro,perfil", [
    ("farmacia", "sensible"), ("salud", "sensible"), ("mutual_salud", "sensible"),
    ("comercio", "estandar"), ("pet_shop", "estandar"), ("servicios", "estandar"), ("otro", "estandar"),
])
def test_perfil_por_rubro(rubro, perfil):
    assert perfil_por_rubro(rubro) == perfil


def test_clasificar_cambios_solo_los_que_cambian():
    actuales = {"duracion_vinculo_dias": 0, "retencion_fuente_dias": 0,
                "retencion_tras_desvinculo_dias": 0, "tope_ia_mensual_usd": None}
    cambios = clasificar_cambios(actuales, {"duracion_vinculo_dias": "30", "retencion_fuente_dias": 0})
    assert cambios == {"duracion_vinculo_dias": "mas_estricto"}
```

- [ ] **Step 2: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_parametros.py -q
```
Esperado: `ModuleNotFoundError: No module named 'app.radar.parametros'`.

- [ ] **Step 3: `parametros.py`**

```python
"""
Parámetros de §2.1: nombres, ámbito, valor inicial y orden de "más estricto".

"Más estricto" (§2.1):
- plazos (duración y retención, en días o meses): cualquier N > 0 es más
  estricto que 0 (0 = sin plazo) y entre valores > 0 gana el menor;
- tope_ia_mensual_usd: NULL = sin tope (lo más laxo) y entre montos gana el
  menor. El spec deja el valor inicial "a definir en el piloto"; NULL juega
  el papel del 0 de las retenciones (decisión de este plan);
- booleanos: apagar es más estricto que encender;
- enums: perfil sensible > estandar; via_llm sincronica > lotes (§7: la vía
  sincrónica es la que admite retención cero).

El perfil `sensible` PROPONE valores más estrictos (§2.1, §7); no los fuerza.
"""

import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

Ambito = Literal["tenant", "linea"]
Tipo = Literal["plazo", "monto", "booleano", "enum"]
Comparacion = Literal["igual", "mas_estricto", "mas_laxo"]


class ValorInvalido(ValueError):
    pass


@dataclass(frozen=True)
class Parametro:
    nombre: str
    ambito: Ambito
    tipo: Tipo
    inicial: Any
    orden: tuple = field(default=())   # enums: de más laxo a más estricto


PARAMETROS: dict[str, Parametro] = {p.nombre: p for p in (
    Parametro("duracion_vinculo_dias", "linea", "plazo", 0),
    Parametro("retencion_fuente_dias", "linea", "plazo", 0),
    Parametro("retencion_tras_desvinculo_dias", "linea", "plazo", 0),
    Parametro("retencion_fichas_meses", "tenant", "plazo", 12),
    Parametro("tope_ia_mensual_usd", "linea", "monto", None),
    Parametro("perfil_de_datos", "tenant", "enum", "estandar", ("estandar", "sensible")),
    Parametro("retener_fragmentos", "tenant", "booleano", False),
    Parametro("ia_habilitada", "tenant", "booleano", True),
    Parametro("via_llm", "tenant", "enum", "lotes", ("lotes", "sincronica")),
)}

DE_LINEA = [n for n, p in PARAMETROS.items() if p.ambito == "linea"]
DE_TENANT = [n for n, p in PARAMETROS.items() if p.ambito == "tenant"]

# §7, tabla de perfil de datos: qué rubros proponen `sensible` por defecto.
RUBROS_SENSIBLES = frozenset({"farmacia", "salud", "mutual_salud"})


def _parametro(nombre: str) -> Parametro:
    try:
        return PARAMETROS[nombre]
    except KeyError:
        raise ValorInvalido(f"parámetro desconocido: {nombre}") from None


def validar(nombre: str, valor: Any) -> Any:
    p = _parametro(nombre)
    if p.tipo == "plazo":
        if isinstance(valor, bool) or valor is None:
            raise ValorInvalido(f"{nombre}: entero >= 0")
        try:
            v = int(valor)
        except (TypeError, ValueError):
            raise ValorInvalido(f"{nombre}: entero >= 0") from None
        if v < 0:
            raise ValorInvalido(f"{nombre}: entero >= 0")
        return v
    if p.tipo == "monto":
        if valor is None:
            return None
        try:
            v = Decimal(str(valor))
        except InvalidOperation:
            raise ValorInvalido(f"{nombre}: monto >= 0 o null") from None
        if not v.is_finite() or v < 0:
            raise ValorInvalido(f"{nombre}: monto >= 0 o null")
        return v
    if p.tipo == "booleano":
        if not isinstance(valor, bool):
            raise ValorInvalido(f"{nombre}: true o false")
        return valor
    if valor not in p.orden:
        raise ValorInvalido(f"{nombre}: uno de {list(p.orden)}")
    return valor


def _rango(p: Parametro, valor: Any) -> float:
    """Posición en el orden de estrictez: menor = más estricto."""
    if p.tipo == "plazo":
        return math.inf if valor == 0 else float(valor)
    if p.tipo == "monto":
        return math.inf if valor is None else float(valor)
    if p.tipo == "booleano":
        return 1.0 if valor else 0.0
    return float(len(p.orden) - 1 - p.orden.index(valor))


def comparar(nombre: str, actual: Any, nuevo: Any) -> Comparacion:
    p = _parametro(nombre)
    a, n = _rango(p, validar(nombre, actual)), _rango(p, validar(nombre, nuevo))
    if n == a:
        return "igual"
    return "mas_estricto" if n < a else "mas_laxo"


def es_mas_estricto(nombre: str, candidato: Any, referencia: Any) -> bool:
    return comparar(nombre, referencia, candidato) == "mas_estricto"


def iniciales(ambito: str) -> dict:
    return {n: p.inicial for n, p in PARAMETROS.items() if p.ambito == ambito}


def propuesta_para_perfil(perfil: str) -> dict:
    validar("perfil_de_datos", perfil)
    if perfil == "sensible":
        return {"retencion_fuente_dias": 7, "ia_habilitada": False,
                "via_llm": "sincronica", "retener_fragmentos": False}
    return {}


def perfil_por_rubro(rubro: str) -> str:
    return "sensible" if rubro in RUBROS_SENSIBLES else "estandar"


def clasificar_cambios(actuales: dict, nuevos: dict) -> dict[str, Comparacion]:
    """{nombre: comparación} solo para los parámetros cuyo valor cambia.
    Valida cada valor nuevo (lanza ValorInvalido)."""
    resultado: dict[str, Comparacion] = {}
    for nombre, nuevo in nuevos.items():
        c = comparar(nombre, actuales[nombre], nuevo)
        if c != "igual":
            resultado[nombre] = c
    return resultado
```

- [ ] **Step 4: Correr**

```bash
python -m pytest tests/radar_tests/test_parametros.py -q
```
Esperado: `44 passed`.

- [ ] **Step 5: Commit**

```bash
git add app/radar/parametros.py tests/radar_tests/test_parametros.py
git commit -m "Radar: parametros de linea y tenant con orden de estrictez"
```

---

### Task 6: Esquema de acceso, líneas y auditoría (migración r0002)

**Files:**
- Create: `migrations_radar/versions/r0002_acceso_lineas_y_auditoria.py`, `tests/radar_tests/helpers.py`
- Modify: `tests/radar_tests/conftest.py` (lista `TABLAS`)
- Test: `tests/radar_tests/test_esquema.py`

**Interfaces:**
- Consumes: `politica_por_tenant`, `grants_app`, `definir_funcion_admin`, `TENANT_KIS_STR`, `radar_tenant_actual()`.
- Produces: tablas `users`, `memberships`, `lines` (con `parametros_propuestos JSONB NULL`, la propuesta pendiente de un admin de KIS), `login_tokens`, `sessions`, `consents`, `support_grants`, `access_audit_log`, `product_events` (todas con `tenant_id UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id)`, RLS forzada y política de `radar_app`); función `radar_auth_usuarios_por_email(p_email text) RETURNS TABLE (user_id uuid, tenant_id uuid)`; helpers de test `crear_tenant_directo(db, nombre="Farmacia Test", rubro="farmacia", perfil="estandar") -> uuid.UUID`, `crear_usuario(db, tenant_id, email, rol, lineas_permitidas=None) -> uuid.UUID`, `crear_linea_directa(db, tenant_id, nombre="Línea 1", estado="sin_vinculo") -> uuid.UUID`.

- [ ] **Step 1: Helpers de test**

`tests/radar_tests/helpers.py`:

```python
"""Atajos de test que escriben directo en la base como radar_app."""

import uuid

from app.radar.db import RadarDB


async def crear_tenant_directo(db: RadarDB, nombre: str = "Farmacia Test", rubro: str = "farmacia",
                               perfil: str = "estandar") -> uuid.UUID:
    tid = uuid.uuid4()
    async with db.tenant_tx(tid) as con:
        await con.fetchval(
            "SELECT radar_admin_crear_tenant($1, $2, $3, $4, 12, FALSE, TRUE, 'lotes')",
            tid, nombre, rubro, perfil,
        )
    return tid


async def crear_usuario(db: RadarDB, tenant_id: uuid.UUID, email: str, rol: str,
                        lineas_permitidas: list[uuid.UUID] | None = None) -> uuid.UUID:
    async with db.tenant_tx(tenant_id) as con:
        uid = await con.fetchval(
            "INSERT INTO users (email, nombre) VALUES ($1, $2) RETURNING id", email, email.split("@")[0])
        await con.execute(
            "INSERT INTO memberships (user_id, rol, lineas_permitidas) VALUES ($1, $2, $3)",
            uid, rol, lineas_permitidas)
    return uid


async def crear_linea_directa(db: RadarDB, tenant_id: uuid.UUID, nombre: str = "Línea 1",
                              estado: str = "sin_vinculo") -> uuid.UUID:
    async with db.tenant_tx(tenant_id) as con:
        lid = await con.fetchval("INSERT INTO lines (nombre) VALUES ($1) RETURNING id", nombre)
        if estado != "sin_vinculo":
            await con.execute("UPDATE lines SET estado = $2 WHERE id = $1", lid, estado)
    return lid
```

- [ ] **Step 2: Tests de esquema (fallan)**

`tests/radar_tests/test_esquema.py`:

```python
"""
Guardas de esquema (§6.4, §7, §9): tenant_id NOT NULL y RLS forzada en todas
las tablas; ninguna columna de texto de conversación ni de identificadores de
WhatsApp en la base de resultados; product_events sin texto libre;
access_audit_log sin teléfonos ni emails; coherencia de líneas.
"""
import json
import uuid

import asyncpg
import pytest

from app.radar.constantes import TENANT_KIS

from .helpers import crear_linea_directa, crear_tenant_directo, crear_usuario

TABLAS_TENANT = {"users", "memberships", "lines", "login_tokens", "sessions", "consents",
                 "support_grants", "access_audit_log", "product_events"}


async def _owner(radar_urls):
    return await asyncpg.connect(radar_urls["migrator"])


async def test_todas_las_tablas_tienen_tenant_id_not_null_y_rls_forzada(radar_urls):
    con = await _owner(radar_urls)
    try:
        tablas = {r["table_name"] for r in await con.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")} - {"alembic_version_radar"}
        assert tablas == TABLAS_TENANT | {"tenants"}
        for t in TABLAS_TENANT:
            col = await con.fetchrow(
                "SELECT is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name = $1 AND column_name = 'tenant_id'", t)
            assert col is not None and col["is_nullable"] == "NO", t
            assert "radar_tenant_actual()" in col["column_default"], t
        for r in await con.fetch(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY($1)",
                list(tablas)):
            assert r["relrowsecurity"] and r["relforcerowsecurity"], r["relname"]
    finally:
        await con.close()


async def test_sin_columnas_de_conversacion_ni_identificadores_de_whatsapp(radar_urls):
    con = await _owner(radar_urls)
    try:
        filas = await con.fetch(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name ~ "
            "'(^|_)(phone|telefono|jid|lid|body|contenido|payload|chat_id|mensaje)(_|$)'")
    finally:
        await con.close()
    assert filas == []


async def test_rls_en_tabla_con_datos(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    b = await crear_tenant_directo(radar_db, "B")
    la = await crear_linea_directa(radar_db, a, "Línea de A")
    async with radar_db.tenant_tx(b) as con:
        assert await con.fetch("SELECT id FROM lines") == []
        assert await con.fetchval("SELECT count(*) FROM lines WHERE id = $1", la) == 0
        assert await con.execute("UPDATE lines SET nombre = 'x' WHERE id = $1", la) == "UPDATE 0"
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with radar_db.tenant_tx(b) as con:
            await con.execute("INSERT INTO lines (tenant_id, nombre) VALUES ($1, 'colada')", a)
    async with radar_db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT nombre FROM lines WHERE id = $1", la) == "Línea de A"


async def test_tenant_id_por_defecto_es_el_de_la_transaccion(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    la = await crear_linea_directa(radar_db, a)
    async with radar_db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT tenant_id FROM lines WHERE id = $1", la) == a
        fila = await con.fetchrow("SELECT estado, almacen_fuente, duracion_vinculo_dias, retencion_fuente_dias, "
                                  "retencion_tras_desvinculo_dias, tope_ia_mensual_usd FROM lines WHERE id = $1", la)
    assert dict(fila) == {"estado": "sin_vinculo", "almacen_fuente": "permanente", "duracion_vinculo_dias": 0,
                          "retencion_fuente_dias": 0, "retencion_tras_desvinculo_dias": 0, "tope_ia_mensual_usd": None}


async def test_linea_purgable_coherente_con_retencion(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    with pytest.raises(asyncpg.CheckViolationError):
        async with radar_db.tenant_tx(a) as con:
            await con.execute("INSERT INTO lines (nombre, retencion_fuente_dias) VALUES ('x', 7)")
    with pytest.raises(asyncpg.CheckViolationError):
        async with radar_db.tenant_tx(a) as con:
            await con.execute("INSERT INTO lines (nombre, almacen_fuente) VALUES ('x', 'purgable')")
    with pytest.raises(asyncpg.CheckViolationError):
        async with radar_db.tenant_tx(a) as con:
            await con.execute("INSERT INTO lines (nombre, estado) VALUES ('x', 'de_baja')")


async def test_admin_solo_en_el_tenant_kis(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    with pytest.raises(asyncpg.CheckViolationError):
        await crear_usuario(radar_db, a, "x@cliente.com", "admin")
    await crear_usuario(radar_db, TENANT_KIS, "x@keepitsimple.com.ar", "admin")


async def test_email_normalizado_en_la_base(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    with pytest.raises(asyncpg.CheckViolationError):
        await crear_usuario(radar_db, a, "Mayus@Cliente.com", "dueno")
    with pytest.raises(asyncpg.CheckViolationError):
        await crear_usuario(radar_db, a, "sin-arroba", "dueno")


async def test_product_events_rechaza_texto_libre(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    async with radar_db.tenant_tx(a) as con:
        await con.execute("INSERT INTO product_events (evento, valores) VALUES ('login_canjeado', $1::jsonb)",
                          json.dumps({"n": 3, "ok": True, "ratio": 0.5}))
    for malo in ({"nombre": "Juan"}, {"anidado": {"t": "x"}}, {"lista": ["a"]}, "\"texto\"", "[1]"):
        with pytest.raises(asyncpg.CheckViolationError):
            async with radar_db.tenant_tx(a) as con:
                await con.execute("INSERT INTO product_events (evento, valores) VALUES ('login_canjeado', $1::jsonb)",
                                  malo if isinstance(malo, str) else json.dumps(malo))
    with pytest.raises(asyncpg.CheckViolationError):
        async with radar_db.tenant_tx(a) as con:
            await con.execute("INSERT INTO product_events (evento) VALUES ('Evento Con Espacios')")


async def test_access_audit_log_rechaza_telefonos_y_emails_en_detalle(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    ok = {"contact_hmac": "ab12" * 16, "parametro": "duracion_vinculo_dias", "valor_nuevo": 30}
    async with radar_db.tenant_tx(a) as con:
        await con.execute("INSERT INTO access_audit_log (actor_rol, accion, tipo_objeto, detalle) "
                          "VALUES ('dueno', 'parametro_cambiado', 'line', $1::jsonb)", json.dumps(ok))
    for malo in ({"t": "+5493411234567"}, {"t": "5493411234567@c.us"}, {"e": "a@b.c"}, {"n": "123456"}):
        with pytest.raises(asyncpg.CheckViolationError):
            async with radar_db.tenant_tx(a) as con:
                await con.execute("INSERT INTO access_audit_log (actor_rol, accion, tipo_objeto, detalle) "
                                  "VALUES ('dueno', 'parametro_cambiado', 'line', $1::jsonb)", json.dumps(malo))


async def test_auditoria_y_eventos_son_solo_de_insercion_para_radar_app(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    async with radar_db.tenant_tx(a) as con:
        await con.execute("INSERT INTO access_audit_log (actor_rol, accion, tipo_objeto) VALUES ('sistema', 'x', 'y')")
        await con.execute("INSERT INTO product_events (evento) VALUES ('x')")
    for sql in ("DELETE FROM access_audit_log", "UPDATE access_audit_log SET accion = 'z'",
                "DELETE FROM product_events", "DELETE FROM consents", "DELETE FROM login_tokens"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with radar_db.tenant_tx(a) as con:
                await con.execute(sql)


async def test_usuarios_por_email_cruza_tenants_y_normaliza(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    b = await crear_tenant_directo(radar_db, "B")
    ua = await crear_usuario(radar_db, a, "dueno@cliente.com", "dueno")
    ub = await crear_usuario(radar_db, b, "dueno@cliente.com", "gestor")
    async with radar_db.sin_tenant() as con:
        filas = await con.fetch("SELECT user_id, tenant_id FROM radar_auth_usuarios_por_email($1)",
                                "  Dueno@Cliente.com ")
        assert await con.fetchval("SELECT count(*) FROM users") == 0   # sin tenant, RLS oculta
    assert {(r["user_id"], r["tenant_id"]) for r in filas} == {(ua, a), (ub, b)}


async def test_support_grants_maximo_72_horas(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    u = await crear_usuario(radar_db, a, "dueno@cliente.com", "dueno")
    async with radar_db.tenant_tx(a) as con:
        await con.execute("INSERT INTO support_grants (otorgado_por, expires_at) VALUES ($1, now() + interval '48 hours')", u)
    with pytest.raises(asyncpg.CheckViolationError):
        async with radar_db.tenant_tx(a) as con:
            await con.execute("INSERT INTO support_grants (otorgado_por, expires_at) VALUES ($1, now() + interval '80 hours')", u)
```

- [ ] **Step 3: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_esquema.py -q
```
Esperado: `asyncpg.exceptions.UndefinedTableError: relation "lines" does not exist` (y fallas de aserción en el test de tablas).

- [ ] **Step 4: Migración r0002**

`migrations_radar/versions/r0002_acceso_lineas_y_auditoria.py`:

```python
"""Radar r0002: usuarios, membresías, líneas, tokens de login, sesiones,
consentimientos, soporte, auditoría y eventos de producto (§2.1, §4.4, §6.4).

Toda tabla lleva tenant_id NOT NULL con DEFAULT radar_tenant_actual(): un
INSERT desde tenant_tx() puede omitirlo y RLS (WITH CHECK) rechaza cualquier
valor distinto del tenant de la transacción.

Revision ID: r0002
Revises: r0001
Create Date: 2026-09-21
"""
from alembic import op

from app.radar.constantes import TENANT_KIS_STR
from app.radar.rls_sql import definir_funcion_admin, grants_app, politica_por_tenant

revision = "r0002"
down_revision = "r0001"
branch_labels = None
depends_on = None

# Privilegios de radar_app por tabla. Sin DELETE en nada que sea evidencia
# (tokens, consentimientos, auditoría, eventos): las purgas son del tramo 6 y
# corren con el rol dueño.
GRANTS = {
    "users": "SELECT, INSERT, UPDATE",
    "memberships": "SELECT, INSERT, UPDATE, DELETE",
    "lines": "SELECT, INSERT, UPDATE",
    "login_tokens": "SELECT, INSERT, UPDATE",
    "sessions": "SELECT, INSERT, UPDATE",
    "consents": "SELECT, INSERT",
    "support_grants": "SELECT, INSERT, UPDATE",
    "access_audit_log": "SELECT, INSERT",
    "product_events": "SELECT, INSERT",
}


def upgrade() -> None:
    op.execute("""
        CREATE TABLE users (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id  UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            email      TEXT NOT NULL CHECK (email = lower(btrim(email)) AND email ~ '^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$'),
            nombre     TEXT NOT NULL DEFAULT '' CHECK (length(nombre) <= 120),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, email)
        );

        CREATE TABLE memberships (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id         UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            rol               TEXT NOT NULL CHECK (rol IN ('admin', 'dueno', 'gestor', 'lector')),
            lineas_permitidas UUID[] NULL,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, user_id),
            CHECK (rol <> 'admin' OR tenant_id = '""" + TENANT_KIS_STR + """')
        );

        CREATE TABLE lines (
            id                             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id                      UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            nombre                         TEXT NOT NULL CHECK (length(nombre) BETWEEN 1 AND 80),
            estado                         TEXT NOT NULL DEFAULT 'sin_vinculo'
                                           CHECK (estado IN ('vinculada', 'sin_vinculo', 'de_baja')),
            almacen_fuente                 TEXT NOT NULL DEFAULT 'permanente'
                                           CHECK (almacen_fuente IN ('permanente', 'purgable')),
            -- Parámetros de ámbito Línea (§2.1)
            duracion_vinculo_dias          INTEGER NOT NULL DEFAULT 0 CHECK (duracion_vinculo_dias >= 0),
            retencion_fuente_dias          INTEGER NOT NULL DEFAULT 0 CHECK (retencion_fuente_dias >= 0),
            retencion_tras_desvinculo_dias INTEGER NOT NULL DEFAULT 0 CHECK (retencion_tras_desvinculo_dias >= 0),
            tope_ia_mensual_usd            NUMERIC(10, 2) NULL CHECK (tope_ia_mensual_usd IS NULL OR tope_ia_mensual_usd >= 0),
            -- Propuesta pendiente de un admin de KIS para aflojar parámetros de una
            -- línea viva (§2.1); la consume el consentimiento del dueño.
            parametros_propuestos          JSONB NULL
                                           CHECK (parametros_propuestos IS NULL OR jsonb_typeof(parametros_propuestos) = 'object'),
            fuente_purgada_hasta           TIMESTAMPTZ NULL,
            de_baja_at                     TIMESTAMPTZ NULL,
            created_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),
            CHECK ((almacen_fuente = 'purgable') = (retencion_fuente_dias > 0)),
            CHECK ((estado = 'de_baja') = (de_baja_at IS NOT NULL))
        );

        CREATE TABLE login_tokens (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id    UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash   TEXT NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
            proposito    TEXT NOT NULL CHECK (proposito IN ('login', 'invitacion')),
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at   TIMESTAMPTZ NOT NULL,
            used_at      TIMESTAMPTZ NULL,
            ip_solicitud TEXT NULL
        );
        CREATE INDEX login_tokens_user_reciente ON login_tokens (tenant_id, user_id, created_at DESC);

        CREATE TABLE sessions (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id    UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            -- Una sesión de soporte referencia a un usuario del tenant KIS: la FK
            -- se valida igual (las comprobaciones de integridad no pasan por RLS).
            user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            rol          TEXT NOT NULL CHECK (rol IN ('admin', 'dueno', 'gestor', 'lector', 'soporte')),
            token_hash   TEXT NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at   TIMESTAMPTZ NOT NULL,
            revoked_at   TIMESTAMPTZ NULL,
            last_seen_at TIMESTAMPTZ NULL,
            ip           TEXT NULL
        );
        CREATE INDEX sessions_user ON sessions (tenant_id, user_id);

        CREATE TABLE consents (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id     UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            line_id       UUID NOT NULL REFERENCES lines(id),
            user_id       UUID NOT NULL REFERENCES users(id),
            version_texto TEXT NOT NULL CHECK (version_texto ~ '^v[0-9]+$'),
            hash_texto    TEXT NOT NULL CHECK (hash_texto ~ '^[0-9a-f]{64}$'),
            opciones      JSONB NOT NULL CHECK (jsonb_typeof(opciones) = 'object'),
            ip            TEXT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX consents_line ON consents (tenant_id, line_id, created_at DESC);

        CREATE TABLE support_grants (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id    UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            otorgado_por UUID NOT NULL REFERENCES users(id),
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at   TIMESTAMPTZ NOT NULL,
            revocado_at  TIMESTAMPTZ NULL,
            CHECK (expires_at > created_at AND expires_at <= created_at + interval '72 hours')
        );

        -- §4.4: actor, rol, acción, tipo de objeto, UUID interno, fecha e IP.
        -- Nunca teléfonos, JID, nombres ni texto. El CHECK rechaza cualquier
        -- string con 6 dígitos seguidos o '@' salvo en contact_hmac (hex).
        CREATE TABLE access_audit_log (
            id            BIGSERIAL PRIMARY KEY,
            tenant_id     UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            actor_user_id UUID NULL,
            actor_rol     TEXT NOT NULL CHECK (actor_rol IN ('admin', 'dueno', 'gestor', 'lector', 'soporte', 'sistema')),
            accion        TEXT NOT NULL CHECK (accion ~ '^[a-z_]{1,40}$'),
            tipo_objeto   TEXT NOT NULL CHECK (tipo_objeto ~ '^[a-z_]{1,40}$'),
            objeto_id     UUID NULL,
            ip            TEXT NULL,
            detalle       JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (
                              jsonb_typeof(detalle) = 'object'
                              AND NOT jsonb_path_exists(detalle,
                                  '$.keyvalue() ? (@.key != "contact_hmac" && @.value.type() == "string" && (@.value like_regex "[0-9]{6}" || @.value like_regex "@"))')
                          ),
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX access_audit_log_tenant_fecha ON access_audit_log (tenant_id, created_at);

        -- §6.4: solo tenant, línea, usuario, nombre de evento, UUID internos y
        -- valores numéricos. Ningún string en `valores`, a ninguna profundidad.
        CREATE TABLE product_events (
            id         BIGSERIAL PRIMARY KEY,
            tenant_id  UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            line_id    UUID NULL REFERENCES lines(id),
            user_id    UUID NULL,
            evento     TEXT NOT NULL CHECK (evento ~ '^[a-z_]{1,40}$'),
            objeto_id  UUID NULL,
            valores    JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (
                           jsonb_typeof(valores) = 'object'
                           AND NOT jsonb_path_exists(valores, '$.** ? (@.type() == "string")')
                       ),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX product_events_tenant_fecha ON product_events (tenant_id, created_at);
    """)

    for tabla, privilegios in GRANTS.items():
        op.execute(politica_por_tenant(tabla))
        op.execute(grants_app(tabla, privilegios))
    op.execute("""
        GRANT USAGE ON SEQUENCE access_audit_log_id_seq TO radar_app;
        GRANT USAGE ON SEQUENCE product_events_id_seq TO radar_app;
    """)

    # Buscar usuarios por email cruza tenants (el que pide el link no sabe su
    # tenant): función SECURITY DEFINER de radar_admin, solo lectura.
    op.execute("""
        GRANT SELECT ON users TO radar_admin;
        CREATE POLICY users_admin ON users FOR SELECT TO radar_admin USING (true);
    """)
    op.execute(definir_funcion_admin(
        "radar_auth_usuarios_por_email(text)",
        """
        CREATE FUNCTION radar_auth_usuarios_por_email(p_email text)
            RETURNS TABLE (user_id uuid, tenant_id uuid)
            LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
            AS $f$
                SELECT u.id, u.tenant_id FROM users u
                WHERE u.email = lower(btrim(p_email)) ORDER BY u.created_at
            $f$;
        """,
    ))


def downgrade() -> None:
    op.execute("""
        DROP FUNCTION IF EXISTS radar_auth_usuarios_por_email(text);
        DROP TABLE IF EXISTS product_events, access_audit_log, support_grants, consents,
                             sessions, login_tokens, lines, memberships, users;
    """)
```

- [ ] **Step 5: Lista de tablas en la fixture**

En `tests/radar_tests/conftest.py`:

```python
# Orden de TRUNCATE: hijas antes que padres.
TABLAS = ["product_events", "access_audit_log", "support_grants", "consents", "sessions",
          "login_tokens", "memberships", "lines", "users", "tenants"]
```

- [ ] **Step 6: Correr**

```bash
python -m pytest tests/radar_tests -q
```
Esperado: todo verde; `test_esquema.py` suma 12.

- [ ] **Step 7: Commit**

```bash
git add migrations_radar tests/radar_tests
git commit -m "Radar: usuarios, lineas, sesiones, consentimientos y auditoria con RLS"
```

---

### Task 7: Escritores de auditoría y de eventos de producto

**Files:**
- Create: `app/radar/auditoria.py`, `app/radar/eventos_producto.py`
- Test: `tests/radar_tests/test_auditoria.py`

**Interfaces:**
- Consumes: `PARAMETROS` (para validar valores de parámetros en `detalle`), tablas de r0002.
- Produces: `ACCIONES: frozenset[str]`, `TIPOS_OBJETO: frozenset[str]`, `ROLES_ACTOR: frozenset[str]`, `CLAVES_DETALLE: dict[str, str]`, `DetalleProhibido(ValueError)`, `validar_detalle(detalle: dict | None) -> dict`, `async registrar(con, *, tenant_id: uuid.UUID, actor_user_id: uuid.UUID | None, actor_rol: str, accion: str, tipo_objeto: str, objeto_id: uuid.UUID | None = None, ip: str | None = None, detalle: dict | None = None) -> int`; `EVENTOS: frozenset[str]`, `ValoresProhibidos(ValueError)`, `validar_valores(valores: dict | None) -> dict`, `async registrar_evento(con, *, tenant_id, evento: str, line_id=None, user_id=None, objeto_id=None, valores=None) -> int`.

- [ ] **Step 1: Tests (fallan)**

`tests/radar_tests/test_auditoria.py`:

```python
"""
access_audit_log y product_events desde la app: lista blanca de claves,
valores sin texto libre y solo inserción.
"""
from decimal import Decimal

import pytest

from app.radar import auditoria, eventos_producto
from app.radar.auditoria import DetalleProhibido
from app.radar.eventos_producto import ValoresProhibidos

from .helpers import crear_tenant_directo, crear_usuario


async def test_registrar_auditoria_y_leer(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    u = await crear_usuario(radar_db, a, "dueno@cliente.com", "dueno")
    async with radar_db.tenant_tx(a) as con:
        id_ = await auditoria.registrar(
            con, tenant_id=a, actor_user_id=u, actor_rol="dueno", accion="parametro_cambiado",
            tipo_objeto="line", objeto_id=u, ip="10.0.0.1",
            detalle={"parametro": "tope_ia_mensual_usd", "valor_anterior": None,
                     "valor_nuevo": Decimal("12.50"), "ambito": "linea"},
        )
        fila = await con.fetchrow("SELECT * FROM access_audit_log WHERE id = $1", id_)
    assert fila["actor_user_id"] == u and fila["actor_rol"] == "dueno"
    assert fila["accion"] == "parametro_cambiado" and fila["tipo_objeto"] == "line"
    assert fila["ip"] == "10.0.0.1"
    import json
    assert json.loads(fila["detalle"]) == {"parametro": "tope_ia_mensual_usd", "valor_anterior": None,
                                           "valor_nuevo": 12.5, "ambito": "linea"}


@pytest.mark.parametrize("detalle", [
    {"nombre": "Juan"},                                   # clave fuera de la lista blanca
    {"telefono": "+5493411234567"},
    {"contact_hmac": "zz"},                               # no es hex de 64
    {"parametro": "no_existe"},
    {"valor_nuevo": "hola"},                              # string que no es enum de parámetro
    {"rol_nuevo": "root"},
    {"ambito": "global"},
    {"longitud_termino": "12"},                           # entero como string
    {"cantidad": True},                                   # bool no es entero
])
def test_validar_detalle_rechaza(detalle):
    with pytest.raises(DetalleProhibido):
        auditoria.validar_detalle(detalle)


def test_validar_detalle_acepta_lo_permitido():
    assert auditoria.validar_detalle(None) == {}
    d = auditoria.validar_detalle({"contact_hmac": "0f" * 32, "valor_nuevo": "sensible",
                                   "rol_anterior": "gestor", "rol_nuevo": "lector", "horas": 48,
                                   "longitud_termino": 5, "resultados": 0, "proposito": "invitacion"})
    assert d["valor_nuevo"] == "sensible" and d["horas"] == 48


async def test_registrar_rechaza_accion_o_tipo_desconocidos(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    async with radar_db.tenant_tx(a) as con:
        with pytest.raises(ValueError):
            await auditoria.registrar(con, tenant_id=a, actor_user_id=None, actor_rol="sistema",
                                      accion="borrar_todo", tipo_objeto="line")
        with pytest.raises(ValueError):
            await auditoria.registrar(con, tenant_id=a, actor_user_id=None, actor_rol="sistema",
                                      accion="login_canjeado", tipo_objeto="telefono")
        with pytest.raises(ValueError):
            await auditoria.registrar(con, tenant_id=a, actor_user_id=None, actor_rol="root",
                                      accion="login_canjeado", tipo_objeto="login_token")


async def test_registrar_evento_y_leer(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    async with radar_db.tenant_tx(a) as con:
        id_ = await eventos_producto.registrar_evento(
            con, tenant_id=a, evento="login_canjeado", valores={"intentos": 1, "demora_ms": 120.5})
        fila = await con.fetchrow("SELECT evento, valores FROM product_events WHERE id = $1", id_)
    import json
    assert fila["evento"] == "login_canjeado"
    assert json.loads(fila["valores"]) == {"intentos": 1, "demora_ms": 120.5}


@pytest.mark.parametrize("valores", [
    {"nombre": "x"}, {"n": None}, {"n": [1]}, {"n": {"m": 1}}, {"Clave Rara": 1},
])
def test_validar_valores_rechaza(valores):
    with pytest.raises(ValoresProhibidos):
        eventos_producto.validar_valores(valores)


async def test_registrar_evento_rechaza_evento_desconocido(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    async with radar_db.tenant_tx(a) as con:
        with pytest.raises(ValueError):
            await eventos_producto.registrar_evento(con, tenant_id=a, evento="cualquier_cosa")
```

- [ ] **Step 2: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_auditoria.py -q
```
Esperado: `ImportError: cannot import name 'auditoria' from 'app.radar'`.

- [ ] **Step 3: `auditoria.py`**

```python
"""
access_audit_log (§4.4). Cada fila: actor, rol, acción, tipo de objeto, UUID
interno del objeto, fecha e IP. NUNCA teléfonos, JID, nombres ni texto:
`detalle` solo admite claves de la lista blanca, con valores numéricos,
booleanos, enums conocidos o un HMAC hex de 64 caracteres. La tabla tiene
además un CHECK que rechaza strings con 6 dígitos seguidos o '@'.

Retención: 24 meses (el job de purga es del tramo 6). radar_app solo inserta.
"""

import json
import re
import uuid
from decimal import Decimal
from typing import Any, Optional

import asyncpg

from app.radar.parametros import PARAMETROS

ACCIONES = frozenset({
    "tenant_creado", "tenants_listados", "linea_creada", "usuario_invitado", "invitacion_reenviada",
    "rol_cambiado", "parametro_cambiado", "parametro_propuesto", "consentimiento_registrado", "login_canjeado",
    "sesion_cerrada", "sesion_revocada", "soporte_otorgado", "soporte_revocado", "acceso_soporte",
})
TIPOS_OBJETO = frozenset({
    "tenant", "line", "user", "membership", "consent", "session", "support_grant", "login_token",
})
ROLES_ACTOR = frozenset({"admin", "dueno", "gestor", "lector", "soporte", "sistema"})

# clave -> tipo admitido
CLAVES_DETALLE = {
    "longitud_termino": "entero", "resultados": "entero", "cantidad": "entero", "horas": "entero",
    "contact_hmac": "hmac",
    "parametro": "parametro", "valor_anterior": "valor_parametro", "valor_nuevo": "valor_parametro",
    "rol_anterior": "rol", "rol_nuevo": "rol",
    "ambito": "ambito", "proposito": "proposito",
}
_HMAC = re.compile(r"^[0-9a-f]{64}$")
_ROLES = frozenset({"admin", "dueno", "gestor", "lector", "soporte"})
_ENUMS_PARAMETROS = frozenset(v for p in PARAMETROS.values() for v in p.orden)


class DetalleProhibido(ValueError):
    pass


def _es_entero(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


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
    }[tipo](valor)
    if not ok:
        raise DetalleProhibido(f"detalle.{clave}: valor no admitido")
    return float(valor) if isinstance(valor, Decimal) else valor


def validar_detalle(detalle: Optional[dict]) -> dict:
    limpio: dict[str, Any] = {}
    for clave, valor in (detalle or {}).items():
        tipo = CLAVES_DETALLE.get(clave)
        if tipo is None:
            raise DetalleProhibido(f"detalle.{clave}: clave fuera de la lista blanca")
        limpio[clave] = _validar_valor(clave, tipo, valor)
    return limpio


async def registrar(con: asyncpg.Connection, *, tenant_id: uuid.UUID, actor_user_id: Optional[uuid.UUID],
                    actor_rol: str, accion: str, tipo_objeto: str, objeto_id: Optional[uuid.UUID] = None,
                    ip: Optional[str] = None, detalle: Optional[dict] = None) -> int:
    if accion not in ACCIONES:
        raise ValueError(f"acción de auditoría desconocida: {accion}")
    if tipo_objeto not in TIPOS_OBJETO:
        raise ValueError(f"tipo de objeto desconocido: {tipo_objeto}")
    if actor_rol not in ROLES_ACTOR:
        raise ValueError(f"rol de actor desconocido: {actor_rol}")
    return await con.fetchval(
        "INSERT INTO access_audit_log (tenant_id, actor_user_id, actor_rol, accion, tipo_objeto, objeto_id, ip, detalle) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb) RETURNING id",
        tenant_id, actor_user_id, actor_rol, accion, tipo_objeto, objeto_id, ip,
        json.dumps(validar_detalle(detalle)),
    )
```

- [ ] **Step 4: `eventos_producto.py`**

```python
"""
product_events (§6.4, §9): instrumentación de producto por línea. Solo tenant,
línea, usuario, nombre de evento, UUID internos y valores numéricos. Nada de
texto libre: lo rechazan esta función y el CHECK de la tabla.
"""

import json
import re
import uuid
from typing import Any, Optional

import asyncpg

EVENTOS = frozenset({
    "invitacion_enviada", "login_canjeado", "consentimiento_registrado", "linea_creada",
})
_CLAVE = re.compile(r"^[a-z_]{1,40}$")


class ValoresProhibidos(ValueError):
    pass


def validar_valores(valores: Optional[dict]) -> dict:
    limpio: dict[str, Any] = {}
    for clave, valor in (valores or {}).items():
        if not isinstance(clave, str) or not _CLAVE.match(clave):
            raise ValoresProhibidos(f"clave inválida: {clave!r}")
        if isinstance(valor, bool) or not isinstance(valor, (int, float)):
            raise ValoresProhibidos(f"valores.{clave}: solo números")
        limpio[clave] = valor
    return limpio


async def registrar_evento(con: asyncpg.Connection, *, tenant_id: uuid.UUID, evento: str,
                           line_id: Optional[uuid.UUID] = None, user_id: Optional[uuid.UUID] = None,
                           objeto_id: Optional[uuid.UUID] = None, valores: Optional[dict] = None) -> int:
    if evento not in EVENTOS:
        raise ValueError(f"evento de producto desconocido: {evento}")
    return await con.fetchval(
        "INSERT INTO product_events (tenant_id, line_id, user_id, evento, objeto_id, valores) "
        "VALUES ($1, $2, $3, $4, $5, $6::jsonb) RETURNING id",
        tenant_id, line_id, user_id, evento, objeto_id, json.dumps(validar_valores(valores)),
    )
```

- [ ] **Step 5: Correr**

```bash
python -m pytest tests/radar_tests/test_auditoria.py -q
```
Esperado: `19 passed`.

- [ ] **Step 6: Commit**

```bash
git add app/radar/auditoria.py app/radar/eventos_producto.py tests/radar_tests/test_auditoria.py
git commit -m "Radar: auditoria sin identificadores y eventos de producto sin texto"
```

---

### Task 8: Mailer, tokens, sesiones y cookie firmada

**Files:**
- Create: `app/radar/mailer.py`, `app/radar/auth.py`
- Modify: `app/radar/contexto.py`, `app/radar/app.py` (`construir_contexto`), `tests/radar_tests/conftest.py` (`radar_ctx`)
- Test: `tests/radar_tests/test_auth.py`

**Interfaces:**
- Consumes: `RadarContexto`, `contexto(request)`, tablas `sessions`, `users`, `memberships`, `tenants`; `SecretStore`, `FileSecretStore`.
- Produces (`mailer.py`): `Email(para, asunto, texto, huella)`, `Mailer` (Protocol `async enviar(mail: Email) -> None`), `MemoryMailer` (`.enviados: list[Email]`), `LogMailer`, `huella_token(token: str) -> str` (8 hex), `dominio_de(email: str) -> str`, `construir_mailer(nombre: str) -> Mailer`.
- Produces (`auth.py`): `COOKIE = "radar_sesion"`, `LINK_MAGICO = timedelta(minutes=15)`, `INVITACION = timedelta(days=7)`, `SESION = timedelta(days=30)`, `MAX_PEDIDOS_LOGIN = 3`, `VENTANA_PEDIDOS = timedelta(minutes=15)`, `ROLES_MEMBRESIA`, `ROLES_SESION`; `generar_token() -> tuple[str, str]`, `hash_token(token) -> str`, `token_valido(token) -> bool`, `firmar(secret, contenido) -> str`, `armar_cookie(secret, tenant_id, token) -> str`, `abrir_cookie(secret, valor) -> tuple[uuid.UUID, str] | None`; `Sesion(id, tenant_id, user_id, rol, email, lineas_permitidas, es_kis)` con `puede_ver_linea(line_id) -> bool`; `async emitir_sesion(con, *, tenant_id, user_id, rol, ip, duracion=SESION) -> str`; `async resolver_sesion(con, tenant_id, token) -> Sesion | None`; `async revocar_sesion(con, session_id) -> None`; `async revocar_sesiones_de(con, user_id) -> int`; `ip_de(request) -> str | None`; dependencias `sesion_actual(request) -> Sesion` (401) y `requiere_rol(*roles)` (403); `set_cookie_sesion(response, ctx, tenant_id, token, max_age=...)`, `borrar_cookie_sesion(response)`.
- `RadarContexto` gana `secretos: SecretStore` y `mailer: Mailer`.

- [ ] **Step 1: Tests (fallan)**

`tests/radar_tests/test_auth.py`:

```python
"""
Tokens hasheados, cookie firmada atada al tenant, sesiones revocables y un
mailer que jamás loguea el token.
"""
import hashlib
import logging
import uuid
from datetime import timedelta

import pytest
from starlette.requests import Request
from starlette.responses import Response

from app.radar import auth
from app.radar.mailer import Email, LogMailer, MemoryMailer, construir_mailer, huella_token
from app.radar.settings import RadarSettings

from .helpers import crear_tenant_directo, crear_usuario

SECRETO = "secreto-de-test-de-32-caracteres!"


def test_generar_token_devuelve_claro_y_sha256():
    claro, h = auth.generar_token()
    assert h == hashlib.sha256(claro.encode()).hexdigest()
    assert auth.token_valido(claro)
    assert not auth.token_valido("corto")
    assert not auth.token_valido(claro + " ")


def test_cookie_firmada_roundtrip_y_adulteraciones():
    tid = uuid.uuid4()
    claro, _ = auth.generar_token()
    valor = auth.armar_cookie(SECRETO, tid, claro)
    assert auth.abrir_cookie(SECRETO, valor) == (tid, claro)
    t, k, firma = valor.split(".")
    otro, _ = auth.generar_token()
    assert auth.abrir_cookie(SECRETO, f"{t}.{otro}.{firma}") is None          # otro token
    assert auth.abrir_cookie(SECRETO, f"{uuid.uuid4()}.{k}.{firma}") is None  # otro tenant
    assert auth.abrir_cookie("otro-secreto", valor) is None
    assert auth.abrir_cookie(SECRETO, "basura") is None
    assert auth.abrir_cookie(SECRETO, "") is None
    # firma con caracteres no ASCII: hmac.compare_digest sobre str lanzaría TypeError (500 en vez de 401)
    assert auth.abrir_cookie(SECRETO, f"{t}.{k}." + "é" * 64) is None
    assert auth.abrir_cookie(SECRETO, f"{t}.{k}.ñ") is None


def test_set_cookie_sesion_emite_secure_httponly_y_samesite():
    """Los tests de API corren con cookie_secure=False; este guarda el
    atributo Secure de producción."""
    class Ctx:
        settings = RadarSettings(_env_file=None, cookie_secret=SECRETO, cookie_secure=True)
    resp = Response()
    claro, _ = auth.generar_token()
    auth.set_cookie_sesion(resp, Ctx(), uuid.uuid4(), claro)
    sc = resp.headers["set-cookie"].lower()
    assert "secure" in sc and "httponly" in sc and "samesite=lax" in sc and "path=/radar" in sc
    assert f"max-age={int(auth.SESION.total_seconds())}" in sc


async def test_emitir_resolver_y_revocar(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    u = await crear_usuario(radar_db, a, "dueno@cliente.com", "dueno")
    async with radar_db.tenant_tx(a) as con:
        token = await auth.emitir_sesion(con, tenant_id=a, user_id=u, rol="dueno", ip="10.0.0.1")
        s = await auth.resolver_sesion(con, a, token)
        assert s is not None
        assert (s.tenant_id, s.user_id, s.rol, s.email, s.lineas_permitidas, s.es_kis) == \
               (a, u, "dueno", "dueno@cliente.com", None, False)
        assert s.puede_ver_linea(uuid.uuid4())
        assert await auth.resolver_sesion(con, a, token[:-1] + "x") is None
        await auth.revocar_sesion(con, s.id)
        assert await auth.resolver_sesion(con, a, token) is None


async def test_sesion_vencida_no_resuelve(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    u = await crear_usuario(radar_db, a, "dueno@cliente.com", "dueno")
    async with radar_db.tenant_tx(a) as con:
        token = await auth.emitir_sesion(con, tenant_id=a, user_id=u, rol="dueno", ip=None,
                                         duracion=timedelta(seconds=-1))
        assert await auth.resolver_sesion(con, a, token) is None


async def test_sesion_no_cruza_tenants(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    b = await crear_tenant_directo(radar_db, "B")
    u = await crear_usuario(radar_db, a, "dueno@cliente.com", "dueno")
    async with radar_db.tenant_tx(a) as con:
        token = await auth.emitir_sesion(con, tenant_id=a, user_id=u, rol="dueno", ip=None)
    async with radar_db.tenant_tx(b) as con:
        assert await auth.resolver_sesion(con, b, token) is None


async def test_revocar_sesiones_de_usuario(radar_db):
    a = await crear_tenant_directo(radar_db, "A")
    u = await crear_usuario(radar_db, a, "g@cliente.com", "gestor", lineas_permitidas=[uuid.uuid4()])
    async with radar_db.tenant_tx(a) as con:
        t1 = await auth.emitir_sesion(con, tenant_id=a, user_id=u, rol="gestor", ip=None)
        t2 = await auth.emitir_sesion(con, tenant_id=a, user_id=u, rol="gestor", ip=None)
        s = await auth.resolver_sesion(con, a, t1)
        assert len(s.lineas_permitidas) == 1 and not s.puede_ver_linea(uuid.uuid4())
        assert await auth.revocar_sesiones_de(con, u) == 2
        assert await auth.resolver_sesion(con, a, t2) is None


async def test_emitir_sesion_rechaza_rol_desconocido():
    with pytest.raises(ValueError):    # falla antes de tocar la conexión
        await auth.emitir_sesion(None, tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), rol="root", ip=None)


def test_ip_de_toma_el_ultimo_salto_de_x_forwarded_for():
    """El primer valor lo escribe el cliente; el proxy de Railway agrega el
    salto real al final. La IP es evidencia (§4.4): no puede ser elegible."""
    scope = {"type": "http", "method": "GET", "path": "/", "headers": [(b"x-forwarded-for", b"1.2.3.4, 5.6.7.8")],
             "client": ("9.9.9.9", 1234), "query_string": b""}
    assert auth.ip_de(Request(scope)) == "5.6.7.8"
    scope["headers"] = [(b"x-forwarded-for", b"1.2.3.4")]
    assert auth.ip_de(Request(scope)) == "1.2.3.4"
    scope["headers"] = []
    assert auth.ip_de(Request(scope)) == "9.9.9.9"


def test_huella_token_es_estable_y_no_revela():
    claro, _ = auth.generar_token()
    h = huella_token(claro)
    assert len(h) == 8 and h == huella_token(claro) and h not in claro


async def test_log_mailer_no_loguea_ni_token_ni_cuerpo(caplog):
    caplog.set_level(logging.INFO, logger="app.radar.mailer")
    claro, _ = auth.generar_token()
    mail = Email(para="dueno@cliente.com", asunto="Tu acceso", texto=f"link: http://x/?k={claro}",
                 huella=huella_token(claro))
    await LogMailer().enviar(mail)
    salida = "\n".join(r.getMessage() for r in caplog.records)
    assert "*@cliente.com" in salida and mail.huella in salida
    assert claro not in salida and "dueno@" not in salida and "link:" not in salida


async def test_memory_mailer_guarda():
    m = MemoryMailer()
    await m.enviar(Email(para="a@b.c", asunto="x", texto="y", huella="00000000"))
    assert m.enviados[0].para == "a@b.c"


def test_construir_mailer():
    assert isinstance(construir_mailer("memoria"), MemoryMailer)
    assert isinstance(construir_mailer("log"), LogMailer)
    with pytest.raises(ValueError):
        construir_mailer("sendgrid")
```

- [ ] **Step 2: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_auth.py -q
```
Esperado: `ImportError: cannot import name 'auth' from 'app.radar'`.

- [ ] **Step 3: `mailer.py`**

```python
"""
Email transaccional de Radar. Los mails llevan solo conteos y un link
autenticado (§3 P5, §7): nunca datos de conversación.

Backends:
- MemoryMailer: guarda los mails en memoria (los tests leen el link de ahí).
- LogMailer: dev/staging. Loguea SOLO `*@dominio`, asunto y la huella del
  token (sha256 truncada); jamás el cuerpo ni el token.
El proveedor de producción es una decisión pendiente del dueño (ver el plan,
Self-Review): construir_mailer() rechaza cualquier otro nombre.
"""

import hashlib
import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger("app.radar.mailer")


@dataclass(frozen=True)
class Email:
    para: str
    asunto: str
    texto: str
    huella: str   # huella_token() del token que viaja en el cuerpo; sirve para correlacionar logs


class Mailer(Protocol):
    async def enviar(self, mail: Email) -> None: ...


class MemoryMailer:
    def __init__(self):
        self.enviados: list[Email] = []

    async def enviar(self, mail: Email) -> None:
        self.enviados.append(mail)


class LogMailer:
    async def enviar(self, mail: Email) -> None:
        logger.info("email enviado a *@%s asunto=%r huella=%s", dominio_de(mail.para), mail.asunto, mail.huella)


def huella_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:8]


def dominio_de(email: str) -> str:
    return email.rsplit("@", 1)[-1] if "@" in email else "?"


def construir_mailer(nombre: str) -> Mailer:
    if nombre == "memoria":
        return MemoryMailer()
    if nombre == "log":
        return LogMailer()
    raise ValueError(f"RADAR_MAILER desconocido: {nombre!r} (log|memoria)")
```

- [ ] **Step 4: `auth.py`**

```python
"""
Tokens, sesiones y cookie de Radar (§3 P0, §7).

- Tokens (link mágico, invitación, sesión): secrets.token_urlsafe(32), guardados
  hasheados con sha256, como en app/services/branch_auth.py. El claro viaja una
  sola vez: en el link mágico (se canjea por POST y se invalida) o en la cookie.
- Cookie `radar_sesion` = "<tenant_id>.<token>.<firma>", firma HMAC-SHA256 con
  RADAR_COOKIE_SECRET sobre "<tenant_id>.<token>". HttpOnly, Secure,
  SameSite=Lax, Path=/radar. La sesión queda atada al tenant dentro del token
  (§7): el lookup corre dentro de tenant_tx(tenant_id) y es por hash.
- Comparaciones con hmac.compare_digest; el lookup por hash usa un índice único.
"""

import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

import asyncpg
from fastapi import Depends, HTTPException, Request, Response

from app.radar.contexto import RadarContexto, contexto

COOKIE = "radar_sesion"
LINK_MAGICO = timedelta(minutes=15)
INVITACION = timedelta(days=7)
SESION = timedelta(days=30)
MAX_PEDIDOS_LOGIN = 3                 # por usuario, cada VENTANA_PEDIDOS
VENTANA_PEDIDOS = timedelta(minutes=15)
ROLES_MEMBRESIA = ("admin", "dueno", "gestor", "lector")
ROLES_SESION = ROLES_MEMBRESIA + ("soporte",)
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
_FIRMA = re.compile(r"^[0-9a-f]{64}$")


def generar_token() -> tuple[str, str]:
    """(token en claro, sha256 hex). El claro se entrega una sola vez."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def token_valido(token: str) -> bool:
    return bool(_TOKEN.match(token or ""))


def firmar(secret: str, contenido: str) -> str:
    return hmac.new(secret.encode(), contenido.encode(), hashlib.sha256).hexdigest()


def armar_cookie(secret: str, tenant_id: uuid.UUID, token: str) -> str:
    contenido = f"{tenant_id}.{token}"
    return f"{contenido}.{firmar(secret, contenido)}"


def abrir_cookie(secret: str, valor: str) -> Optional[tuple[uuid.UUID, str]]:
    partes = (valor or "").split(".")
    if len(partes) != 3:
        return None
    tid, token, firma = partes
    # Forma antes de comparar: compare_digest sobre str exige ASCII y Starlette
    # decodifica la cookie como latin-1; un byte raro daría TypeError (500).
    if not _FIRMA.match(firma) or not token_valido(token):
        return None
    if not hmac.compare_digest(firma.encode(), firmar(secret, f"{tid}.{token}").encode()):
        return None
    try:
        return uuid.UUID(tid), token
    except ValueError:
        return None


@dataclass
class Sesion:
    id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    rol: str
    email: Optional[str]                       # None en sesiones de soporte (el usuario vive en el tenant KIS)
    lineas_permitidas: Optional[list[uuid.UUID]]   # None = todas
    es_kis: bool

    def puede_ver_linea(self, line_id: uuid.UUID) -> bool:
        return self.lineas_permitidas is None or line_id in self.lineas_permitidas


async def emitir_sesion(con: asyncpg.Connection, *, tenant_id: uuid.UUID, user_id: uuid.UUID, rol: str,
                        ip: Optional[str], duracion: timedelta = SESION) -> str:
    if rol not in ROLES_SESION:
        raise ValueError(f"rol de sesión desconocido: {rol}")
    token, h = generar_token()
    await con.execute(
        "INSERT INTO sessions (tenant_id, user_id, rol, token_hash, expires_at, ip) "
        "VALUES ($1, $2, $3, $4, now() + $5::interval, $6)",
        tenant_id, user_id, rol, h, duracion, ip,
    )
    return token


async def resolver_sesion(con: asyncpg.Connection, tenant_id: uuid.UUID, token: str) -> Optional[Sesion]:
    fila = await con.fetchrow(
        """
        SELECT s.id, s.user_id, s.rol, u.email, m.lineas_permitidas, t.es_kis
        FROM sessions s
        JOIN tenants t ON t.id = s.tenant_id
        LEFT JOIN users u ON u.id = s.user_id
        LEFT JOIN memberships m ON m.user_id = s.user_id AND m.tenant_id = s.tenant_id
        WHERE s.token_hash = $1 AND s.revoked_at IS NULL AND s.expires_at > now()
        """,
        hash_token(token),
    )
    if fila is None:
        return None
    await con.execute(
        "UPDATE sessions SET last_seen_at = now() WHERE id = $1 "
        "AND (last_seen_at IS NULL OR last_seen_at < now() - interval '5 minutes')",
        fila["id"],
    )
    permitidas = fila["lineas_permitidas"]
    return Sesion(
        id=fila["id"], tenant_id=tenant_id, user_id=fila["user_id"], rol=fila["rol"], email=fila["email"],
        lineas_permitidas=list(permitidas) if permitidas is not None else None, es_kis=fila["es_kis"],
    )


async def revocar_sesion(con: asyncpg.Connection, session_id: uuid.UUID) -> None:
    await con.execute("UPDATE sessions SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL", session_id)


async def revocar_sesiones_de(con: asyncpg.Connection, user_id: uuid.UUID) -> int:
    estado = await con.execute(
        "UPDATE sessions SET revoked_at = now() WHERE user_id = $1 AND revoked_at IS NULL", user_id)
    return int(estado.split()[-1])


def ip_de(request: Request) -> Optional[str]:
    """Radar corre detrás de un único proxy confiable (Railway), que agrega el
    salto real al FINAL de X-Forwarded-For; el primero lo puede inventar el
    cliente. La IP es evidencia (consents, access_audit_log): se toma el último."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[-1].strip()[:64]
    return request.client.host if request.client else None


async def sesion_actual(request: Request) -> Sesion:
    ctx = contexto(request)
    abierta = abrir_cookie(ctx.settings.cookie_secret, request.cookies.get(COOKIE, ""))
    if abierta is None:
        raise HTTPException(status_code=401, detail="sin sesión")
    tenant_id, token = abierta
    async with ctx.db.tenant_tx(tenant_id) as con:
        sesion = await resolver_sesion(con, tenant_id, token)
    if sesion is None:
        raise HTTPException(status_code=401, detail="sesión inválida o vencida")
    return sesion


def requiere_rol(*roles: str):
    async def _dep(sesion: Sesion = Depends(sesion_actual)) -> Sesion:
        if sesion.rol not in roles:
            raise HTTPException(status_code=403, detail="rol insuficiente")
        return sesion
    return _dep


def set_cookie_sesion(response: Response, ctx: RadarContexto, tenant_id: uuid.UUID, token: str,
                      max_age: int = int(SESION.total_seconds())) -> None:
    response.set_cookie(
        COOKIE, armar_cookie(ctx.settings.cookie_secret, tenant_id, token),
        httponly=True, secure=ctx.settings.cookie_secure, samesite="lax", path="/radar", max_age=max_age,
    )


def borrar_cookie_sesion(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/radar")
```

- [ ] **Step 5: Contexto con secretos y mailer**

`app/radar/contexto.py` completo:

```python
"""
Contexto de Radar: lo que los routers necesitan y que en tests se reemplaza
por fakes. Vive en app.state.radar; lo arma el lifespan (producción) o la
fixture (tests).
"""

from dataclasses import dataclass

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

    async def cerrar(self) -> None:
        await self.db.close()
        await self.fuente.close()


def contexto(request: Request) -> RadarContexto:
    ctx = request.app.state.radar
    if ctx is None:
        raise HTTPException(status_code=503, detail="Radar arrancando")
    return ctx
```

En `app/radar/app.py`, `construir_contexto` pasa a:

```python
async def construir_contexto(rs: RadarSettings) -> RadarContexto:
    db = RadarDB(rs.database_url)
    await db.connect()
    fuente = FuenteStore(rs.fuente_database_url)
    await fuente.connect()
    return RadarContexto(settings=rs, db=db, fuente=fuente,
                         secretos=FileSecretStore(rs.secrets_dir), mailer=construir_mailer(rs.mailer))
```

con los imports `from app.radar.mailer import construir_mailer` y `from app.radar.secrets import FileSecretStore`.

En `tests/radar_tests/conftest.py`, la fixture `radar_ctx` construye:

```python
    ctx = RadarContexto(settings=rs, db=radar_db, fuente=fuente,
                        secretos=FileSecretStore(rs.secrets_dir), mailer=MemoryMailer())
```

con `from app.radar.mailer import MemoryMailer` y `from app.radar.secrets import FileSecretStore`.

- [ ] **Step 6: Correr**

```bash
python -m pytest tests/radar_tests -q
```
Esperado: todo verde; `test_auth.py` suma 13.

- [ ] **Step 7: Commit**

```bash
git add app/radar tests/radar_tests
git commit -m "Radar: tokens hasheados, sesiones revocables, cookie firmada y mailer"
```

---

### Task 9: Login por link mágico y `/radar/api/yo`

**Files:**
- Create: `app/radar/links.py`, `app/radar/routers/login.py`, `app/radar/routers/cuenta.py`
- Modify: `app/radar/app.py` (montar routers), `tests/radar_tests/conftest.py` (fixture `cliente`), `tests/radar_tests/helpers.py` (`entrar`)
- Test: `tests/radar_tests/test_login.py`

**Interfaces:**
- Consumes: `auth.*`, `auditoria.registrar`, `eventos_producto.registrar_evento`, `radar_auth_usuarios_por_email`, `RadarContexto.mailer`.
- Produces: `normalizar_email(email: str) -> str` (lanza `EmailInvalido`), `url_canje(base: str, tenant_id, token) -> str` (`…/radar/login/canjear?t=<tenant>#k=<token>`: el token va en el fragmento), `async enviar_link(ctx, *, tenant_id, user_id, email, proposito: Literal["login","invitacion"], ip) -> bool`; endpoints `POST /radar/login` (202 siempre), `GET /radar/login/canjear?t=` (HTML con formulario POST y CSP; un script inline copia `#k` al campo, sin auto-envío), `POST /radar/login/canjear` (form `t`, `k`; 303 a `/radar/api/yo` con cookie; 400 si inválido/vencido/usado; 403 sin membresía), `POST /radar/logout` (200, revoca), `GET /radar/api/yo` (200 `{user_id, tenant_id, rol, email, es_kis, lineas_permitidas}`; 401 sin sesión); fixture `cliente` (httpx `AsyncClient`); helper `entrar(cliente, ctx, tenant_id, user_id, rol)` y constante `DOMINIO_COOKIE = "testserver.local"`.

- [ ] **Step 1: Fixture y helper**

En `tests/radar_tests/conftest.py`:

```python
from httpx import ASGITransport, AsyncClient
from app.radar.app import crear_app_radar


@pytest.fixture
async def cliente(radar_ctx):
    app = crear_app_radar(radar_ctx.settings, contexto=radar_ctx)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c
```

En `tests/radar_tests/helpers.py`:

```python
from app.radar.auth import COOKIE, armar_cookie, emitir_sesion

# Dominio con el que hay que setear una cookie A MANO en el cliente httpx con
# base_url="http://testserver". http.cookiejar trata un host sin punto como
# "testserver.local": una cookie con domain="testserver" NUNCA se manda, y una
# sin domain convive con la que el servidor setea (Set-Cookie queda bajo
# "testserver.local") y cliente.cookies.get() lanza CookieConflict. Con el host
# efectivo, la del servidor reemplaza a la manual. Verificado con httpx 0.27/0.28.
DOMINIO_COOKIE = "testserver.local"


async def entrar(cliente, ctx, tenant_id: uuid.UUID, user_id: uuid.UUID, rol: str) -> None:
    """Deja al cliente httpx con una cookie de sesión válida (sin pasar por el email)."""
    async with ctx.db.tenant_tx(tenant_id) as con:
        token = await emitir_sesion(con, tenant_id=tenant_id, user_id=user_id, rol=rol, ip="127.0.0.1")
    cliente.cookies.clear()
    cliente.cookies.set(COOKIE, armar_cookie(ctx.settings.cookie_secret, tenant_id, token),
                        domain=DOMINIO_COOKIE, path="/radar")
```

- [ ] **Step 2: Tests (fallan)**

`tests/radar_tests/test_login.py`:

```python
"""
Link mágico de un solo uso con vencimiento, token en el fragmento (nunca
llega al servidor ni a los logs), canjeado por POST, cookie HttpOnly atada
al tenant (§3 P0, §7).
"""
import re
import uuid
from datetime import timedelta

from app.radar.auth import COOKIE, generar_token
from app.radar.mailer import huella_token

from .helpers import DOMINIO_COOKIE, crear_tenant_directo, crear_usuario, entrar

LINK = re.compile(r"/radar/login/canjear\?t=([0-9a-f-]+)#k=([A-Za-z0-9_-]+)")


def _link(mail) -> tuple[str, str]:
    m = LINK.search(mail.texto)
    assert m, "el mail no trae el link"
    return m.group(1), m.group(2)


async def test_flujo_completo_de_login(cliente, radar_ctx):
    a = await crear_tenant_directo(radar_ctx.db, "Farmacia A")
    u = await crear_usuario(radar_ctx.db, a, "dueno@cliente.com", "dueno")

    r = await cliente.post("/radar/login", json={"email": " Dueno@Cliente.com "})
    assert r.status_code == 202 and r.json() == {"ok": True}
    assert len(radar_ctx.mailer.enviados) == 1
    mail = radar_ctx.mailer.enviados[0]
    assert mail.para == "dueno@cliente.com" and "Farmacia A" in mail.asunto
    t, k = _link(mail)
    assert t == str(a) and mail.huella == huella_token(k)

    # la página de canje no recibe el token (va en #k): ni en el HTML ni en el log
    r = await cliente.get(f"/radar/login/canjear?t={t}")
    assert r.status_code == 200 and 'name="k"' in r.text and "<form" in r.text
    assert k not in r.text
    assert ".submit()" not in r.text              # sin auto-envío: un escáner que ejecuta JS no consume el token
    assert r.headers["content-security-policy"].startswith("default-src 'none'; script-src 'sha256-")

    r = await cliente.post("/radar/login/canjear", data={"t": t, "k": k})
    assert r.status_code == 303 and r.headers["location"] == "/radar/api/yo"
    set_cookie = r.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=lax" in set_cookie and "path=/radar" in set_cookie
    assert COOKIE in set_cookie

    r = await cliente.get("/radar/api/yo")
    assert r.status_code == 200
    assert r.json() == {"user_id": str(u), "tenant_id": str(a), "rol": "dueno",
                        "email": "dueno@cliente.com", "es_kis": False, "lineas_permitidas": None}

    async with radar_ctx.db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT count(*) FROM access_audit_log WHERE accion = 'login_canjeado' "
                                  "AND actor_user_id = $1", u) == 1
        assert await con.fetchval("SELECT count(*) FROM product_events WHERE evento = 'login_canjeado'") == 1

    # un solo uso
    r = await cliente.post("/radar/login/canjear", data={"t": t, "k": k})
    assert r.status_code == 400


async def test_email_desconocido_o_invalido_responde_igual(cliente, radar_ctx):
    for email in ("nadie@cliente.com", "no-es-un-email", ""):
        r = await cliente.post("/radar/login", json={"email": email})
        assert r.status_code == 202 and r.json() == {"ok": True}
    assert radar_ctx.mailer.enviados == []


async def test_limite_de_tres_pedidos_por_usuario(cliente, radar_ctx):
    a = await crear_tenant_directo(radar_ctx.db, "A")
    await crear_usuario(radar_ctx.db, a, "dueno@cliente.com", "dueno")
    for _ in range(5):
        r = await cliente.post("/radar/login", json={"email": "dueno@cliente.com"})
        assert r.status_code == 202
    assert len(radar_ctx.mailer.enviados) == 3


async def test_mismo_email_en_dos_tenants_recibe_un_link_por_tenant(cliente, radar_ctx):
    a = await crear_tenant_directo(radar_ctx.db, "A")
    b = await crear_tenant_directo(radar_ctx.db, "B")
    await crear_usuario(radar_ctx.db, a, "dueno@cliente.com", "dueno")
    await crear_usuario(radar_ctx.db, b, "dueno@cliente.com", "lector")
    await cliente.post("/radar/login", json={"email": "dueno@cliente.com"})
    tenants = {_link(m)[0] for m in radar_ctx.mailer.enviados}
    assert tenants == {str(a), str(b)}


async def test_link_vencido_no_canjea(cliente, radar_ctx):
    a = await crear_tenant_directo(radar_ctx.db, "A")
    u = await crear_usuario(radar_ctx.db, a, "dueno@cliente.com", "dueno")
    claro, h = generar_token()
    async with radar_ctx.db.tenant_tx(a) as con:
        await con.execute("INSERT INTO login_tokens (user_id, token_hash, proposito, expires_at) "
                          "VALUES ($1, $2, 'login', now() - interval '1 second')", u, h)
    r = await cliente.post("/radar/login/canjear", data={"t": str(a), "k": claro})
    assert r.status_code == 400


async def test_link_no_canjea_en_otro_tenant(cliente, radar_ctx):
    a = await crear_tenant_directo(radar_ctx.db, "A")
    b = await crear_tenant_directo(radar_ctx.db, "B")
    await crear_usuario(radar_ctx.db, a, "dueno@cliente.com", "dueno")
    await cliente.post("/radar/login", json={"email": "dueno@cliente.com"})
    _, k = _link(radar_ctx.mailer.enviados[0])
    r = await cliente.post("/radar/login/canjear", data={"t": str(b), "k": k})
    assert r.status_code == 400
    r = await cliente.post("/radar/login/canjear", data={"t": str(a), "k": k})
    assert r.status_code == 303      # el token sigue vivo: el intento ajeno no lo consumió


async def test_link_con_token_malformado(cliente, radar_ctx):
    a = await crear_tenant_directo(radar_ctx.db, "A")
    assert (await cliente.post("/radar/login/canjear", data={"t": str(a), "k": "x"})).status_code == 400
    assert (await cliente.post("/radar/login/canjear", data={"t": str(a), "k": "<script>" * 5})).status_code == 400
    assert (await cliente.get("/radar/login/canjear?t=no-uuid")).status_code == 422


async def test_usuario_sin_membresia_no_entra(cliente, radar_ctx):
    a = await crear_tenant_directo(radar_ctx.db, "A")
    async with radar_ctx.db.tenant_tx(a) as con:
        await con.execute("INSERT INTO users (email) VALUES ('sin@cliente.com')")
    await cliente.post("/radar/login", json={"email": "sin@cliente.com"})
    t, k = _link(radar_ctx.mailer.enviados[0])
    r = await cliente.post("/radar/login/canjear", data={"t": t, "k": k})
    assert r.status_code == 403


async def test_yo_sin_cookie_o_con_cookie_adulterada(cliente, radar_ctx):
    assert (await cliente.get("/radar/api/yo")).status_code == 401
    a = await crear_tenant_directo(radar_ctx.db, "A")
    u = await crear_usuario(radar_ctx.db, a, "dueno@cliente.com", "dueno")
    await entrar(cliente, radar_ctx, a, u, "dueno")
    assert (await cliente.get("/radar/api/yo")).status_code == 200   # guarda: la cookie manual SÍ viaja
    valor = cliente.cookies.get(COOKIE)
    cliente.cookies.set(COOKIE, valor[:-1] + ("0" if valor[-1] != "0" else "1"), domain=DOMINIO_COOKIE, path="/radar")
    assert (await cliente.get("/radar/api/yo")).status_code == 401


async def test_logout_revoca_la_sesion(cliente, radar_ctx):
    a = await crear_tenant_directo(radar_ctx.db, "A")
    u = await crear_usuario(radar_ctx.db, a, "dueno@cliente.com", "dueno")
    await entrar(cliente, radar_ctx, a, u, "dueno")
    r = await cliente.post("/radar/logout")
    assert r.status_code == 200
    assert (await cliente.get("/radar/api/yo")).status_code == 401
    async with radar_ctx.db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT count(*) FROM sessions WHERE revoked_at IS NOT NULL") == 1
        assert await con.fetchval("SELECT count(*) FROM access_audit_log WHERE accion = 'sesion_cerrada'") == 1
```

- [ ] **Step 3: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_login.py -q
```
Esperado: `10 failed` (todas las rutas responden 404: los routers no existen).

- [ ] **Step 4: `links.py`**

```python
"""
Links mágicos (§3 P0): un solo uso, con vencimiento, canjeados por POST.
Un único módulo para login e invitación; cambian el vencimiento y el texto.
Límite: MAX_PEDIDOS_LOGIN por usuario cada VENTANA_PEDIDOS, contado sobre
login_tokens. El email nunca lleva datos de conversación.
"""

import logging
import re
import uuid
from typing import Literal, Optional

from app.radar.auth import INVITACION, LINK_MAGICO, MAX_PEDIDOS_LOGIN, VENTANA_PEDIDOS, generar_token
from app.radar.contexto import RadarContexto
from app.radar.mailer import Email, huella_token

logger = logging.getLogger("app.radar.links")

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

TEXTOS = {
    "login": (
        "Tu acceso a Radar de {tenant}",
        "Entrá a Radar de {tenant} con este link. Vence en 15 minutos y sirve una sola vez.\n\n"
        "{url}\n\nSi no lo pediste, ignorá este mail.\n",
    ),
    "invitacion": (
        "Invitación a Radar de {tenant}",
        "Te invitaron a Radar, el tablero de conversaciones de WhatsApp de {tenant}. "
        "Entrá con este link; vence en 7 días y sirve una sola vez.\n\n{url}\n",
    ),
}


class EmailInvalido(ValueError):
    pass


def normalizar_email(email: str) -> str:
    e = (email or "").strip().lower()
    if len(e) > 254 or not _EMAIL.match(e):
        raise EmailInvalido("email inválido")
    return e


def url_canje(base: str, tenant_id: uuid.UUID, token: str) -> str:
    """El token va en el FRAGMENTO: el navegador nunca lo manda al servidor, así
    no queda en el access log de uvicorn (path?query) ni en los logs HTTP de
    Railway. La página de canje lo copia al formulario POST."""
    return f"{base.rstrip('/')}/radar/login/canjear?t={tenant_id}#k={token}"


async def enviar_link(ctx: RadarContexto, *, tenant_id: uuid.UUID, user_id: uuid.UUID, email: str,
                      proposito: Literal["login", "invitacion"], ip: Optional[str]) -> bool:
    """Crea un login_token y manda el mail. False si el usuario superó el límite."""
    duracion = INVITACION if proposito == "invitacion" else LINK_MAGICO
    async with ctx.db.tenant_tx(tenant_id) as con:
        recientes = await con.fetchval(
            "SELECT count(*) FROM login_tokens WHERE user_id = $1 AND created_at > now() - $2::interval",
            user_id, VENTANA_PEDIDOS)
        if recientes >= MAX_PEDIDOS_LOGIN:
            logger.info("link no enviado: límite por usuario alcanzado (user %s)", user_id)
            return False
        token, h = generar_token()
        await con.execute(
            "INSERT INTO login_tokens (tenant_id, user_id, token_hash, proposito, expires_at, ip_solicitud) "
            "VALUES ($1, $2, $3, $4, now() + $5::interval, $6)",
            tenant_id, user_id, h, proposito, duracion, ip)
        nombre_tenant = await con.fetchval("SELECT nombre FROM tenants WHERE id = $1", tenant_id)
    asunto, cuerpo = TEXTOS[proposito]
    url = url_canje(ctx.settings.public_base_url, tenant_id, token)
    await ctx.mailer.enviar(Email(
        para=email, asunto=asunto.format(tenant=nombre_tenant),
        texto=cuerpo.format(tenant=nombre_tenant, url=url), huella=huella_token(token)))
    return True
```

- [ ] **Step 5: Router de login**

`app/radar/routers/login.py`:

```python
"""
POST /radar/login          pide un link mágico (202 siempre: no revela si el email existe)
GET  /radar/login/canjear  página mínima: formulario POST + script que copia #k al campo, SIN auto-envío
                           (un escáner de links, ejecute JS o no, no consume el token), con CSP
POST /radar/login/canjear  canjea el token (UPDATE ... WHERE used_at IS NULL: un solo uso), emite la sesión
POST /radar/logout         revoca la sesión
"""

import base64
import hashlib
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.radar import auditoria, eventos_producto
from app.radar.auth import (Sesion, borrar_cookie_sesion, emitir_sesion, hash_token, ip_de,
                            revocar_sesion, sesion_actual, set_cookie_sesion, token_valido)
from app.radar.contexto import contexto
from app.radar.links import EmailInvalido, enviar_link, normalizar_email

router = APIRouter(prefix="/radar", tags=["radar-login"])


class PedidoLogin(BaseModel):
    email: str = Field(max_length=254)


@router.post("/login", status_code=202)
async def pedir_link(pedido: PedidoLogin, request: Request):
    ctx = contexto(request)
    try:
        email = normalizar_email(pedido.email)
    except EmailInvalido:
        return {"ok": True}
    async with ctx.db.sin_tenant() as con:
        filas = await con.fetch("SELECT user_id, tenant_id FROM radar_auth_usuarios_por_email($1)", email)
    ip = ip_de(request)
    for f in filas:
        await enviar_link(ctx, tenant_id=f["tenant_id"], user_id=f["user_id"], email=email,
                          proposito="login", ip=ip)
    return {"ok": True}


# Único script de la página: copia el token del fragmento (#k=...) al campo
# oculto. No envía el formulario: eso lo hace la persona con el botón.
_SCRIPT_CANJE = (
    "var m = /^#k=([A-Za-z0-9_-]{32,64})$/.exec(location.hash);"
    "if (m) { document.forms[0].k.value = m[1]; }"
)
# CSP (§7, "aplicar CSP"): nada se carga ni se ejecuta salvo ese script, por hash.
_CSP = (
    "default-src 'none'; script-src 'sha256-"
    + base64.b64encode(hashlib.sha256(_SCRIPT_CANJE.encode()).digest()).decode()
    + "'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)

_PAGINA_CANJE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>Radar</title></head>
<body>
<form method="post" action="/radar/login/canjear">
  <input type="hidden" name="t" value="{t}">
  <input type="hidden" name="k" value="">
  <button type="submit">Entrar a Radar</button>
</form>
<noscript>Este link necesita JavaScript para completar el ingreso.</noscript>
<script>{script}</script>
</body></html>"""


@router.get("/login/canjear", response_class=HTMLResponse)
async def pagina_canje(t: uuid.UUID):
    """Solo recibe el tenant: el token viaja en el fragmento y nunca llega acá."""
    return HTMLResponse(_PAGINA_CANJE.format(t=t, script=_SCRIPT_CANJE),
                        headers={"Content-Security-Policy": _CSP})


@router.post("/login/canjear")
async def canjear(request: Request, t: uuid.UUID = Form(...), k: str = Form(...)):
    ctx = contexto(request)
    if not token_valido(k):
        raise HTTPException(status_code=400, detail="link inválido")
    ip = ip_de(request)
    async with ctx.db.tenant_tx(t) as con:
        fila = await con.fetchrow(
            "UPDATE login_tokens SET used_at = now() "
            "WHERE token_hash = $1 AND used_at IS NULL AND expires_at > now() "
            "RETURNING id, user_id, proposito",
            hash_token(k))
        if fila is None:
            raise HTTPException(status_code=400, detail="link inválido o vencido")
        rol = await con.fetchval("SELECT rol FROM memberships WHERE user_id = $1", fila["user_id"])
        if rol is None:
            raise HTTPException(status_code=403, detail="sin membresía")
        token = await emitir_sesion(con, tenant_id=t, user_id=fila["user_id"], rol=rol, ip=ip)
        await auditoria.registrar(con, tenant_id=t, actor_user_id=fila["user_id"], actor_rol=rol,
                                  accion="login_canjeado", tipo_objeto="login_token", objeto_id=fila["id"],
                                  ip=ip, detalle={"proposito": fila["proposito"]})
        await eventos_producto.registrar_evento(con, tenant_id=t, evento="login_canjeado", user_id=fila["user_id"])
    resp = RedirectResponse("/radar/api/yo", status_code=303)
    set_cookie_sesion(resp, ctx, t, token)
    return resp


@router.post("/logout")
async def salir(request: Request, sesion: Sesion = Depends(sesion_actual)):
    ctx = contexto(request)
    async with ctx.db.tenant_tx(sesion.tenant_id) as con:
        await revocar_sesion(con, sesion.id)
        await auditoria.registrar(con, tenant_id=sesion.tenant_id, actor_user_id=sesion.user_id,
                                  actor_rol=sesion.rol, accion="sesion_cerrada", tipo_objeto="session",
                                  objeto_id=sesion.id, ip=ip_de(request))
    resp = JSONResponse({"ok": True})
    borrar_cookie_sesion(resp)
    return resp
```

- [ ] **Step 6: Router de cuenta (solo `/yo` por ahora) y montaje**

`app/radar/routers/cuenta.py`:

```python
"""API autenticada de la cuenta: quién soy, líneas, usuarios y roles."""

from fastapi import APIRouter, Depends

from app.radar.auth import Sesion, sesion_actual

router = APIRouter(prefix="/radar/api", tags=["radar-cuenta"])


def _sesion_json(sesion: Sesion) -> dict:
    return {
        "user_id": str(sesion.user_id),
        "tenant_id": str(sesion.tenant_id),
        "rol": sesion.rol,
        "email": sesion.email,
        "es_kis": sesion.es_kis,
        "lineas_permitidas": [str(x) for x in sesion.lineas_permitidas]
        if sesion.lineas_permitidas is not None else None,
    }


@router.get("/yo")
async def yo(sesion: Sesion = Depends(sesion_actual)):
    return _sesion_json(sesion)
```

En `app/radar/app.py`: `from app.radar.routers import cuenta, health, login` y en `crear_app_radar`:

```python
    app.include_router(health.router)
    app.include_router(login.router)
    app.include_router(cuenta.router)
```

- [ ] **Step 7: Correr**

```bash
python -m pytest tests/radar_tests -q
```
Esperado: todo verde; `test_login.py` suma 10.

- [ ] **Step 8: Commit**

```bash
git add app/radar tests/radar_tests
git commit -m "Radar: login por link magico de un solo uso y sesion en cookie HttpOnly"
```

---

### Task 10: Admin de KIS: primer admin, alta de cliente con línea e invitación al dueño

**Files:**
- Create: `app/radar/lineas.py`, `app/radar/admin_kis.py`, `app/radar/routers/admin.py`, `scripts/radar_admin.py`
- Modify: `app/radar/app.py` (montar `admin.router`)
- Test: `tests/radar_tests/test_admin.py`

**Interfaces:**
- Consumes: `radar_admin_crear_tenant`, `radar_admin_listar_tenants`, `crear_k_tenant`/`destruir_k_tenant`, `enviar_link`, `auditoria`, `eventos_producto`, `parametros.*`, `ALMACENES_DISPONIBLES`.
- Produces (`lineas.py`): `AlmacenNoDisponible(ValueError)`, `almacen_para(retencion_fuente_dias: int) -> str`, `verificar_almacen(valores: dict) -> str`, `resolver_valores_linea(parciales: dict) -> dict` (iniciales + explícitos, validados), `async crear_linea(con, *, tenant_id, nombre, valores) -> uuid.UUID`, `async leer_linea(con, line_id) -> dict | None` (incluye `parametros_propuestos`), `async listar_lineas(con, permitidas: list[uuid.UUID] | None) -> list[dict]`, `propuesta_de_linea(fila: dict) -> dict` (JSON de `parametros_propuestos`, `{}` si es NULL), `linea_json(fila: dict) -> dict`.
- Produces (`admin_kis.py`): `resolver_valores_tenant(perfil: str, parciales: dict) -> dict` (iniciales → propuesta del perfil → explícitos), `async crear_admin_kis(ctx, *, email, nombre, ip=None) -> uuid.UUID`, `AltaTenant(tenant_id, line_id, user_id, invitacion_enviada)`, `async crear_tenant_con_dueno(ctx, *, actor: Sesion, ip, nombre, rubro, perfil, parametros_tenant, dueno_email, dueno_nombre, linea_nombre, parametros_linea) -> AltaTenant`.
- Endpoints (`/radar/admin`, rol `admin`): `GET /propuesta?rubro=` → `{perfil_de_datos, parametros_tenant, parametros_linea, almacenes_disponibles}`; `POST /tenants` (201 `{tenant_id, line_id, user_id, invitacion_enviada}`; 422 si un parámetro es inválido o pide un almacén inexistente; 409 email del dueño repetido); `GET /tenants` (lista, auditado en el tenant KIS); `POST /tenants/{tenant_id}/lineas` (201 `{line_id}`); `POST /tenants/{tenant_id}/invitaciones` (202 `{enviada: bool}`).
- Script: `python scripts/radar_admin.py crear-admin --email … --nombre …` imprime el link de invitación una sola vez.

- [ ] **Step 1: Tests (fallan)**

`tests/radar_tests/test_admin.py`:

```python
"""
P0 (§3): un admin de KIS crea la cuenta, registra la línea con sus
parámetros e invita al dueño. Todo lo cruzado entre tenants pasa por las
funciones SECURITY DEFINER y queda auditado.
"""
import re
import uuid
from pathlib import Path

import pytest

from app.radar.admin_kis import crear_admin_kis
from app.radar.constantes import TENANT_KIS
from app.radar.secrets import obtener_k_tenant

from .helpers import crear_tenant_directo, crear_usuario, entrar

LINK = re.compile(r"/radar/login/canjear\?t=([0-9a-f-]+)#k=([A-Za-z0-9_-]+)")

ALTA = {
    "nombre": "Farmacia del Centro", "rubro": "farmacia",
    "dueno": {"email": "Dueno@Farmacia.com", "nombre": "Ana"},
    "linea": {"nombre": "Local centro", "parametros": {"duracion_vinculo_dias": 30}},
}


async def _admin(cliente, ctx) -> uuid.UUID:
    uid = await crear_usuario(ctx.db, TENANT_KIS, "admin@keepitsimple.com.ar", "admin")
    await entrar(cliente, ctx, TENANT_KIS, uid, "admin")
    return uid


async def test_crear_admin_kis_es_idempotente_y_manda_invitacion(radar_ctx):
    u1 = await crear_admin_kis(radar_ctx, email="Admin@KeepItSimple.com.ar", nombre="Mariano")
    u2 = await crear_admin_kis(radar_ctx, email="admin@keepitsimple.com.ar", nombre="Mariano O.")
    assert u1 == u2
    async with radar_ctx.db.tenant_tx(TENANT_KIS) as con:
        assert await con.fetchval("SELECT rol FROM memberships WHERE user_id = $1", u1) == "admin"
        assert await con.fetchval("SELECT nombre FROM users WHERE id = $1", u1) == "Mariano O."
    assert len(radar_ctx.mailer.enviados) == 2
    assert "Keep IT Simple" in radar_ctx.mailer.enviados[0].asunto


async def test_propuesta_por_rubro(cliente, radar_ctx):
    await _admin(cliente, radar_ctx)
    r = await cliente.get("/radar/admin/propuesta", params={"rubro": "farmacia"})
    assert r.status_code == 200
    assert r.json() == {
        "perfil_de_datos": "sensible",
        "parametros_tenant": {"ia_habilitada": False, "via_llm": "sincronica", "retener_fragmentos": False},
        "parametros_linea": {"retencion_fuente_dias": 7},
        "almacenes_disponibles": ["permanente"],
    }
    r = await cliente.get("/radar/admin/propuesta", params={"rubro": "pet_shop"})
    assert r.json()["perfil_de_datos"] == "estandar" and r.json()["parametros_linea"] == {}


async def test_alta_de_cliente_e_invitacion_del_dueno(cliente, radar_ctx):
    admin = await _admin(cliente, radar_ctx)
    r = await cliente.post("/radar/admin/tenants", json=ALTA)
    assert r.status_code == 201, r.text
    cuerpo = r.json()
    tid, lid, uid = uuid.UUID(cuerpo["tenant_id"]), uuid.UUID(cuerpo["line_id"]), uuid.UUID(cuerpo["user_id"])
    assert cuerpo["invitacion_enviada"] is True

    assert len(obtener_k_tenant(radar_ctx.secretos, tid)) == 32
    async with radar_ctx.db.tenant_tx(tid) as con:
        t = await con.fetchrow("SELECT nombre, rubro, perfil_de_datos, ia_habilitada, via_llm FROM tenants WHERE id = $1", tid)
        # §7: un tenant `sensible` arranca con IA apagada y vía sincrónica; el
        # admin puede pisarlo explícitamente ("propone, no fuerza"), pero no por omisión.
        assert dict(t) == {"nombre": "Farmacia del Centro", "rubro": "farmacia", "perfil_de_datos": "sensible",
                           "ia_habilitada": False, "via_llm": "sincronica"}
        linea = await con.fetchrow("SELECT nombre, estado, almacen_fuente, duracion_vinculo_dias FROM lines WHERE id = $1", lid)
        assert dict(linea) == {"nombre": "Local centro", "estado": "sin_vinculo",
                               "almacen_fuente": "permanente", "duracion_vinculo_dias": 30}
        assert await con.fetchval("SELECT email FROM users WHERE id = $1", uid) == "dueno@farmacia.com"
        assert await con.fetchval("SELECT rol FROM memberships WHERE user_id = $1", uid) == "dueno"
        acciones = {f["accion"]: f for f in await con.fetch("SELECT accion, actor_user_id, objeto_id FROM access_audit_log")}
        assert set(acciones) == {"tenant_creado", "linea_creada", "usuario_invitado"}
        assert acciones["tenant_creado"]["actor_user_id"] == admin and acciones["tenant_creado"]["objeto_id"] == tid
        eventos = {f["evento"] for f in await con.fetch("SELECT evento FROM product_events")}
        assert eventos == {"linea_creada", "invitacion_enviada"}

    mail = radar_ctx.mailer.enviados[-1]
    assert mail.para == "dueno@farmacia.com" and "Farmacia del Centro" in mail.asunto
    t, k = LINK.search(mail.texto).groups()
    cliente.cookies.clear()
    r = await cliente.post("/radar/login/canjear", data={"t": t, "k": k})
    assert r.status_code == 303
    r = await cliente.get("/radar/api/yo")
    assert r.status_code == 200 and r.json()["rol"] == "dueno" and r.json()["tenant_id"] == str(tid)


async def test_alta_con_parametros_explicitos_del_perfil_sensible(cliente, radar_ctx):
    """El perfil propone, el admin decide: un valor explícito pisa la propuesta
    (ia_habilitada=True sobre farmacia queda a cargo de la cláusula contractual
    y el dictamen de §7, fuera del código). Lo no enviado conserva la propuesta."""
    await _admin(cliente, radar_ctx)
    alta = dict(ALTA, parametros={"ia_habilitada": True, "retencion_fichas_meses": 6})
    r = await cliente.post("/radar/admin/tenants", json=alta)
    assert r.status_code == 201
    async with radar_ctx.db.tenant_tx(uuid.UUID(r.json()["tenant_id"])) as con:
        t = await con.fetchrow("SELECT ia_habilitada, via_llm, retencion_fichas_meses, retener_fragmentos FROM tenants")
    assert dict(t) == {"ia_habilitada": True, "via_llm": "sincronica", "retencion_fichas_meses": 6,
                       "retener_fragmentos": False}
    # perfil estandar explícito sobre un rubro sensible: no hereda la propuesta
    alta = dict(ALTA, perfil_de_datos="estandar", dueno={"email": "otro@farmacia.com", "nombre": "Bo"})
    r = await cliente.post("/radar/admin/tenants", json=alta)
    assert r.status_code == 201
    async with radar_ctx.db.tenant_tx(uuid.UUID(r.json()["tenant_id"])) as con:
        t = await con.fetchrow("SELECT perfil_de_datos, ia_habilitada, via_llm FROM tenants")
    assert dict(t) == {"perfil_de_datos": "estandar", "ia_habilitada": True, "via_llm": "lotes"}


async def test_alta_rechaza_almacen_purgable_y_no_deja_secreto_colgado(cliente, radar_ctx):
    await _admin(cliente, radar_ctx)
    alta = dict(ALTA, linea={"nombre": "L", "parametros": {"retencion_fuente_dias": 7}})
    r = await cliente.post("/radar/admin/tenants", json=alta)
    assert r.status_code == 422 and "purgable" in r.text
    async with radar_ctx.db.sin_tenant() as con:
        assert await con.fetch("SELECT * FROM radar_admin_listar_tenants()") == []
    assert list(Path(radar_ctx.settings.secrets_dir).glob("*")) == []      # la k_tenant se destruyó


@pytest.mark.parametrize("malo", [
    dict(ALTA, rubro="Farmacia Con Espacios"),
    dict(ALTA, linea={"nombre": "", "parametros": {}}),
    dict(ALTA, linea={"nombre": "L", "parametros": {"duracion_vinculo_dias": -1}}),
    dict(ALTA, parametros={"via_llm": "streaming"}),
    dict(ALTA, perfil_de_datos="secreto"),
    dict(ALTA, dueno={"email": "sin-arroba", "nombre": ""}),
])
async def test_alta_invalida(cliente, radar_ctx, malo):
    await _admin(cliente, radar_ctx)
    assert (await cliente.post("/radar/admin/tenants", json=malo)).status_code == 422


async def test_alta_requiere_rol_admin(cliente, radar_ctx):
    assert (await cliente.post("/radar/admin/tenants", json=ALTA)).status_code == 401
    a = await crear_tenant_directo(radar_ctx.db, "A")
    u = await crear_usuario(radar_ctx.db, a, "dueno@cliente.com", "dueno")
    await entrar(cliente, radar_ctx, a, u, "dueno")
    assert (await cliente.post("/radar/admin/tenants", json=ALTA)).status_code == 403
    assert (await cliente.get("/radar/admin/tenants")).status_code == 403


async def test_listar_tenants_auditado(cliente, radar_ctx):
    admin = await _admin(cliente, radar_ctx)
    await crear_tenant_directo(radar_ctx.db, "A")
    await crear_tenant_directo(radar_ctx.db, "B")
    r = await cliente.get("/radar/admin/tenants")
    assert r.status_code == 200
    assert [t["nombre"] for t in r.json()] == ["A", "B"]
    async with radar_ctx.db.tenant_tx(TENANT_KIS) as con:
        fila = await con.fetchrow("SELECT actor_user_id, detalle FROM access_audit_log WHERE accion = 'tenants_listados'")
    assert fila["actor_user_id"] == admin and '"cantidad": 2' in fila["detalle"]


async def test_segunda_linea_para_cliente_existente(cliente, radar_ctx):
    await _admin(cliente, radar_ctx)
    a = await crear_tenant_directo(radar_ctx.db, "A")
    r = await cliente.post(f"/radar/admin/tenants/{a}/lineas",
                           json={"nombre": "Sucursal norte", "parametros": {"tope_ia_mensual_usd": "12.50"}})
    assert r.status_code == 201
    async with radar_ctx.db.tenant_tx(a) as con:
        fila = await con.fetchrow("SELECT nombre, tope_ia_mensual_usd FROM lines WHERE id = $1", uuid.UUID(r.json()["line_id"]))
        assert fila["nombre"] == "Sucursal norte" and str(fila["tope_ia_mensual_usd"]) == "12.50"
        assert await con.fetchval("SELECT count(*) FROM access_audit_log WHERE accion = 'linea_creada'") == 1
    assert (await cliente.post(f"/radar/admin/tenants/{uuid.uuid4()}/lineas",
                               json={"nombre": "x", "parametros": {}})).status_code == 404


async def test_reenviar_invitacion(cliente, radar_ctx):
    await _admin(cliente, radar_ctx)
    a = await crear_tenant_directo(radar_ctx.db, "A")
    await crear_usuario(radar_ctx.db, a, "dueno@cliente.com", "dueno")
    r = await cliente.post(f"/radar/admin/tenants/{a}/invitaciones", json={"email": "dueno@cliente.com"})
    assert r.status_code == 202 and r.json() == {"enviada": True}
    assert radar_ctx.mailer.enviados[-1].para == "dueno@cliente.com"
    r = await cliente.post(f"/radar/admin/tenants/{a}/invitaciones", json={"email": "nadie@cliente.com"})
    assert r.status_code == 404


async def test_script_crear_admin_imprime_el_link(radar_urls, tmp_path, monkeypatch, capsys):
    import scripts.radar_admin as script
    from app.radar.settings import get_radar_settings
    monkeypatch.setenv("RADAR_DATABASE_URL", radar_urls["app"])
    monkeypatch.setenv("RADAR_MIGRATOR_DATABASE_URL", radar_urls["migrator"])
    monkeypatch.setenv("RADAR_FUENTE_DATABASE_URL", radar_urls["fuente"])
    monkeypatch.setenv("RADAR_COOKIE_SECRET", "secreto-de-test-de-32-caracteres!")
    monkeypatch.setenv("RADAR_SECRETS_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("RADAR_PUBLIC_BASE_URL", "https://radar.test")
    get_radar_settings.cache_clear()
    try:
        script.main(["crear-admin", "--email", "primer-admin@keepitsimple.com.ar", "--nombre", "Mariano"])
    finally:
        get_radar_settings.cache_clear()
    salida = capsys.readouterr().out
    assert "https://radar.test/radar/login/canjear?t=00000000-0000-0000-0000-000000000001#k=" in salida
```

- [ ] **Step 2: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_admin.py -q
```
Esperado: `ModuleNotFoundError: No module named 'app.radar.admin_kis'`.

- [ ] **Step 3: `lineas.py`**

```python
"""
Líneas (§2.1, §6.4): alta con sus parámetros de ámbito Línea, lectura y
listado. El almacén de fuente se deriva de retencion_fuente_dias
(0 = permanente); el purgable llega en el tramo 6, así que pedirlo es 422.
"""

import json
import uuid
from typing import Optional

import asyncpg

from app.radar.fuente import ALMACENES_DISPONIBLES
from app.radar.parametros import DE_LINEA, iniciales, validar

COLUMNAS = ("id", "nombre", "estado", "almacen_fuente", *DE_LINEA, "parametros_propuestos",
            "fuente_purgada_hasta", "created_at")
_SELECT = "SELECT " + ", ".join(COLUMNAS) + " FROM lines"


class AlmacenNoDisponible(ValueError):
    pass


def almacen_para(retencion_fuente_dias: int) -> str:
    return "purgable" if retencion_fuente_dias > 0 else "permanente"


def verificar_almacen(valores: dict) -> str:
    almacen = almacen_para(valores["retencion_fuente_dias"])
    if almacen not in ALMACENES_DISPONIBLES:
        raise AlmacenNoDisponible(
            "retencion_fuente_dias > 0 requiere el almacén purgable, que no existe en este despliegue (tramo 6)")
    return almacen


def resolver_valores_linea(parciales: dict) -> dict:
    """Iniciales de §2.1 pisados por los valores explícitos (None = no enviado)."""
    valores = iniciales("linea")
    for nombre, valor in parciales.items():
        if nombre not in DE_LINEA:
            raise ValueError(f"no es un parámetro de línea: {nombre}")
        if valor is not None:
            valores[nombre] = validar(nombre, valor)
    return valores


async def crear_linea(con: asyncpg.Connection, *, tenant_id: uuid.UUID, nombre: str, valores: dict) -> uuid.UUID:
    almacen = verificar_almacen(valores)
    return await con.fetchval(
        "INSERT INTO lines (tenant_id, nombre, almacen_fuente, duracion_vinculo_dias, retencion_fuente_dias, "
        "retencion_tras_desvinculo_dias, tope_ia_mensual_usd) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
        tenant_id, nombre, almacen, valores["duracion_vinculo_dias"], valores["retencion_fuente_dias"],
        valores["retencion_tras_desvinculo_dias"], valores["tope_ia_mensual_usd"],
    )


async def leer_linea(con: asyncpg.Connection, line_id: uuid.UUID) -> Optional[dict]:
    fila = await con.fetchrow(_SELECT + " WHERE id = $1", line_id)
    return dict(fila) if fila else None


async def listar_lineas(con: asyncpg.Connection, permitidas: Optional[list[uuid.UUID]]) -> list[dict]:
    filas = await con.fetch(
        _SELECT + " WHERE estado <> 'de_baja' AND ($1::uuid[] IS NULL OR id = ANY($1::uuid[])) ORDER BY created_at",
        permitidas)
    return [dict(f) for f in filas]


def propuesta_de_linea(fila: dict) -> dict:
    """Propuesta pendiente de un admin de KIS (lines.parametros_propuestos, JSONB
    que asyncpg entrega como str). {} si no hay."""
    crudo = fila.get("parametros_propuestos")
    return json.loads(crudo) if crudo else {}


def linea_json(fila: dict) -> dict:
    return {
        "id": str(fila["id"]),
        "nombre": fila["nombre"],
        "estado": fila["estado"],
        "almacen_fuente": fila["almacen_fuente"],
        "parametros": {
            "duracion_vinculo_dias": fila["duracion_vinculo_dias"],
            "retencion_fuente_dias": fila["retencion_fuente_dias"],
            "retencion_tras_desvinculo_dias": fila["retencion_tras_desvinculo_dias"],
            "tope_ia_mensual_usd": str(fila["tope_ia_mensual_usd"]) if fila["tope_ia_mensual_usd"] is not None else None,
        },
        "parametros_propuestos": propuesta_de_linea(fila),
        "fuente_purgada_hasta": fila["fuente_purgada_hasta"].isoformat() if fila["fuente_purgada_hasta"] else None,
        "created_at": fila["created_at"].isoformat(),
    }
```

- [ ] **Step 4: `admin_kis.py`**

```python
"""
Operaciones de los admins de KIS (§3 P0): alta del primer admin y alta de un
cliente con su línea, su dueño y la invitación. Crear el tenant pasa por
radar_admin_crear_tenant (SECURITY DEFINER) dentro del mismo tenant_tx que el
resto del alta, así todo es atómico. La k_tenant se crea antes y se destruye
si el alta falla.
"""

import uuid
from dataclasses import dataclass
from typing import Optional

from app.radar import auditoria, eventos_producto
from app.radar.auth import Sesion
from app.radar.constantes import TENANT_KIS
from app.radar.contexto import RadarContexto
from app.radar.lineas import crear_linea, resolver_valores_linea
from app.radar.links import enviar_link, normalizar_email
from app.radar.parametros import DE_TENANT, iniciales, propuesta_para_perfil, validar
from app.radar.secrets import crear_k_tenant, destruir_k_tenant


def resolver_valores_tenant(perfil: str, parciales: dict) -> dict:
    """Iniciales de §2.1, pisados por la propuesta del perfil (§7: `sensible`
    arranca con IA apagada y vía sincrónica) y estos por los valores explícitos
    del admin (None = no enviado). "Propone, no fuerza" (§2.1): el admin puede
    pisar la propuesta, pero no queda IA encendida por omisión sobre datos de
    salud. La parte de LÍNEA de la propuesta (`retencion_fuente_dias = 7`) no
    se aplica: exige el almacén purgable, que no existe hasta el tramo 6
    (Decisión 5); GET /radar/admin/propuesta la muestra igual."""
    valores = iniciales("tenant")
    valores["perfil_de_datos"] = validar("perfil_de_datos", perfil)
    for nombre, valor in propuesta_para_perfil(valores["perfil_de_datos"]).items():
        if nombre in DE_TENANT:
            valores[nombre] = valor
    for nombre, valor in parciales.items():
        if nombre not in DE_TENANT:
            raise ValueError(f"no es un parámetro de tenant: {nombre}")
        if valor is not None:
            valores[nombre] = validar(nombre, valor)
    return valores


async def crear_admin_kis(ctx: RadarContexto, *, email: str, nombre: str, ip: Optional[str] = None) -> uuid.UUID:
    email = normalizar_email(email)
    async with ctx.db.tenant_tx(TENANT_KIS) as con:
        uid = await con.fetchval(
            "INSERT INTO users (email, nombre) VALUES ($1, $2) "
            "ON CONFLICT (tenant_id, email) DO UPDATE SET nombre = EXCLUDED.nombre RETURNING id",
            email, nombre)
        await con.execute(
            "INSERT INTO memberships (user_id, rol) VALUES ($1, 'admin') "
            "ON CONFLICT (tenant_id, user_id) DO UPDATE SET rol = 'admin'", uid)
        await auditoria.registrar(con, tenant_id=TENANT_KIS, actor_user_id=None, actor_rol="sistema",
                                  accion="usuario_invitado", tipo_objeto="user", objeto_id=uid, ip=ip,
                                  detalle={"rol_nuevo": "admin"})
    await enviar_link(ctx, tenant_id=TENANT_KIS, user_id=uid, email=email, proposito="invitacion", ip=ip)
    return uid


@dataclass(frozen=True)
class AltaTenant:
    tenant_id: uuid.UUID
    line_id: uuid.UUID
    user_id: uuid.UUID
    invitacion_enviada: bool


async def crear_tenant_con_dueno(ctx: RadarContexto, *, actor: Sesion, ip: Optional[str], nombre: str, rubro: str,
                                 perfil: str, parametros_tenant: dict, dueno_email: str, dueno_nombre: str,
                                 linea_nombre: str, parametros_linea: dict) -> AltaTenant:
    valores_t = resolver_valores_tenant(perfil, parametros_tenant)
    valores_l = resolver_valores_linea(parametros_linea)
    email = normalizar_email(dueno_email)
    tenant_id = uuid.uuid4()
    crear_k_tenant(ctx.secretos, tenant_id)
    try:
        async with ctx.db.tenant_tx(tenant_id) as con:
            await con.fetchval(
                "SELECT radar_admin_crear_tenant($1, $2, $3, $4, $5, $6, $7, $8)",
                tenant_id, nombre, rubro, valores_t["perfil_de_datos"], valores_t["retencion_fichas_meses"],
                valores_t["retener_fragmentos"], valores_t["ia_habilitada"], valores_t["via_llm"])
            line_id = await crear_linea(con, tenant_id=tenant_id, nombre=linea_nombre, valores=valores_l)
            user_id = await con.fetchval("INSERT INTO users (email, nombre) VALUES ($1, $2) RETURNING id",
                                         email, dueno_nombre)
            await con.execute("INSERT INTO memberships (user_id, rol) VALUES ($1, 'dueno')", user_id)
            comun = dict(tenant_id=tenant_id, actor_user_id=actor.user_id, actor_rol=actor.rol, ip=ip)
            await auditoria.registrar(con, accion="tenant_creado", tipo_objeto="tenant", objeto_id=tenant_id, **comun)
            await auditoria.registrar(con, accion="linea_creada", tipo_objeto="line", objeto_id=line_id, **comun)
            await auditoria.registrar(con, accion="usuario_invitado", tipo_objeto="user", objeto_id=user_id,
                                      detalle={"rol_nuevo": "dueno"}, **comun)
            await eventos_producto.registrar_evento(con, tenant_id=tenant_id, evento="linea_creada", line_id=line_id)
    except Exception:
        destruir_k_tenant(ctx.secretos, tenant_id)
        raise
    enviada = await enviar_link(ctx, tenant_id=tenant_id, user_id=user_id, email=email, proposito="invitacion", ip=ip)
    if enviada:
        async with ctx.db.tenant_tx(tenant_id) as con:
            await eventos_producto.registrar_evento(con, tenant_id=tenant_id, evento="invitacion_enviada",
                                                    line_id=line_id, user_id=user_id)
    return AltaTenant(tenant_id=tenant_id, line_id=line_id, user_id=user_id, invitacion_enviada=enviada)
```

- [ ] **Step 5: Router de admin**

`app/radar/routers/admin.py`:

```python
"""
/radar/admin/*: operaciones de los admins de KIS sobre los clientes. El
tenant destino va en la ruta; la sesión del admin vive en el tenant KIS.
Lo cruzado entre tenants pasa por las funciones SECURITY DEFINER y queda
auditado.
"""

import uuid
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.radar import auditoria
from app.radar.admin_kis import crear_tenant_con_dueno
from app.radar.auth import Sesion, ip_de, requiere_rol
from app.radar.constantes import TENANT_KIS
from app.radar.contexto import contexto
from app.radar.fuente import ALMACENES_DISPONIBLES
from app.radar.lineas import AlmacenNoDisponible, crear_linea, resolver_valores_linea
from app.radar.links import EmailInvalido, enviar_link, normalizar_email
from app.radar.parametros import ValorInvalido, perfil_por_rubro, propuesta_para_perfil

router = APIRouter(prefix="/radar/admin", tags=["radar-admin"])
_RUBRO = r"^[a-z_]{1,40}$"


class ParametrosTenantIn(BaseModel):
    retencion_fichas_meses: Optional[int] = None
    retener_fragmentos: Optional[bool] = None
    ia_habilitada: Optional[bool] = None
    via_llm: Optional[str] = None


class ParametrosLineaIn(BaseModel):
    duracion_vinculo_dias: Optional[int] = None
    retencion_fuente_dias: Optional[int] = None
    retencion_tras_desvinculo_dias: Optional[int] = None
    tope_ia_mensual_usd: Optional[Decimal] = None


class LineaIn(BaseModel):
    nombre: str = Field(min_length=1, max_length=80)
    parametros: ParametrosLineaIn = ParametrosLineaIn()


class DuenoIn(BaseModel):
    email: str = Field(max_length=254)
    nombre: str = Field(default="", max_length=120)


class TenantIn(BaseModel):
    nombre: str = Field(min_length=1, max_length=120)
    rubro: str = Field(pattern=_RUBRO)
    perfil_de_datos: Optional[str] = None
    parametros: ParametrosTenantIn = ParametrosTenantIn()
    dueno: DuenoIn
    linea: LineaIn


class InvitacionIn(BaseModel):
    email: str = Field(max_length=254)


def _422(e: Exception) -> HTTPException:
    return HTTPException(status_code=422, detail=str(e))


@router.get("/propuesta")
async def propuesta(rubro: str = Query(pattern=_RUBRO), admin: Sesion = Depends(requiere_rol("admin"))):
    perfil = perfil_por_rubro(rubro)
    p = propuesta_para_perfil(perfil)
    return {
        "perfil_de_datos": perfil,
        "parametros_tenant": {k: v for k, v in p.items() if k != "retencion_fuente_dias"},
        "parametros_linea": {k: v for k, v in p.items() if k == "retencion_fuente_dias"},
        "almacenes_disponibles": sorted(ALMACENES_DISPONIBLES),
    }


@router.post("/tenants", status_code=201)
async def crear_tenant(body: TenantIn, request: Request, admin: Sesion = Depends(requiere_rol("admin"))):
    ctx = contexto(request)
    try:
        alta = await crear_tenant_con_dueno(
            ctx, actor=admin, ip=ip_de(request), nombre=body.nombre, rubro=body.rubro,
            perfil=body.perfil_de_datos or perfil_por_rubro(body.rubro),
            parametros_tenant=body.parametros.model_dump(), dueno_email=body.dueno.email,
            dueno_nombre=body.dueno.nombre, linea_nombre=body.linea.nombre,
            parametros_linea=body.linea.parametros.model_dump())
    except (ValorInvalido, AlmacenNoDisponible, EmailInvalido) as e:
        raise _422(e)
    return {"tenant_id": str(alta.tenant_id), "line_id": str(alta.line_id), "user_id": str(alta.user_id),
            "invitacion_enviada": alta.invitacion_enviada}


@router.get("/tenants")
async def listar_tenants(request: Request, admin: Sesion = Depends(requiere_rol("admin"))):
    ctx = contexto(request)
    async with ctx.db.sin_tenant() as con:
        filas = await con.fetch("SELECT * FROM radar_admin_listar_tenants()")
    async with ctx.db.tenant_tx(TENANT_KIS) as con:
        await auditoria.registrar(con, tenant_id=TENANT_KIS, actor_user_id=admin.user_id, actor_rol=admin.rol,
                                  accion="tenants_listados", tipo_objeto="tenant", ip=ip_de(request),
                                  detalle={"cantidad": len(filas)})
    return [{"id": str(f["id"]), "nombre": f["nombre"], "rubro": f["rubro"], "perfil_de_datos": f["perfil_de_datos"],
             "created_at": f["created_at"].isoformat()} for f in filas]


@router.post("/tenants/{tenant_id}/lineas", status_code=201)
async def crear_linea_admin(tenant_id: uuid.UUID, body: LineaIn, request: Request,
                            admin: Sesion = Depends(requiere_rol("admin"))):
    ctx = contexto(request)
    try:
        valores = resolver_valores_linea(body.parametros.model_dump())
    except ValorInvalido as e:
        raise _422(e)
    async with ctx.db.tenant_tx(tenant_id) as con:
        if await con.fetchval("SELECT count(*) FROM tenants WHERE id = $1 AND NOT es_kis", tenant_id) == 0:
            raise HTTPException(status_code=404, detail="tenant inexistente")
        try:
            line_id = await crear_linea(con, tenant_id=tenant_id, nombre=body.nombre, valores=valores)
        except AlmacenNoDisponible as e:
            raise _422(e)
        await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=admin.user_id, actor_rol=admin.rol,
                                  accion="linea_creada", tipo_objeto="line", objeto_id=line_id, ip=ip_de(request))
    return {"line_id": str(line_id)}


@router.post("/tenants/{tenant_id}/invitaciones", status_code=202)
async def reenviar_invitacion(tenant_id: uuid.UUID, body: InvitacionIn, request: Request,
                              admin: Sesion = Depends(requiere_rol("admin"))):
    ctx = contexto(request)
    try:
        email = normalizar_email(body.email)
    except EmailInvalido as e:
        raise _422(e)
    async with ctx.db.tenant_tx(tenant_id) as con:
        user_id = await con.fetchval("SELECT id FROM users WHERE email = $1", email)
        if user_id is None:
            raise HTTPException(status_code=404, detail="usuario inexistente en este tenant")
        await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=admin.user_id, actor_rol=admin.rol,
                                  accion="invitacion_reenviada", tipo_objeto="user", objeto_id=user_id,
                                  ip=ip_de(request))
    enviada = await enviar_link(ctx, tenant_id=tenant_id, user_id=user_id, email=email,
                                proposito="invitacion", ip=ip_de(request))
    return {"enviada": enviada}
```

En `app/radar/app.py`: importar `admin` y agregar `app.include_router(admin.router)`.

- [ ] **Step 6: Script del primer admin**

`scripts/radar_admin.py`:

```python
"""
Alta del primer admin de KIS en un despliegue de Radar. Se corre en el
servidor (Railway shell) con las variables RADAR_* del entorno:

    python scripts/radar_admin.py crear-admin --email mariano@keepitsimple.com.ar --nombre "Mariano"

Imprime UNA vez el link de invitación (7 días, un solo uso) en esta terminal
en lugar de mandarlo por email: el primer admin no tiene todavía cómo pedir
un link desde /radar/login. Es idempotente: repetirlo actualiza el nombre y
reenvía la invitación (máximo 3 cada 15 minutos).
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.radar.admin_kis import crear_admin_kis          # noqa: E402
from app.radar.app import construir_contexto, validar_settings   # noqa: E402
from app.radar.mailer import MemoryMailer                # noqa: E402
from app.radar.settings import get_radar_settings        # noqa: E402


async def _crear_admin(email: str, nombre: str) -> None:
    rs = get_radar_settings()
    validar_settings(rs)
    ctx = await construir_contexto(rs)
    ctx.mailer = MemoryMailer()
    try:
        uid = await crear_admin_kis(ctx, email=email, nombre=nombre)
        print(f"admin {uid} listo")
        if ctx.mailer.enviados:
            print(ctx.mailer.enviados[0].texto)
        else:
            print("límite de links alcanzado para este usuario: esperá 15 minutos y repetí")
    finally:
        await ctx.cerrar()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Administración de Radar")
    sub = parser.add_subparsers(dest="comando", required=True)
    c = sub.add_parser("crear-admin", help="crea (o actualiza) un admin de KIS e imprime su invitación")
    c.add_argument("--email", required=True)
    c.add_argument("--nombre", default="")
    args = parser.parse_args(argv)
    asyncio.run(_crear_admin(args.email, args.nombre))


if __name__ == "__main__":
    main()
```

Crear `scripts/__init__.py` vacío para que el test pueda importar `scripts.radar_admin`.

- [ ] **Step 7: Correr**

```bash
python -m pytest tests/radar_tests -q
```
Esperado: todo verde; `test_admin.py` suma 16.

- [ ] **Step 8: Commit**

```bash
git add app/radar scripts/__init__.py scripts/radar_admin.py tests/radar_tests
git commit -m "Radar: alta de clientes por admin de KIS, lineas con parametros e invitacion al dueno"
```

---

### Task 11: API de cuenta: líneas, usuarios y roles

**Files:**
- Modify: `app/radar/routers/cuenta.py`
- Test: `tests/radar_tests/test_cuenta.py`

**Interfaces:**
- Consumes: `listar_lineas`, `linea_json`, `enviar_link`, `revocar_sesiones_de`, `auditoria`.
- Produces: `GET /radar/api/lineas` (todos los roles; gestor y lector limitados por `lineas_permitidas`) → `[linea_json]`; `GET /radar/api/usuarios` (dueño) → `[{user_id, email, nombre, rol, lineas_permitidas}]`; `POST /radar/api/usuarios` (dueño; body `{email, nombre, rol: gestor|lector, lineas_permitidas}`; 201 `{user_id, invitacion_enviada}`; 409 si el email ya existe); `PUT /radar/api/usuarios/{user_id}/rol` (dueño; body `{rol: dueno|gestor|lector, lineas_permitidas}`; revoca las sesiones del usuario; 400 si el dueño intenta quitarse su propio rol; 404 si no existe).

- [ ] **Step 1: Tests (fallan)**

`tests/radar_tests/test_cuenta.py`:

```python
"""
Roles (§4.4): el dueño ve todo e invita; el gestor puede limitarse a líneas
determinadas; el lector solo lee. El rol se valida en el servidor en cada
consulta.
"""
import re
import uuid

from .helpers import DOMINIO_COOKIE, crear_linea_directa, crear_tenant_directo, crear_usuario, entrar

LINK = re.compile(r"/radar/login/canjear\?t=([0-9a-f-]+)#k=([A-Za-z0-9_-]+)")


async def _tenant_con_dueno(ctx):
    a = await crear_tenant_directo(ctx.db, "A")
    d = await crear_usuario(ctx.db, a, "dueno@cliente.com", "dueno")
    l1 = await crear_linea_directa(ctx.db, a, "Centro")
    l2 = await crear_linea_directa(ctx.db, a, "Norte")
    return a, d, l1, l2


async def test_lineas_segun_rol(cliente, radar_ctx):
    a, d, l1, l2 = await _tenant_con_dueno(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    r = await cliente.get("/radar/api/lineas")
    assert r.status_code == 200
    assert [x["nombre"] for x in r.json()] == ["Centro", "Norte"]
    assert r.json()[0]["parametros"] == {"duracion_vinculo_dias": 0, "retencion_fuente_dias": 0,
                                         "retencion_tras_desvinculo_dias": 0, "tope_ia_mensual_usd": None}

    g = await crear_usuario(radar_ctx.db, a, "gestor@cliente.com", "gestor", lineas_permitidas=[l2])
    await entrar(cliente, radar_ctx, a, g, "gestor")
    assert [x["nombre"] for x in (await cliente.get("/radar/api/lineas")).json()] == ["Norte"]

    le = await crear_usuario(radar_ctx.db, a, "lector@cliente.com", "lector")
    await entrar(cliente, radar_ctx, a, le, "lector")
    assert len((await cliente.get("/radar/api/lineas")).json()) == 2


async def test_lineas_no_cruzan_tenants(cliente, radar_ctx):
    a, d, l1, l2 = await _tenant_con_dueno(radar_ctx)
    b = await crear_tenant_directo(radar_ctx.db, "B")
    await crear_linea_directa(radar_ctx.db, b, "De B")
    db_ = await crear_usuario(radar_ctx.db, b, "dueno@otro.com", "dueno")
    await entrar(cliente, radar_ctx, b, db_, "dueno")
    assert [x["nombre"] for x in (await cliente.get("/radar/api/lineas")).json()] == ["De B"]


async def test_dueno_invita_gestor_y_este_entra_con_su_rol(cliente, radar_ctx):
    a, d, l1, l2 = await _tenant_con_dueno(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    r = await cliente.post("/radar/api/usuarios", json={"email": "Gestor@Cliente.com", "nombre": "Gus",
                                                        "rol": "gestor", "lineas_permitidas": [str(l1)]})
    assert r.status_code == 201 and r.json()["invitacion_enviada"] is True
    uid = uuid.UUID(r.json()["user_id"])
    async with radar_ctx.db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT count(*) FROM access_audit_log WHERE accion = 'usuario_invitado' "
                                  "AND objeto_id = $1 AND actor_user_id = $2", uid, d) == 1
    t, k = LINK.search(radar_ctx.mailer.enviados[-1].texto).groups()
    cliente.cookies.clear()
    assert (await cliente.post("/radar/login/canjear", data={"t": t, "k": k})).status_code == 303
    yo = (await cliente.get("/radar/api/yo")).json()
    assert yo["rol"] == "gestor" and yo["lineas_permitidas"] == [str(l1)]
    assert [x["id"] for x in (await cliente.get("/radar/api/lineas")).json()] == [str(l1)]

    # repetido → 409; rol dueño por invitación → 422
    await entrar(cliente, radar_ctx, a, d, "dueno")
    assert (await cliente.post("/radar/api/usuarios", json={"email": "gestor@cliente.com", "rol": "lector"})).status_code == 409
    assert (await cliente.post("/radar/api/usuarios", json={"email": "otro@cliente.com", "rol": "dueno"})).status_code == 422


async def test_listar_usuarios(cliente, radar_ctx):
    a, d, l1, l2 = await _tenant_con_dueno(radar_ctx)
    await crear_usuario(radar_ctx.db, a, "lector@cliente.com", "lector")
    await entrar(cliente, radar_ctx, a, d, "dueno")
    r = await cliente.get("/radar/api/usuarios")
    assert r.status_code == 200
    assert sorted((u["email"], u["rol"]) for u in r.json()) == [("dueno@cliente.com", "dueno"), ("lector@cliente.com", "lector")]


async def test_cambio_de_rol_revoca_sesiones_y_audita(cliente, radar_ctx):
    a, d, l1, l2 = await _tenant_con_dueno(radar_ctx)
    g = await crear_usuario(radar_ctx.db, a, "gestor@cliente.com", "gestor")
    await entrar(cliente, radar_ctx, a, g, "gestor")
    cookie_gestor = cliente.cookies.get("radar_sesion")

    await entrar(cliente, radar_ctx, a, d, "dueno")
    r = await cliente.put(f"/radar/api/usuarios/{g}/rol", json={"rol": "lector"})
    assert r.status_code == 200 and r.json() == {"user_id": str(g), "rol": "lector", "lineas_permitidas": None}
    async with radar_ctx.db.tenant_tx(a) as con:
        fila = await con.fetchrow("SELECT detalle FROM access_audit_log WHERE accion = 'rol_cambiado'")
        assert '"rol_anterior": "gestor"' in fila["detalle"] and '"rol_nuevo": "lector"' in fila["detalle"]

    cliente.cookies.clear()
    cliente.cookies.set("radar_sesion", cookie_gestor, domain=DOMINIO_COOKIE, path="/radar")
    assert (await cliente.get("/radar/api/yo")).status_code == 401      # la sesión vieja murió

    await entrar(cliente, radar_ctx, a, d, "dueno")
    assert (await cliente.put(f"/radar/api/usuarios/{d}/rol", json={"rol": "lector"})).status_code == 400
    assert (await cliente.put(f"/radar/api/usuarios/{uuid.uuid4()}/rol", json={"rol": "lector"})).status_code == 404


async def test_solo_el_dueno_administra_usuarios(cliente, radar_ctx):
    a, d, l1, l2 = await _tenant_con_dueno(radar_ctx)
    for rol in ("gestor", "lector"):
        u = await crear_usuario(radar_ctx.db, a, f"{rol}@cliente.com", rol)
        await entrar(cliente, radar_ctx, a, u, rol)
        assert (await cliente.get("/radar/api/usuarios")).status_code == 403
        assert (await cliente.post("/radar/api/usuarios", json={"email": "x@cliente.com", "rol": "lector"})).status_code == 403
        assert (await cliente.put(f"/radar/api/usuarios/{d}/rol", json={"rol": "lector"})).status_code == 403
```

- [ ] **Step 2: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_cuenta.py -q
```
Esperado: `6 failed` (404 en `/radar/api/lineas` y `/radar/api/usuarios`).

- [ ] **Step 3: Router de cuenta completo**

`app/radar/routers/cuenta.py`:

```python
"""API autenticada de la cuenta: quién soy, líneas, usuarios y roles (§4.4)."""

import uuid
from typing import Literal, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.radar import auditoria
from app.radar.auth import Sesion, ip_de, requiere_rol, revocar_sesiones_de, sesion_actual
from app.radar.contexto import contexto
from app.radar.lineas import linea_json, listar_lineas
from app.radar.links import EmailInvalido, enviar_link, normalizar_email

router = APIRouter(prefix="/radar/api", tags=["radar-cuenta"])


def _sesion_json(sesion: Sesion) -> dict:
    return {
        "user_id": str(sesion.user_id),
        "tenant_id": str(sesion.tenant_id),
        "rol": sesion.rol,
        "email": sesion.email,
        "es_kis": sesion.es_kis,
        "lineas_permitidas": [str(x) for x in sesion.lineas_permitidas]
        if sesion.lineas_permitidas is not None else None,
    }


def _usuario_json(fila) -> dict:
    return {"user_id": str(fila["id"]), "email": fila["email"], "nombre": fila["nombre"], "rol": fila["rol"],
            "lineas_permitidas": [str(x) for x in fila["lineas_permitidas"]]
            if fila["lineas_permitidas"] is not None else None}


class InvitacionUsuarioIn(BaseModel):
    email: str = Field(max_length=254)
    nombre: str = Field(default="", max_length=120)
    rol: Literal["gestor", "lector"]
    lineas_permitidas: Optional[list[uuid.UUID]] = None


class RolIn(BaseModel):
    rol: Literal["dueno", "gestor", "lector"]
    lineas_permitidas: Optional[list[uuid.UUID]] = None


@router.get("/yo")
async def yo(sesion: Sesion = Depends(sesion_actual)):
    return _sesion_json(sesion)


@router.get("/lineas")
async def lineas(request: Request, sesion: Sesion = Depends(sesion_actual)):
    ctx = contexto(request)
    async with ctx.db.tenant_tx(sesion.tenant_id) as con:
        filas = await listar_lineas(con, sesion.lineas_permitidas)
    return [linea_json(f) for f in filas]


@router.get("/usuarios")
async def usuarios(request: Request, sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    async with ctx.db.tenant_tx(sesion.tenant_id) as con:
        filas = await con.fetch(
            "SELECT u.id, u.email, u.nombre, m.rol, m.lineas_permitidas FROM users u "
            "JOIN memberships m ON m.user_id = u.id ORDER BY u.created_at")
    return [_usuario_json(f) for f in filas]


@router.post("/usuarios", status_code=201)
async def invitar_usuario(body: InvitacionUsuarioIn, request: Request,
                          sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    try:
        email = normalizar_email(body.email)
    except EmailInvalido as e:
        raise HTTPException(status_code=422, detail=str(e))
    ip = ip_de(request)
    async with ctx.db.tenant_tx(sesion.tenant_id) as con:
        try:
            user_id = await con.fetchval("INSERT INTO users (email, nombre) VALUES ($1, $2) RETURNING id",
                                         email, body.nombre)
        except asyncpg.UniqueViolationError:
            raise HTTPException(status_code=409, detail="ese email ya tiene usuario en esta cuenta")
        await con.execute("INSERT INTO memberships (user_id, rol, lineas_permitidas) VALUES ($1, $2, $3)",
                          user_id, body.rol, body.lineas_permitidas)
        await auditoria.registrar(con, tenant_id=sesion.tenant_id, actor_user_id=sesion.user_id, actor_rol=sesion.rol,
                                  accion="usuario_invitado", tipo_objeto="user", objeto_id=user_id, ip=ip,
                                  detalle={"rol_nuevo": body.rol})
    enviada = await enviar_link(ctx, tenant_id=sesion.tenant_id, user_id=user_id, email=email,
                                proposito="invitacion", ip=ip)
    return {"user_id": str(user_id), "invitacion_enviada": enviada}


@router.put("/usuarios/{user_id}/rol")
async def cambiar_rol(user_id: uuid.UUID, body: RolIn, request: Request,
                      sesion: Sesion = Depends(requiere_rol("dueno"))):
    if user_id == sesion.user_id and body.rol != "dueno":
        raise HTTPException(status_code=400, detail="no podés quitarte tu propio rol de dueño")
    ctx = contexto(request)
    async with ctx.db.tenant_tx(sesion.tenant_id) as con:
        anterior = await con.fetchval("SELECT rol FROM memberships WHERE user_id = $1", user_id)
        if anterior is None:
            raise HTTPException(status_code=404, detail="usuario inexistente")
        await con.execute("UPDATE memberships SET rol = $2, lineas_permitidas = $3 WHERE user_id = $1",
                          user_id, body.rol, body.lineas_permitidas)
        await revocar_sesiones_de(con, user_id)
        await auditoria.registrar(con, tenant_id=sesion.tenant_id, actor_user_id=sesion.user_id, actor_rol=sesion.rol,
                                  accion="rol_cambiado", tipo_objeto="membership", objeto_id=user_id, ip=ip_de(request),
                                  detalle={"rol_anterior": anterior, "rol_nuevo": body.rol})
    return {"user_id": str(user_id), "rol": body.rol,
            "lineas_permitidas": [str(x) for x in body.lineas_permitidas] if body.lineas_permitidas is not None else None}
```

- [ ] **Step 4: Correr**

```bash
python -m pytest tests/radar_tests -q
```
Esperado: todo verde; `test_cuenta.py` suma 6.

- [ ] **Step 5: Commit**

```bash
git add app/radar/routers/cuenta.py tests/radar_tests/test_cuenta.py
git commit -m "Radar: lineas por rol, invitacion de usuarios y cambio de rol con revocacion"
```

---

### Task 12: Cambios de parámetros y consentimientos por línea

**Files:**
- Create: `app/radar/consentimiento.py`, `app/radar/parametros_service.py`, `app/radar/routers/parametros.py`
- Modify: `app/radar/routers/admin.py` (dos `PUT`), `app/radar/app.py` (montar `parametros.router`)
- Test: `tests/radar_tests/test_parametros_api.py`

**Interfaces:**
- Consumes: `parametros.*`, `lineas.leer_linea/verificar_almacen/AlmacenNoDisponible`, `auditoria`, `eventos_producto`.
- Produces (`consentimiento.py`): `VERSIONES: dict[str, str]` (`{"v1": TEXTO_V1}`), `hash_texto(version: str) -> str`, `async registrar_consentimiento(con, *, tenant_id, line_id, user_id, version, opciones: dict, ip) -> uuid.UUID`, `async listar_consentimientos(con, line_id) -> list[dict]`.
- Produces (`parametros_service.py`): `CambioRechazado(status: int, detalle: dict)`, `a_json(valores: dict) -> dict` (Decimal → str), `async leer_parametros_tenant(con, tenant_id) -> dict`, `async leer_propuesta_tenant(con, tenant_id) -> dict`, `coincide_con_propuesta(propuesta: dict, nombre: str, valor) -> bool`, `async lineas_vivas_sin_consentir(con, valores_tenant: dict) -> list[uuid.UUID]`, `async cambiar_parametros_linea(con, *, tenant_id, line_id, nuevos, actor_user_id, actor_rol, ip, solo_endurecer: bool, consentido: bool = False) -> dict`, `async cambiar_parametros_tenant(con, *, tenant_id, nuevos, actor_user_id, actor_rol, ip, solo_endurecer: bool) -> dict`, `async proponer_parametros_linea(con, *, tenant_id, line_id, propuesta: dict, actor_user_id, actor_rol, ip) -> dict`, `async proponer_parametros_tenant(con, *, tenant_id, propuesta: dict, actor_user_id, actor_rol, ip) -> dict`, `async consumir_propuesta_linea(con, line_id, nombres: list[str]) -> None`, `async consumir_propuesta_tenant(con, tenant_id, nombres: list[str]) -> None`.
- Endpoints: `GET /radar/api/lineas/{line_id}/parametros` (cualquier rol con acceso a la línea) → `{linea, estado, almacen_fuente, tenant, propuesta: {linea, tenant}}`; `PUT /radar/api/lineas/{line_id}/parametros` (dueño, solo endurecer) → parámetros finales; `PUT /radar/api/cuenta/parametros` (dueño, solo endurecer); `POST /radar/api/lineas/{line_id}/consentimientos` (dueño; solo valores que coincidan con la propuesta) → 201 `{consent_id, hash_texto, tenant_aplicado, lineas_pendientes}`; `GET /radar/api/lineas/{line_id}/consentimientos` (dueño); `PUT /radar/admin/tenants/{tenant_id}/lineas/{line_id}/parametros` y `PUT /radar/admin/tenants/{tenant_id}/parametros` (admin, cualquier sentido; 409 `requiere_consentimiento` sobre líneas vivas **y guarda la propuesta**).
- Códigos: 403 `{"error": "solo_endurecer", "parametros": [...]}`; 409 `{"error": "requiere_consentimiento", "parametros": [...], "lineas": [...]?}`; 422 `{"error": "valor_invalido" | "almacen_no_disponible", "detalle": str}` y `422 {"error": "sin_propuesta", "parametros": [...]}`; 404 `{"error": "linea_inexistente"}`.

- [ ] **Step 1: Tests (fallan)**

`tests/radar_tests/test_parametros_api.py`:

```python
"""
Regla de §2.1: los parámetros los fija un admin de KIS; el cliente solo
endurece; endurecer se aplica directo y queda auditado; aflojar sobre una
línea viva exige una fila nueva en consents que acepte lo que KIS propuso.
"""
import hashlib
import uuid

from app.radar.consentimiento import VERSIONES
from app.radar.constantes import TENANT_KIS

from .helpers import crear_linea_directa, crear_tenant_directo, crear_usuario, entrar

CONSENT = {"version_texto": "v1", "acepta": True, "titular": True}


async def _base(ctx, estado="sin_vinculo"):
    a = await crear_tenant_directo(ctx.db, "A")
    d = await crear_usuario(ctx.db, a, "dueno@cliente.com", "dueno")
    l = await crear_linea_directa(ctx.db, a, "Centro", estado=estado)
    adm = await crear_usuario(ctx.db, TENANT_KIS, "admin@keepitsimple.com.ar", "admin")
    return a, d, l, adm


async def _auditados(ctx, tenant_id, accion="parametro_cambiado"):
    async with ctx.db.tenant_tx(tenant_id) as con:
        return [f["detalle"] for f in await con.fetch(
            "SELECT detalle FROM access_audit_log WHERE accion = $1 ORDER BY id", accion)]


async def test_get_parametros_de_linea_y_tenant(cliente, radar_ctx):
    a, d, l, _ = await _base(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    r = await cliente.get(f"/radar/api/lineas/{l}/parametros")
    assert r.status_code == 200
    assert r.json() == {
        "linea": {"duracion_vinculo_dias": 0, "retencion_fuente_dias": 0,
                  "retencion_tras_desvinculo_dias": 0, "tope_ia_mensual_usd": None},
        "estado": "sin_vinculo", "almacen_fuente": "permanente",
        "tenant": {"retencion_fichas_meses": 12, "perfil_de_datos": "estandar", "retener_fragmentos": False,
                   "ia_habilitada": True, "via_llm": "lotes"},
        "propuesta": {"linea": {}, "tenant": {}},
    }
    g = await crear_usuario(radar_ctx.db, a, "g@cliente.com", "gestor", lineas_permitidas=[uuid.uuid4()])
    await entrar(cliente, radar_ctx, a, g, "gestor")
    assert (await cliente.get(f"/radar/api/lineas/{l}/parametros")).status_code == 404


async def test_gestor_y_lector_no_cambian_parametros(cliente, radar_ctx):
    """requiere_rol("dueno") en los dos PUT del cliente: el rol se valida en el
    servidor en cada consulta (§4.4)."""
    a, d, l, _ = await _base(radar_ctx)
    for rol in ("gestor", "lector"):
        u = await crear_usuario(radar_ctx.db, a, f"{rol}@cliente.com", rol)
        await entrar(cliente, radar_ctx, a, u, rol)
        assert (await cliente.put(f"/radar/api/lineas/{l}/parametros", json={"duracion_vinculo_dias": 1})).status_code == 403
        assert (await cliente.put("/radar/api/cuenta/parametros", json={"ia_habilitada": False})).status_code == 403
        assert (await cliente.get(f"/radar/api/lineas/{l}/consentimientos")).status_code == 403
    async with radar_ctx.db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT duracion_vinculo_dias FROM lines WHERE id = $1", l) == 0
        assert await con.fetchval("SELECT ia_habilitada FROM tenants") is True


async def test_dueno_endurece_directo_y_queda_auditado(cliente, radar_ctx):
    a, d, l, _ = await _base(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    r = await cliente.put(f"/radar/api/lineas/{l}/parametros",
                          json={"duracion_vinculo_dias": 30, "tope_ia_mensual_usd": "20.00"})
    assert r.status_code == 200
    assert r.json()["duracion_vinculo_dias"] == 30 and r.json()["tope_ia_mensual_usd"] == "20.00"
    detalles = await _auditados(radar_ctx, a)
    assert len(detalles) == 2
    assert '"parametro": "duracion_vinculo_dias"' in detalles[0] and '"valor_anterior": 0' in detalles[0] \
        and '"valor_nuevo": 30' in detalles[0] and '"ambito": "linea"' in detalles[0]


async def test_dueno_no_puede_aflojar(cliente, radar_ctx):
    a, d, l, _ = await _base(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    await cliente.put(f"/radar/api/lineas/{l}/parametros", json={"duracion_vinculo_dias": 30})
    r = await cliente.put(f"/radar/api/lineas/{l}/parametros", json={"duracion_vinculo_dias": 0})
    assert r.status_code == 403 and r.json()["detail"] == {"error": "solo_endurecer", "parametros": ["duracion_vinculo_dias"]}
    r = await cliente.put(f"/radar/api/lineas/{l}/parametros", json={"duracion_vinculo_dias": 60})
    assert r.status_code == 403
    r = await cliente.put(f"/radar/api/lineas/{l}/parametros", json={"duracion_vinculo_dias": 7})
    assert r.status_code == 200 and r.json()["duracion_vinculo_dias"] == 7


async def test_valores_invalidos_y_almacen(cliente, radar_ctx):
    a, d, l, adm = await _base(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    r = await cliente.put(f"/radar/api/lineas/{l}/parametros", json={"duracion_vinculo_dias": -3})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "valor_invalido"
    r = await cliente.put(f"/radar/api/lineas/{l}/parametros", json={"inventado": 1})
    assert r.status_code == 422
    r = await cliente.put(f"/radar/api/lineas/{l}/parametros", json={"retencion_fuente_dias": 7})   # más estricto, pero sin almacén
    assert r.status_code == 422 and r.json()["detail"]["error"] == "almacen_no_disponible"
    assert (await cliente.put(f"/radar/api/lineas/{uuid.uuid4()}/parametros", json={"duracion_vinculo_dias": 1})).status_code == 404


async def test_admin_afloja_directo_sobre_linea_sin_vinculo(cliente, radar_ctx):
    a, d, l, adm = await _base(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    await cliente.put(f"/radar/api/lineas/{l}/parametros", json={"duracion_vinculo_dias": 30})
    await entrar(cliente, radar_ctx, TENANT_KIS, adm, "admin")
    r = await cliente.put(f"/radar/admin/tenants/{a}/lineas/{l}/parametros", json={"duracion_vinculo_dias": 0})
    assert r.status_code == 200 and r.json()["duracion_vinculo_dias"] == 0
    detalles = await _auditados(radar_ctx, a)
    assert '"valor_anterior": 30' in detalles[-1] and '"valor_nuevo": 0' in detalles[-1]


async def test_aflojar_linea_viva_exige_consentimiento(cliente, radar_ctx):
    a, d, l, adm = await _base(radar_ctx, estado="vinculada")
    await entrar(cliente, radar_ctx, a, d, "dueno")
    assert (await cliente.put(f"/radar/api/lineas/{l}/parametros", json={"duracion_vinculo_dias": 30})).status_code == 200

    await entrar(cliente, radar_ctx, TENANT_KIS, adm, "admin")
    r = await cliente.put(f"/radar/admin/tenants/{a}/lineas/{l}/parametros", json={"duracion_vinculo_dias": 0})
    assert r.status_code == 409 and r.json()["detail"] == {"error": "requiere_consentimiento", "parametros": ["duracion_vinculo_dias"]}
    async with radar_ctx.db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT duracion_vinculo_dias FROM lines WHERE id = $1", l) == 30
        # el 409 dejó la propuesta guardada y auditada
        assert await con.fetchval("SELECT parametros_propuestos FROM lines WHERE id = $1", l) == '{"duracion_vinculo_dias": 0}'
        prop = await con.fetchrow("SELECT actor_user_id, detalle FROM access_audit_log WHERE accion = 'parametro_propuesto'")
        assert prop["actor_user_id"] == adm and '"valor_nuevo": 0' in prop["detalle"]

    await entrar(cliente, radar_ctx, a, d, "dueno")
    r = await cliente.get(f"/radar/api/lineas/{l}/parametros")
    assert r.json()["propuesta"] == {"linea": {"duracion_vinculo_dias": 0}, "tenant": {}}
    # otro valor que el propuesto: no hay consentimiento sin propuesta
    r = await cliente.post(f"/radar/api/lineas/{l}/consentimientos",
                           json=dict(CONSENT, parametros_linea={"duracion_vinculo_dias": 7}))
    assert r.status_code == 422 and r.json()["detail"] == {"error": "sin_propuesta", "parametros": ["duracion_vinculo_dias"]}

    r = await cliente.post(f"/radar/api/lineas/{l}/consentimientos",
                           json=dict(CONSENT, parametros_linea={"duracion_vinculo_dias": 0}))
    assert r.status_code == 201, r.text
    cuerpo = r.json()
    assert cuerpo["hash_texto"] == hashlib.sha256(VERSIONES["v1"].encode()).hexdigest()
    assert cuerpo["tenant_aplicado"] is True and cuerpo["lineas_pendientes"] == []
    async with radar_ctx.db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT duracion_vinculo_dias FROM lines WHERE id = $1", l) == 0
        assert await con.fetchval("SELECT parametros_propuestos FROM lines WHERE id = $1", l) is None   # consumida
        c = await con.fetchrow("SELECT version_texto, hash_texto, user_id, ip, opciones FROM consents WHERE id = $1",
                               uuid.UUID(cuerpo["consent_id"]))
        assert c["version_texto"] == "v1" and c["hash_texto"] == cuerpo["hash_texto"] and c["user_id"] == d
        assert c["ip"] is not None
        assert '"duracion_vinculo_dias": 0' in c["opciones"] and '"parametros_tenant"' in c["opciones"]
        assert await con.fetchval("SELECT count(*) FROM access_audit_log WHERE accion = 'consentimiento_registrado'") == 1
        assert await con.fetchval("SELECT count(*) FROM product_events WHERE evento = 'consentimiento_registrado'") == 1
        assert await con.fetchval("SELECT count(*) FROM consents") == 1        # el 422 no dejó fila

    r = await cliente.get(f"/radar/api/lineas/{l}/consentimientos")
    assert r.status_code == 200 and len(r.json()) == 1 and r.json()[0]["version_texto"] == "v1"


async def test_consentimiento_solo_acepta_lo_propuesto_por_kis(cliente, radar_ctx):
    """§2.1: los parámetros los fija un admin de KIS y el cliente solo endurece.
    Sin propuesta previa, el consentimiento no afloja nada: ni de línea, ni de
    tenant, ni el camino sensible → estandar (§7: solo un admin de KIS)."""
    a = await crear_tenant_directo(radar_ctx.db, "A", perfil="sensible")
    d = await crear_usuario(radar_ctx.db, a, "dueno@cliente.com", "dueno")
    l = await crear_linea_directa(radar_ctx.db, a, "Centro", estado="vinculada")
    await entrar(cliente, radar_ctx, a, d, "dueno")
    assert (await cliente.put(f"/radar/api/lineas/{l}/parametros", json={"duracion_vinculo_dias": 30})).status_code == 200
    assert (await cliente.put("/radar/api/cuenta/parametros", json={"ia_habilitada": False})).status_code == 200
    for malo, parametro in (({"parametros_tenant": {"perfil_de_datos": "estandar"}}, "perfil_de_datos"),
                            ({"parametros_tenant": {"ia_habilitada": True}}, "ia_habilitada"),
                            ({"parametros_linea": {"duracion_vinculo_dias": 0}}, "duracion_vinculo_dias"),
                            ({"parametros_linea": {"duracion_vinculo_dias": 30}}, "duracion_vinculo_dias")):   # ni el valor actual
        r = await cliente.post(f"/radar/api/lineas/{l}/consentimientos", json=dict(CONSENT, **malo))
        assert r.status_code == 422 and r.json()["detail"] == {"error": "sin_propuesta", "parametros": [parametro]}, malo
    async with radar_ctx.db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT perfil_de_datos FROM tenants") == "sensible"
        assert await con.fetchval("SELECT ia_habilitada FROM tenants") is False
        assert await con.fetchval("SELECT duracion_vinculo_dias FROM lines WHERE id = $1", l) == 30
        assert await con.fetchval("SELECT count(*) FROM consents") == 0


async def test_consentimiento_invalido(cliente, radar_ctx):
    a, d, l, _ = await _base(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    for malo in (dict(CONSENT, acepta=False), dict(CONSENT, titular=False), dict(CONSENT, version_texto="v99"),
                 dict(CONSENT, parametros_linea={"ia_habilitada": False}),
                 dict(CONSENT, parametros_tenant={"duracion_vinculo_dias": 1}),
                 dict(CONSENT, parametros_linea={"duracion_vinculo_dias": 0})):    # sin propuesta
        r = await cliente.post(f"/radar/api/lineas/{l}/consentimientos", json=malo)
        assert r.status_code == 422, malo
    le = await crear_usuario(radar_ctx.db, a, "lector@cliente.com", "lector")
    await entrar(cliente, radar_ctx, a, le, "lector")
    assert (await cliente.post(f"/radar/api/lineas/{l}/consentimientos", json=CONSENT)).status_code == 403


async def test_parametros_de_tenant_del_dueno(cliente, radar_ctx):
    a, d, l, _ = await _base(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    r = await cliente.put("/radar/api/cuenta/parametros", json={"ia_habilitada": False, "perfil_de_datos": "sensible"})
    assert r.status_code == 200 and r.json()["ia_habilitada"] is False and r.json()["perfil_de_datos"] == "sensible"
    detalles = await _auditados(radar_ctx, a)
    assert any('"ambito": "tenant"' in x and '"parametro": "perfil_de_datos"' in x for x in detalles)
    r = await cliente.put("/radar/api/cuenta/parametros", json={"perfil_de_datos": "estandar"})
    assert r.status_code == 403        # el camino inverso solo lo hace un admin de KIS


async def test_tenant_mas_laxo_con_lineas_vivas(cliente, radar_ctx):
    a, d, l1, adm = await _base(radar_ctx, estado="vinculada")
    l2 = await crear_linea_directa(radar_ctx.db, a, "Norte", estado="vinculada")
    await entrar(cliente, radar_ctx, a, d, "dueno")
    assert (await cliente.put("/radar/api/cuenta/parametros", json={"ia_habilitada": False})).status_code == 200

    await entrar(cliente, radar_ctx, TENANT_KIS, adm, "admin")
    r = await cliente.put(f"/radar/admin/tenants/{a}/parametros", json={"ia_habilitada": True})
    assert r.status_code == 409
    assert set(r.json()["detail"]["lineas"]) == {str(l1), str(l2)}
    async with radar_ctx.db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT parametros_propuestos FROM tenants") == '{"ia_habilitada": true}'

    await entrar(cliente, radar_ctx, a, d, "dueno")
    assert (await cliente.get(f"/radar/api/lineas/{l2}/parametros")).json()["propuesta"] == {"linea": {}, "tenant": {"ia_habilitada": True}}
    r = await cliente.post(f"/radar/api/lineas/{l1}/consentimientos", json=dict(CONSENT, parametros_tenant={"ia_habilitada": True}))
    assert r.status_code == 201 and r.json()["tenant_aplicado"] is False and r.json()["lineas_pendientes"] == [str(l2)]
    async with radar_ctx.db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT ia_habilitada FROM tenants") is False
        assert await con.fetchval("SELECT parametros_propuestos FROM tenants") is not None   # sigue pendiente
    r = await cliente.post(f"/radar/api/lineas/{l2}/consentimientos", json=dict(CONSENT, parametros_tenant={"ia_habilitada": True}))
    assert r.status_code == 201 and r.json()["tenant_aplicado"] is True
    async with radar_ctx.db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT ia_habilitada FROM tenants") is True
        assert await con.fetchval("SELECT parametros_propuestos FROM tenants") is None       # consumida

    await entrar(cliente, radar_ctx, TENANT_KIS, adm, "admin")
    assert (await cliente.put(f"/radar/admin/tenants/{a}/parametros", json={"retencion_fichas_meses": 0})).status_code == 409


async def test_admin_tenant_sin_lineas_vivas_aplica_directo(cliente, radar_ctx):
    a, d, l, adm = await _base(radar_ctx)
    await entrar(cliente, radar_ctx, TENANT_KIS, adm, "admin")
    r = await cliente.put(f"/radar/admin/tenants/{a}/parametros", json={"retencion_fichas_meses": 0, "via_llm": "lotes"})
    assert r.status_code == 200 and r.json()["retencion_fichas_meses"] == 0
    assert len(await _auditados(radar_ctx, a)) == 1           # via_llm no cambió: no se audita
```

- [ ] **Step 2: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_parametros_api.py -q
```
Esperado: `ModuleNotFoundError: No module named 'app.radar.consentimiento'`.

- [ ] **Step 3: `consentimiento.py`**

```python
"""
Consentimiento por línea (§3 P2): versión del texto, hash, fecha, IP y
opciones elegidas. Los textos viven acá, versionados; se guarda sha256 del
texto para poder probar después exactamente qué se aceptó. Ningún texto de
consentimiento lleva datos de conversación.
"""

import hashlib
import json
import uuid
from typing import Optional

import asyncpg

TEXTO_V1 = """Qué hacemos: leemos los mensajes de tus chats individuales para calcular métricas y detectar patrones.

Qué no hacemos: no enviamos mensajes, no marcamos chats como leídos, no aparecemos "en línea", no leemos grupos, estados ni canales, no descargamos fotos, audios ni documentos.

Importante: mientras tu línea esté conectada, WhatsApp le entrega a nuestro servidor de conexión una copia técnica de todos tus chats individuales, incluidos los personales, y la sigue actualizando con cada mensaje nuevo. No podemos filtrarla chat por chat. Un proceso automático la recorre una vez, al conectar, para contar mensajes y sugerirte qué excluir; no guarda el texto de los chats que excluyas, y ninguna persona los ve. Después de eso solo leemos los chats que elegiste. La copia se borra completa cuando desconectás desde Radar. Si quitás el dispositivo desde tu teléfono, se borra dentro de las 72 horas.

Cuánto dura: la conexión no vence sola: queda activa hasta que la desconectes. Conservamos el texto de los chats que elegiste mientras tu línea siga dada de alta en Keep IT Simple, aunque la desconectes de WhatsApp. Para borrarlo usá "Desconectar y borrar todo".

Quién procesa y dónde: Keep IT Simple. Los datos se alojan en Railway (EE. UU.). Para el análisis se transfieren a Anthropic y, para agrupar preguntas, a OpenAI (ambos en EE. UU.). Antes de enviar nada reemplazamos teléfonos y nombres de contacto por códigos y tachamos DNI, tarjetas, CBU y direcciones que detectamos. El texto de los mensajes sí se envía: puede incluir datos que tus clientes escribieron y no garantizamos detectarlos todos. Anthropic y OpenAI pueden conservar hasta 30 días lo que les enviamos, por seguridad y prevención de abuso. No lo usan para entrenar sus modelos.

Para qué usamos el resultado: para mostrarte este diagnóstico y configurar el servicio que contrataste. No usamos tus conversaciones para ningún otro fin ni las cruzamos con otros clientes.

Riesgo que tenés que conocer: la conexión usa "dispositivos vinculados" mediante un cliente no oficial, que WhatsApp no avala. No enviamos mensajes ni hacemos las acciones de envío que se conocen como causa de bloqueo. Aun así, nadie garantiza que un vínculo de solo lectura sea seguro: el riesgo existe y no lo podemos cuantificar. Mientras dure la conexión el riesgo es continuo, no de una sola vez. La conexión ocupa uno de tus dispositivos vinculados, y si el teléfono pasa unos 14 días sin internet, WhatsApp la corta.

Soy titular o responsable de esta línea y confirmo el acuerdo de tratamiento de datos de mi contrato.
"""

VERSIONES: dict[str, str] = {"v1": TEXTO_V1}


def hash_texto(version: str) -> str:
    return hashlib.sha256(VERSIONES[version].encode("utf-8")).hexdigest()


async def registrar_consentimiento(con: asyncpg.Connection, *, tenant_id: uuid.UUID, line_id: uuid.UUID,
                                   user_id: uuid.UUID, version: str, opciones: dict, ip: Optional[str]) -> uuid.UUID:
    return await con.fetchval(
        "INSERT INTO consents (tenant_id, line_id, user_id, version_texto, hash_texto, opciones, ip) "
        "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7) RETURNING id",
        tenant_id, line_id, user_id, version, hash_texto(version), json.dumps(opciones, default=str), ip)


async def listar_consentimientos(con: asyncpg.Connection, line_id: uuid.UUID) -> list[dict]:
    filas = await con.fetch(
        "SELECT id, version_texto, hash_texto, user_id, created_at, opciones FROM consents "
        "WHERE line_id = $1 ORDER BY created_at DESC", line_id)
    return [{"id": str(f["id"]), "version_texto": f["version_texto"], "hash_texto": f["hash_texto"],
             "user_id": str(f["user_id"]), "created_at": f["created_at"].isoformat(),
             "opciones": json.loads(f["opciones"])} for f in filas]
```

- [ ] **Step 4: `parametros_service.py`**

```python
"""
Cambios de parámetros con la regla de §2.1:
- endurecer se aplica directo y queda auditado (una fila por parámetro);
- el cliente solo puede endurecer (solo_endurecer=True → 403 si algo es más laxo);
- un valor más laxo sobre una línea `vinculada` exige un consentimiento nuevo
  del dueño: 409 salvo consentido=True, que solo usa el endpoint de consentimientos;
- un valor de tenant más laxo con líneas vivas exige que CADA línea vinculada
  tenga un consentimiento (el último) cuyas opciones.parametros_tenant incluyan
  esos valores; hasta entonces, 409 con las líneas que faltan.
- Los valores más laxos los PROPONE un admin de KIS ("los fija un admin de
  KIS", §2.1): el 409 del admin guarda la propuesta en
  lines/tenants.parametros_propuestos, y el consentimiento del dueño solo
  acepta valores que coincidan con ella (coincide_con_propuesta). Al aplicarse,
  los nombres consentidos se quitan de la propuesta (consumir_propuesta_*).
"""

import json
import uuid
from decimal import Decimal
from typing import Any, Optional

import asyncpg

from app.radar import auditoria
from app.radar.lineas import AlmacenNoDisponible, verificar_almacen
from app.radar.parametros import DE_LINEA, DE_TENANT, ValorInvalido, clasificar_cambios, validar


class CambioRechazado(Exception):
    def __init__(self, status: int, detalle: dict):
        super().__init__(detalle)
        self.status = status
        self.detalle = detalle


def a_json(valores: dict) -> dict:
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in valores.items()}


def _cambios(actuales: dict, nuevos: dict, permitidos: list[str]) -> dict:
    ajenos = [n for n in nuevos if n not in permitidos]
    if ajenos:
        raise CambioRechazado(422, {"error": "valor_invalido", "detalle": f"parámetros desconocidos: {ajenos}"})
    try:
        return clasificar_cambios(actuales, nuevos)
    except ValorInvalido as e:
        raise CambioRechazado(422, {"error": "valor_invalido", "detalle": str(e)})


def _rechazar_laxos(cambios: dict, solo_endurecer: bool) -> list[str]:
    laxos = [n for n, c in cambios.items() if c == "mas_laxo"]
    if laxos and solo_endurecer:
        raise CambioRechazado(403, {"error": "solo_endurecer", "parametros": laxos})
    return laxos


async def _auditar(con, *, tenant_id, objeto_id, tipo_objeto, actor_user_id, actor_rol, ip, ambito, actuales, finales, cambios):
    for nombre in cambios:
        await auditoria.registrar(
            con, tenant_id=tenant_id, actor_user_id=actor_user_id, actor_rol=actor_rol, accion="parametro_cambiado",
            tipo_objeto=tipo_objeto, objeto_id=objeto_id, ip=ip,
            detalle={"parametro": nombre, "valor_anterior": actuales[nombre], "valor_nuevo": finales[nombre],
                     "ambito": ambito})


async def leer_parametros_tenant(con: asyncpg.Connection, tenant_id: uuid.UUID) -> dict:
    fila = await con.fetchrow("SELECT " + ", ".join(DE_TENANT) + " FROM tenants WHERE id = $1", tenant_id)
    return {n: fila[n] for n in DE_TENANT}


async def leer_propuesta_tenant(con: asyncpg.Connection, tenant_id: uuid.UUID) -> dict:
    crudo = await con.fetchval("SELECT parametros_propuestos FROM tenants WHERE id = $1", tenant_id)
    return json.loads(crudo) if crudo else {}


def coincide_con_propuesta(propuesta: dict, nombre: str, valor: Any) -> bool:
    """True si `nombre` está en la propuesta con exactamente ese valor. Se
    compara validado (Decimal("20") == Decimal("20.00")), no como texto."""
    return nombre in propuesta and validar(nombre, propuesta[nombre]) == valor


async def _auditar_propuesta(con, *, tenant_id, objeto_id, tipo_objeto, ambito, actor_user_id, actor_rol, ip, valores):
    for nombre, valor in valores.items():
        await auditoria.registrar(
            con, tenant_id=tenant_id, actor_user_id=actor_user_id, actor_rol=actor_rol, accion="parametro_propuesto",
            tipo_objeto=tipo_objeto, objeto_id=objeto_id, ip=ip,
            detalle={"parametro": nombre, "valor_nuevo": valor, "ambito": ambito})


async def proponer_parametros_linea(con: asyncpg.Connection, *, tenant_id: uuid.UUID, line_id: uuid.UUID, propuesta: dict,
                                    actor_user_id: uuid.UUID, actor_rol: str, ip: Optional[str]) -> dict:
    """Guarda (fusionando con la anterior) la propuesta de un admin de KIS para
    una línea viva y la audita. Devuelve la parte nueva en JSON."""
    valores = {n: validar(n, v) for n, v in propuesta.items()}
    await con.execute(
        "UPDATE lines SET parametros_propuestos = COALESCE(parametros_propuestos, '{}'::jsonb) || $2::jsonb, "
        "updated_at = now() WHERE id = $1",
        line_id, json.dumps(a_json(valores)))
    await _auditar_propuesta(con, tenant_id=tenant_id, objeto_id=line_id, tipo_objeto="line", ambito="linea",
                             actor_user_id=actor_user_id, actor_rol=actor_rol, ip=ip, valores=valores)
    return a_json(valores)


async def proponer_parametros_tenant(con: asyncpg.Connection, *, tenant_id: uuid.UUID, propuesta: dict,
                                     actor_user_id: uuid.UUID, actor_rol: str, ip: Optional[str]) -> dict:
    valores = {n: validar(n, v) for n, v in propuesta.items()}
    await con.execute(
        "UPDATE tenants SET parametros_propuestos = COALESCE(parametros_propuestos, '{}'::jsonb) || $2::jsonb, "
        "updated_at = now() WHERE id = $1",
        tenant_id, json.dumps(a_json(valores)))
    await _auditar_propuesta(con, tenant_id=tenant_id, objeto_id=tenant_id, tipo_objeto="tenant", ambito="tenant",
                             actor_user_id=actor_user_id, actor_rol=actor_rol, ip=ip, valores=valores)
    return a_json(valores)


async def consumir_propuesta_linea(con: asyncpg.Connection, line_id: uuid.UUID, nombres: list[str]) -> None:
    """Quita de la propuesta los parámetros ya consentidos; NULL si no queda nada."""
    await con.execute(
        "UPDATE lines SET parametros_propuestos = "
        "NULLIF(COALESCE(parametros_propuestos, '{}'::jsonb) - $2::text[], '{}'::jsonb) WHERE id = $1",
        line_id, nombres)


async def consumir_propuesta_tenant(con: asyncpg.Connection, tenant_id: uuid.UUID, nombres: list[str]) -> None:
    await con.execute(
        "UPDATE tenants SET parametros_propuestos = "
        "NULLIF(COALESCE(parametros_propuestos, '{}'::jsonb) - $2::text[], '{}'::jsonb) WHERE id = $1",
        tenant_id, nombres)


async def lineas_vivas_sin_consentir(con: asyncpg.Connection, valores_tenant: dict) -> list[uuid.UUID]:
    filas = await con.fetch(
        """
        SELECT l.id FROM lines l
        WHERE l.estado = 'vinculada' AND NOT EXISTS (
            SELECT 1 FROM consents c
            WHERE c.id = (SELECT c2.id FROM consents c2 WHERE c2.line_id = l.id ORDER BY c2.created_at DESC LIMIT 1)
              AND c.opciones->'parametros_tenant' @> $1::jsonb)
        ORDER BY l.created_at
        """,
        json.dumps(valores_tenant, default=str))
    return [f["id"] for f in filas]


async def cambiar_parametros_linea(con: asyncpg.Connection, *, tenant_id: uuid.UUID, line_id: uuid.UUID, nuevos: dict,
                                   actor_user_id: uuid.UUID, actor_rol: str, ip: Optional[str],
                                   solo_endurecer: bool, consentido: bool = False) -> dict:
    fila = await con.fetchrow("SELECT estado, " + ", ".join(DE_LINEA) + " FROM lines WHERE id = $1 FOR UPDATE", line_id)
    if fila is None:
        raise CambioRechazado(404, {"error": "linea_inexistente"})
    actuales = {n: fila[n] for n in DE_LINEA}
    cambios = _cambios(actuales, nuevos, DE_LINEA)
    if not cambios:
        return actuales
    laxos = _rechazar_laxos(cambios, solo_endurecer)
    if laxos and fila["estado"] == "vinculada" and not consentido:
        raise CambioRechazado(409, {"error": "requiere_consentimiento", "parametros": laxos})
    finales = {**actuales, **{n: validar(n, nuevos[n]) for n in cambios}}
    try:
        almacen = verificar_almacen(finales)
    except AlmacenNoDisponible as e:
        raise CambioRechazado(422, {"error": "almacen_no_disponible", "detalle": str(e)})
    await con.execute(
        "UPDATE lines SET duracion_vinculo_dias = $2, retencion_fuente_dias = $3, retencion_tras_desvinculo_dias = $4, "
        "tope_ia_mensual_usd = $5, almacen_fuente = $6, updated_at = now() WHERE id = $1",
        line_id, finales["duracion_vinculo_dias"], finales["retencion_fuente_dias"],
        finales["retencion_tras_desvinculo_dias"], finales["tope_ia_mensual_usd"], almacen)
    await _auditar(con, tenant_id=tenant_id, objeto_id=line_id, tipo_objeto="line", actor_user_id=actor_user_id,
                   actor_rol=actor_rol, ip=ip, ambito="linea", actuales=actuales, finales=finales, cambios=cambios)
    return finales


async def cambiar_parametros_tenant(con: asyncpg.Connection, *, tenant_id: uuid.UUID, nuevos: dict,
                                    actor_user_id: uuid.UUID, actor_rol: str, ip: Optional[str],
                                    solo_endurecer: bool) -> dict:
    fila = await con.fetchrow("SELECT " + ", ".join(DE_TENANT) + " FROM tenants WHERE id = $1 FOR UPDATE", tenant_id)
    actuales = {n: fila[n] for n in DE_TENANT}
    cambios = _cambios(actuales, nuevos, DE_TENANT)
    if not cambios:
        return actuales
    laxos = _rechazar_laxos(cambios, solo_endurecer)
    if laxos:
        pendientes = await lineas_vivas_sin_consentir(con, {n: validar(n, nuevos[n]) for n in laxos})
        if pendientes:
            raise CambioRechazado(409, {"error": "requiere_consentimiento", "parametros": laxos,
                                        "lineas": [str(x) for x in pendientes]})
    finales = {**actuales, **{n: validar(n, nuevos[n]) for n in cambios}}
    await con.execute(
        "UPDATE tenants SET retencion_fichas_meses = $2, perfil_de_datos = $3, retener_fragmentos = $4, "
        "ia_habilitada = $5, via_llm = $6, updated_at = now() WHERE id = $1",
        tenant_id, finales["retencion_fichas_meses"], finales["perfil_de_datos"], finales["retener_fragmentos"],
        finales["ia_habilitada"], finales["via_llm"])
    await _auditar(con, tenant_id=tenant_id, objeto_id=tenant_id, tipo_objeto="tenant", actor_user_id=actor_user_id,
                   actor_rol=actor_rol, ip=ip, ambito="tenant", actuales=actuales, finales=finales, cambios=cambios)
    return finales
```

- [ ] **Step 5: Router de parámetros y consentimientos**

`app/radar/routers/parametros.py`:

```python
"""
Parámetros por línea y por cuenta para el cliente (solo endurecer) y el
consentimiento por línea, único camino que aplica valores más laxos sobre
una línea viva (§2.1, §3 P2). El consentimiento acepta lo que un admin de
KIS propuso (parametros_propuestos); no es un canal para aflojar solo.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.radar import auditoria, eventos_producto
from app.radar.auth import Sesion, ip_de, requiere_rol, sesion_actual
from app.radar.consentimiento import VERSIONES, hash_texto, listar_consentimientos, registrar_consentimiento
from app.radar.contexto import contexto
from app.radar.lineas import leer_linea, propuesta_de_linea
from app.radar.parametros import DE_LINEA, DE_TENANT, ValorInvalido, validar
from app.radar.parametros_service import (CambioRechazado, a_json, cambiar_parametros_linea,
                                          cambiar_parametros_tenant, coincide_con_propuesta,
                                          consumir_propuesta_linea, consumir_propuesta_tenant,
                                          leer_parametros_tenant, leer_propuesta_tenant)

router = APIRouter(prefix="/radar/api", tags=["radar-parametros"])


class ConsentimientoIn(BaseModel):
    version_texto: str
    acepta: bool
    titular: bool
    parametros_linea: dict[str, Any] = {}
    parametros_tenant: dict[str, Any] = {}


def _http(e: CambioRechazado) -> HTTPException:
    return HTTPException(status_code=e.status, detail=e.detalle)


async def _linea_visible(con, sesion: Sesion, line_id: uuid.UUID) -> dict:
    fila = await leer_linea(con, line_id)
    if fila is None or not sesion.puede_ver_linea(line_id):
        raise HTTPException(status_code=404, detail={"error": "linea_inexistente"})
    return fila


def _validar_parciales(parciales: dict, permitidos: list[str]) -> dict:
    ajenos = [n for n in parciales if n not in permitidos]
    if ajenos:
        raise HTTPException(status_code=422, detail={"error": "valor_invalido", "detalle": f"parámetros desconocidos: {ajenos}"})
    try:
        return {n: validar(n, v) for n, v in parciales.items()}
    except ValorInvalido as e:
        raise HTTPException(status_code=422, detail={"error": "valor_invalido", "detalle": str(e)})


@router.get("/lineas/{line_id}/parametros")
async def ver_parametros(line_id: uuid.UUID, request: Request, sesion: Sesion = Depends(sesion_actual)):
    ctx = contexto(request)
    async with ctx.db.tenant_tx(sesion.tenant_id) as con:
        fila = await _linea_visible(con, sesion, line_id)
        tenant = await leer_parametros_tenant(con, sesion.tenant_id)
        propuesta_t = await leer_propuesta_tenant(con, sesion.tenant_id)
    return {"linea": a_json({n: fila[n] for n in DE_LINEA}), "estado": fila["estado"],
            "almacen_fuente": fila["almacen_fuente"], "tenant": a_json(tenant),
            "propuesta": {"linea": propuesta_de_linea(fila), "tenant": propuesta_t}}


@router.put("/lineas/{line_id}/parametros")
async def endurecer_linea(line_id: uuid.UUID, body: dict[str, Any], request: Request,
                          sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    try:
        async with ctx.db.tenant_tx(sesion.tenant_id) as con:
            await _linea_visible(con, sesion, line_id)
            finales = await cambiar_parametros_linea(
                con, tenant_id=sesion.tenant_id, line_id=line_id, nuevos=body, actor_user_id=sesion.user_id,
                actor_rol=sesion.rol, ip=ip_de(request), solo_endurecer=True)
    except CambioRechazado as e:
        raise _http(e)
    return a_json(finales)


@router.put("/cuenta/parametros")
async def endurecer_tenant(body: dict[str, Any], request: Request, sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    try:
        async with ctx.db.tenant_tx(sesion.tenant_id) as con:
            finales = await cambiar_parametros_tenant(
                con, tenant_id=sesion.tenant_id, nuevos=body, actor_user_id=sesion.user_id, actor_rol=sesion.rol,
                ip=ip_de(request), solo_endurecer=True)
    except CambioRechazado as e:
        raise _http(e)
    return a_json(finales)


@router.post("/lineas/{line_id}/consentimientos", status_code=201)
async def consentir(line_id: uuid.UUID, body: ConsentimientoIn, request: Request,
                    sesion: Sesion = Depends(requiere_rol("dueno"))):
    if not (body.acepta and body.titular):
        raise HTTPException(status_code=422, detail={"error": "consentimiento_no_aceptado"})
    if body.version_texto not in VERSIONES:
        raise HTTPException(status_code=422, detail={"error": "version_desconocida"})
    nuevos_linea = _validar_parciales(body.parametros_linea, DE_LINEA)
    nuevos_tenant = _validar_parciales(body.parametros_tenant, DE_TENANT)
    ctx = contexto(request)
    ip = ip_de(request)
    try:
        async with ctx.db.tenant_tx(sesion.tenant_id) as con:
            fila = await _linea_visible(con, sesion, line_id)
            # §2.1: el dueño acepta lo que KIS propuso; cualquier valor que no
            # coincida con la propuesta (ni siquiera el actual) se rechaza antes
            # de escribir nada.
            propuesta_l = propuesta_de_linea(fila)
            propuesta_t = await leer_propuesta_tenant(con, sesion.tenant_id)
            sin_propuesta = [n for n, v in nuevos_linea.items() if not coincide_con_propuesta(propuesta_l, n, v)] + \
                            [n for n, v in nuevos_tenant.items() if not coincide_con_propuesta(propuesta_t, n, v)]
            if sin_propuesta:
                raise HTTPException(status_code=422, detail={"error": "sin_propuesta", "parametros": sin_propuesta})
            finales_l = {**{n: fila[n] for n in DE_LINEA}, **nuevos_linea}
            finales_t = {**(await leer_parametros_tenant(con, sesion.tenant_id)), **nuevos_tenant}
            opciones = {"parametros_linea": a_json(finales_l), "parametros_tenant": a_json(finales_t)}
            cid = await registrar_consentimiento(con, tenant_id=sesion.tenant_id, line_id=line_id, user_id=sesion.user_id,
                                                 version=body.version_texto, opciones=opciones, ip=ip)
            await auditoria.registrar(con, tenant_id=sesion.tenant_id, actor_user_id=sesion.user_id, actor_rol=sesion.rol,
                                      accion="consentimiento_registrado", tipo_objeto="consent", objeto_id=cid, ip=ip)
            await eventos_producto.registrar_evento(con, tenant_id=sesion.tenant_id, evento="consentimiento_registrado",
                                                    line_id=line_id, user_id=sesion.user_id, objeto_id=cid)
            if nuevos_linea:
                await cambiar_parametros_linea(
                    con, tenant_id=sesion.tenant_id, line_id=line_id, nuevos=nuevos_linea, actor_user_id=sesion.user_id,
                    actor_rol=sesion.rol, ip=ip, solo_endurecer=False, consentido=True)
                await consumir_propuesta_linea(con, line_id, list(nuevos_linea))
            tenant_aplicado, pendientes = True, []
            if nuevos_tenant:
                try:
                    await cambiar_parametros_tenant(
                        con, tenant_id=sesion.tenant_id, nuevos=nuevos_tenant, actor_user_id=sesion.user_id,
                        actor_rol=sesion.rol, ip=ip, solo_endurecer=False)
                    await consumir_propuesta_tenant(con, sesion.tenant_id, list(nuevos_tenant))
                except CambioRechazado as e:
                    if e.status != 409:
                        raise
                    tenant_aplicado, pendientes = False, e.detalle["lineas"]   # la propuesta sigue pendiente
    except CambioRechazado as e:
        raise _http(e)
    return {"consent_id": str(cid), "hash_texto": hash_texto(body.version_texto),
            "tenant_aplicado": tenant_aplicado, "lineas_pendientes": pendientes}


@router.get("/lineas/{line_id}/consentimientos")
async def consentimientos(line_id: uuid.UUID, request: Request, sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    async with ctx.db.tenant_tx(sesion.tenant_id) as con:
        await _linea_visible(con, sesion, line_id)
        return await listar_consentimientos(con, line_id)
```

Agregar a `app/radar/routers/admin.py` (imports: `from typing import Any`; `from app.radar.parametros_service import CambioRechazado, a_json, cambiar_parametros_linea, cambiar_parametros_tenant, proponer_parametros_linea, proponer_parametros_tenant`):

```python
@router.put("/tenants/{tenant_id}/lineas/{line_id}/parametros")
async def cambiar_linea_admin(tenant_id: uuid.UUID, line_id: uuid.UUID, body: dict[str, Any], request: Request,
                              admin: Sesion = Depends(requiere_rol("admin"))):
    """Cualquier sentido. Un valor más laxo sobre una línea viva no se aplica:
    responde 409 y deja la PROPUESTA guardada (§2.1: los fija un admin de KIS;
    el dueño la acepta con su consentimiento). El 409 sale del tenant_tx con
    rollback, por eso la propuesta se guarda en una transacción propia."""
    ctx = contexto(request)
    ip = ip_de(request)
    try:
        async with ctx.db.tenant_tx(tenant_id) as con:
            finales = await cambiar_parametros_linea(
                con, tenant_id=tenant_id, line_id=line_id, nuevos=body, actor_user_id=admin.user_id,
                actor_rol=admin.rol, ip=ip, solo_endurecer=False)
    except CambioRechazado as e:
        if e.status == 409:
            async with ctx.db.tenant_tx(tenant_id) as con:
                await proponer_parametros_linea(
                    con, tenant_id=tenant_id, line_id=line_id, propuesta={n: body[n] for n in e.detalle["parametros"]},
                    actor_user_id=admin.user_id, actor_rol=admin.rol, ip=ip)
        raise HTTPException(status_code=e.status, detail=e.detalle)
    return a_json(finales)


@router.put("/tenants/{tenant_id}/parametros")
async def cambiar_tenant_admin(tenant_id: uuid.UUID, body: dict[str, Any], request: Request,
                               admin: Sesion = Depends(requiere_rol("admin"))):
    ctx = contexto(request)
    ip = ip_de(request)
    try:
        async with ctx.db.tenant_tx(tenant_id) as con:
            if await con.fetchval("SELECT count(*) FROM tenants WHERE id = $1 AND NOT es_kis", tenant_id) == 0:
                raise HTTPException(status_code=404, detail="tenant inexistente")
            finales = await cambiar_parametros_tenant(
                con, tenant_id=tenant_id, nuevos=body, actor_user_id=admin.user_id, actor_rol=admin.rol,
                ip=ip, solo_endurecer=False)
    except CambioRechazado as e:
        if e.status == 409:
            async with ctx.db.tenant_tx(tenant_id) as con:
                await proponer_parametros_tenant(
                    con, tenant_id=tenant_id, propuesta={n: body[n] for n in e.detalle["parametros"]},
                    actor_user_id=admin.user_id, actor_rol=admin.rol, ip=ip)
        raise HTTPException(status_code=e.status, detail=e.detalle)
    return a_json(finales)
```

En `app/radar/app.py`: importar `parametros` y agregar `app.include_router(parametros.router)`.

- [ ] **Step 6: Correr**

```bash
python -m pytest tests/radar_tests -q
```
Esperado: todo verde; `test_parametros_api.py` suma 12.

- [ ] **Step 7: Commit**

```bash
git add app/radar tests/radar_tests/test_parametros_api.py
git commit -m "Radar: cambios de parametros con regla de estrictez y consentimientos por linea"
```

---

### Task 13: Soporte KIS con vencimiento, visible y auditado

**Files:**
- Create: `app/radar/routers/soporte.py`
- Modify: `app/radar/routers/admin.py` (`POST /tenants/{tenant_id}/sesion-soporte`), `app/radar/app.py`
- Test: `tests/radar_tests/test_soporte.py`

**Interfaces:**
- Consumes: tabla `support_grants`, `emitir_sesion`, `set_cookie_sesion`, `auditoria`.
- Produces: `POST /radar/api/soporte` (dueño; body `{horas: 24..72}`; 201 `{id, created_at, expires_at, otorgado_por}`); `GET /radar/api/soporte` (cualquier rol; grants vigentes); `DELETE /radar/api/soporte/{grant_id}` (dueño; revoca el grant y toda sesión `soporte` del tenant; 404 si no existe o ya fue revocado); `POST /radar/admin/tenants/{tenant_id}/sesion-soporte` (admin; 403 `{"error": "sin_grant"}` sin grant vigente; 200 `{tenant_id, rol: "soporte", expires_at}` + cookie de sesión de ese tenant que vence con el grant).

- [ ] **Step 1: Tests (fallan)**

`tests/radar_tests/test_soporte.py`:

```python
"""
Soporte KIS (§4.4): sin acceso por defecto; el dueño lo otorga con
vencimiento de 24–72 h, el cliente lo ve, y cada acceso queda auditado.
"""
import uuid

from app.radar.constantes import TENANT_KIS

from .helpers import DOMINIO_COOKIE, crear_linea_directa, crear_tenant_directo, crear_usuario, entrar


async def _base(ctx):
    a = await crear_tenant_directo(ctx.db, "A")
    d = await crear_usuario(ctx.db, a, "dueno@cliente.com", "dueno")
    await crear_linea_directa(ctx.db, a, "Centro")
    adm = await crear_usuario(ctx.db, TENANT_KIS, "soporte@keepitsimple.com.ar", "admin")
    return a, d, adm


async def test_dueno_otorga_y_el_cliente_lo_ve(cliente, radar_ctx):
    a, d, adm = await _base(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    for horas in (12, 73, 0):
        assert (await cliente.post("/radar/api/soporte", json={"horas": horas})).status_code == 422
    r = await cliente.post("/radar/api/soporte", json={"horas": 48})
    assert r.status_code == 201 and r.json()["otorgado_por"] == str(d)
    gid = r.json()["id"]
    async with radar_ctx.db.tenant_tx(a) as con:
        fila = await con.fetchrow("SELECT detalle FROM access_audit_log WHERE accion = 'soporte_otorgado'")
        assert '"horas": 48' in fila["detalle"]
    le = await crear_usuario(radar_ctx.db, a, "lector@cliente.com", "lector")
    await entrar(cliente, radar_ctx, a, le, "lector")
    r = await cliente.get("/radar/api/soporte")
    assert r.status_code == 200 and [g["id"] for g in r.json()] == [gid]
    assert (await cliente.post("/radar/api/soporte", json={"horas": 24})).status_code == 403
    assert (await cliente.delete(f"/radar/api/soporte/{gid}")).status_code == 403


async def test_admin_sin_grant_no_entra(cliente, radar_ctx):
    a, d, adm = await _base(radar_ctx)
    await entrar(cliente, radar_ctx, TENANT_KIS, adm, "admin")
    r = await cliente.post(f"/radar/admin/tenants/{a}/sesion-soporte")
    assert r.status_code == 403 and r.json()["detail"] == {"error": "sin_grant"}
    async with radar_ctx.db.tenant_tx(a) as con:     # vencido tampoco
        await con.execute("INSERT INTO support_grants (otorgado_por, created_at, expires_at) "
                          "VALUES ($1, now() - interval '3 days', now() - interval '1 day')", d)
    assert (await cliente.post(f"/radar/admin/tenants/{a}/sesion-soporte")).status_code == 403


async def test_acceso_de_soporte_con_grant(cliente, radar_ctx):
    a, d, adm = await _base(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    gid = (await cliente.post("/radar/api/soporte", json={"horas": 24})).json()["id"]

    await entrar(cliente, radar_ctx, TENANT_KIS, adm, "admin")
    r = await cliente.post(f"/radar/admin/tenants/{a}/sesion-soporte")
    assert r.status_code == 200, r.text
    assert r.json()["rol"] == "soporte" and r.json()["tenant_id"] == str(a)
    assert "radar_sesion" in r.headers["set-cookie"]

    yo = (await cliente.get("/radar/api/yo")).json()
    assert yo == {"user_id": str(adm), "tenant_id": str(a), "rol": "soporte", "email": None,
                  "es_kis": False, "lineas_permitidas": None}
    assert [x["nombre"] for x in (await cliente.get("/radar/api/lineas")).json()] == ["Centro"]
    assert (await cliente.post("/radar/api/usuarios", json={"email": "x@cliente.com", "rol": "lector"})).status_code == 403
    assert (await cliente.get("/radar/admin/tenants")).status_code == 403     # ya no es una sesión de admin

    async with radar_ctx.db.tenant_tx(a) as con:
        fila = await con.fetchrow("SELECT actor_user_id, objeto_id FROM access_audit_log WHERE accion = 'acceso_soporte'")
        assert fila["actor_user_id"] == adm and fila["objeto_id"] == uuid.UUID(gid)
        vence = await con.fetchrow("SELECT s.expires_at = g.expires_at AS igual FROM sessions s, support_grants g "
                                   "WHERE s.rol = 'soporte' AND g.id = $1", uuid.UUID(gid))
        assert vence["igual"] is True


async def test_revocar_grant_cierra_las_sesiones_de_soporte(cliente, radar_ctx):
    a, d, adm = await _base(radar_ctx)
    await entrar(cliente, radar_ctx, a, d, "dueno")
    gid = (await cliente.post("/radar/api/soporte", json={"horas": 24})).json()["id"]
    await entrar(cliente, radar_ctx, TENANT_KIS, adm, "admin")
    await cliente.post(f"/radar/admin/tenants/{a}/sesion-soporte")
    # el Set-Cookie del servidor reemplazó a la cookie manual (mismo dominio efectivo): un solo valor
    cookie_soporte = cliente.cookies.get("radar_sesion")

    await entrar(cliente, radar_ctx, a, d, "dueno")
    r = await cliente.delete(f"/radar/api/soporte/{gid}")
    assert r.status_code == 200
    assert (await cliente.get("/radar/api/soporte")).json() == []
    assert (await cliente.delete(f"/radar/api/soporte/{gid}")).status_code == 404

    cliente.cookies.clear()
    cliente.cookies.set("radar_sesion", cookie_soporte, domain=DOMINIO_COOKIE, path="/radar")
    assert (await cliente.get("/radar/api/yo")).status_code == 401
    async with radar_ctx.db.tenant_tx(a) as con:
        assert await con.fetchval("SELECT count(*) FROM access_audit_log WHERE accion = 'soporte_revocado'") == 1


async def test_grant_de_otro_tenant_no_sirve(cliente, radar_ctx):
    a, d, adm = await _base(radar_ctx)
    b = await crear_tenant_directo(radar_ctx.db, "B")
    await entrar(cliente, radar_ctx, a, d, "dueno")
    await cliente.post("/radar/api/soporte", json={"horas": 24})
    await entrar(cliente, radar_ctx, TENANT_KIS, adm, "admin")
    assert (await cliente.post(f"/radar/admin/tenants/{b}/sesion-soporte")).status_code == 403
```

- [ ] **Step 2: Correr y ver la falla**

```bash
python -m pytest tests/radar_tests/test_soporte.py -q
```
Esperado: `5 failed` (404 en `/radar/api/soporte` y en `sesion-soporte`).

- [ ] **Step 3: Router de soporte**

`app/radar/routers/soporte.py`:

```python
"""
support_grants (§4.4): el dueño otorga acceso a Soporte KIS por 24–72 h; es
visible para el cliente, revocable y auditado. Un grant es por tenant, no por
persona de KIS: el dueño no conoce a los usuarios de KIS.
"""

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.radar import auditoria
from app.radar.auth import Sesion, ip_de, requiere_rol, sesion_actual
from app.radar.contexto import contexto

router = APIRouter(prefix="/radar/api/soporte", tags=["radar-soporte"])


class GrantIn(BaseModel):
    horas: int = Field(ge=24, le=72)


def _grant_json(f) -> dict:
    return {"id": str(f["id"]), "created_at": f["created_at"].isoformat(), "expires_at": f["expires_at"].isoformat(),
            "otorgado_por": str(f["otorgado_por"])}


@router.post("", status_code=201)
async def otorgar(body: GrantIn, request: Request, sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    async with ctx.db.tenant_tx(sesion.tenant_id) as con:
        fila = await con.fetchrow(
            "INSERT INTO support_grants (otorgado_por, expires_at) VALUES ($1, now() + $2::interval) "
            "RETURNING id, created_at, expires_at, otorgado_por",
            sesion.user_id, timedelta(hours=body.horas))
        await auditoria.registrar(con, tenant_id=sesion.tenant_id, actor_user_id=sesion.user_id, actor_rol=sesion.rol,
                                  accion="soporte_otorgado", tipo_objeto="support_grant", objeto_id=fila["id"],
                                  ip=ip_de(request), detalle={"horas": body.horas})
    return _grant_json(fila)


@router.get("")
async def vigentes(request: Request, sesion: Sesion = Depends(sesion_actual)):
    ctx = contexto(request)
    async with ctx.db.tenant_tx(sesion.tenant_id) as con:
        filas = await con.fetch(
            "SELECT id, created_at, expires_at, otorgado_por FROM support_grants "
            "WHERE revocado_at IS NULL AND expires_at > now() ORDER BY created_at")
    return [_grant_json(f) for f in filas]


@router.delete("/{grant_id}")
async def revocar(grant_id: uuid.UUID, request: Request, sesion: Sesion = Depends(requiere_rol("dueno"))):
    ctx = contexto(request)
    async with ctx.db.tenant_tx(sesion.tenant_id) as con:
        revocado = await con.fetchval(
            "UPDATE support_grants SET revocado_at = now() WHERE id = $1 AND revocado_at IS NULL RETURNING id", grant_id)
        if revocado is None:
            raise HTTPException(status_code=404, detail="grant inexistente o ya revocado")
        await con.execute("UPDATE sessions SET revoked_at = now() WHERE rol = 'soporte' AND revoked_at IS NULL")
        await auditoria.registrar(con, tenant_id=sesion.tenant_id, actor_user_id=sesion.user_id, actor_rol=sesion.rol,
                                  accion="soporte_revocado", tipo_objeto="support_grant", objeto_id=grant_id,
                                  ip=ip_de(request))
    return {"ok": True}
```

Agregar a `app/radar/routers/admin.py` (imports: `from fastapi.responses import JSONResponse`; `from app.radar.auth import ..., emitir_sesion, set_cookie_sesion`):

```python
@router.post("/tenants/{tenant_id}/sesion-soporte")
async def sesion_soporte(tenant_id: uuid.UUID, request: Request, admin: Sesion = Depends(requiere_rol("admin"))):
    """Con un grant vigente, emite una sesión de ese tenant con rol `soporte`
    que vence con el grant. Reemplaza la cookie de admin: para volver a
    /radar/admin hay que pedir un link mágico de nuevo."""
    ctx = contexto(request)
    ip = ip_de(request)
    async with ctx.db.tenant_tx(tenant_id) as con:
        grant = await con.fetchrow(
            "SELECT id, expires_at, expires_at - now() AS resta FROM support_grants "
            "WHERE revocado_at IS NULL AND expires_at > now() ORDER BY expires_at DESC LIMIT 1")
        if grant is None:
            raise HTTPException(status_code=403, detail={"error": "sin_grant"})
        token = await emitir_sesion(con, tenant_id=tenant_id, user_id=admin.user_id, rol="soporte", ip=ip,
                                    duracion=grant["resta"])
        await auditoria.registrar(con, tenant_id=tenant_id, actor_user_id=admin.user_id, actor_rol="admin",
                                  accion="acceso_soporte", tipo_objeto="support_grant", objeto_id=grant["id"], ip=ip)
    resp = JSONResponse({"tenant_id": str(tenant_id), "rol": "soporte", "expires_at": grant["expires_at"].isoformat()})
    set_cookie_sesion(resp, ctx, tenant_id, token, max_age=int(grant["resta"].total_seconds()))
    return resp
```

En `app/radar/app.py`: importar `soporte` y agregar `app.include_router(soporte.router)`. El bloque final queda:

```python
    for r in (health, login, cuenta, parametros, soporte, admin):
        app.include_router(r.router)
```

- [ ] **Step 4: Correr**

```bash
python -m pytest tests/radar_tests -q
```
Esperado: todo verde; `test_soporte.py` suma 5.

- [ ] **Step 5: Commit**

```bash
git add app/radar tests/radar_tests/test_soporte.py
git commit -m "Radar: acceso de soporte KIS otorgado por el dueno, con vencimiento y auditado"
```

---

### Task 14: Documentación operativa y corrida completa

**Files:**
- Create: `docs/radar-despliegue.md`
- Modify: `DEVELOPMENT.md`, `.env.example`
- Test: la suite completa (`python -m pytest -q`).

**Interfaces:**
- Consumes: todo lo anterior.
- Produces: documentación de despliegue de Radar (variables, roles, secretos, arranque, primer admin, health) y la sección de Radar en `DEVELOPMENT.md`.

- [ ] **Step 1: `docs/radar-despliegue.md`**

```markdown
# Radar — despliegue propio (tramo 1)

Radar corre con la misma imagen que el bot, en un servicio de Railway aparte,
con `APP_MODE=radar`, Postgres y Redis propios. Nunca comparte la base de un
cliente de Remedia (spec §6.2, S7).

## Variables

| Variable | Qué es |
|---|---|
| `APP_MODE=radar` | Monta los routers de Radar y ninguno del bot. Por defecto `bot`. |
| `RADAR_MIGRATOR_DATABASE_URL` | Rol dueño de las tablas (en Railway, el usuario por defecto). Solo lo usa Alembic en el arranque. |
| `RADAR_DATABASE_URL` | Rol `radar_app`: `NOSUPERUSER`, `NOBYPASSRLS`, no dueño. La app aborta si conecta con un superusuario. |
| `RADAR_FUENTE_DATABASE_URL` | Postgres del almacén de fuente permanente (servidor distinto del de resultados, con backups). En este tramo solo tiene `fuente_meta`. |
| `RADAR_SECRETS_DIR` | Directorio de `FileSecretStore` para las `k_tenant`: un volumen de Railway montado ahí, fuera de los backups de Postgres. |
| `RADAR_COOKIE_SECRET` | 32+ caracteres aleatorios para firmar la cookie de sesión (`python -c "import secrets; print(secrets.token_urlsafe(48))"`). Con menos de 32 la app no arranca. |
| `RADAR_COOKIE_SECURE` | `true` en producción (default). `false` solo para http local. |
| `RADAR_PUBLIC_BASE_URL` | Base de los links mágicos, ej. `https://radar.keepitsimple.com.ar`. |
| `RADAR_MAILER` | `log` (default: loguea solo dominio y huella del token) o `memoria` (tests). El proveedor de producción está por definir. |

## Primera vez

1. Crear los roles en el Postgres de resultados, como superusuario:
   `psql "$URL_SUPERUSUARIO" -v migrator=postgres -f scripts/radar_bootstrap_roles.sql`
   y fijar la contraseña de `radar_app` con `ALTER ROLE radar_app PASSWORD '...'`.
2. Arrancar el servicio: el lifespan corre `alembic upgrade head` sobre
   `migrations_radar/` (tabla `alembic_version_radar`) y `migrations_fuente/`
   (`alembic_version_fuente`). Si una migración falla, el servicio no arranca.
3. Crear el primer admin de KIS desde la shell del servicio:
   `python scripts/radar_admin.py crear-admin --email mariano@keepitsimple.com.ar --nombre "Mariano"`
   Imprime una sola vez el link de invitación (7 días, un solo uso).
4. `GET /health` responde `{"status": "ok", "modo": "radar", "resultados": {...}, "fuente": {...}}`.

## Secretos

`k_tenant` (32 bytes por tenant) vive en `RADAR_SECRETS_DIR`, un archivo por
tenant, permisos 0600. No está en Postgres ni en sus backups. Se destruye
solo en la baja del cliente (tramo 6). Si se pierde el volumen, los
`contact_hmac` existentes quedan sin correspondencia: hacer backup del
volumen por separado y cifrado.

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

## Qué NO existe todavía

WAHA y vínculos (tramo 2), ingesta y tablas de conversación en el almacén de
fuente (tramo 3), KPI (4), IA (5), purga, supresiones, "borrar todo", emails
de hallazgos y el almacén purgable (6). Ver el plan
`docs/superpowers/plans/2026-09-21-radar-tramo1-acceso-y-datos.md`.
```

- [ ] **Step 2: `DEVELOPMENT.md` y `.env.example`**

Agregar al final de `DEVELOPMENT.md`:

````markdown
## Radar (APP_MODE=radar)

Radar es un despliegue aparte con la misma imagen. Sus migraciones viven en
`migrations_radar/` (tabla `alembic_version_radar`) y `migrations_fuente/`
(`alembic_version_fuente`); **no** corren en el arranque del bot y nunca
tocan `DATABASE_URL`.

```bash
python -m pytest tests/radar_tests -q      # pgserver + roles reales de RLS
alembic -c alembic_radar.ini -x url=postgresql://... upgrade head   # a mano, si hace falta
```

Los tests crean `radar_migrator`, `radar_app` y `radar_admin` en el Postgres
embebido y conectan como `radar_app`: sin eso los tests de RLS no probarían
nada. Ver `docs/radar-despliegue.md`.
````

Agregar al final de `.env.example`:

```ini
# ── Radar (solo con APP_MODE=radar; ver docs/radar-despliegue.md) ────────────
APP_MODE=bot
RADAR_MIGRATOR_DATABASE_URL=
RADAR_DATABASE_URL=
RADAR_FUENTE_DATABASE_URL=
RADAR_SECRETS_DIR=/data/radar-secrets
RADAR_COOKIE_SECRET=
RADAR_PUBLIC_BASE_URL=
RADAR_MAILER=log
```

- [ ] **Step 3: Corrida completa**

```bash
python -m pytest -q
```
Esperado: toda la suite en verde, la del bot y la de Radar (`tests/radar_tests`: 5 + 10 + 8 + 30 + 44 + 12 + 19 + 13 + 10 + 16 + 6 + 12 + 5 = 190 tests; en Windows uno `skipped`).

- [ ] **Step 4: Commit**

```bash
git add docs/radar-despliegue.md DEVELOPMENT.md .env.example
git commit -m "Radar: documentacion de despliegue del tramo 1"
```

---

## Self-Review

### (a) Cobertura del spec (tramo 1 de §8)

| Requisito del spec | Dónde queda |
|---|---|
| Despliegue propio; el router del bot no se monta (§6.2, S7) | Task 1 (`APP_MODE`, `crear_app`), Task 3 (lifespan de radar) |
| Tablas de Radar fuera de la base de un cliente del bot | Task 2 (`migrations_radar/`, `alembic_version_radar`, `alembic_env.correr` sin `DATABASE_URL`), test `test_resultados_no_usa_la_tabla_de_version_del_bot` |
| Tenants | Task 2 (`tenants`, `radar_admin_crear_tenant`, `radar_admin_listar_tenants`), Task 10 (alta) |
| Líneas con parámetros y estado `vinculada|sin_vinculo|de_baja` | Task 6 (`lines` con `CHECK`), Task 10 (`crear_linea`), Task 11 (`GET /lineas`) |
| `lines.almacen_fuente` (`permanente|purgable`, default `permanente`) | Task 6 (columna + `CHECK` de coherencia), Task 10 (`verificar_almacen`) |
| Usuarios, membresías, roles `admin` (KIS), `dueno`, `gestor`, `lector` | Task 6 (`users`, `memberships`, `CHECK` de admin solo en KIS), Task 11 (invitación, cambio de rol, `lineas_permitidas`) |
| `support_grants` (Soporte KIS otorgado por el dueño, 24–72 h, visible, auditado) | Task 6 (tabla), Task 13 (endpoints y sesión `soporte`) |
| Login por email con link mágico de un solo uso y con vencimiento | Task 8 (tokens), Task 9 (`/radar/login`, token en el fragmento, canje por POST sin auto-envío y con CSP, `UPDATE … WHERE used_at IS NULL`) |
| Cookie HttpOnly, Secure, SameSite atada al tenant; nunca tokens en la URL salvo el link | Task 8 (`armar_cookie`, `set_cookie_sesion`, test del atributo `Secure`; `abrir_cookie` valida la forma antes de `compare_digest`), Task 9 |
| Alta por invitación: admin crea tenant + línea con parámetros e invita al dueño (P0) | Task 10 (`crear_tenant_con_dueno`, `POST /radar/admin/tenants`, script del primer admin) |
| `consents` por línea: versión, hash, fecha, IP, opciones | Task 6 (tabla), Task 12 (`consentimiento.py`, `POST …/consentimientos`); la IP es el último salto de `X-Forwarded-For` (Task 8, `ip_de`) |
| Aflojar sobre línea viva exige consentimiento; endurecer se aplica directo y auditado; el cliente solo endurece; "los fija un admin de KIS"; orden "más estricto" de §2.1 | Task 5 (`comparar`, `es_mas_estricto`), Task 12 (`cambiar_parametros_linea/_tenant`; el 409 del admin guarda `parametros_propuestos` y el consentimiento solo acepta esa propuesta: `coincide_con_propuesta`, `422 sin_propuesta`; tests `test_consentimiento_solo_acepta_lo_propuesto_por_kis` y `test_gestor_y_lector_no_cambian_parametros`) |
| Perfil `sensible` propone (no fuerza) valores más estrictos (§7: IA apagada por defecto, vía sincrónica); rubro propone el perfil | Task 5 (`propuesta_para_perfil`, `perfil_por_rubro`), Task 10 (`GET /radar/admin/propuesta`; `resolver_valores_tenant` parte de la propuesta del perfil y el admin la pisa con valores explícitos; `retencion_fuente_dias = 7` queda como recomendación visible hasta que exista el almacén purgable, Decisión 5) |
| Tabla de parámetros exacta (nombres, ámbitos, iniciales) | Task 5 (`PARAMETROS`, test `test_tabla_exacta_del_spec`), Task 2/6 (columnas en `tenants`/`lines`) |
| `access_audit_log` sin teléfonos, JID, nombres ni texto | Task 6 (`CHECK` jsonpath), Task 7 (`validar_detalle` con lista blanca); registra cambios y propuestas de parámetros, cambios de rol, accesos de KIS |
| Escapar todo texto y aplicar CSP (§7) | Task 9: la única página HTML de este tramo (`GET /radar/login/canjear`) no interpola entrada de usuario (solo un UUID validado) y lleva `Content-Security-Policy: default-src 'none'; script-src 'sha256-…'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'` |
| RLS efectiva: dos roles, `ENABLE`+`FORCE`, políticas sobre `app.tenant_id`, `tenant_tx`, prohibición de `execute()/fetch()` | Task 2 (`RadarDB`, `politica_por_tenant`, fixture con roles), Task 6 (todas las tablas) |
| Tests obligatorios de RLS: conexión devuelta al pool sin tenant; A no lee a B ni por SQL directo; sin tenant cero filas | Task 2 (`test_rls.py`), Task 6 (`test_rls_en_tabla_con_datos`) |
| Camino explícito y auditado para operaciones cruzadas de admins de KIS | Task 2 (`SECURITY DEFINER` de `radar_admin`), Task 10 (`tenants_listados`, `tenant_creado`) |
| Almacén de fuente separado: conexión, config, health, asignación por línea | Task 3 (`FuenteStore`, `f0001`, `/health`), Task 6/10 (`almacen_fuente`) |
| `k_tenant` fuera de la base; `contact_hmac` y `lid_hmac` HMAC-SHA256; E.164 | Task 4 (`secrets.py`, `telefonos.py`), Task 10 (se crea con cada tenant) |
| `product_events` sin texto libre (test de esquema) | Task 6 (`CHECK` jsonpath), Task 7 (`validar_valores`) |
| `tenant_id NOT NULL` en todas las tablas | Task 6 (test `test_todas_las_tablas_tienen_tenant_id_not_null_y_rls_forzada`) |
| Emails solo con conteos y link autenticado; sin token en logs | Task 8 (`LogMailer`), Task 9 (`TEXTOS`) |
| Ids UUID opacos; nada de f-strings con entrada de usuario en SQL | Todas las tareas: parámetros `$n`; las únicas interpolaciones son nombres de columna de `DE_LINEA`/`DE_TENANT` y de tabla en migraciones |

**Fuera de este plan (y dónde cae):**

- Todo WAHA: `links`, `link_status_events`, `waha_workers`, gestor de sesiones, QR, código de vinculación, verificación posterior a la creación, estados P1–P3, reconexión, sesión muda, salud de cuenta, job de fin de vínculo, `estado = vinculada` real → **tramo 2**. (En este tramo `vinculada` solo se fuerza en tests para probar la regla de consentimiento.)
- Webhook `/webhook/waha`, `webhook_inbox`, pasada de conteo, backfill, reconciliación, `observado_hasta`, P4, tablas `wa_chats`, `wa_messages`, `conversations`, `wa_message_bodies`, `wa_message_provider_ids`, `wa_contact_identities`, `suppressions`, roles del almacén de fuente, uso real de `contact_hmac`/`lid_hmac` → **tramo 3**.
- E0, KPI 1–5, bandeja de pendientes, P5, `conversation_facts` → **tramo 4**.
- E1–E4, `analysis_runs`, `conversation_analysis`, `analysis_evidence`, `faq_clusters`, `findings`, `analysis_feedback`, drill-down, corrección, revisión guiada, tope de IA → **tramo 5**.
- Almacén `fuente_purgable`, purga de fuente, fichas sin texto, vencimiento de fichas, `kpi_period_snapshots`, `retencion_tras_desvinculo_dias` en acción, "Suprimir contacto", "Desconectar y borrar todo", baja de línea/tenant (borrado a los 30 días, destrucción de `k_tenant`), recordatorio semestral, emails de hallazgos, purga de `access_audit_log` a 24 meses, proveedor de email de producción, reescritura de doc1/doc3, dictamen → **tramo 6**.
- Tabla `jobs` y proceso `worker` (§6.2): no hay trabajo asincrónico en este tramo → **tramo 2/3**.
- UI del dashboard y pantallas P0–P6: en este tramo solo hay API (`/radar/api/yo`, `/lineas`, …) y la página mínima de canje del link → **tramos 2+**.
- Claves Redis con prefijo `t:{tenant}:`: Radar no usa Redis en este tramo → **tramo 3**.

### (b) Barrido de placeholders

Se buscó en el plan (después de aplicar las correcciones de la revisión) `TBD`, `TODO`, `implement later`, `similar to`, `add appropriate`, `write tests for`, `…` dentro de bloques de código y pasos sin código. No hay ninguno (los `...` que quedan son cuerpos de `Protocol`, Ellipsis válido de Python; los `…` restantes están en prosa o en strings de documentación): cada paso de código muestra el archivo completo (o el fragmento exacto a insertar con su ubicación), cada comando trae su salida esperada y todo tipo o función que se usa está definido en alguna tarea anterior o en la misma. Se verificaron además, contra el entorno real, tres puntos que la revisión señaló: (1) con httpx 0.27.2/0.28.1 y `base_url="http://testserver"`, una cookie seteada a mano con `domain="testserver"` nunca viaja y una sin `domain` provoca `CookieConflict` tras un `Set-Cookie` del servidor, mientras que `domain="testserver.local"` viaja y es reemplazada por la del servidor (de ahí `DOMINIO_COOKIE`); (2) el `CMD` del Dockerfile y el `Procfile` arrancan uvicorn con el access log por defecto, que escribe `path?query` (de ahí el token en el fragmento); (3) `COALESCE(jsonb, '{}') || $2::jsonb`, `jsonb - $2::text[]` y `NULLIF(…, '{}'::jsonb)` funcionan con asyncpg pasando `json.dumps(...)` y una `list[str]`, y Postgres devuelve `'{"duracion_vinculo_dias": 0}'` y `'{"ia_habilitada": true}'` con exactamente ese espaciado (lo que asumen las aserciones).

### (c) Consistencia de nombres y tipos entre tareas

- `RadarDB.tenant_tx(tenant_id: uuid.UUID)` (Task 2) es lo que usan todos los servicios y routers; `sin_tenant()` solo en `pedir_link`, `listar_tenants`, `salud()` y tests.
- `RadarContexto` se define en Task 3 con `settings, db, fuente` y se redefine completa en Task 8 con `secretos, mailer`; `construir_contexto` y la fixture `radar_ctx` se actualizan en el mismo paso.
- `politica_por_tenant`, `grants_app`, `definir_funcion_admin` (Task 2) son los únicos generadores de SQL de RLS; r0002 (Task 6) los consume con los mismos nombres.
- `radar_tenant_actual()` (r0001) es el default de `tenant_id` en r0002 y la base de todas las políticas.
- `generar_token()/hash_token()` (Task 8) siguen la firma de `app/services/branch_auth.py` y se usan en `links.py`, `login.py` y `admin.py`.
- `Sesion` (Task 8) es el tipo que reciben `requiere_rol`, `crear_tenant_con_dueno(actor: Sesion)` y todos los routers; `Sesion.email` es `Optional[str]` porque la sesión de soporte (Task 13) no puede leer `users` del tenant KIS.
- `enviar_link(ctx, *, tenant_id, user_id, email, proposito, ip) -> bool` (Task 9) se usa igual en `login.py`, `admin_kis.py`, `cuenta.py` y `admin.py`.
- `CambioRechazado(status, detalle)` (Task 12) se traduce a `HTTPException(status, detail=detalle)` en `parametros.py` y `admin.py`; los tests comparan `r.json()["detail"]` con esos dicts. En `admin.py` el 409 además llama a `proponer_parametros_linea/_tenant` en un `tenant_tx` propio (el que lanzó hizo rollback).
- `parametros_propuestos` existe en `tenants` (r0001) y `lines` (r0002) con el mismo tipo y `CHECK`; lo leen `lineas.propuesta_de_linea(fila)` (columna incluida en `lineas.COLUMNAS`) y `parametros_service.leer_propuesta_tenant`, lo escriben `proponer_parametros_*` y lo vacían `consumir_propuesta_*`; el router `parametros.py` usa exactamente esos nombres y `coincide_con_propuesta`.
- `auditoria.registrar(...)` (Task 7) recibe solo acciones de `ACCIONES` y tipos de `TIPOS_OBJETO`; toda acción usada en Tasks 9–13 (`login_canjeado`, `sesion_cerrada`, `tenant_creado`, `tenants_listados`, `linea_creada`, `usuario_invitado`, `invitacion_reenviada`, `rol_cambiado`, `parametro_cambiado`, `parametro_propuesto`, `consentimiento_registrado`, `soporte_otorgado`, `soporte_revocado`, `acceso_soporte`) está en esa lista, y toda clave de `detalle` (`rol_nuevo`, `rol_anterior`, `parametro`, `valor_anterior`, `valor_nuevo`, `ambito`, `proposito`, `cantidad`, `horas`) está en `CLAVES_DETALLE`. `_auditar_propuesta` pasa el valor validado (Decimal, no str) para que `valor_parametro` lo acepte.
- `DOMINIO_COOKIE` se define una sola vez en `tests/radar_tests/helpers.py` (Task 9) y lo importan `test_login.py`, `test_cuenta.py` y `test_soporte.py`, los únicos tests que setean una cookie a mano fuera de `entrar`.
- `LINK` (regex del link mágico con `#k=`) aparece con el mismo patrón en `test_login.py`, `test_admin.py` y `test_cuenta.py`, y coincide con `links.url_canje`.
- `validar_settings` (Task 3) exige `cookie_secret` de 32+ caracteres; la fixture `radar_ctx`, `test_lifespan_migra_y_arma_el_contexto`, `test_script_crear_admin_imprime_el_link` y `test_auth.SECRETO` usan el mismo `"secreto-de-test-de-32-caracteres!"` (33).
- `eventos_producto.EVENTOS` incluye exactamente los cuatro eventos emitidos (`invitacion_enviada`, `login_canjeado`, `consentimiento_registrado`, `linea_creada`).
- Nombres de tabla y columna coinciden con §6.4/§2.1: `tenants`, `users`, `memberships`, `login_tokens`, `consents`, `support_grants`, `access_audit_log`, `lines`, `product_events`; parámetros `duracion_vinculo_dias`, `retencion_fuente_dias`, `retencion_tras_desvinculo_dias`, `retencion_fichas_meses`, `tope_ia_mensual_usd`, `perfil_de_datos`, `retener_fragmentos`, `ia_habilitada`, `via_llm`; estados `vinculada|sin_vinculo|de_baja`; almacenes `permanente|purgable`.
- Helpers de test (`crear_tenant_directo`, `crear_usuario`, `crear_linea_directa`, `entrar`) se definen en Task 6 y Task 9 y se usan con la misma firma en Tasks 7–13.

### Decisiones que el spec no cerraba (para el dueño)

1. Proveedor de email transaccional de producción (`RADAR_MAILER` solo acepta `log|memoria`).
2. Dónde viven las `k_tenant` en producción: este plan usa un volumen de Railway (`FileSecretStore`); un gestor de secretos externo sería otra implementación de `SecretStore`.
3. `tope_ia_mensual_usd` arranca en `NULL` (sin tope) hasta que el piloto lo fije.
4. Los admins de KIS viven en un tenant fijo (`TENANT_KIS`) y operan sobre clientes por `/radar/admin/*`; una sesión de soporte reemplaza la cookie de admin.
5. Aflojar un parámetro de tenant con líneas vivas exige un consentimiento por cada línea vinculada, y todo consentimiento acepta solo lo que un admin propuso (decisión 9). Si el dueño quiere un valor más laxo, lo pide a KIS y KIS lo propone: no hay autoservicio.
6. Un tenant `sensible` puede recibir `ia_habilitada = true` explícito en el alta (o después, por `/radar/admin`): el código no verifica la cláusula contractual ni el dictamen de §7; eso queda a cargo del admin de KIS que lo activa.
7. Radar asume un único proxy confiable (Railway) para `X-Forwarded-For`. Si el despliegue cambia (otro proxy delante), hay que revisar `ip_de`.
8. La página de canje del link exige JavaScript (copia `#k` al formulario) y no auto-envía: un click más para la persona, a cambio de que ningún escáner de correo consuma el token.
