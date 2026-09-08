# remedia-agent — Conector de catálogo ObServer Gestión → Remedia

Agente local en **Rust** que corre como servicio de Windows dentro de la red de la farmacia. Lee el catálogo del ERP **ObServer Gestión** (Praxys / Ksoft) por su API REST local, detecta cambios y los sube a la API de Remedia. Además mantiene un websocket saliente para consultas de stock/precio en tiempo real.

Nunca acepta conexiones entrantes. Todo el tráfico es saliente (ERP en LAN, Remedia por HTTPS).

---

## 1. API del ERP (verificado contra ObServer Gestión, `ServiciosGestion.exe`)

- Base: `http://<ip-server>:60064` (self-hosted Web API sobre HTTP.sys, header `Server: Microsoft-HTTPAPI/2.0`).
- Sin autenticación. Devuelve JSON si se manda `Accept: application/json` (sin ese header puede devolver XML).
- Todos los métodos chequean un flag de instalación `API_Productos`. Si está deshabilitado responden **401** con texto `"No autorizado para obtener información de productos."`. El agente debe reportar ese estado en el heartbeat como `erp_status = "no_autorizado"`.
- Controllers descubiertos (ILSpy sobre `ServiciosRestGestion.dll`): `ProductosController`, `StockController`, `GeneralesController`, `PaquetesController`, `CajasMostradorCierreController`, `ComprobantesProveedoresController`, `LibrosIVAVentasController`, `TestController`.

### Endpoints que usa el agente

| Método | Ruta | Uso |
|---|---|---|
| GET | `/api/productos/lote/{n}` | Sync completo. `n` desde 1. |
| POST | `/api/productos/codigosBarras` | Lookup en vivo por lista de CB. Body: `["7795336085205","7506306214972"]`. |
| GET | `/api/productos/{idProducto}` | Lookup en vivo para productos sin CB. |
| GET | `/api/productos?codigoBarras=X` | Alternativa de lookup unitario (no se usa). |
| GET | `/api/stock/{idProducto}` | `StockController.ObtenerStockPorId`. Ruta exacta **sin verificar**; ver `stock7454.json` si existe. |

### `GET /api/productos/lote/{n}`

Respuesta:
```json
{ "cantidadLotes": 49, "productos": [ ProductoDTO, ... ] }
```
- Medido: 49 lotes, ~0,3 s cada uno, **54.235 productos en total, todos con `Baja=false`**.
- Los lotes parecen ser rangos de ~2000 `idProducto` (lote 1 trajo IDs 14..1674, 1107 productos), no cantidades fijas.
- Pasado el último lote responde **400** `{"Message":"El numeroLote=50 no genera un conjunto de productos"}`. El agente itera `1..=cantidadLotes` y trata 400 como fin.
- **PENDIENTE**: el lote filtra algo. Por ID hay ~89.400 productos (72.264 activos y visibles); por lote 54.235. Se sospecha `precio > 0` u otro criterio. Verificar con `diff_lote.py` (ver §6) o leyendo `ProductosCtl.ObtenerProductoPorLote` en `Controladores.dll` con ILSpy. Si el lote excluye productos con stock, el adapter debe complementar con un barrido `GET /api/productos/{id}` por rango (1..~100.300, concurrencia máx. 4, timeout 30 s, reintentos) una vez por día.

### `ProductoDTO`

```json
{
  "idProducto": 7454,
  "troquel": 4479051,
  "codigoBarras": ["7795336085205"],
  "descripcion": "CLARITROMICINA RICHET 500 mg COM x    8",
  "stockSucursal": 0,
  "precio": 19641.2600,
  "categoria": "Medicamentos",
  "rubro": "Medicamentos",
  "subrubro": "Medicamentos",
  "formaFarmaceutica": "Comprimidos",
  "accionesTerapeuticas": ["Antibiótico"],
  "laboratorio": "Richet",
  "nombresDrogas": "Claritromicina",
  "ofertas": [
    { "his_IdCondicionComercial": 11, "porcentaje": -3.1000, "descripcion": "-3,10% con crédito 3 cuotas LaPos.." }
  ],
  "esVisibleEnVenta": true,
  "visiblesMismoCB": 1,
  "Baja": false
}
```

Notas de datos (medidas sobre el catálogo real de una farmacia):
- `codigoBarras` es lista; puede estar vacía (~6.300 productos) o tener varios (~10.100). Hay ~1.200 CB compartidos por más de un producto → **la clave es `idProducto`**, el CB es índice secundario.
- `visiblesMismoCB`: cantidad de productos que comparten ese CB (viene `null` en consultas por ID). Cuando es >1, la búsqueda por CB devuelve solo uno.
- `descripcion` trae espacios múltiples (`COM x    8`): colapsar a uno antes de hashear y de guardar.
- `precio` puede ser `0` (~15.700 productos): significa "sin precio", no gratis.
- `nombresDrogas` vacío en perfumería/accesorios. `troquel` 0 en no-medicamentos.
- `ofertas`: en esta farmacia son **las mismas 5 en todos los productos** (condiciones de pago LaPos/GetNet). **Se ignoran en esta versión**: no se hashean ni se envían. Dejar el hook para incluirlas más adelante.
- `accionesTerapeuticas` se envía pero Remedia no lo usa para responder al cliente.
- Distribución: ~60% Perfumería, ~34% Medicamentos, resto Accesorios/Varios. Solo ~5.700 productos con `stockSucursal > 0`.

### `POST /api/productos/codigosBarras`

Body: array JSON de strings. Responde `List<ProductoDTO>` (mismo DTO). Probado con 2 CB: responde ambos en un solo request. Usarlo para lookups en vivo de hasta ~20 CB por llamada. 404 si ninguno existe.

### Rendimiento y límites del ERP
- Un request unitario tarda decenas de ms. Con 20 conexiones concurrentes el ERP empieza a dar timeouts (13% de fallos en un barrido de 100k). **Concurrencia máxima recomendada: 4.** El sync por lotes es secuencial y alcanza (49 × 0,3 s ≈ 15 s).

---

## 2. Arquitectura del agente

```
remedia-agent/
├── Cargo.toml
├── src/
│   ├── main.rs            # clap: install | uninstall | run | sync-now | status
│   ├── config.rs          # agent.toml
│   ├── service.rs         # windows-service (arranque, stop, run loop)
│   ├── erp/
│   │   ├── mod.rs         # trait ErpAdapter
│   │   ├── observer.rs    # implementación ObServer Gestión
│   │   └── model.rs       # ProductoDTO (serde)
│   ├── catalog/
│   │   ├── item.rs        # CatalogItem normalizado + hash blake3
│   │   ├── state.rs       # SQLite: external_id -> hash, cola de envíos pendientes
│   │   └── sync.rs        # loop de sync: fetch_all -> diff -> push
│   ├── remedia/
│   │   ├── client.rs      # POST /v1/sync/catalog, /v1/sync/heartbeat
│   │   └── ws.rs          # websocket saliente, ops lookup
│   └── logging.rs         # tracing + tracing-appender (rotación diaria)
└── tests/
    ├── fixtures/lote1.json
    └── observer_mock.rs   # mock HTTP del ERP para tests
```

### `trait ErpAdapter`
```rust
#[async_trait]
pub trait ErpAdapter: Send + Sync {
    async fn fetch_all(&self) -> Result<Vec<ProductoDTO>, ErpError>;
    async fn lookup_by_barcodes(&self, barcodes: &[String]) -> Result<Vec<ProductoDTO>, ErpError>;
    async fn lookup_by_id(&self, id: i64) -> Result<Option<ProductoDTO>, ErpError>;
}

pub enum ErpError { Unreachable, NotAuthorized, Http(u16, String), Decode(String) }
```
Un segundo ERP = otro archivo en `erp/`.

### `CatalogItem` (lo que viaja a Remedia)
```rust
pub struct CatalogItem {
    pub external_id: String,        // idProducto
    pub hash: String,               // blake3 de los campos de abajo (no de ofertas)
    pub barcodes: Vec<String>,
    pub troquel: Option<i64>,       // None si 0
    pub name: String,               // descripcion con espacios colapsados
    pub brand: Option<String>,      // laboratorio
    pub drug: Option<String>,       // nombresDrogas si no vacío
    pub form: Option<String>,       // formaFarmaceutica
    pub category: String, pub rubro: String, pub subrubro: String,
    pub therapeutic_actions: Vec<String>,
    pub price: Option<Decimal>,     // None si 0 (rust_decimal, 2 decimales)
    pub stock: i32,
    pub visible: bool,              // esVisibleEnVenta
    pub active: bool,               // !Baja
}
```

### Loop de sync
1. `fetch_all()` (lotes secuenciales). Si `NotAuthorized` → heartbeat con `erp_status=no_autorizado`, dormir hasta el próximo ciclo.
2. Normalizar → `CatalogItem` → hash.
3. Comparar contra `state.sqlite`. Delta = nuevos + hash distinto.
4. `POST /v1/sync/catalog` en lotes de 500, `mode: "delta"`. Si Remedia falla, encolar en SQLite y reintentar con backoff exponencial (30 s → 10 min).
5. Una vez cada 24 h enviar `mode: "full"` con **todos** los `external_id` + hash (sin payload completo) para que Remedia marque como `active=false` lo que ya no está.
6. Actualizar `state.sqlite` solo tras 2xx de Remedia.

Intervalo por defecto: 15 min. Configurable.

### Contrato con Remedia

`POST /v1/sync/catalog` — `Authorization: Bearer <token-sucursal>`
```json
{
  "schema_version": 1,
  "branch_id": "farmacia-xxx",
  "source": "observer-gestion",
  "mode": "delta",
  "batch": 1, "total_batches": 3,
  "generated_at": "2026-09-08T03:12:00-03:00",
  "items": [ CatalogItem, ... ]
}
```
Respuesta: `{ "received": 500, "upserted": 37, "unchanged": 463 }`.

`POST /v1/sync/full-manifest` (modo full): `{ "branch_id", "generated_at", "items": [ {"external_id","hash"}, ... ] }` → Remedia responde con los `external_id` cuyo hash no coincide para que el agente los reenvíe, y desactiva los ausentes.

`POST /v1/sync/heartbeat` cada 5 min:
```json
{ "branch_id": "...", "agent_version": "0.1.0", "erp_version": "2.5.5293.5",
  "erp_status": "ok|no_autorizado|inalcanzable", "last_sync_ok_at": "...",
  "catalog_count": 54235, "pending_batches": 0 }
```

Websocket `wss://<remedia>/v1/agent/ws` (Bearer en el handshake). Mensajes:
```json
→ { "op": "lookup", "req_id": "abc", "barcodes": ["779..."], "ids": [123] }
← { "op": "lookup_result", "req_id": "abc", "items": [ CatalogItem... ], "missing": ["779..."] }
→ { "op": "sync_now" }
← { "op": "pong" }  // respuesta a ping cada 30 s
```
Reconexión automática con backoff. Timeout del lookup contra el ERP: 3 s.

### `agent.toml`
```toml
branch_id = "farmacia-xxx"
remedia_url = "https://api.remedia.ar"
token = "..."
[erp]
kind = "observer"
base_url = "http://192.168.1.156:60064"
sync_interval_secs = 900
max_concurrency = 4
request_timeout_secs = 30
[log]
dir = "C:\\ProgramData\\RemediaAgent\\logs"
```

### CLI
- `agent.exe install --token T --erp http://IP:60064 --branch B` → escribe `agent.toml` en `C:\ProgramData\RemediaAgent\`, registra el servicio `RemediaAgent` (arranque automático, cuenta LocalService o NetworkService) y lo inicia.
- `agent.exe uninstall`, `agent.exe run` (foreground, para debug), `agent.exe sync-now`, `agent.exe status` (lee `state.sqlite` y último heartbeat).

### Crates
`tokio`, `reqwest` (rustls), `tokio-tungstenite`, `serde` + `serde_json`, `rusqlite` (bundled), `blake3`, `rust_decimal`, `windows-service`, `tracing` + `tracing-appender`, `clap`, `async-trait`, `thiserror`, `toml`.

Target: `x86_64-pc-windows-msvc`, release con `lto = true`, `panic = "abort"`, un solo `.exe`.

---

## 3. Casos que el agente debe manejar
- ERP apagado o IP cambiada → `erp_status=inalcanzable`, no borra estado local, sigue heartbeat.
- 401 `API_Productos` → `no_autorizado`.
- `cantidadLotes` cambia entre ciclos → usar siempre el valor del lote 1 de ese ciclo.
- Producto que desaparece del ERP → solo se detecta en el `full-manifest` diario.
- Remedia caído → cola local, sin pérdida.
- Reinicio de la PC → servicio automático, `state.sqlite` persiste.
- Log rotativo, máximo 7 días.

---

## 4. Tests
- Unit: normalización (`"COM x    8"` → `"COM x 8"`), hash estable ante reorden de `codigoBarras` (ordenarlos), `precio 0 → None`.
- Integración: mock HTTP del ERP con `tests/fixtures/lote1.json` (lote 1 real, 1107 productos) y `cantidadLotes=1`; verificar delta vacío en segunda corrida, delta de 1 al cambiar un stock.
- Simular 401 y 400 de fin de lotes.

---

## 5. Fuera de alcance de esta versión
- Ofertas / condiciones de pago por producto o por sucursal.
- Uso de `StockController` (evaluar cuando se verifique la ruta).
- Lectura de `ObserverGestion_<n>_Ext` (SQL Server, tablas `Estadisticas.ProductosVendidos`, `ControlActualizacion`) para rotación de productos.

---

## 6. Fixtures y material disponible
- `productos.json`: dump completo por ID (89.391 productos, ~80 MB).
- `lote1.json`: respuesta cruda de `/api/productos/lote/1`.
- `faltantes.txt`: IDs que dan 404 (huecos de ~1000 por reserva de secuencias).
- `observer_dlls/`: `ServiciosRestGestion.dll`, `Controladores.dll`, `Dominio.dll` + configs, para consultar con ILSpy.
- Pendiente: `ids_lote.txt` (IDs de los 49 lotes) para correr `diff_lote.py` y determinar el filtro del lote.

---

## 7. Anexo: decisiones de implementación (2026-09-08)

Acordadas antes de empezar a codificar.

1. **Ubicación.** El agente vive en `remedia-agent/` dentro de este repo, con su propio `Cargo.toml`. No participa del Dockerfile ni del deploy de Railway. Puede extraerse a un repo aparte sin cambios.
2. **Alcance.** Secciones 2, 3 y 4 completas. El lado servidor de Remedia (`/v1/sync/catalog`, `/v1/sync/full-manifest`, `/v1/sync/heartbeat`, `/v1/agent/ws`) **no existe todavía** en `app/` y queda fuera de este trabajo; el agente se valida contra mocks HTTP/WS.
3. **Fixtures.** No están disponibles en disco los archivos de §6. `tests/fixtures/lote1.json` se genera sintético con los casos medidos: producto sin CB, con varios CB, CB compartido por dos productos (`visiblesMismoCB = 2`), `precio = 0`, `troquel = 0`, `nombresDrogas` vacío, descripción con espacios múltiples, ofertas presentes. Reemplazar por el real cuando exista; los tests no dependen de cantidades absolutas.
4. **Filtro del lote (PENDIENTE de §1).** No verificable sin ERP. Se implementa el barrido diario por rango de `idProducto` como opción `[erp] daily_id_scan = false` (rango `id_scan_max`, concurrencia `max_concurrency`, reintentos). Apagado por defecto.
5. **Fuera de esta versión.** Ofertas (se parsean pero no se hashean ni se envían; `CatalogItem` no las incluye), `StockController`, SQL Server.
6. **Servicio de Windows.** Se compila y prueba en modo `run`. El registro real del servicio (`install`) requiere admin y no se ejecuta en la máquina de desarrollo.
7. **Lookup por CB con CB compartido.** Cuando el ERP devuelve un solo producto para un CB con `visiblesMismoCB > 1`, el agente devuelve ese producto; no intenta resolver los demás. Los `ids` del lookup se resuelven con `GET /api/productos/{id}` en paralelo (máx. `max_concurrency`).
8. **Hash.** blake3 sobre la serialización canónica de `CatalogItem` sin `hash`: barcodes ordenados y deduplicados, `therapeutic_actions` en el orden del ERP, `name` con espacios colapsados y recortados, `price` con 2 decimales.
9. **Estado local.** `state.sqlite` en el mismo directorio de `agent.toml` (`C:\ProgramData\RemediaAgent\` en producción; `--data-dir` para desarrollo). Tablas: `items(external_id PK, hash, updated_at)`, `pending(id PK, kind, payload, attempts, next_try_at)`, `meta(key, value)` (último sync OK, último full-manifest, último heartbeat).
