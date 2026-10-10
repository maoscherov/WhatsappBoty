# Radar en Railway (producción)

Cómo montar Radar de producción. Es el mismo despliegue que [staging](radar-staging-railway.md), con cuatro diferencias: proyecto de Railway propio, WAHA sin dominio público, dominio propio y backups.

**Antes de conectar clientes reales** tienen que estar la ingesta (tramo 3) y la parte legal (textos firmados por el cliente, dictamen). Hasta entonces, producción se valida solo con la línea de prueba.

## Requisitos

- El PR de `feature/radar-tramo2` mergeado en `develop`: producción despliega `develop`.
- Git Bash (no el `bash` de WSL que abre `cmd`), con la CLI de Railway logueada (`railway whoami`).
- Un dominio para Radar (por ejemplo `radar.keepitsimple.com.ar`) y acceso a su DNS.
- Una cuenta de correo para mandar los links (Google Workspace con contraseña de aplicación, u otro SMTP).
- La línea de prueba con consentimiento. Nunca tu línea personal ni la de un cliente.

## El asistente

Desde la raíz de un checkout del repo:

```bash
"C:\Program Files\Git\bin\bash.exe" scripts/radar_produccion_railway.sh
```

Guarda lo que generás o pegás en `~/.radar-produccion.env`, fuera del repo, incluida la clave admin del WAHA de producción: copiala también a tu gestor de contraseñas.

| Paso | Qué hacés vos | Qué hace el asistente |
|---|---|---|
| 1. Antes de empezar | — | Verifica Git Bash, herramientas y que `origin/develop` ya tenga Radar |
| 2. Proyecto | Confirmás | `railway init --name Radar` (o elegís uno existente) |
| 3. Dos Postgres | Los creás, los renombrás `RadarResultados` y `RadarFuente` y les activás backups | Verifica que existan |
| 4. WAHA de producción | Confirmás la versión (`gows-2026.9.2` por defecto) | Genera la clave, crea el servicio con la imagen fija y las variables endurecidas, y su volumen |
| 5. Servicio de Radar | Lo creás desde el repo, lo llamás `radar`, rama `develop` | Lo vincula a la carpeta |
| 6. Volumen de claves | Le activás backups | `railway volume add --mount-path /data` |
| 7. Dominio propio | Cargás el CNAME en tu DNS | `railway domain <dominio> --port 8000` |
| 8. Correo saliente | Contraseña de aplicación o datos del SMTP | La guarda oculta |
| 9. Variables | Confirmás | Genera las claves y carga todo |
| 10. Desplegar | Esperás | Consulta `https://<dominio>/health` hasta 15 minutos |
| 11. Entrar | Pedís el link y lo abrís | Plan B: `railway ssh` + `crear-admin` |
| 12. WAHA en la Consola | Lo registrás con la URL privada que te muestra | Lee el dominio privado del servicio de WAHA |
| 13. Validar | Alta de "KIS prueba", QR, escaneo con la línea de prueba, runbook, borrar todo | Busca `POST /webhook/waha` en los logs |

## Qué cambia respecto de staging

| | Staging | Producción |
|---|---|---|
| Proyecto de Railway | El del WAHA de staging | Uno propio (`Radar`) |
| Rama | `feature/radar-tramo2` | `develop` |
| WAHA | El de staging, con dominio público e imagen sin versión fija | Uno nuevo, sin dominio público, con `devlikeapro/waha:gows-<versión>` |
| Radar → WAHA | URL pública | `http://<servicio-waha>.railway.internal:3000` |
| WAHA → Radar (webhook) | `https://<dominio de radar>/webhook/waha` | `http://${{radar.RAILWAY_PRIVATE_DOMAIN}}:8000/webhook/waha`, con `UVICORN_HOST=::` en Radar |
| Dominio | El que genera Railway | Propio |
| Backups | Ninguno | Las dos bases y el volumen `/data` de Radar. **Nunca** el volumen de WAHA |

## Si algo falla

- **`/health` no responde:** si el DNS todavía no resuelve, esperá y re-corré el asistente (retoma lo guardado). Si no, `railway logs --service radar --deployment`.
- **La Consola dice "No se pudo conectar" al registrar el WAHA:** Radar no llega a WAHA por la red privada. Revisá que WAHA escuche en el 3000 (`WHATSAPP_API_PORT` y `PORT`) y que la URL sea la del dominio privado.
- **El webhook no aparece en los logs de Radar:** WAHA no llega a Radar. Revisá que `UVICORN_HOST` sea `::` y que `/health` siga respondiendo por el dominio público.
- **El webhook entra con `401`:** el formato de `X-Webhook-Hmac` de WAHA no coincide con el de Radar. No escanees: ver la sección 1.6 de [radar-waha-runbook-tramo2.md](radar-waha-runbook-tramo2.md).
- **Sesión `default` en WAHA:** WAHA la recrea en cada arranque. No tiene dominio público ni se escanea; Radar la ignora.
