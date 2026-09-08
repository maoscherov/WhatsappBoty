//! Run loop del agente y su integración como servicio de Windows (`RemediaAgent`).
//!
//! `run_agent` es el corazón: lo usan tanto `agent.exe run` (foreground) como el
//! Service Control Manager. Las funciones de registro del servicio solo existen
//! en Windows; en otros targets compilan a un error explicativo.

use crate::catalog::state::{META_ERP_STATUS, META_LAST_FULL_MANIFEST, META_LAST_ID_SCAN};
use crate::catalog::{State, SyncEngine};
use crate::config::{Config, CONFIG_FILE, STATE_FILE};
use crate::erp::build_adapter;
use crate::remedia::client::now_rfc3339;
use crate::remedia::ws::run_ws;
use crate::remedia::RemediaClient;
use chrono::{DateTime, Local};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;
use tracing::{error, info, warn};

pub const SERVICE_NAME: &str = "RemediaAgent";
pub const SERVICE_DISPLAY_NAME: &str = "Remedia Agent";
pub const SERVICE_DESCRIPTION: &str =
    "Sincroniza el catálogo de ObServer Gestión con Remedia. Solo tráfico saliente.";
pub const FLUSH_INTERVAL: Duration = Duration::from_secs(60);
pub const FULL_MANIFEST_EVERY: chrono::Duration = chrono::Duration::hours(24);

pub fn resolve_data_dir(opt: Option<PathBuf>) -> PathBuf {
    opt.unwrap_or_else(Config::default_data_dir)
}

/// `true` si nunca se hizo o si el último fue hace más de 24 h. Una fecha
/// ilegible cuenta como "nunca".
pub fn should_run_full_manifest(last: Option<&str>, now: DateTime<Local>) -> bool {
    is_due(last, now, FULL_MANIFEST_EVERY)
}

fn is_due(last: Option<&str>, now: DateTime<Local>, every: chrono::Duration) -> bool {
    match last.and_then(|s| DateTime::parse_from_rfc3339(s).ok()) {
        Some(t) => now.signed_duration_since(t.with_timezone(&Local)) >= every,
        None => true,
    }
}

/// Carga `agent.toml`, abre `state.sqlite` y arma el motor de sync.
pub fn build_engine(data_dir: &Path) -> anyhow::Result<Arc<SyncEngine>> {
    let cfg = Arc::new(Config::load(&data_dir.join(CONFIG_FILE))?);
    let erp = build_adapter(&cfg.erp, cfg.request_timeout())?;
    let remedia = Arc::new(RemediaClient::new(&cfg.remedia_url, &cfg.token, Duration::from_secs(60)));
    let state = Arc::new(State::open(&data_dir.join(STATE_FILE))?);
    Ok(Arc::new(SyncEngine::new(erp, remedia, state, cfg)))
}

/// Loop principal. Termina cuando `shutdown` se cancela.
pub async fn run_agent(data_dir: PathBuf, shutdown: CancellationToken) -> anyhow::Result<()> {
    let engine = build_engine(&data_dir)?;
    let cfg = Arc::clone(&engine.cfg);
    info!(
        version = crate::AGENT_VERSION,
        branch = %cfg.branch_id,
        erp = %cfg.erp.base_url,
        remedia = %cfg.remedia_url,
        data_dir = %data_dir.display(),
        "agente iniciado"
    );

    let (sync_tx, mut sync_rx) = mpsc::channel::<()>(1);
    let mut tasks = tokio::task::JoinSet::new();

    tasks.spawn(run_ws(
        engine.remedia.ws_url(),
        cfg.token.clone(),
        Arc::clone(&engine.erp),
        sync_tx,
        Arc::clone(&cfg),
        shutdown.clone(),
    ));

    {
        let engine = Arc::clone(&engine);
        let shutdown = shutdown.clone();
        let every = cfg.heartbeat_interval();
        tasks.spawn(async move {
            let mut tick = tokio::time::interval(every);
            loop {
                tokio::select! {
                    _ = shutdown.cancelled() => return,
                    _ = tick.tick() => {
                        if let Err(e) = engine.send_heartbeat().await {
                            warn!(error = %e, "heartbeat falló");
                        }
                    }
                }
            }
        });
    }

    {
        let engine = Arc::clone(&engine);
        let shutdown = shutdown.clone();
        tasks.spawn(async move {
            let mut tick = tokio::time::interval(FLUSH_INTERVAL);
            tick.tick().await;
            loop {
                tokio::select! {
                    _ = shutdown.cancelled() => return,
                    _ = tick.tick() => {
                        match engine.flush_pending().await {
                            Ok(0) => {}
                            Ok(n) => info!(n, "lotes pendientes enviados"),
                            Err(e) => error!(error = %e, "flush de pendientes falló"),
                        }
                    }
                }
            }
        });
    }

    // Loop de sync: un ciclo ahora, después cada `sync_interval` o ante `sync_now`.
    loop {
        run_cycle(&engine).await;
        tokio::select! {
            _ = shutdown.cancelled() => break,
            _ = tokio::time::sleep(cfg.sync_interval()) => {}
            Some(()) = sync_rx.recv() => info!("ciclo adelantado por sync_now"),
        }
    }

    info!("deteniendo agente");
    shutdown.cancel();
    while tasks.join_next().await.is_some() {}
    Ok(())
}

/// Un ciclo completo: barrido por ID si toca, sync delta, full-manifest si toca.
pub async fn run_cycle(engine: &SyncEngine) {
    let now = Local::now();
    let state = &engine.state;

    let extra = if engine.cfg.erp.daily_id_scan
        && is_due(state.get_meta(META_LAST_ID_SCAN).ok().flatten().as_deref(), now, FULL_MANIFEST_EVERY)
    {
        match engine.daily_id_scan().await {
            Ok(v) => {
                let _ = state.set_meta(META_LAST_ID_SCAN, &now_rfc3339());
                v
            }
            Err(e) => {
                warn!(error = %e, "barrido por ID falló, se usa solo el lote");
                Vec::new()
            }
        }
    } else {
        Vec::new()
    };

    match engine.run_once_with(extra).await {
        Ok(r) => info!(
            erp = %r.erp_status, fetched = r.fetched, changed = r.changed,
            sent = r.sent_batches, queued = r.queued_batches, flushed = r.flushed_batches,
            "ciclo de sync terminado"
        ),
        Err(e) => {
            error!(error = %e, "ciclo de sync falló");
            return;
        }
    }

    let erp_ok = state.get_meta(META_ERP_STATUS).ok().flatten().as_deref() == Some("ok");
    let last = state.get_meta(META_LAST_FULL_MANIFEST).ok().flatten();
    if erp_ok && should_run_full_manifest(last.as_deref(), now) {
        match engine.full_manifest().await {
            Ok(n) => info!(resent = n, "full-manifest diario enviado"),
            Err(e) => warn!(error = %e, "full-manifest falló, se reintenta en el próximo ciclo"),
        }
    }
}

// ---------------------------------------------------------------------------
// Servicio de Windows
// ---------------------------------------------------------------------------

#[cfg(windows)]
pub mod win {
    use super::*;
    use std::ffi::OsString;
    use std::sync::OnceLock;
    use windows_service::service::{
        ServiceAccess, ServiceControl, ServiceControlAccept, ServiceErrorControl, ServiceExitCode,
        ServiceInfo, ServiceStartType, ServiceState, ServiceStatus, ServiceType,
    };
    use windows_service::service_control_handler::{self, ServiceControlHandlerResult};
    use windows_service::service_manager::{ServiceManager, ServiceManagerAccess};
    use windows_service::{define_windows_service, service_dispatcher};

    pub const SERVICE_ACCOUNT: &str = r"NT AUTHORITY\NetworkService";

    static DATA_DIR: OnceLock<PathBuf> = OnceLock::new();

    define_windows_service!(ffi_service_main, service_main);

    /// Entrada del SCM (`agent.exe service --data-dir D`). Bloquea hasta el Stop.
    pub fn run_as_service(data_dir: PathBuf) -> anyhow::Result<()> {
        let _ = DATA_DIR.set(data_dir);
        service_dispatcher::start(SERVICE_NAME, ffi_service_main)?;
        Ok(())
    }

    fn service_main(_args: Vec<OsString>) {
        let data_dir = DATA_DIR.get().cloned().unwrap_or_else(Config::default_data_dir);
        let log_dir = Config::load(&data_dir.join(CONFIG_FILE))
            .map(|c| c.log.dir)
            .unwrap_or_else(|_| data_dir.join("logs"));
        let _guard = crate::logging::init(Some(&log_dir), false).ok();

        let shutdown = CancellationToken::new();
        let sd = shutdown.clone();
        let handle = match service_control_handler::register(SERVICE_NAME, move |control| match control {
            ServiceControl::Stop | ServiceControl::Shutdown => {
                sd.cancel();
                ServiceControlHandlerResult::NoError
            }
            ServiceControl::Interrogate => ServiceControlHandlerResult::NoError,
            _ => ServiceControlHandlerResult::NotImplemented,
        }) {
            Ok(h) => h,
            Err(e) => {
                error!(error = %e, "no se pudo registrar el handler del servicio");
                return;
            }
        };

        let status = |state: ServiceState, code: u32| ServiceStatus {
            service_type: ServiceType::OWN_PROCESS,
            current_state: state,
            controls_accepted: ServiceControlAccept::STOP | ServiceControlAccept::SHUTDOWN,
            exit_code: ServiceExitCode::Win32(code),
            checkpoint: 0,
            wait_hint: Duration::default(),
            process_id: None,
        };
        let _ = handle.set_service_status(status(ServiceState::Running, 0));

        let result = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .map_err(anyhow::Error::from)
            .and_then(|rt| rt.block_on(run_agent(data_dir, shutdown)));
        let code = match result {
            Ok(()) => 0,
            Err(e) => {
                error!(error = %e, "el agente terminó con error");
                1
            }
        };
        let _ = handle.set_service_status(status(ServiceState::Stopped, code));
    }

    /// Registra el servicio (arranque automático, cuenta NetworkService) y lo inicia.
    pub fn install(data_dir: &Path, exe: &Path) -> anyhow::Result<()> {
        grant_data_dir(data_dir);
        let manager = ServiceManager::local_computer(
            None::<&str>,
            ServiceManagerAccess::CONNECT | ServiceManagerAccess::CREATE_SERVICE,
        )?;
        let info = ServiceInfo {
            name: OsString::from(SERVICE_NAME),
            display_name: OsString::from(SERVICE_DISPLAY_NAME),
            service_type: ServiceType::OWN_PROCESS,
            start_type: ServiceStartType::AutoStart,
            error_control: ServiceErrorControl::Normal,
            executable_path: exe.to_path_buf(),
            launch_arguments: vec![
                OsString::from("service"),
                OsString::from("--data-dir"),
                OsString::from(data_dir),
            ],
            dependencies: vec![],
            account_name: Some(OsString::from(SERVICE_ACCOUNT)),
            account_password: None,
        };
        let service = manager.create_service(&info, ServiceAccess::CHANGE_CONFIG | ServiceAccess::START)?;
        service.set_description(SERVICE_DESCRIPTION)?;
        service.start::<&str>(&[])?;
        Ok(())
    }

    /// Detiene (si corre) y elimina el servicio.
    pub fn uninstall() -> anyhow::Result<()> {
        let manager = ServiceManager::local_computer(None::<&str>, ServiceManagerAccess::CONNECT)?;
        let service = manager.open_service(
            SERVICE_NAME,
            ServiceAccess::QUERY_STATUS | ServiceAccess::STOP | ServiceAccess::DELETE,
        )?;
        if service.query_status()?.current_state != ServiceState::Stopped {
            let _ = service.stop();
            for _ in 0..20 {
                if service.query_status()?.current_state == ServiceState::Stopped {
                    break;
                }
                std::thread::sleep(Duration::from_millis(500));
            }
        }
        service.delete()?;
        Ok(())
    }

    /// La cuenta del servicio necesita escribir `state.sqlite` y los logs.
    fn grant_data_dir(data_dir: &Path) {
        let _ = std::fs::create_dir_all(data_dir);
        let out = std::process::Command::new("icacls")
            .arg(data_dir)
            .arg("/grant")
            .arg(format!("{SERVICE_ACCOUNT}:(OI)(CI)M"))
            .arg("/T")
            .output();
        match out {
            Ok(o) if o.status.success() => {}
            Ok(o) => warn!(stderr = %String::from_utf8_lossy(&o.stderr), "icacls devolvió error; revisá permisos del directorio de datos"),
            Err(e) => warn!(error = %e, "no se pudo ejecutar icacls"),
        }
    }
}

#[cfg(not(windows))]
pub mod win {
    use super::*;
    pub fn run_as_service(_data_dir: PathBuf) -> anyhow::Result<()> {
        anyhow::bail!("el modo servicio solo existe en Windows")
    }
    pub fn install(_data_dir: &Path, _exe: &Path) -> anyhow::Result<()> {
        anyhow::bail!("install solo existe en Windows")
    }
    pub fn uninstall() -> anyhow::Result<()> {
        anyhow::bail!("uninstall solo existe en Windows")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn full_manifest_due_rules() {
        let now = Local::now();
        assert!(should_run_full_manifest(None, now));
        assert!(should_run_full_manifest(Some("garbage"), now));
        let h25 = (now - chrono::Duration::hours(25)).to_rfc3339();
        assert!(should_run_full_manifest(Some(&h25), now));
        let h1 = (now - chrono::Duration::hours(1)).to_rfc3339();
        assert!(!should_run_full_manifest(Some(&h1), now));
        let h23 = (now - chrono::Duration::hours(23) - chrono::Duration::minutes(59)).to_rfc3339();
        assert!(!should_run_full_manifest(Some(&h23), now));
    }

    #[test]
    fn data_dir_defaults_to_programdata() {
        let d = resolve_data_dir(None);
        assert!(d.ends_with("RemediaAgent"), "{}", d.display());
        assert_eq!(resolve_data_dir(Some(PathBuf::from("x"))), PathBuf::from("x"));
    }
}
