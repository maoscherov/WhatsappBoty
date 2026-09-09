# remedia-agent Tray Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Icono de bandeja (`agent.exe tray`) que muestra estado y latencias del agente y permite cambiar token, URL del ERP y URL de Remedia sin admin, hablando con el servicio por un named pipe.

**Architecture:** El servicio gana `Metrics`, un `Runtime` con adapter/cliente intercambiables en caliente (`arc-swap`) y un servidor de named pipe con protocolo JSON de una línea. El tray es un proceso de usuario (`native-windows-gui`) que consulta `status` cada 5 s y manda comandos. `install` registra el tray en `HKLM\...\Run`.

**Tech Stack:** Rust, tokio named pipes, arc-swap, native-windows-gui, winreg, windows-sys.

**Spec:** `docs/superpowers/specs/2026-09-09-remedia-agent-tray-design.md`

## Global Constraints

- Pipe `\\.\pipe\RemediaAgent`, SDDL `D:(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGW;;;AU)`, una request por conexión, JSON por línea, timeout 10 s.
- `set_config`: token/URL de Remedia inválidos → rechazar sin guardar; ERP que no responde → guardar con warning.
- Nada entrante por red. Sin valores de token en logs.
- Textos en castellano para personal no técnico.
- Build/test con `~/.cargo/cargo-msvc.cmd` desde `remedia-agent/`.

---

### Task 1: `Metrics` y medición en el engine

**Files:** Create `src/metrics.rs`; Modify `src/erp/mod.rs` (`FetchResult`), `src/erp/observer.rs`, `src/catalog/sync.rs`, `src/remedia/client.rs` (`Heartbeat.metrics`), `src/remedia/ws.rs` (lookup ms, ws_connected, reconnects), `src/lib.rs`, tests que usan `fetch_all`.

**Interfaces:**
```rust
pub struct FetchResult { pub productos: Vec<ProductoDTO>, pub lotes: u32 }   // ErpAdapter::fetch_all -> Result<FetchResult, ErpError>
pub struct Metrics { .. }  // Arc<Metrics>
impl Metrics { pub fn new() -> Arc<Metrics>; record_fetch(ms: u64, lotes: u32); record_push(ms: u64); record_sync_total(ms); record_lookup(ms); record_heartbeat(ms); set_ws_connected(bool); inc_ws_reconnects(); snapshot() -> MetricsSnapshot }
#[derive(Serialize, Deserialize, Clone, Default, PartialEq)] pub struct MetricsSnapshot { erp_fetch_ms, erp_lote_avg_ms, remedia_push_avg_ms, sync_total_ms, lookup_last_ms, lookup_avg_ms, heartbeat_ms: Option<u64>, ws_reconnects: u64, ws_connected: bool, uptime_secs: u64 }
```
`SyncEngine` recibe `Arc<Metrics>` (campo `metrics`), persiste `META_METRICS_JSON` al final de `run_once_with`. `run_ws` recibe `Arc<Metrics>`.

- [x] Tests unit en `metrics.rs`: media móvil de lookup (α=0.2), `erp_lote_avg_ms = fetch/lotes`, snapshot default.
- [x] Implementar; adaptar `sync_flow.rs`/`observer_adapter.rs`/`ws_lookup.rs` a `FetchResult` y al nuevo parámetro.
- [x] `cargo test` verde. Commit.

### Task 2: Protocolo IPC + `Runtime` con configuración en caliente

**Files:** Create `src/ipc/mod.rs`, `src/runtime.rs`; Modify `src/service.rs` (`run_agent` usa `Runtime`), `src/catalog/sync.rs` (engine toma `Arc<ArcSwap<..>>`? → no: `SyncEngine` guarda `Arc<Runtime>`-independiente: campos `erp: ArcSwap<Arc<dyn ErpAdapter>>`? Decisión: `SyncEngine.erp: Arc<ArcSwap<Arc<dyn ErpAdapter>>>` y `remedia: Arc<ArcSwap<RemediaClient>>`, con helpers `erp()`/`remedia()` que hacen `load_full()`).

**Interfaces:**
```rust
// ipc/mod.rs
#[derive(Serialize, Deserialize)] #[serde(tag = "cmd", rename_all = "snake_case")]
pub enum Request { Status, SyncNow, TestErp { url: Option<String> }, TestRemedia { url: Option<String>, token: Option<String> }, SetConfig { token: Option<String>, erp_url: Option<String>, remedia_url: Option<String> } }
#[derive(Serialize, Deserialize)] #[serde(untagged)] pub enum Response { Status(StatusReport), Ok { ok: bool, #[serde(default)] warnings: Vec<String>, #[serde(default)] error: Option<String>, #[serde(default)] ms: Option<u64>, #[serde(default)] productos: Option<usize>, #[serde(default)] cantidad_lotes: Option<u32>, #[serde(default)] status: Option<String> } }
pub struct StatusReport { ...campos de spec §3... }
pub const PIPE_NAME: &str = r"\\.\pipe\RemediaAgent";
// runtime.rs
pub struct Runtime { pub cfg: RwLock<Config>, pub config_path: PathBuf, pub data_dir: PathBuf, pub erp: Arc<ArcSwap<Arc<dyn ErpAdapter>>>, pub remedia: Arc<ArcSwap<RemediaClient>>, pub state: Arc<State>, pub metrics: Arc<Metrics>, pub engine: Arc<SyncEngine>, sync_tx: mpsc::Sender<()>, ws: Mutex<Option<(CancellationToken, JoinHandle<()>)>>, shutdown: CancellationToken }
impl Runtime { pub fn build(data_dir, shutdown) -> Result<Arc<Runtime>>; pub fn start_ws(self: &Arc<Self>); pub async fn handle(self: &Arc<Self>, req: Request) -> Response; pub fn status(&self) -> Result<StatusReport>; async fn test_erp(url) ; async fn test_remedia(url, token); async fn set_config(..) }
```

- [x] Tests unit: serde de `Request`/`Response` (round trip de cada variante).
- [x] Tests integración `tests/runtime_config.rs`: `Runtime::build` sobre tempdir con `agent.toml` apuntando a mocks; `handle(Status)` devuelve `branch_id`; `handle(SetConfig{token:"bad"})` con Remedia respondiendo 401 → `ok:false` y `agent.toml` intacto; `SetConfig{token:"good", remedia_url: mock2}` → `ok:true`, archivo actualizado, `runtime.remedia.load().base_url() == mock2`; `SetConfig{erp_url: "http://127.0.0.1:1"}` → `ok:true` con 1 warning; `TestErp` contra mock → `productos == 12`.
- [x] Implementar. `service::run_agent` → `Runtime::build` + `start_ws` + loops existentes. Commit.

### Task 3: Servidor y cliente de named pipe

**Files:** Create `src/ipc/server.rs`, `src/ipc/client.rs`; Modify `src/service.rs` (spawn del server en `run_agent`), `src/main.rs` (`status` usa el pipe si está), `Cargo.toml` (`windows-sys` con `Win32_Security_Authorization`, `Win32_System_Pipes`).

**Interfaces:**
```rust
pub async fn serve(rt: Arc<Runtime>, shutdown: CancellationToken) -> anyhow::Result<()>;   // loop: ServerOptions con SECURITY_ATTRIBUTES del SDDL; por conexión: leer línea, rt.handle, escribir línea, cerrar
pub async fn call(req: &Request) -> anyhow::Result<Response>;   // ClientOptions::open(PIPE_NAME), timeout 10 s; error claro si no existe (servicio detenido)
pub fn call_blocking(req: &Request) -> anyhow::Result<Response>; // para el tray (runtime tokio propio)
pub fn security_attributes_from_sddl(sddl: &str) -> anyhow::Result<SecurityAttributes>; // guarda el descriptor vivo
```

- [x] Test integración `tests/ipc_pipe.rs` (cfg(windows)): pipe con nombre aleatorio (`PIPE_NAME` parametrizable via `serve_on(name, ..)`), `call_on(name, Status)` → `StatusReport`; `SyncNow` → `ok:true` y el receiver del canal recibe; request inválida → `ok:false` con error.
- [x] Implementar. Commit.

### Task 4: Iconos y tray

**Files:** Create `assets/tray_ok.ico`, `tray_warn.ico`, `tray_err.ico` (script `scripts/make_icons.py` en el scratchpad, resultado commiteado), `src/tray/mod.rs`, `src/tray/config_window.rs`, `src/tray/detail_window.rs`, `src/tray/state.rs` (`icon_state`, formateo de textos, puro); Modify `src/main.rs` (`Cmd::Tray { data_dir }`), `Cargo.toml` (`native-windows-gui`, `native-windows-derive`).

**Interfaces:**
```rust
pub enum IconState { Ok, Warn, Err, Down }
pub fn icon_state(report: Option<&StatusReport>) -> IconState;   // None = servicio detenido → Down
pub fn menu_lines(report: Option<&StatusReport>, now: DateTime<Local>) -> Vec<String>; // 4 líneas de estado
pub fn tooltip(report: Option<&StatusReport>, now) -> String;
pub fn run_tray(data_dir: Option<PathBuf>) -> anyhow::Result<()>;   // nwg::init, single-instance mutex, timer 5 s, menú, ventanas
```

- [x] Tests unit en `state.rs`: `icon_state` (None → Down; erp no_autorizado → Err; pendientes > 0 → Warn; ws desconectado → Warn; todo bien → Ok); `menu_lines` formatea "hace N min" y ms.
- [x] Implementar tray con nwg: `TrayNotification` + `Menu` + `AnimationTimer`/`Timer`; ventanas de configuración (3 campos, Probar, Guardar con confirmación) y detalle (texto + Copiar); acciones vía `ipc::client::call_blocking`.
- [x] Verificación manual: `agent.exe run --data-dir X` + `agent.exe tray --data-dir X`: icono aparece, menú muestra estado, "Sincronizar ahora" se ve en el log del servicio.
- [x] Commit.

### Task 5: Instalación del tray y `status` por pipe

**Files:** Modify `src/service.rs` (`win::install`: clave `Run` HKLM + lanzar tray; `win::uninstall`: borrar clave + evento `Global\RemediaAgentTrayQuit`), `src/main.rs` (`status` intenta pipe primero), `README.md`, `Cargo.toml` (`winreg`).

- [x] Implementar. `cargo test`, `cargo clippy`, `cargo build --release`. README: sección "Tray" (qué muestra, cómo cambiar token/URLs, qué hace cada color).
- [x] Commit.

## Self-Review
- Spec §2 protocolo → Task 2/3. §3 métricas → Task 1. §4 tray → Task 4. §5 instalación → Task 5. §7 casos: servicio detenido (client error → Down), set_config con sync en curso (ArcSwap), token rotado (heartbeat error → `last_error`, Task 1 registra `META_LAST_HEARTBEAT_ERROR`), status por consola (Task 5).
- Tipos: `FetchResult` usado en engine y tests; `Request/Response` compartidos por server, client, tray.
