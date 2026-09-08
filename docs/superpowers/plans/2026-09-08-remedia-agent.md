# remedia-agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Un servicio de Windows en Rust (`remedia-agent/`) que lee el catálogo de ObServer Gestión por su API local, detecta cambios y los sube a Remedia, con websocket saliente para lookups en vivo.

**Architecture:** Un binario `agent.exe` con CLI (`install | uninstall | run | sync-now | status`). Módulos aislados: `erp/` (adapter ObServer detrás de `trait ErpAdapter`), `catalog/` (normalización + hash, estado SQLite, motor de sync), `remedia/` (cliente HTTP y websocket), `service.rs` (integración windows-service) y `logging.rs`. Todo el I/O es saliente; la lógica pura (normalizar, hashear, diff) está separada del I/O para testearla sin red.

**Tech Stack:** Rust 1.85 (`x86_64-pc-windows-msvc`), tokio, reqwest (rustls), tokio-tungstenite, serde/serde_json, rusqlite (bundled), blake3, rust_decimal, windows-service, tracing + tracing-appender, clap, async-trait, thiserror, toml, chrono, tokio-util. Tests con wiremock.

**Spec:** `docs/superpowers/specs/2026-09-08-remedia-agent-design.md`

## Global Constraints

- Directorio del proyecto: `remedia-agent/` en la raíz del repo. Binario `agent` (`agent.exe`).
- Target `x86_64-pc-windows-msvc`; perfil release con `lto = true`, `panic = "abort"`, `codegen-units = 1`.
- Nunca acepta conexiones entrantes.
- ERP: base `http://<ip>:60064`, header `Accept: application/json`, sin auth. 401 → `ErpError::NotAuthorized`. Conexión rechazada/timeout → `ErpError::Unreachable`.
- Concurrencia máxima contra el ERP: 4 (`max_concurrency`). Sync por lotes secuencial. Timeout de request 30 s; timeout de lookup en vivo 3 s.
- Clave del catálogo: `idProducto` (`external_id`). CB es índice secundario.
- `descripcion`: colapsar espacios múltiples y recortar antes de hashear y guardar.
- `precio == 0` → `price: None`. `troquel == 0` → `troquel: None`. `nombresDrogas` vacío → `drug: None`.
- `ofertas`: se parsean, no se hashean ni se envían.
- Hash blake3 sobre serialización canónica; `barcodes` ordenados y dedupe.
- Delta en lotes de 500, `mode: "delta"`. Full-manifest cada 24 h. Heartbeat cada 5 min. Intervalo de sync por defecto 900 s.
- Backoff de reintentos hacia Remedia: 30 s → 10 min exponencial.
- `state.sqlite` se actualiza solo tras 2xx de Remedia.
- Logs rotativos diarios, máximo 7 archivos, en `[log] dir`.
- Compilar en Windows: `cargo build` desde `remedia-agent/`. Tests: `cargo test` desde `remedia-agent/`.

---

## File Structure

```
remedia-agent/
├── Cargo.toml
├── .gitignore                 # target/, *.sqlite
├── README.md                  # instalación y operación
├── src/
│   ├── main.rs                # clap + dispatch de comandos
│   ├── config.rs              # Config (agent.toml), paths por defecto
│   ├── logging.rs             # tracing: stdout + archivo rotativo (7 días)
│   ├── service.rs             # windows-service: install/uninstall/dispatcher; run_agent()
│   ├── erp/
│   │   ├── mod.rs             # trait ErpAdapter, ErpError, build_adapter()
│   │   ├── model.rs           # ProductoDTO, Oferta, LoteResponse
│   │   └── observer.rs        # ObserverAdapter (reqwest)
│   ├── catalog/
│   │   ├── mod.rs
│   │   ├── item.rs            # CatalogItem, normalize_name(), from_dto(), hash
│   │   ├── state.rs           # State (SQLite): items, pending, meta
│   │   └── sync.rs            # SyncEngine: run_once, full_manifest, flush_pending, heartbeat
│   └── remedia/
│       ├── mod.rs
│       ├── client.rs          # RemediaClient + tipos del contrato
│       └── ws.rs              # run_ws(): websocket saliente, lookup / sync_now / ping
└── tests/
    ├── fixtures/lote1.json    # lote sintético con casos raros
    ├── common/mod.rs          # helpers: mock ERP con wiremock, State temporal
    ├── observer_adapter.rs    # fetch_all, 400 fin de lotes, 401, lookups
    ├── sync_flow.rs           # delta vacío, delta 1, cola cuando Remedia falla, full-manifest
    └── ws_lookup.rs           # servidor WS de prueba: lookup → lookup_result, sync_now
```

---

### Task 1: Scaffold del crate, config y logging

**Files:**
- Create: `remedia-agent/Cargo.toml`, `remedia-agent/.gitignore`, `remedia-agent/src/main.rs`, `remedia-agent/src/config.rs`, `remedia-agent/src/logging.rs`, `remedia-agent/src/erp/mod.rs` (vacío por ahora), `remedia-agent/src/catalog/mod.rs`, `remedia-agent/src/remedia/mod.rs`

**Interfaces:**
- Produces: `Config { branch_id: String, remedia_url: String, token: String, heartbeat_interval_secs: u64, erp: ErpConfig, log: LogConfig }`, `ErpConfig { kind: String, base_url: String, sync_interval_secs: u64, max_concurrency: usize, request_timeout_secs: u64, daily_id_scan: bool, id_scan_max: i64 }`, `LogConfig { dir: PathBuf }`. `Config::load(path: &Path) -> anyhow::Result<Config>`, `Config::from_toml(s: &str) -> anyhow::Result<Config>`, `Config::default_data_dir() -> PathBuf` (`C:\ProgramData\RemediaAgent`), `Config::write(&self, path) -> anyhow::Result<()>`. `logging::init(dir: Option<&Path>, to_stdout: bool) -> anyhow::Result<WorkerGuard>`.

- [x] **Step 1: Cargo.toml**

```toml
[package]
name = "remedia-agent"
version = "0.1.0"
edition = "2021"

[[bin]]
name = "agent"
path = "src/main.rs"

[dependencies]
tokio = { version = "1", features = ["rt-multi-thread", "macros", "time", "sync", "signal"] }
tokio-util = "0.7"
reqwest = { version = "0.12", default-features = false, features = ["rustls-tls", "json"] }
tokio-tungstenite = { version = "0.24", features = ["rustls-tls-webpki-roots"] }
futures-util = "0.3"
serde = { version = "1", features = ["derive"] }
serde_json = "1"
rusqlite = { version = "0.32", features = ["bundled"] }
blake3 = "1"
rust_decimal = { version = "1", features = ["serde"] }
tracing = "0.1"
tracing-subscriber = { version = "0.3", features = ["env-filter", "fmt"] }
tracing-appender = "0.2"
clap = { version = "4", features = ["derive"] }
async-trait = "0.1"
thiserror = "1"
anyhow = "1"
toml = "0.8"
chrono = { version = "0.4", features = ["serde", "clock"] }
http = "1"

[target.'cfg(windows)'.dependencies]
windows-service = "0.7"

[dev-dependencies]
wiremock = "0.6"
tempfile = "3"

[profile.release]
lto = true
panic = "abort"
codegen-units = 1
strip = true
```

- [x] **Step 2: Test de config (unit en `config.rs`)**

```rust
#[cfg(test)]
mod tests {
    use super::*;
    const SAMPLE: &str = r#"
branch_id = "farmacia-xxx"
remedia_url = "https://api.remedia.ar"
token = "abc"
[erp]
kind = "observer"
base_url = "http://192.168.1.156:60064"
[log]
dir = "C:\\ProgramData\\RemediaAgent\\logs"
"#;
    #[test]
    fn parses_with_defaults() {
        let c = Config::from_toml(SAMPLE).unwrap();
        assert_eq!(c.branch_id, "farmacia-xxx");
        assert_eq!(c.erp.sync_interval_secs, 900);
        assert_eq!(c.erp.max_concurrency, 4);
        assert_eq!(c.erp.request_timeout_secs, 30);
        assert!(!c.erp.daily_id_scan);
        assert_eq!(c.heartbeat_interval_secs, 300);
    }
    #[test]
    fn roundtrip_write() {
        let c = Config::from_toml(SAMPLE).unwrap();
        let s = toml::to_string(&c).unwrap();
        let c2 = Config::from_toml(&s).unwrap();
        assert_eq!(c2.erp.base_url, c.erp.base_url);
    }
}
```

- [x] **Step 3: Implementar `config.rs`** con `#[derive(Serialize, Deserialize)]` y `#[serde(default = "...")]` para cada default (900, 4, 30, false, 100_300, 300; `log.dir` default `<data_dir>\logs`).

- [x] **Step 4: Implementar `logging.rs`**: `tracing_subscriber::registry()` con `EnvFilter` (`RUST_LOG`, default `info`), capa fmt a stdout si `to_stdout`, capa fmt a `RollingFileAppender::builder().rotation(Rotation::DAILY).max_log_files(7).filename_prefix("agent").filename_suffix("log").build(dir)` si `dir` es `Some`. Devuelve el `WorkerGuard`.

- [x] **Step 5: `main.rs` mínimo** con clap: enum `Cmd { Install{token, erp, branch, data_dir}, Uninstall, Run{config}, SyncNow{config}, Status{config} }`, todos imprimen "no implementado" por ahora salvo que compile. `mod config; mod logging; mod erp; mod catalog; mod remedia;`.

- [x] **Step 6: `cargo test` pasa, `cargo build` compila. Commit** `remedia-agent: scaffold, config y logging`.

---

### Task 2: Modelo del ERP y `CatalogItem` con hash

**Files:**
- Create: `remedia-agent/src/erp/model.rs`, `remedia-agent/src/catalog/item.rs`
- Modify: `remedia-agent/src/erp/mod.rs`, `remedia-agent/src/catalog/mod.rs`

**Interfaces:**
- Produces:
  ```rust
  // erp/model.rs
  #[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
  #[serde(rename_all = "camelCase")]
  pub struct ProductoDTO {
      pub id_producto: i64,
      #[serde(default)] pub troquel: i64,
      #[serde(default)] pub codigo_barras: Vec<String>,
      #[serde(default)] pub descripcion: String,
      #[serde(default)] pub stock_sucursal: f64,
      #[serde(default)] pub precio: Decimal,
      #[serde(default)] pub categoria: String,
      #[serde(default)] pub rubro: String,
      #[serde(default)] pub subrubro: String,
      #[serde(default)] pub forma_farmaceutica: Option<String>,
      #[serde(default)] pub acciones_terapeuticas: Vec<String>,
      #[serde(default)] pub laboratorio: Option<String>,
      #[serde(default)] pub nombres_drogas: Option<String>,
      #[serde(default)] pub ofertas: Vec<Oferta>,
      #[serde(default = "default_true")] pub es_visible_en_venta: bool,
      #[serde(default, rename = "visiblesMismoCB")] pub visibles_mismo_cb: Option<i32>,
      #[serde(default, rename = "Baja")] pub baja: bool,
  }
  pub struct Oferta { pub his_id_condicion_comercial: i64, pub porcentaje: Decimal, pub descripcion: String } // #[serde(rename_all="camelCase")] + rename "his_IdCondicionComercial"
  pub struct LoteResponse { pub cantidad_lotes: u32, pub productos: Vec<ProductoDTO> }
  // catalog/item.rs
  #[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
  pub struct CatalogItem { external_id, hash, barcodes, troquel: Option<i64>, name, brand: Option<String>, drug: Option<String>, form: Option<String>, category, rubro, subrubro, therapeutic_actions: Vec<String>, price: Option<Decimal>, stock: i32, visible: bool, active: bool }
  pub fn normalize_name(s: &str) -> String;
  impl CatalogItem { pub fn from_dto(dto: &ProductoDTO) -> CatalogItem; pub fn compute_hash(&self) -> String; }
  ```

- [x] **Step 1: Tests unitarios en `item.rs`**

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use rust_decimal_macros::dec; // NO: usar Decimal::from_str para evitar dep extra
    fn dto() -> ProductoDTO { serde_json::from_str(r#"{
      "idProducto": 7454, "troquel": 4479051, "codigoBarras": ["7795336085205","1111"],
      "descripcion": "CLARITROMICINA RICHET 500 mg COM x    8", "stockSucursal": 0,
      "precio": 19641.2600, "categoria": "Medicamentos", "rubro": "Medicamentos", "subrubro": "Medicamentos",
      "formaFarmaceutica": "Comprimidos", "accionesTerapeuticas": ["Antibiótico"], "laboratorio": "Richet",
      "nombresDrogas": "Claritromicina",
      "ofertas": [{"his_IdCondicionComercial": 11, "porcentaje": -3.1, "descripcion": "-3,10%"}],
      "esVisibleEnVenta": true, "visiblesMismoCB": 1, "Baja": false }"#).unwrap() }

    #[test] fn collapses_spaces() { assert_eq!(normalize_name("  COM x    8 "), "COM x 8"); }
    #[test] fn hash_stable_on_barcode_reorder() {
        let a = CatalogItem::from_dto(&dto());
        let mut d = dto(); d.codigo_barras.reverse();
        let b = CatalogItem::from_dto(&d);
        assert_eq!(a.hash, b.hash); assert_eq!(a.barcodes, vec!["1111", "7795336085205"]);
    }
    #[test] fn price_zero_is_none_and_troquel_zero_is_none() {
        let mut d = dto(); d.precio = Decimal::ZERO; d.troquel = 0;
        let it = CatalogItem::from_dto(&d);
        assert!(it.price.is_none()); assert!(it.troquel.is_none());
    }
    #[test] fn ofertas_do_not_affect_hash() {
        let a = CatalogItem::from_dto(&dto());
        let mut d = dto(); d.ofertas.clear();
        assert_eq!(a.hash, CatalogItem::from_dto(&d).hash);
    }
    #[test] fn stock_change_changes_hash() {
        let a = CatalogItem::from_dto(&dto());
        let mut d = dto(); d.stock_sucursal = 3.0;
        assert_ne!(a.hash, CatalogItem::from_dto(&d).hash);
    }
    #[test] fn price_has_two_decimals_and_name_normalized() {
        let it = CatalogItem::from_dto(&dto());
        assert_eq!(it.price.unwrap().to_string(), "19641.26");
        assert_eq!(it.name, "CLARITROMICINA RICHET 500 mg COM x 8");
        assert_eq!(it.drug.as_deref(), Some("Claritromicina"));
    }
    #[test] fn empty_drug_is_none() {
        let mut d = dto(); d.nombres_drogas = Some("  ".into());
        assert!(CatalogItem::from_dto(&d).drug.is_none());
    }
}
```

- [x] **Step 2: Implementar.** `normalize_name` = `s.split_whitespace().collect::<Vec<_>>().join(" ")`. `from_dto`: barcodes `sort_unstable(); dedup()`, filtrar vacíos; `price = if precio.is_zero() {None} else {Some(precio.round_dp(2))}`; `stock = stock_sucursal.round() as i32`; `active = !baja`; luego `hash = compute_hash()`. `compute_hash`: serializar con `serde_json` una tupla/struct `HashInput<'a>` con todos los campos menos `hash` en orden fijo, `blake3::hash(bytes).to_hex()`.

- [x] **Step 3: `cargo test` pasa. Commit** `remedia-agent: ProductoDTO y CatalogItem con hash blake3`.

---

### Task 3: `trait ErpAdapter` + `ObserverAdapter` con mock HTTP

**Files:**
- Create: `remedia-agent/src/erp/observer.rs`, `remedia-agent/tests/fixtures/lote1.json`, `remedia-agent/tests/common/mod.rs`, `remedia-agent/tests/observer_adapter.rs`
- Modify: `remedia-agent/src/erp/mod.rs`

**Interfaces:**
- Produces:
  ```rust
  #[derive(Debug, thiserror::Error)]
  pub enum ErpError { #[error("ERP inalcanzable: {0}")] Unreachable(String), #[error("no autorizado (API_Productos deshabilitado)")] NotAuthorized, #[error("HTTP {0}: {1}")] Http(u16, String), #[error("decode: {0}")] Decode(String) }
  #[async_trait] pub trait ErpAdapter: Send + Sync {
      async fn fetch_all(&self) -> Result<Vec<ProductoDTO>, ErpError>;
      async fn lookup_by_barcodes(&self, barcodes: &[String]) -> Result<Vec<ProductoDTO>, ErpError>;
      async fn lookup_by_id(&self, id: i64) -> Result<Option<ProductoDTO>, ErpError>;
      async fn version(&self) -> Option<String> { None }
  }
  pub fn build_adapter(cfg: &ErpConfig) -> anyhow::Result<Arc<dyn ErpAdapter>>;
  pub struct ObserverAdapter; impl ObserverAdapter { pub fn new(base_url: &str, timeout: Duration) -> Self }
  ```
  `tests/common/mod.rs`: `pub async fn mock_erp(fixture: &str, cantidad_lotes: u32) -> MockServer` (monta `GET /api/productos/lote/1` → fixture; `lote/{n>cantidad}` → 400 JSON `{"Message":"El numeroLote=N no genera un conjunto de productos"}`), `pub fn lote1() -> LoteResponse`.

- [x] **Step 1: Fixture `lote1.json`** con `cantidadLotes: 1` y ~12 productos: id 7454 (el de la spec), uno sin CB, uno con 3 CB, dos con el mismo CB `7790000000001` y `visiblesMismoCB: 2`, uno con `precio: 0`, uno de Perfumería con `troquel: 0` y `nombresDrogas: ""`, uno con `esVisibleEnVenta: false`, uno con `stockSucursal: 12`, todos con las 5 ofertas LaPos/GetNet.

- [x] **Step 2: Tests de integración `observer_adapter.rs`**

```rust
mod common;
use remedia_agent::erp::{ErpAdapter, ErpError, observer::ObserverAdapter};
use wiremock::{Mock, ResponseTemplate, matchers::{method, path, body_json}};

#[tokio::test] async fn fetch_all_reads_all_lotes_and_stops_on_400() {
    let s = common::mock_erp("lote1.json", 1).await;
    let a = ObserverAdapter::new(&s.uri(), std::time::Duration::from_secs(5));
    let p = a.fetch_all().await.unwrap();
    assert_eq!(p.len(), common::lote1().productos.len());
}
#[tokio::test] async fn fetch_all_401_is_not_authorized() {
    let s = wiremock::MockServer::start().await;
    Mock::given(method("GET")).and(path("/api/productos/lote/1"))
        .respond_with(ResponseTemplate::new(401).set_body_string("No autorizado para obtener información de productos.")).mount(&s).await;
    let a = ObserverAdapter::new(&s.uri(), std::time::Duration::from_secs(5));
    assert!(matches!(a.fetch_all().await, Err(ErpError::NotAuthorized)));
}
#[tokio::test] async fn unreachable_erp() {
    let a = ObserverAdapter::new("http://127.0.0.1:1", std::time::Duration::from_secs(2));
    assert!(matches!(a.fetch_all().await, Err(ErpError::Unreachable(_))));
}
#[tokio::test] async fn lookup_by_barcodes_posts_list_and_404_is_empty() { /* POST /api/productos/codigosBarras body ["779..."] → [dto]; segundo caso 404 → Ok(vec![]) */ }
#[tokio::test] async fn lookup_by_id_404_is_none() { /* GET /api/productos/99 → 404 → Ok(None); GET /api/productos/7454 → Some */ }
#[tokio::test] async fn cantidad_lotes_from_first_lote_used_even_if_later_lotes_differ() { /* lote1 dice 2, lote2 dice 5, lote3 400 → fetch pide 1 y 2 solamente */ }
```
Exportar la lib: agregar `remedia-agent/src/lib.rs` con `pub mod config; pub mod erp; pub mod catalog; pub mod remedia; pub mod logging;` y hacer que `main.rs` use `remedia_agent::*`.

- [x] **Step 3: Implementar `observer.rs`.** Cliente reqwest con `default_headers(Accept: application/json)`, `timeout`. Helper `classify(resp) -> Result<Response, ErpError>`: 401 → NotAuthorized; error de conexión/timeout → Unreachable. `fetch_all`: `n=1`, primer lote define `total`; loop `for n in 1..=total` GET `lote/{n}`; 400 corta (`warn!`), otros ≥400 → `Http`. `lookup_by_barcodes`: POST JSON; 404 → `Ok(vec![])`. `lookup_by_id`: GET; 404 → `Ok(None)`. `build_adapter`: `kind == "observer"` → ObserverAdapter; otro → `bail!`.

- [x] **Step 4: `cargo test` pasa. Commit** `remedia-agent: adapter ObServer Gestión con tests contra mock HTTP`.

---

### Task 4: Estado SQLite

**Files:**
- Create: `remedia-agent/src/catalog/state.rs`
- Test: unit tests dentro de `state.rs` con `tempfile`

**Interfaces:**
- Produces:
  ```rust
  pub struct State { conn: Mutex<rusqlite::Connection> }
  #[derive(Debug, Clone)] pub struct Pending { pub id: i64, pub kind: String, pub payload: String, pub attempts: u32, pub next_try_at: i64 }
  impl State {
      pub fn open(path: &Path) -> anyhow::Result<State>;   // crea tablas si no existen
      pub fn open_in_memory() -> anyhow::Result<State>;
      pub fn known_hashes(&self) -> anyhow::Result<HashMap<String, String>>;
      pub fn upsert_hashes(&self, items: &[(String, String)]) -> anyhow::Result<()>; // transacción
      pub fn remove_items(&self, ids: &[String]) -> anyhow::Result<()>;
      pub fn count_items(&self) -> anyhow::Result<i64>;
      pub fn enqueue(&self, kind: &str, payload: &str) -> anyhow::Result<i64>;
      pub fn due_pending(&self, now: i64) -> anyhow::Result<Vec<Pending>>;   // next_try_at <= now, orden id
      pub fn pending_count(&self) -> anyhow::Result<i64>;
      pub fn mark_failed(&self, id: i64, next_try_at: i64) -> anyhow::Result<()>; // attempts+1
      pub fn remove_pending(&self, id: i64) -> anyhow::Result<()>;
      pub fn set_meta(&self, key: &str, value: &str) -> anyhow::Result<()>;
      pub fn get_meta(&self, key: &str) -> anyhow::Result<Option<String>>;
  }
  ```
  Claves de meta usadas: `last_sync_ok_at`, `last_full_manifest_at`, `last_heartbeat_at`, `erp_status`, `erp_version`, `catalog_count`.

- [x] **Step 1: Tests** — `upsert_then_known_hashes`, `enqueue_due_and_backoff` (encolar, `due_pending(now)` lo devuelve, `mark_failed(id, now+30)` → no due en `now`, sí en `now+30`, attempts=1), `meta_roundtrip`, `open_creates_file_and_reopens` (tempdir).

- [x] **Step 2: Implementar** con `PRAGMA journal_mode=WAL`, schema:
```sql
CREATE TABLE IF NOT EXISTS items(external_id TEXT PRIMARY KEY, hash TEXT NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS pending(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_try_at INTEGER NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
```

- [x] **Step 3: `cargo test` pasa. Commit** `remedia-agent: estado local SQLite (items, cola pendiente, meta)`.

---

### Task 5: Cliente Remedia (HTTP) y tipos del contrato

**Files:**
- Create: `remedia-agent/src/remedia/client.rs`
- Modify: `remedia-agent/src/remedia/mod.rs`

**Interfaces:**
- Produces:
  ```rust
  #[derive(Serialize, Deserialize)] pub struct CatalogBatch { pub schema_version: u32, pub branch_id: String, pub source: String, pub mode: String, pub batch: u32, pub total_batches: u32, pub generated_at: String, pub items: Vec<CatalogItem> }
  #[derive(Serialize, Deserialize, Default)] pub struct SyncResponse { pub received: u64, pub upserted: u64, pub unchanged: u64 }
  #[derive(Serialize, Deserialize)] pub struct ManifestEntry { pub external_id: String, pub hash: String }
  #[derive(Serialize, Deserialize)] pub struct FullManifest { pub branch_id: String, pub generated_at: String, pub items: Vec<ManifestEntry> }
  #[derive(Serialize, Deserialize, Default)] pub struct FullManifestResponse { #[serde(default)] pub resend: Vec<String>, #[serde(default)] pub deactivated: u64 }
  #[derive(Serialize, Deserialize)] pub struct Heartbeat { pub branch_id: String, pub agent_version: String, pub erp_version: Option<String>, pub erp_status: String, pub last_sync_ok_at: Option<String>, pub catalog_count: i64, pub pending_batches: i64 }
  #[derive(Debug, thiserror::Error)] pub enum RemediaError { #[error("remedia inalcanzable: {0}")] Unreachable(String), #[error("HTTP {0}: {1}")] Http(u16, String), #[error("decode: {0}")] Decode(String) }
  pub struct RemediaClient; impl RemediaClient {
      pub fn new(base_url: &str, token: &str, timeout: Duration) -> RemediaClient;
      pub async fn push_catalog(&self, b: &CatalogBatch) -> Result<SyncResponse, RemediaError>;   // POST /v1/sync/catalog
      pub async fn full_manifest(&self, m: &FullManifest) -> Result<FullManifestResponse, RemediaError>; // POST /v1/sync/full-manifest
      pub async fn heartbeat(&self, h: &Heartbeat) -> Result<(), RemediaError>;                  // POST /v1/sync/heartbeat
      pub fn ws_url(&self) -> String; // https→wss, http→ws, + /v1/agent/ws
  }
  pub fn now_rfc3339() -> String; // chrono::Local::now().to_rfc3339_opts(Secs, false)
  ```

- [x] **Step 1: Test** en `tests/remedia_client.rs`: wiremock con `header("authorization", "Bearer tok")` + `path("/v1/sync/catalog")` → 200 `{"received":1,"upserted":1,"unchanged":0}`; 500 → `Err(Http(500,_))`; `ws_url()` de `https://api.remedia.ar` = `wss://api.remedia.ar/v1/agent/ws`.

- [x] **Step 2: Implementar.** `Http` cuando `!status.is_success()`, cuerpo truncado a 300 chars.

- [x] **Step 3: `cargo test` pasa. Commit** `remedia-agent: cliente HTTP de Remedia (catalog, full-manifest, heartbeat)`.

---

### Task 6: Motor de sync (delta, cola, full-manifest, heartbeat)

**Files:**
- Create: `remedia-agent/src/catalog/sync.rs`, `remedia-agent/tests/sync_flow.rs`

**Interfaces:**
- Consumes: `ErpAdapter`, `State`, `RemediaClient`, `CatalogItem`.
- Produces:
  ```rust
  pub const BATCH_SIZE: usize = 500;
  pub const BACKOFF_MIN_SECS: u64 = 30; pub const BACKOFF_MAX_SECS: u64 = 600;
  pub fn compute_delta<'a>(items: &'a [CatalogItem], known: &HashMap<String,String>) -> Vec<&'a CatalogItem>;
  pub fn backoff_secs(attempts: u32) -> u64; // min(30 * 2^attempts, 600)
  #[derive(Debug, Default, PartialEq)] pub struct SyncReport { pub fetched: usize, pub changed: usize, pub sent_batches: usize, pub queued_batches: usize }
  pub struct SyncEngine { pub erp: Arc<dyn ErpAdapter>, pub remedia: Arc<RemediaClient>, pub state: Arc<State>, pub cfg: Arc<Config> }
  impl SyncEngine {
      pub async fn run_once(&self) -> anyhow::Result<SyncReport>;   // fetch → normalize → delta → push (o cola) → flush_pending
      pub async fn flush_pending(&self) -> anyhow::Result<usize>;    // reintenta lo vencido, actualiza items al 2xx
      pub async fn full_manifest(&self) -> anyhow::Result<usize>;    // manda todos los (id,hash) de state; reenvía los `resend` con un fetch fresco
      pub async fn send_heartbeat(&self) -> anyhow::Result<()>;
      pub async fn daily_id_scan(&self) -> anyhow::Result<Vec<ProductoDTO>>; // solo si cfg.erp.daily_id_scan; semáforo max_concurrency; 404 → skip
  }
  ```
  Comportamiento clave de `run_once`:
  1. `fetch_all`. `Err(NotAuthorized)` → `set_meta(erp_status, "no_autorizado")`, `Ok(report vacío)`. `Err(Unreachable)` → `"inalcanzable"`. `Ok` → `"ok"`.
  2. Items = `from_dto` de cada producto (si `daily_id_scan`, unir por `external_id` el resultado del scan).
  3. `delta = compute_delta(&items, &known)`, cortar en lotes de 500, `total_batches = ceil`.
  4. Por lote: `push_catalog`. 2xx → `upsert_hashes` de ese lote. Error → `enqueue("catalog", json del CatalogBatch)` con `next_try_at = now + 30`.
  5. Al final, si no hubo errores: `set_meta(last_sync_ok_at, now)`, `catalog_count = items.len()`.
  `flush_pending`: por cada `due_pending(now)`: deserializar `CatalogBatch`, `push_catalog`; 2xx → `upsert_hashes` + `remove_pending`; error → `mark_failed(id, now + backoff_secs(attempts+1))`; si `kind == "manifest"` lo mismo con `full_manifest`.
  `full_manifest`: entries desde `known_hashes()`; respuesta `resend` → `fetch_all` fresco, filtrar esos ids, `push_catalog` en lotes (misma lógica de cola); `set_meta(last_full_manifest_at)`.

- [x] **Step 1: Tests `sync_flow.rs`** (mock ERP de `common` + mock Remedia con wiremock; `State::open` en tempdir; `Config::from_toml` apuntando a ambos mocks):

```rust
#[tokio::test] async fn first_run_sends_everything_second_run_sends_nothing() {
    // Remedia: POST /v1/sync/catalog → 200 {"received":N,"upserted":N,"unchanged":0}
    let r1 = engine.run_once().await.unwrap();
    assert_eq!(r1.changed, common::lote1().productos.len()); assert_eq!(r1.sent_batches, 1);
    let r2 = engine.run_once().await.unwrap();
    assert_eq!(r2.changed, 0); assert_eq!(r2.sent_batches, 0);
}
#[tokio::test] async fn stock_change_produces_delta_of_one() {
    // primer run con fixture; luego remontar mock ERP con fixture donde 7454 tiene stockSucursal 5 (usar common::mock_erp_with(|lote| ...))
    assert_eq!(r2.changed, 1);
    // verificar por body_json_partial que el item enviado tiene external_id "7454" y stock 5
}
#[tokio::test] async fn remedia_down_queues_and_keeps_state_unchanged() {
    // Remedia 503 → run_once ok, report.queued_batches == 1, state.pending_count()==1, state.count_items()==0
    // luego Remedia 200 → flush_pending() (con next_try_at forzado a 0 vía state.mark_failed(id, 0)) → pending 0, count_items == N
}
#[tokio::test] async fn erp_401_sets_no_autorizado_and_sends_nothing() { /* erp_status meta == "no_autorizado", report.changed == 0 */ }
#[tokio::test] async fn full_manifest_resends_mismatched() {
    // tras run_once, mock /v1/sync/full-manifest → {"resend":["7454"],"deactivated":0}; full_manifest() → 1 item reenviado (segundo POST catalog con 1 item)
}
#[tokio::test] async fn compute_delta_and_backoff_unit() { assert_eq!(backoff_secs(0),30); assert_eq!(backoff_secs(1),60); assert_eq!(backoff_secs(10),600); }
```
`common::mock_erp_with(f: impl FnOnce(&mut LoteResponse))` para mutar el fixture antes de montarlo.

- [x] **Step 2: Implementar `sync.rs`** según el comportamiento de arriba. `send_heartbeat` arma `Heartbeat` desde meta (`erp_status` default `"inalcanzable"` si nunca corrió), `agent_version = env!("CARGO_PKG_VERSION")`, `erp_version = erp.version().await`, `pending_batches = pending_count()`.

- [x] **Step 3: `cargo test` pasa. Commit** `remedia-agent: motor de sync con delta, cola con backoff, full-manifest y heartbeat`.

---

### Task 7: Websocket saliente

**Files:**
- Create: `remedia-agent/src/remedia/ws.rs`, `remedia-agent/tests/ws_lookup.rs`

**Interfaces:**
- Produces:
  ```rust
  #[derive(Deserialize)] #[serde(tag = "op", rename_all = "snake_case")]
  pub enum Inbound { Lookup { req_id: String, #[serde(default)] barcodes: Vec<String>, #[serde(default)] ids: Vec<i64> }, SyncNow, Pong, Ping }
  #[derive(Serialize)] #[serde(tag = "op", rename_all = "snake_case")]
  pub enum Outbound { LookupResult { req_id: String, items: Vec<CatalogItem>, missing: Vec<String> }, Ping, Pong, Hello { branch_id: String, agent_version: String } }
  pub async fn handle_lookup(erp: &dyn ErpAdapter, barcodes: Vec<String>, ids: Vec<i64>, timeout: Duration, max_concurrency: usize) -> (Vec<CatalogItem>, Vec<String>);
  pub async fn run_ws(url: String, token: String, erp: Arc<dyn ErpAdapter>, sync_now: mpsc::Sender<()>, cfg: Arc<Config>, shutdown: CancellationToken);
  ```
  `run_ws`: loop hasta `shutdown`: conectar con `tokio_tungstenite::connect_async` a un `Request` con header `Authorization: Bearer`; enviar `Hello`; `select!` entre mensajes entrantes, `interval(30s)` → enviar `Ping` (JSON `{"op":"ping"}`) y cerrar si no llegó `pong` en 2 intervalos, y `shutdown`. Reconexión con backoff 5 s → 5 min. `Lookup` → `handle_lookup` con `timeout` 3 s por request al ERP; `missing` = barcodes/ids sin resultado (ids como string).

- [x] **Step 1: Test `ws_lookup.rs`**: levantar `TcpListener` + `tokio_tungstenite::accept_hdr_async` verificando header Authorization; el test envía `{"op":"lookup","req_id":"abc","barcodes":["7795336085205"],"ids":[999999]}` y espera un `lookup_result` con 1 item (`external_id == "7454"`) y `missing == ["999999"]` (ERP mock de `common` con `POST codigosBarras` y `GET /api/productos/999999` → 404). Segundo test: enviar `{"op":"sync_now"}` y verificar que `sync_now_rx.recv()` recibe. Cancelar el token al final.

- [x] **Step 2: Implementar.**

- [x] **Step 3: `cargo test` pasa. Commit** `remedia-agent: websocket saliente con lookup en vivo y sync_now`.

---

### Task 8: Run loop, servicio de Windows y CLI completa

**Files:**
- Create: `remedia-agent/src/service.rs`, `remedia-agent/README.md`
- Modify: `remedia-agent/src/main.rs`

**Interfaces:**
- Produces:
  ```rust
  pub async fn run_agent(cfg: Arc<Config>, data_dir: PathBuf, shutdown: CancellationToken) -> anyhow::Result<()>;
  // arma erp/remedia/state/engine; spawnea: loop de sync (interval sync_interval_secs + canal sync_now), loop de heartbeat (heartbeat_interval_secs), full-manifest cuando last_full_manifest_at > 24h, flush_pending cada 60 s, run_ws.
  #[cfg(windows)] pub fn install(data_dir: &Path, exe: &Path) -> anyhow::Result<()>;   // ServiceManager::local_computer + create_service(RemediaAgent, AutoStart, LocalService, args ["service", "--data-dir", dir]) + start
  #[cfg(windows)] pub fn uninstall() -> anyhow::Result<()>;
  #[cfg(windows)] pub fn run_as_service(data_dir: PathBuf) -> anyhow::Result<()>;      // define_windows_service! + service_dispatcher::start; handler Stop/Shutdown → cancel token
  ```
  CLI final: `install --token T --erp URL --branch B [--remedia URL] [--data-dir D]` escribe `agent.toml` (`Config::write`), llama `service::install`. `uninstall`. `run [--data-dir D]` (foreground, logs a stdout + archivo, Ctrl+C cancela). `service --data-dir D` (oculto; lo invoca el SCM). `sync-now [--data-dir D]` (un `run_once` + `flush_pending`, imprime `SyncReport`). `status [--data-dir D]` imprime meta + `count_items` + `pending_count`.

- [x] **Step 1: Test** unit en `service.rs`: `fn should_run_full_manifest(last: Option<&str>, now: DateTime<Local>) -> bool` (None → true; hace 25 h → true; hace 1 h → false).

- [x] **Step 2: Implementar `service.rs` y `main.rs`.** `cargo build --release` produce `target/release/agent.exe`. Probar manualmente `agent.exe run --data-dir <tmp>` con un `agent.toml` apuntando a un ERP inexistente: debe loguear `inalcanzable` y seguir vivo; Ctrl+C sale limpio.

- [x] **Step 3: README** con instalación (`agent.exe install ...`), operación (`status`, `sync-now`, logs), variables de `agent.toml`, y estado del punto pendiente del lote.

- [x] **Step 4: `cargo test` + `cargo build --release` pasan. Commit** `remedia-agent: run loop, servicio de Windows y CLI`.

---

## Self-Review

- **Cobertura de spec:** §1 endpoints (Task 3), DTO (Task 2), rendimiento/concurrencia (Task 3, 6, 7), §2 estructura (File Structure), trait (Task 3), CatalogItem (Task 2), loop de sync pasos 1-6 (Task 6, 8), contrato HTTP (Task 5), websocket (Task 7), agent.toml (Task 1), CLI (Task 8), crates y perfil release (Task 1), §3 casos: ERP apagado/401 (Task 6), cantidadLotes cambia (Task 3), desaparecidos vía full-manifest (Task 6), Remedia caído (Task 6), reinicio/servicio automático (Task 8), log 7 días (Task 1). §4 tests: cubiertos en Tasks 2, 3, 6. §7 anexo: id scan opcional (Task 6), meta keys (Task 4).
- **Placeholders:** ninguno; los tests marcados con comentario `/* ... */` describen el arreglo de mocks y las aserciones exactas.
- **Consistencia de tipos:** `State` API usada en Task 6 coincide con Task 4; `CatalogBatch`/`FullManifest` de Task 5 usadas en Task 6; `handle_lookup` de Task 7 usa `ErpAdapter` de Task 3 y `CatalogItem::from_dto` de Task 2.
