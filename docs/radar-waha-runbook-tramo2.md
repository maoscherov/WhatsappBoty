# Radar tramo 2 — prueba manual contra el WAHA GOWS de staging

**Para:** Mariano · **Cuándo:** después de que `python -m pytest tests/radar_tests -q` esté en verde.
**Con qué:** el contenedor GOWS del runbook `docs/superpowers/plans/2026-09-21-radar-gows-staging-runbook.md`
(seguilo para levantar el servicio, generar la clave admin y verificar el despliegue — no se repite acá) y
**una línea de prueba con consentimiento** (nunca la línea personal ni la de un cliente).
**Reglas:** no uses herramientas MCP de WAHA; no pegues claves, QR, códigos ni números en ningún chat ni
archivo del repo; anotá solo estados y sí/no.

## 0. Preparación

1. Radar local contra un Postgres local (ver `docs/radar-despliegue.md`), con `APP_MODE=radar`,
   `RADAR_COOKIE_SECURE=false`, `RADAR_PUBLIC_BASE_URL=http://localhost:8000` y `RADAR_WAHA_WEBHOOK_HMAC_KEY`
   generada al momento (`python -c "import secrets; print(secrets.token_urlsafe(48))"`).
2. Túnel para que WAHA llegue al receptor: `cloudflared tunnel --url http://localhost:8000`. Poné
   `RADAR_WAHA_WEBHOOK_URL=https://<túnel>/webhook/waha` y reiniciá Radar.
3. Registrá el worker (la clave en claro solo en la variable de la shell, nunca por argumento):

       export WAHA_ADMIN_KEY='...'
       python scripts/radar_workers.py registrar --nombre gows-staging \
           --base-url https://TU-DOMINIO --engine GOWS --max-sesiones 5 --disco-max-gb 5
       python scripts/radar_workers.py listar

   O desde la Consola, ya con la sesión del paso 4: sección «Servidores WAHA», con los mismos datos; por ahí la
   clave se prueba contra el WAHA antes de guardarse. Igual dejá `WAHA_ADMIN_KEY` en la variable de la shell: la
   usan los `curl` de verificación de los pasos siguientes.
4. Creá el primer admin (`python scripts/radar_admin.py crear-admin --email ... --nombre ...`), entrá con el
   link, y creá un cliente de prueba con su línea: desde la Consola, sección «Alta» → «Cliente nuevo», o con
   `POST /radar/admin/tenants`.

## 1. Consola: consentimiento asistido y QR

1. Abrí `http://localhost:8000/radar/consola`, tocá **Operar** en la línea.
2. Marcá la aceptación, elegí el modo, escribí un nombre y registrá. Esperado: aviso de copia enviada (con
   `RADAR_MAILER=log`, en el log aparece solo `*@dominio` y la huella).
3. **Generar QR.** Esperado: el QR aparece y se renueva solo.
4. Verificá la sesión con la clave admin (sin copiar la salida a ningún lado):

       curl -s -H "X-Api-Key: $WAHA_ADMIN_KEY" https://TU-DOMINIO/api/sessions?all=true | python -c "import json,sys; [print(s['name'], s['status']) for s in json.load(sys.stdin)]"
       curl -s -H "X-Api-Key: $WAHA_ADMIN_KEY" https://TU-DOMINIO/api/sessions/v_XXXXXXXXXXXX | python -c "import json,sys; c=json.load(sys.stdin)['config']; print(sorted(c)); print(c.get('ignore')); print(c.get('gows')); print(sorted(c.get('metadata', {}))); print([ (w['url'][:30], sorted(w['events'])) for w in c['webhooks']])"

   Esperado: `ignore` con los cuatro en `true`, `gows.storage` como lo pidió Radar
   (`app/radar/waha/sesion.py::cuerpo_sesion`), `metadata` con `link_id`, `line_id`, `tenant_id`, y un único
   webhook a tu túnel con los cinco eventos.
5. En los logs del contenedor (Railway → Deployments → Logs) **no** tiene que aparecer el QR
   (`WAHA_PRINT_QR=False`).
6. **Formato de `X-Webhook-Hmac` contra WAHA real (antes de dar por buena la conexión).** El receptor espera
   el HMAC-SHA512 del cuerpo crudo en hex (`app/radar/routers/webhook_waha.py::verificar_hmac`), y eso solo se
   probó contra el WAHA falso. Con la sesión recién creada WAHA ya manda un `session.status` (`STARTING` →
   `SCAN_QR_CODE`); si no, tocá **Generar QR** de nuevo para forzar otro evento. En el log de Radar (el access
   log de uvicorn) buscá las líneas `POST /webhook/waha`:
   - **`200`** → el formato coincide. Confirmalo en la base: `SELECT waha_status, origen FROM
     link_status_events ORDER BY id` tiene filas con origen `webhook`.
   - **`401`** → la firma no coincide y **todos** los eventos se van a rechazar (los vínculos solo avanzarían
     por polling y chequeo de salud). No sigas con el resto del runbook. Revisá, en este orden:
     1. que `RADAR_WAHA_WEBHOOK_HMAC_KEY` sea la misma con la que se creó la sesión (si la cambiaste, reiniciá
        Radar, cerrá la sesión y generá un QR nuevo: la clave viaja en la config de la sesión);
     2. el formato de la cabecera: apuntá un vínculo de prueba a un receptor descartable en tu máquina (por el
        mismo túnel) que imprima **solo** el largo de `X-Webhook-Hmac`, si es hex o base64 y si trae prefijo
        (`sha512=`), nunca el valor ni el cuerpo; y el algoritmo que anuncie `X-Webhook-Hmac-Algorithm`, si viene.
     Con eso ajustá `verificar_hmac` y su test en `tests/radar_tests/test_webhook_waha.py` **antes** del piloto,
     y repetí este paso hasta ver `200`.

| Punto a validar | Resultado (sí/no) |
|---|---|
| La sesión se llama `v_` + 12 hex y no hay otras sesiones de Radar vivas | |
| `ignore`, `storage`, `metadata` y webhook coinciden | |
| El QR no aparece en los logs de WAHA | |
| El webhook de WAHA entra con `200` (no `401`): el formato de `X-Webhook-Hmac` coincide | |

## 2. Clave de solo lectura

    curl -s -H "X-Api-Key: $WAHA_ADMIN_KEY" https://TU-DOMINIO/api/keys | python -c "import json,sys; [print(k.get('id'), k.get('session'), k.get('isAdmin'), k.get('actions')) for k in json.load(sys.stdin)]"

| Punto a validar | Resultado |
|---|---|
| Hay exactamente una clave para la sesión, con `isAdmin: false` | |
| `actions` quedó como lo mandó Radar (`read: true`, el resto `false`) y **no** en `null` | |
| `POST /api/keys` devolvió el valor en el campo `key` (si Radar abortó con `waha_error`, este es el primer sospechoso) | |

Si los nombres de `actions` no son los que usa WAHA, corregí `ACCIONES_CLAVE_LECTURA` en
`app/radar/waha/sesion.py` y su test **antes** del piloto.

## 3. Escaneo y estados

1. Escaneá con el teléfono de prueba. Esperado: "Conectado: …NNNN. ¿Es esta la línea del negocio?".
2. En la Consola la línea aparece en verde con el sufijo y el worker `gows-staging (1/5)`.
3. En la base: `SELECT waha_status, origen FROM link_status_events ORDER BY id` (como dueño de las tablas)
   muestra `WORKING` con origen `webhook`.

| Punto a validar | Resultado |
|---|---|
| Los `session.status` llegan por webhook (origen `webhook`, no solo `polling`) | |
| El `session.status` trae `metadata` (si no, el receptor lo ignora y el estado avanza solo por polling y chequeo) | |
| `WORKING` trae `me.id` (si no, no se muestra el sufijo) | |

## 4. Código de vinculación y QR vencido

1. Desconectá (paso 6), vinculá de nuevo y usá "vincular con código" con el número de la línea de prueba.
   Esperado: código `ABCD-EFGH`; el QR sigue disponible si falla.
2. Vinculá de nuevo y **no escanees** durante ~3 min. Esperado: "El código venció. Generá uno nuevo."; hasta
   3 reintentos; al cuarto, el mensaje de ayuda.

| Punto a validar | Resultado |
|---|---|
| El código funciona y convive con la rotación del QR | |
| Tras el vencimiento, WAHA emite `FAILED` (o `STOPPED`) y el reinicio muestra un QR nuevo | |

## 5. Desvínculo desde el teléfono

1. Con la línea vinculada, quitá el dispositivo desde el teléfono (Dispositivos vinculados).
2. Esperado en ≤ 5 min: la línea en rojo, estado "Tu WhatsApp se desconectó el DD/MM. Tocá «Reconectar»." y,
   en el log del mailer, un aviso de caída al dueño (solo `*@dominio`).
3. **Reconectar** desde la Consola, escanear, y verificar que el vínculo anterior pasa a `cerrado` con causa
   `reemplazado`.

| Punto a validar | Resultado |
|---|---|
| Qué estado emite GOWS al quitar el dispositivo (`FAILED`, `STOPPED`, `SCAN_QR_CODE`) | |
| El vínculo viejo se cierra al llegar el nuevo a `WORKING` | |

## 6. Desconectar y verificar el borrado

1. **Desconectar** desde la Consola. Esperado en ≤ 1 min: estado "Desconectado.", email de aviso (en el log).
   `app/radar/waha/gestor.py::terminar_sesion` sigue todos los pasos aunque alguno falle (lectura, start
   opcional, `DELETE`, borrado de claves, verificación), así que si un paso falló igual hay que revisar los
   siguientes puntos.
2. Verificá:

       curl -s -o /dev/null -w "%{http_code}\n" -H "X-Api-Key: $WAHA_ADMIN_KEY" https://TU-DOMINIO/api/sessions/v_XXXXXXXXXXXX   # 404
       curl -s -H "X-Api-Key: $WAHA_ADMIN_KEY" https://TU-DOMINIO/api/keys | python -c "import json,sys; print(sum(1 for k in json.load(sys.stdin) if k.get('session','').startswith('v_')))"   # 0

3. En la shell del contenedor WAHA: `ls /app/.sessions` no tiene el directorio de esa sesión.
4. En el teléfono, el dispositivo ya no figura.
5. En los logs de WAHA no hay ningún `POST .../logout` (el fin de vínculo nunca llama a `logout`).
6. Si el job de fin de vínculo quedó en `fallido` (reintentos agotados en `radar_jobs`), el vínculo queda en
   `cerrando`: revisá `radar_jobs` y reintentalo a mano antes de dar el paso por cerrado.

| Punto a validar | Resultado |
|---|---|
| 404 de la sesión y cero claves de sesiones `v_` | |
| Directorio de la sesión borrado del volumen | |
| El dispositivo desapareció del teléfono (`desvinculo_confirmado = true` en `links`) | |

## 7. Restricción y admisión

1. En la Consola, **Marcar restricción** sin fecha; **Reconectar** tiene que dar "La cuenta tiene una
   restricción activa"; **Desconectar** tiene que funcionar. Levantala.
2. `python scripts/radar_workers.py disco --nombre gows-staging --usado-gb 4` (80 % de 5 GB; o el botón «Disco»
   del servidor en la Consola, sección «Servidores WAHA») y generá QR en otra línea. Esperado: "En este momento no hay lugar para una conexión nueva. Probá de nuevo más tarde o
   escribinos." y ninguna sesión nueva en WAHA. Volvé el disco a 0.

## 8. Cierre

- Borrá todas las sesiones `v_` que hayan quedado (Desconectar desde la Consola) y verificá el paso 6.
- Anotá los resultados de las tablas en el plan del spike (sin identificadores). Cualquier "no" en los pasos
  2 y 3 bloquea el piloto hasta corregir el código.
