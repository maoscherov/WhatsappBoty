# remedia-agent

Servicio de Windows en Rust que corre dentro de la red de la farmacia. Lee el
catálogo de **ObServer Gestión** (API REST local en `:60064`), detecta cambios y
los sube a la API de Remedia. Mantiene un websocket saliente para consultas de
stock/precio en tiempo real. **Nunca acepta conexiones entrantes.**

Spec: [`docs/superpowers/specs/2026-09-08-remedia-agent-design.md`](../docs/superpowers/specs/2026-09-08-remedia-agent-design.md).

## Instalación en la farmacia

Desde una consola **como administrador**, con el `agent.exe` descargado:

```bat
agent.exe install --token TOKEN_SUCURSAL --erp http://192.168.1.156:60064 --branch farmacia-xxx
```

Esto:

1. Escribe `C:\ProgramData\RemediaAgent\agent.toml`.
2. Copia el ejecutable a `C:\ProgramData\RemediaAgent\agent.exe`.
3. Registra el servicio `RemediaAgent` (arranque automático, cuenta
   `NT AUTHORITY\NetworkService`, con permisos sobre el directorio de datos) y lo inicia.

4. Registra el **icono de bandeja** (`agent.exe tray`) para que arranque al iniciar
   sesión cualquier usuario de la PC, y lo abre en la sesión actual.

Otros comandos:

| Comando | Qué hace |
|---|---|
| `agent.exe status` | Estado vivo por el pipe si el servicio corre (con latencias); si no, el último estado guardado. |
| `agent.exe sync-now` | Un ciclo de sync (pendientes + delta) en primer plano y termina. |
| `agent.exe run` | Corre el agente en foreground con logs en consola (Ctrl+C para salir). |
| `agent.exe tray` | Abre el icono de bandeja a mano (normalmente arranca solo). |
| `agent.exe uninstall` | Cierra los trays, quita el autoarranque, detiene y elimina el servicio. No borra `C:\ProgramData\RemediaAgent`. |

Todos aceptan `--data-dir D` para usar otro directorio (útil en desarrollo).

**Actualizar** = volver a correr `agent.exe install ...` con el exe nuevo: detiene
el servicio, cierra los trays, reemplaza el binario, actualiza la configuración
del servicio y lo reinicia. Config y `state.sqlite` se conservan.

## Icono de bandeja

Pensado para que lo mire el personal de la farmacia. El color dice todo sin abrir el menú:

| Color | Significa |
|---|---|
| Verde | ERP ok, conectado con Remedia, sin pendientes, sincronizado hace menos de 1 h. |
| Amarillo | Hay lotes sin enviar, el websocket está caído o hace más de 1 h que no sincroniza. |
| Rojo | El ERP no responde o no autoriza, o Remedia rechaza el token. |
| Gris | El servicio no está corriendo. |

Clic derecho muestra el estado (ERP, Remedia, último sync, pendientes, último
error) y estas acciones:

- **Sincronizar ahora**: adelanta el ciclo.
- **Probar conexión con el ERP**: lee el lote 1 y muestra los ms.
- **Ver detalle…**: todas las latencias (lectura completa del ERP, por lote,
  envío a Remedia, ciclo completo, consulta en vivo, heartbeat), versiones,
  reconexiones, rutas. Botón "Copiar" para pegarlo en un mensaje a soporte.
- **Configuración…**: cambiar la dirección de Remedia, la del ERP o el token,
  **sin permisos de administrador**. "Probar" verifica cada uno y muestra ms.
  "Guardar" pide confirmación; un token o dirección de Remedia inválidos se
  rechazan sin guardar; un ERP apagado se guarda con aviso. El servicio aplica
  el cambio en caliente (no hace falta reiniciar nada).
- **Abrir carpeta de logs**.
- **Salir del icono**: cierra solo el tray; el servicio sigue.

El tray habla con el servicio por el named pipe local `\\.\pipe\RemediaAgent`
(no es una conexión de red). Solo hay una instancia por sesión.

## `agent.toml`

```toml
branch_id = "farmacia-xxx"
remedia_url = "https://cerca.remedia.ar"
token = "..."
heartbeat_interval_secs = 300   # opcional

[erp]
kind = "observer"
base_url = "http://192.168.1.156:60064"
sync_interval_secs = 900        # opcional, default 900
max_concurrency = 4             # opcional, máximo recomendado contra el ERP
request_timeout_secs = 30       # opcional
daily_id_scan = false           # ver "Pendiente" más abajo
id_scan_max = 100300            # rango del barrido por ID

[log]
dir = "C:\\ProgramData\\RemediaAgent\\logs"   # rotación diaria, 7 archivos
```

## Qué hace, en orden

- **Sync (cada 15 min o ante `sync_now` por websocket):** reenvía los lotes
  pendientes vencidos, lee todos los lotes del ERP, normaliza a `CatalogItem`,
  calcula el hash blake3, compara con `state.sqlite` y manda solo lo nuevo o
  cambiado a `POST /v1/sync/catalog` en lotes de 500. El estado local se
  actualiza **solo** tras un 2xx de Remedia; si falla, el lote queda en la cola
  con backoff 30 s → 10 min.
- **Full-manifest (una vez por día, si el ERP respondió):** manda todos los
  `external_id` + hash a `POST /v1/sync/full-manifest`. Remedia desactiva lo
  ausente y devuelve los ids a reenviar. Se poda del estado local lo que ya no
  está en el ERP.
- **Heartbeat (cada 5 min):** `POST /v1/sync/heartbeat` con `erp_status`
  (`ok | no_autorizado | inalcanzable | error`), conteo del catálogo, lotes
  pendientes y un objeto `metrics` con las latencias (Remedia puede ignorarlo).
- **Websocket saliente** a `wss://<remedia>/v1/agent/ws` (Bearer en el
  handshake): `lookup` por CB o id (timeout 3 s por request al ERP),
  `sync_now`, ping/pong cada 30 s, reconexión con backoff 5 s → 5 min.

Ofertas/condiciones de pago se parsean pero no se hashean ni se envían.

## Desarrollo

```bash
cd remedia-agent
cargo test
cargo build --release      # target/release/agent.exe (LTO, panic=abort, strip)
```

Los tests de integración levantan mocks HTTP (wiremock) del ERP y de Remedia y
un servidor websocket de prueba; no necesitan red ni ERP real. El fixture
`tests/fixtures/lote1.json` es **sintético** con los casos raros medidos en la
spec (sin CB, varios CB, CB compartido, precio 0, troquel 0, espacios múltiples).
Reemplazarlo por el lote real cuando esté disponible.

Correr contra un ERP real sin instalar el servicio:

```bash
mkdir dev-data && cp agent.toml.example dev-data/agent.toml   # editar
cargo run -- run --data-dir dev-data
```

### Si el build falla con `LNK1104: cannot open file 'msvcrt.lib'`

rustc eligió una instalación de Visual Studio incompleta. Compilar desde una
consola con el entorno de los Build Tools cargado (`vcvars64.bat`), o crear un
wrapper que lo cargue antes de invocar `cargo`.

## Pendiente (spec §1)

El lote del ERP trae ~54k productos y por ID hay ~89k; no se sabe qué filtra.
Si se confirma que excluye productos relevantes, activar en `agent.toml`:

```toml
[erp]
daily_id_scan = true
```

Una vez por día el agente barre `GET /api/productos/{id}` de `1..=id_scan_max`
con concurrencia `max_concurrency` y une el resultado al lote (el lote gana ante
el mismo id).

## Lado servidor

Los endpoints `/v1/sync/catalog`, `/v1/sync/full-manifest`,
`/v1/sync/heartbeat` y `/v1/agent/ws` viven en `app/routers/sync_api.py` y
`app/routers/agent_ws.py`; el alta de sucursales y tokens en
`app/routers/backoffice_branches.py`. Spec:
[`2026-09-08-remedia-sync-server-design.md`](../docs/superpowers/specs/2026-09-08-remedia-sync-server-design.md).
El contrato del lado agente está en
[`src/remedia/client.rs`](src/remedia/client.rs) / [`src/remedia/ws.rs`](src/remedia/ws.rs).
