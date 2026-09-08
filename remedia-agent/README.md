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

Otros comandos:

| Comando | Qué hace |
|---|---|
| `agent.exe status` | Estado local: ERP ok/no_autorizado/inalcanzable, ítems, pendientes, último sync y heartbeat. |
| `agent.exe sync-now` | Un ciclo de sync (pendientes + delta) en primer plano y termina. |
| `agent.exe run` | Corre el agente en foreground con logs en consola (Ctrl+C para salir). |
| `agent.exe uninstall` | Detiene y elimina el servicio. No borra `C:\ProgramData\RemediaAgent`. |

Todos aceptan `--data-dir D` para usar otro directorio (útil en desarrollo).

## `agent.toml`

```toml
branch_id = "farmacia-xxx"
remedia_url = "https://api.remedia.ar"
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
  (`ok | no_autorizado | inalcanzable | error`), conteo del catálogo y lotes pendientes.
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
`/v1/sync/heartbeat` y `/v1/agent/ws` **todavía no existen** en la API de
Remedia (`app/`). El contrato está en la spec y en
[`src/remedia/client.rs`](src/remedia/client.rs) / [`src/remedia/ws.rs`](src/remedia/ws.rs).
