# Runbook: segundo servidor WAHA (GOWS) para el spike de Radar

**Para:** Mariano · **Fecha:** 2026-09-21 · **Alcance:** solo el contenedor de staging del spike (§8 del spec de Radar). No es la topología de producción.

Todos los nombres de variables están verificados contra la documentación de WAHA (`waha.devlike.pro/docs`) y, donde la documentación se contradice, contra el código fuente (`devlikeapro/waha`, rama `core`, 2026-09-09).

## Qué vamos a levantar

- Un segundo contenedor de WAHA, motor **GOWS**, misma versión que el actual (2026.8.2).
- Separado de la instancia que tiene tu línea personal: otro servicio, otro volumen, otra clave.
- Endurecido para uso de solo lectura.
- Con el límite de historial de 90 días en la primera vinculación, para medir completitud y si WhatsApp respeta el límite. Una segunda vinculación sin límite es opcional.

## Por qué un contenedor aparte

- El motor es del servidor, no de la sesión: la comparación NOWEB vs. GOWS exige otro contenedor.
- Varias variables (presencia, `ignore`, medios) son globales y reinician el contenedor.
- La clave admin de un servidor ve todas sus sesiones. Tu línea personal no tiene que estar en un servidor de experimentos.

## Paso 1 — Generar la clave de API

WAHA acepta la clave hasheada, así la clave en claro no vive en Railway. Corré esto en Git Bash:

```bash
key=$(openssl rand -hex 32); echo "CLAVE EN CLARO (guardala en tu gestor de contraseñas): $key"; echo "WAHA_API_KEY=sha512:$(printf '%s' "$key" | sha512sum | cut -d' ' -f1)"
```

- La línea `WAHA_API_KEY=sha512:...` va a Railway.
- La clave en claro va a tu gestor de contraseñas y, más adelante, a la variable `WAHA_ADMIN_KEY` del arnés del spike. No la pegues en el chat.
- **Trampa:** si la clave o la contraseña del dashboard coinciden con un valor "común" (`admin`, `waha`, `123`, 32 ceros, el sha512 de ejemplo de la documentación), WAHA la descarta sin avisar y genera una al azar en cada arranque.

Generá también una contraseña larga para el dashboard, aunque quede deshabilitado:

```bash
openssl rand -base64 24
```

## Paso 2 — Crear el servicio en Railway

1. En el proyecto de Railway que elijas, **New → Empty Service**.
2. En **Settings → Source**, elegí *Docker Image* y poné:
   `devlikeapro/waha:gows-2026.8.2`
   - No existe el tag `2026.8.2` a secas. El tag `gows-` no trae Chromium y pesa ~300 MB menos.
   - Si preferís la imagen completa: `devlikeapro/waha:latest-2026.8.2`. Las dos traen GOWS.
3. En **Settings → Volumes → Add Volume**, montá un volumen en `/app/.sessions`.
   - **No actives backups del volumen.** Es la copia técnica completa de los chats de la línea de prueba; un respaldo sobreviviría al borrado que el spike tiene que verificar (punto 14).
4. En **Settings → Networking → Public Networking → Generate Domain**, puerto `3000`.
   - Para el spike alcanza dominio público + clave de API. El arnés corre en tu máquina y necesita llegar a la API.
   - En producción esto cambia (red privada), pero eso lo define el tramo 2 del MVP.
5. En **Settings → Deploy → Healthcheck Path**, poné `/ping`.
   - Es el único endpoint sin clave. `/health` exige la clave, y el healthcheck de Railway no manda headers.
6. Región: la misma que el resto de tus servicios.

## Paso 3 — Variables de entorno

En **Variables → Raw Editor**, pegá el bloque completo y reemplazá los tres `PLACEHOLDER`.

```ini
# Motor
WHATSAPP_DEFAULT_ENGINE=GOWS

# HTTP (Railway inyecta PORT y WAHA lo respeta; no hace falta tocarlo)
TZ=America/Argentina/Buenos_Aires

# Seguridad
WAHA_API_KEY=sha512:PLACEHOLDER_HASH_DEL_PASO_1
WAHA_DASHBOARD_ENABLED=false
WAHA_DASHBOARD_USERNAME=radar
WAHA_DASHBOARD_PASSWORD=PLACEHOLDER_PASSWORD_LARGA
WHATSAPP_SWAGGER_ENABLED=false
WHATSAPP_SWAGGER_USERNAME=radar
WHATSAPP_SWAGGER_PASSWORD=PLACEHOLDER_PASSWORD_LARGA
WAHA_APPS_ENABLED=false
WAHA_PRINT_QR=False

# Sesiones y worker
WAHA_WORKER_ID=radar-spike-gows
WAHA_WORKER_RESTART_SESSIONS=True
WHATSAPP_RESTART_ALL_SESSIONS=False
WAHA_LOCAL_STORE_BASE_DIR=/app/.sessions
WAHA_NAMESPACE=all

# Solo lectura, sin ruido en el teléfono del cliente
WAHA_PRESENCE_AUTO_ONLINE=False
WAHA_SESSION_CONFIG_IGNORE_STATUS=true
WAHA_SESSION_CONFIG_IGNORE_GROUPS=true
WAHA_SESSION_CONFIG_IGNORE_CHANNELS=true
WAHA_SESSION_CONFIG_IGNORE_BROADCAST=true

# Medios: no descargar nada
WAHA_EVENTS_DOWNLOAD_MEDIA=false
WAHA_API_DOWNLOAD_MEDIA=false
WAHA_MEDIA_STORAGE=LOCAL
WHATSAPP_FILES_LIFETIME=180

# Logs
WAHA_LOG_LEVEL=info
WAHA_LOG_FORMAT=JSON
WAHA_HTTP_LOG_LEVEL=info

# Webhooks globales: NO se definen (los configura el arnés por sesión)
# WHATSAPP_HOOK_URL=
# WHATSAPP_HOOK_EVENTS=

# Profundidad del historial de GOWS: ver "Rondas" abajo. Ronda 1 = con estos límites.
# El tope por chat va alto a propósito: en el spike medimos completitud y un tope bajo la confundiría.
WAHA_GOWS_DEVICE_REQUIRE_FULL_SYNC=false
WAHA_GOWS_DEVICE_HISTORY_SYNC_FULL_SYNC_DAYS_LIMIT=90
WAHA_GOWS_DEVICE_HISTORY_SYNC_RECENT_SYNC_DAYS_LIMIT=90
WAHA_GOWS_DEVICE_HISTORY_SYNC_INITIAL_SYNC_MAX_MESSAGES_PER_CHAT=5000
```

Notas:

- **Dashboard y Swagger deshabilitados.** El arnés hace todo por API, incluido pedir el QR. Las credenciales quedan definidas igual, por si alguna vez los habilitás: así no arrancan con las de fábrica.
- **`WAHA_APPS_ENABLED=false`.** Desde 2026.3.1 las apps vienen habilitadas por defecto. En este contenedor no hace falta el MCP.
- **`ignore` a nivel servidor** es defensa en profundidad. El arnés además lo manda en la configuración de cada sesión, que es lo que el punto 7 del spike verifica.
- **Logs.** El código de WAHA solo redacta la clave de API en los logs; no hay opción para redactar cuerpos de mensajes. Con `info` no se registran cuerpos, y no uses `debug` ni `trace` con la línea de prueba conectada.

## Paso 4 — Verificar el despliegue

Reemplazá `TU-DOMINIO` por el dominio que generó Railway. El primero no necesita clave:

```bash
curl -s https://TU-DOMINIO/ping
```

Esperado: `{"message":"pong"}`.

Para los siguientes, exportá la clave en claro en tu terminal (no en un archivo del repo):

```bash
export WAHA_ADMIN_KEY='pegá-acá-la-clave-en-claro'
```

```bash
curl -s -H "X-Api-Key: $WAHA_ADMIN_KEY" https://TU-DOMINIO/api/server/version
```

Esperado: `"version":"2026.8.2"`, `"engine":"GOWS"`, `"tier":"CORE"`.

```bash
curl -s -H "X-Api-Key: $WAHA_ADMIN_KEY" "https://TU-DOMINIO/api/sessions?all=true"
```

Esperado: `[]`. Si devuelve `401`, la clave no coincide con el hash, o WAHA la descartó por "débil" (mirá los logs del servicio: en ese caso imprime la clave generada).

Y una prueba de que la clave equivocada rebota:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "X-Api-Key: incorrecta" https://TU-DOMINIO/api/sessions
```

Esperado: `401`.

## Rondas de vinculación para el punto 2 del spike

Las variables `WAHA_GOWS_DEVICE_*` son indicaciones que se mandan a WhatsApp al registrar el dispositivo. Son experimentales, funcionan desde 2026.6.3, y **cambiarlas exige reiniciar el contenedor y volver a escanear**.

| Ronda | Variables | Qué mide |
|---|---|---|
| 1 (`gw0` del plan) | Las cuatro definidas, con 90 días | Completitud contra el teléfono en los últimos 3 meses, y si WhatsApp respeta el límite. Es el [VALIDAR] de §6.2 del spec: si no lo respeta, la copia técnica trae años de chats, y ya lo vas a ver en esta misma ronda. |
| 2 (`gw2`, opcional) | Sin definir (WhatsApp decide) | Solo si la ronda 1 respetó los 90 días y querés conocer el techo real del historial de una línea Business. La documentación dice "muchos años". |

Entre rondas: borrar la sesión (el arnés lo hace con un único `DELETE`), cambiar las variables, redeploy, escanear de nuevo. Los valores de producto (por ejemplo 200 mensajes por chat) se fijan después, con lo que muestre el spike.

## Lo que este servidor NO es

- No es el servidor de producción. Ahí no hay dominio público, y las sesiones pueden ir a PostgreSQL propio de WAHA (spec §6.2).
- No comparte nada con tu instancia actual: ni volumen, ni base, ni clave, ni `WAHA_WORKER_ID`.

## El receptor de webhooks del arnés

WAHA tiene que poder llamar al receptor del spike, que corre en tu máquina. La opción sin cuenta es un túnel rápido de Cloudflare:

```bash
cloudflared tunnel --url http://localhost:8787
```

Imprime una URL `https://algo.trycloudflare.com`, que va a `SPIKE_WEBHOOK_PUBLIC_URL`. Cambia en cada arranque del túnel, así que el arnés la lee en el momento de crear la sesión. El plan del spike trae el detalle.

## Alternativa: Docker Compose en un VPS

Si preferís no usar Railway para esto:

```yaml
services:
  waha-gows:
    image: devlikeapro/waha:gows-2026.8.2
    restart: unless-stopped
    ports:
      - "127.0.0.1:3000:3000"
    env_file: .env.waha-gows
    volumes:
      - waha_gows_sessions:/app/.sessions
volumes:
  waha_gows_sessions:
```

Con el mismo bloque de variables en `.env.waha-gows` (fuera del repo), y un reverse proxy con TLS adelante del puerto 3000.
