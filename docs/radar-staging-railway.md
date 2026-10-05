# Radar en Railway (staging)

Cómo dejar Radar andando en Railway para probarlo de punta a punta: login, alta de un cliente, servidor WAHA, consentimiento y QR. No reemplaza a [radar-despliegue.md](radar-despliegue.md), que explica cada variable y los roles de Postgres.

## La forma corta: el asistente

En **Git Bash**, desde la raíz del worktree de Radar, con la CLI de Railway instalada y logueada (`railway whoami`):

```bash
bash scripts/radar_staging_railway.sh
```

Te lleva por 13 pasos, abre cada pantalla y carga por vos lo que se puede cargar con la CLI (variables, volumen, dominio). Guarda lo que generás o pegás en `~/.radar-staging.env`, fuera del repo: si lo cortás, al volver a correrlo retoma sin regenerar las claves.

| Paso | Qué hacés vos | Qué hace el asistente |
|---|---|---|
| 1. Antes de empezar | — | Verifica carpeta, rama, `railway`, `curl`, `python` |
| 2. Subir la rama | Confirmás | `git push -u origin feature/radar-tramo2` (solo esa rama) |
| 3. Proyecto | Elegís el proyecto del WAHA GOWS | `railway link` |
| 4. Dos Postgres | Los creás y renombrás `RadarResultados` y `RadarFuente` | Verifica que existan |
| 5. Servicio de Radar | Lo creás desde el repo, lo llamás `radar` y elegís la rama | Lo vincula a la carpeta |
| 6. Volumen | Confirmás | `railway volume add --mount-path /data` |
| 7. Dominio | — | `railway domain --port 8000` |
| 8. Correo saliente | Creás una contraseña de aplicación de Google (u otro SMTP) | La guarda oculta |
| 9. Variables | Confirmás | Genera las claves y carga todas las variables |
| 10. Desplegar | Esperás | Consulta `/health` hasta que responda `ok` en modo radar |
| 11. Entrar | Pedís el link en `/radar/login` y lo abrís | Plan B sin mail: `railway ssh` + `crear-admin` |
| 12. Servidor WAHA | Lo cargás en la Consola con la clave en claro | — |
| 13. Cliente de prueba y QR | Alta, consentimiento, QR | Busca `POST /webhook/waha` en los logs |

## Qué queda en Railway

- **`RadarResultados`** (Postgres): tablas de Radar, con RLS. Al arrancar, Radar crea los roles `radar_app` y `radar_admin` (`RADAR_BOOTSTRAP_ROLES=true`) y corre las migraciones.
- **`RadarFuente`** (Postgres): el almacén de fuente. En este tramo solo tiene su marcador de esquema.
- **`radar`**: la misma imagen que el bot con `APP_MODE=radar`, desde la rama `feature/radar-tramo2`, con un volumen en `/data` para las claves (las de cada cliente y la admin de WAHA) y un dominio público.
- **WAHA GOWS**: el de staging que ya existe. Radar lo llama por su URL pública y WAHA le avisa a Radar por `https://<dominio de radar>/webhook/waha`, firmado con HMAC.

## Las variables que carga el asistente

| Variable | Valor |
|---|---|
| `APP_MODE` | `radar` |
| `PORT` | `8000` (el dominio apunta a ese puerto) |
| `RADAR_BOOTSTRAP_ROLES` | `true` |
| `RADAR_MIGRATOR_DATABASE_URL` | `${{RadarResultados.DATABASE_URL}}` |
| `RADAR_DATABASE_URL` | `postgresql://radar_app:<generada>@${{RadarResultados.PGHOST}}:${{RadarResultados.PGPORT}}/${{RadarResultados.PGDATABASE}}` |
| `RADAR_FUENTE_DATABASE_URL` | `${{RadarFuente.DATABASE_URL}}` |
| `RADAR_SECRETS_DIR` | `/data/radar-secrets` |
| `RADAR_COOKIE_SECRET` | generada |
| `RADAR_COOKIE_SECURE` | `true` |
| `RADAR_PUBLIC_BASE_URL` | `https://<dominio>` |
| `RADAR_ADMINS_INICIALES` | los emails de KIS que pongas |
| `RADAR_MAILER` | `smtp` (o `log` si no hay correo saliente) |
| `RADAR_SMTP_HOST`, `RADAR_SMTP_PORT`, `RADAR_SMTP_SEGURIDAD`, `RADAR_SMTP_USUARIO`, `RADAR_SMTP_PASSWORD`, `RADAR_REMITENTE` | los del paso 8 |
| `RADAR_WAHA_WEBHOOK_URL` | `https://<dominio>/webhook/waha` |
| `RADAR_WAHA_WEBHOOK_HMAC_KEY` | generada |

Si preferís cargarlas a mano, van en **radar → Variables → Raw Editor** con los mismos nombres.

## Correo saliente con Google Workspace

1. La cuenta necesita la verificación en dos pasos activa.
2. En `https://myaccount.google.com/apppasswords`, creá una contraseña de aplicación llamada "Radar".
3. Servidor `smtp.gmail.com`, puerto `587`, seguridad `starttls`, usuario = la cuenta, contraseña = la de aplicación.
4. `RADAR_REMITENTE` tiene que ser esa cuenta o un alias configurado en ella. Si no, Gmail reescribe el remitente.

Si la página de contraseñas de aplicación dice que no está disponible, falta la verificación en dos pasos o el admin de Workspace las bloqueó.

## Si algo falla

- **`/health` no responde `ok`:** `railway logs --service radar --deployment`. Lo más común es un nombre de base mal escrito en las referencias `${{...}}` o el volumen sin montar.
- **No llega el mail del login:** mirá spam, y en los logs buscá `link no enviado`. Plan B: `railway ssh --service radar -- sh -c "cd /app && python scripts/radar_admin.py crear-admin --email <tu email>"` imprime un link de invitación en tu terminal. No lo pegues en ningún chat.
- **"WAHA rechazó la clave" al cargar el servidor:** es la clave en claro del gestor de contraseñas, no el hash `sha512:`.
- **El webhook entra con `401`:** el formato de `X-Webhook-Hmac` de WAHA no coincide con el que espera Radar. No escanees y avisá: está en la sección 1.6 de [radar-waha-runbook-tramo2.md](radar-waha-runbook-tramo2.md).

## Qué no es esto

- No es la topología de producción: ahí WAHA va sin dominio público, por la red privada de Railway, y el webhook también (`UVICORN_HOST=::` si la red privada lo pide).
- No hay ingesta ni tablero todavía: Radar vincula la línea y la mantiene sana. Los datos llegan con el tramo 3.
