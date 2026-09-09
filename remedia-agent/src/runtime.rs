//! `Runtime`: todo lo que vive mientras el agente corre (config, motor de sync,
//! métricas, websocket) más los comandos que el tray manda por el pipe:
//! estado, pruebas de conexión y cambio de configuración en caliente.

use crate::catalog::state::{
    META_CATALOG_COUNT, META_ERP_STATUS, META_ERP_VERSION, META_LAST_HEARTBEAT,
    META_LAST_HEARTBEAT_ERROR, META_LAST_SYNC_AT, META_LAST_SYNC_CHANGED, META_LAST_SYNC_ERROR,
    META_LAST_SYNC_FETCHED, META_LAST_SYNC_OK,
};
use crate::catalog::{State, SyncEngine};
use crate::config::{Config, CONFIG_FILE, STATE_FILE};
use crate::erp::{build_adapter, ErpAdapter};
use crate::ipc::{Request, Response, StatusReport};
use crate::metrics::{elapsed_ms, Metrics};
use crate::remedia::ws::run_ws;
use crate::remedia::RemediaClient;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;
use tracing::{info, warn};

/// Timeout de las pruebas de conexión pedidas desde el tray.
pub const PROBE_TIMEOUT: Duration = Duration::from_secs(8);
pub const REMEDIA_TIMEOUT: Duration = Duration::from_secs(60);

struct WsTask {
    cancel: CancellationToken,
    handle: JoinHandle<()>,
}

pub struct Runtime {
    pub data_dir: PathBuf,
    pub config_path: PathBuf,
    cfg: RwLock<Config>,
    pub engine: Arc<SyncEngine>,
    pub state: Arc<State>,
    pub metrics: Arc<Metrics>,
    sync_tx: mpsc::Sender<()>,
    ws: Mutex<Option<WsTask>>,
    shutdown: CancellationToken,
}

impl Runtime {
    /// Carga `agent.toml`, abre `state.sqlite` y arma el motor. Devuelve también
    /// el receptor de `sync_now` para el loop de sync.
    pub fn build(data_dir: &Path, shutdown: CancellationToken) -> anyhow::Result<(Arc<Runtime>, mpsc::Receiver<()>)> {
        let config_path = data_dir.join(CONFIG_FILE);
        let cfg = Config::load(&config_path)?;
        let erp = build_adapter(&cfg.erp, cfg.request_timeout())?;
        let remedia = Arc::new(RemediaClient::new(&cfg.remedia_url, &cfg.token, REMEDIA_TIMEOUT));
        let state = Arc::new(State::open(&data_dir.join(STATE_FILE))?);
        let metrics = Metrics::new();
        let engine = Arc::new(SyncEngine::new(
            erp,
            remedia,
            Arc::clone(&state),
            Arc::new(cfg.clone()),
            Arc::clone(&metrics),
        ));
        let (sync_tx, sync_rx) = mpsc::channel(1);
        let rt = Arc::new(Runtime {
            data_dir: data_dir.to_path_buf(),
            config_path,
            cfg: RwLock::new(cfg),
            engine,
            state,
            metrics,
            sync_tx,
            ws: Mutex::new(None),
            shutdown,
        });
        Ok((rt, sync_rx))
    }

    pub fn config(&self) -> Config {
        self.cfg.read().unwrap_or_else(|e| e.into_inner()).clone()
    }

    pub fn sync_now(&self) -> bool {
        self.sync_tx.try_send(()).is_ok()
    }

    /// (Re)lanza la tarea del websocket con la configuración actual.
    pub fn start_ws(self: &Arc<Self>) {
        let cfg = self.config();
        let cancel = self.shutdown.child_token();
        let handle = tokio::spawn(run_ws(
            self.engine.remedia().ws_url(),
            cfg.token.clone(),
            self.engine.erp(),
            self.sync_tx.clone(),
            Arc::new(cfg),
            Arc::clone(&self.metrics),
            cancel.clone(),
        ));
        let prev = self.ws.lock().unwrap_or_else(|e| e.into_inner()).replace(WsTask { cancel, handle });
        if let Some(p) = prev {
            p.cancel.cancel();
            p.handle.abort();
        }
    }

    pub async fn stop_ws(&self) {
        let prev = self.ws.lock().unwrap_or_else(|e| e.into_inner()).take();
        if let Some(p) = prev {
            p.cancel.cancel();
            let _ = tokio::time::timeout(Duration::from_secs(3), p.handle).await;
        }
    }

    // ---- comandos del pipe -------------------------------------------------

    pub async fn handle(self: &Arc<Self>, req: Request) -> Response {
        match req {
            Request::Status => match self.status_report() {
                Ok(r) => Response::status(r),
                Err(e) => Response::err(format!("no se pudo leer el estado: {e}")),
            },
            Request::SyncNow => {
                if self.sync_now() {
                    Response::ok()
                } else {
                    // El canal tiene capacidad 1: ya hay un pedido en curso.
                    Response { ok: true, warnings: vec!["Ya había una sincronización pedida".into()], ..Default::default() }
                }
            }
            Request::TestErp { url } => self.test_erp(url.as_deref()).await,
            Request::TestRemedia { url, token } => self.test_remedia(url.as_deref(), token.as_deref()).await,
            Request::SetConfig { token, erp_url, remedia_url } => self.set_config(token, erp_url, remedia_url).await,
        }
    }

    pub fn status_report(&self) -> anyhow::Result<StatusReport> {
        let cfg = self.config();
        let get = |k: &str| self.state.get_meta(k);
        let non_empty = |v: Option<String>| v.filter(|s| !s.is_empty());
        let num = |v: Option<String>| v.and_then(|s| s.parse::<u64>().ok());
        let last_error = non_empty(get(META_LAST_HEARTBEAT_ERROR)?).or(non_empty(get(META_LAST_SYNC_ERROR)?));
        Ok(StatusReport {
            agent_version: crate::AGENT_VERSION.to_string(),
            branch_id: cfg.branch_id.clone(),
            erp_url: cfg.erp.base_url.clone(),
            remedia_url: cfg.remedia_url.clone(),
            config_path: self.config_path.display().to_string(),
            log_dir: cfg.log.dir.display().to_string(),
            erp_status: get(META_ERP_STATUS)?.unwrap_or_else(|| "desconocido".into()),
            erp_version: non_empty(get(META_ERP_VERSION)?),
            ws_connected: self.metrics.ws_connected(),
            last_sync_ok_at: get(META_LAST_SYNC_OK)?,
            last_sync_at: get(META_LAST_SYNC_AT)?,
            last_sync_fetched: num(get(META_LAST_SYNC_FETCHED)?),
            last_sync_changed: num(get(META_LAST_SYNC_CHANGED)?),
            catalog_count: num(get(META_CATALOG_COUNT)?),
            pending_batches: self.state.pending_count()? as u64,
            last_heartbeat_at: get(META_LAST_HEARTBEAT)?,
            last_error,
            metrics: self.metrics.snapshot(),
        })
    }

    async fn test_erp(&self, url: Option<&str>) -> Response {
        let cfg = self.config();
        let mut erp_cfg = cfg.erp.clone();
        if let Some(u) = url {
            if let Err(e) = validate_url(u) {
                return Response::err(e);
            }
            erp_cfg.base_url = u.to_string();
        }
        let adapter = match build_adapter(&erp_cfg, PROBE_TIMEOUT) {
            Ok(a) => a,
            Err(e) => return Response::err(e.to_string()),
        };
        probe_erp(adapter.as_ref()).await
    }

    async fn test_remedia(&self, url: Option<&str>, token: Option<&str>) -> Response {
        let cfg = self.config();
        let url = url.unwrap_or(&cfg.remedia_url);
        if let Err(e) = validate_url(url) {
            return Response::err(e);
        }
        let token = token.unwrap_or(&cfg.token);
        if token.trim().is_empty() {
            return Response::err("El token no puede estar vacío");
        }
        let client = RemediaClient::new(url, token, PROBE_TIMEOUT);
        self.probe_remedia(&client).await
    }

    async fn probe_remedia(&self, client: &RemediaClient) -> Response {
        let hb = match self.engine.build_heartbeat() {
            Ok(h) => h,
            Err(e) => return Response::err(e.to_string()),
        };
        let t = Instant::now();
        match client.heartbeat(&hb).await {
            Ok(()) => Response { ok: true, ms: Some(elapsed_ms(t)), ..Default::default() },
            Err(e) => Response::err(remedia_error_text(&e)),
        }
    }

    /// Valida, prueba, escribe `agent.toml` y aplica en caliente.
    async fn set_config(
        self: &Arc<Self>,
        token: Option<String>,
        erp_url: Option<String>,
        remedia_url: Option<String>,
    ) -> Response {
        let token = token.map(|t| t.trim().to_string()).filter(|t| !t.is_empty());
        let erp_url = erp_url.map(|u| u.trim().trim_end_matches('/').to_string()).filter(|u| !u.is_empty());
        let remedia_url = remedia_url.map(|u| u.trim().trim_end_matches('/').to_string()).filter(|u| !u.is_empty());
        if token.is_none() && erp_url.is_none() && remedia_url.is_none() {
            return Response::err("No hay nada para cambiar");
        }
        for u in [&erp_url, &remedia_url].into_iter().flatten() {
            if let Err(e) = validate_url(u) {
                return Response::err(e);
            }
        }

        let current = self.config();
        let mut next = current.clone();
        if let Some(t) = &token {
            next.token = t.clone();
        }
        if let Some(u) = &erp_url {
            next.erp.base_url = u.clone();
        }
        if let Some(u) = &remedia_url {
            next.remedia_url = u.clone();
        }
        let remedia_changed = next.token != current.token || next.remedia_url != current.remedia_url;
        let erp_changed = next.erp.base_url != current.erp.base_url;
        let mut warnings = Vec::new();

        // Token o URL de Remedia inválidos: se rechaza sin guardar.
        let new_remedia = Arc::new(RemediaClient::new(&next.remedia_url, &next.token, REMEDIA_TIMEOUT));
        if remedia_changed {
            let probe = RemediaClient::new(&next.remedia_url, &next.token, PROBE_TIMEOUT);
            let r = self.probe_remedia(&probe).await;
            if !r.ok {
                return Response::err(format!(
                    "Remedia no aceptó la configuración nueva: {}",
                    r.error.unwrap_or_default()
                ));
            }
        }

        // ERP que no responde: se guarda igual, con aviso.
        let new_erp: Arc<dyn ErpAdapter> = match build_adapter(&next.erp, next.request_timeout()) {
            Ok(a) => a,
            Err(e) => return Response::err(e.to_string()),
        };
        if erp_changed {
            let probe = build_adapter(&next.erp, PROBE_TIMEOUT).ok();
            let r = match probe {
                Some(p) => probe_erp(p.as_ref()).await,
                None => Response::err("adapter"),
            };
            if !r.ok {
                warnings.push(format!(
                    "El ERP no respondió en {}: se guardó igual. {}",
                    next.erp.base_url,
                    r.error.unwrap_or_default()
                ));
            }
        }

        if let Err(e) = next.write(&self.config_path) {
            return Response::err(format!("No se pudo escribir agent.toml: {e}"));
        }
        *self.cfg.write().unwrap_or_else(|e| e.into_inner()) = next;
        if erp_changed {
            self.engine.set_erp(new_erp);
        }
        if remedia_changed {
            self.engine.set_remedia(new_remedia);
        }
        if (remedia_changed || erp_changed) && self.ws.lock().unwrap_or_else(|e| e.into_inner()).is_some() {
            self.start_ws();
        }
        let mut fields = Vec::new();
        if token.is_some() {
            fields.push("token");
        }
        if erp_changed {
            fields.push("erp_url");
        }
        if remedia_changed {
            fields.push("remedia_url");
        }
        info!(campos = ?fields, "configuración cambiada desde el tray");
        if !warnings.is_empty() {
            warn!(?warnings, "configuración guardada con avisos");
        }
        Response { ok: true, warnings, ..Default::default() }
    }
}

async fn probe_erp(adapter: &dyn ErpAdapter) -> Response {
    let t = Instant::now();
    match adapter.probe().await {
        Ok(p) => Response {
            ok: true,
            ms: Some(elapsed_ms(t)),
            productos: Some(p.productos),
            cantidad_lotes: Some(p.cantidad_lotes),
            ..Default::default()
        },
        Err(e) => Response {
            ok: false,
            error: Some(erp_error_text(&e)),
            status_label: Some(e.status_label().to_string()),
            ms: Some(elapsed_ms(t)),
            ..Default::default()
        },
    }
}

/// Acepta solo la base del servidor (`https://host[:puerto]`). Una ruta como
/// `/bo/branches` es un error típico: el agente agrega `/v1/sync/...` solo.
pub fn validate_url(u: &str) -> Result<(), String> {
    let parsed = reqwest::Url::parse(u).map_err(|_| format!("Dirección inválida: {u}"))?;
    match parsed.scheme() {
        "http" | "https" => {}
        s => return Err(format!("La dirección tiene que empezar con http:// o https:// (no {s}://)")),
    }
    let Some(host) = parsed.host_str() else {
        return Err(format!("La dirección no tiene servidor: {u}"));
    };
    let has_path = !matches!(parsed.path(), "" | "/");
    if has_path || parsed.query().is_some() || parsed.fragment().is_some() {
        let port = parsed.port().map(|p| format!(":{p}")).unwrap_or_default();
        return Err(format!(
            "Poné solo el servidor, sin ruta: {}://{host}{port} (no {})",
            parsed.scheme(),
            parsed.path()
        ));
    }
    Ok(())
}

/// Textos para personal no técnico.
pub fn erp_error_text(e: &crate::erp::ErpError) -> String {
    use crate::erp::ErpError::*;
    match e {
        NotAuthorized => "El ERP tiene deshabilitada la API de productos (configuración de ObServer: flag API_Productos).".into(),
        Unreachable(_) => "No se pudo conectar con el ERP. Revisá que la PC del ERP esté encendida y la dirección sea correcta.".into(),
        Http(code, _) => format!("El ERP respondió con error HTTP {code}."),
        Decode(d) => format!("El ERP respondió algo que no se entiende: {d}"),
    }
}

pub fn remedia_error_text(e: &crate::remedia::RemediaError) -> String {
    use crate::remedia::RemediaError::*;
    match e {
        Unreachable(_) => "No se pudo conectar con Remedia. Revisá la conexión a internet y la dirección.".into(),
        Http(401, _) | Http(403, _) => "Remedia rechazó el token (no es válido o pertenece a otra sucursal).".into(),
        Http(404, _) => "Remedia respondió 404: esa dirección no es la de la API. Tiene que ser solo el servidor, por ejemplo https://cerca.remedia.ar".into(),
        Http(code, _) => format!("Remedia respondió con error HTTP {code}."),
        Decode(d) => format!("Remedia respondió algo que no se entiende: {d}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn url_validation() {
        assert!(validate_url("http://192.168.1.156:60064").is_ok());
        assert!(validate_url("https://api.remedia.ar/").is_ok());
        assert!(validate_url("192.168.1.156:60064").is_err());
        assert!(validate_url("ftp://x").is_err());
        assert!(validate_url("").is_err());
        let e = validate_url("https://cerca.remedia.ar/bo/branches").unwrap_err();
        assert!(e.contains("https://cerca.remedia.ar"), "{e}");
        assert!(e.contains("/bo/branches"), "{e}");
        let e = validate_url("http://192.168.1.156:60064/api/productos").unwrap_err();
        assert!(e.contains("http://192.168.1.156:60064"), "{e}");
        assert!(validate_url("https://x.ar/?a=1").is_err());
    }
}
