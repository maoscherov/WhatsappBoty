//! Run loop del agente y su integración como servicio de Windows (`RemediaAgent`).
//!
//! `run_agent` es el corazón: lo usan tanto `agent.exe run` (foreground) como el
//! Service Control Manager. Las funciones de registro del servicio solo existen
//! en Windows; en otros targets compilan a un error explicativo.

use crate::catalog::state::{META_ERP_STATUS, META_LAST_FULL_MANIFEST, META_LAST_ID_SCAN};
use crate::catalog::{State, SyncEngine};
use crate::config::{Config, CONFIG_FILE, STATE_FILE};
use crate::erp::build_adapter;
use crate::metrics::Metrics;
use crate::remedia::client::now_rfc3339;
use crate::remedia::RemediaClient;
use crate::runtime::Runtime;
use chrono::{DateTime, Local};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;
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
    Ok(Arc::new(SyncEngine::new(erp, remedia, state, cfg, Metrics::new())))
}

/// Loop principal. Termina cuando `shutdown` se cancela.
pub async fn run_agent(data_dir: PathBuf, shutdown: CancellationToken) -> anyhow::Result<()> {
    let (rt, mut sync_rx) = Runtime::build(&data_dir, shutdown.clone())?;
    let engine = Arc::clone(&rt.engine);
    let cfg = Arc::clone(&engine.cfg);
    info!(
        version = crate::AGENT_VERSION,
        branch = %cfg.branch_id,
        erp = %cfg.erp.base_url,
        remedia = %cfg.remedia_url,
        data_dir = %data_dir.display(),
        "agente iniciado"
    );

    let mut tasks = tokio::task::JoinSet::new();
    rt.start_ws();

    {
        let rt = Arc::clone(&rt);
        let shutdown = shutdown.clone();
        tasks.spawn(async move {
            if let Err(e) = crate::ipc::server::serve(rt, shutdown).await {
                error!(error = %e, "el pipe IPC (tray) no pudo iniciarse; el tray no va a funcionar");
            }
        });
    }

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
    rt.stop_ws().await;
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

    /// Detiene el servicio si existe y está corriendo (para poder reemplazar el
    /// exe). Devuelve `true` si el servicio existía.
    pub fn stop_if_running() -> anyhow::Result<bool> {
        let manager = ServiceManager::local_computer(None::<&str>, ServiceManagerAccess::CONNECT)?;
        let service = match manager.open_service(SERVICE_NAME, ServiceAccess::QUERY_STATUS | ServiceAccess::STOP) {
            Ok(s) => s,
            Err(windows_service::Error::Winapi(e)) if e.raw_os_error() == Some(ERROR_SERVICE_DOES_NOT_EXIST) => {
                return Ok(false)
            }
            Err(e) => return Err(e.into()),
        };
        stop_and_wait(&service)?;
        Ok(true)
    }

    const ERROR_SERVICE_DOES_NOT_EXIST: i32 = 1060;
    const ERROR_SERVICE_EXISTS: i32 = 1073;

    fn stop_and_wait(service: &windows_service::service::Service) -> anyhow::Result<()> {
        if service.query_status()?.current_state == ServiceState::Stopped {
            return Ok(());
        }
        let _ = service.stop();
        for _ in 0..30 {
            if service.query_status()?.current_state == ServiceState::Stopped {
                // Un instante más: el SCM libera el exe después de reportar Stopped.
                std::thread::sleep(Duration::from_millis(500));
                return Ok(());
            }
            std::thread::sleep(Duration::from_millis(500));
        }
        anyhow::bail!("el servicio no se detuvo a tiempo")
    }

    /// Registra el servicio (arranque automático, cuenta NetworkService) y lo
    /// inicia. Si ya existe, actualiza su configuración y lo reinicia: el
    /// mismo comando sirve para instalar y para actualizar.
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
        let access = ServiceAccess::CHANGE_CONFIG | ServiceAccess::START | ServiceAccess::STOP | ServiceAccess::QUERY_STATUS;
        let service = match manager.create_service(&info, access) {
            Ok(s) => s,
            Err(windows_service::Error::Winapi(e)) if e.raw_os_error() == Some(ERROR_SERVICE_EXISTS) => {
                let s = manager.open_service(SERVICE_NAME, access)?;
                stop_and_wait(&s)?;
                s.change_config(&info)?;
                s
            }
            Err(e) => return Err(e.into()),
        };
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

    // ---- tray -------------------------------------------------------------

    const RUN_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";
    const RUN_VALUE: &str = "RemediaAgentTray";

    /// El tray arranca al iniciar sesión, para todos los usuarios de la PC (HKLM).
    pub fn register_tray_autostart(tray_exe: &Path) -> anyhow::Result<()> {
        use winreg::enums::HKEY_LOCAL_MACHINE;
        use winreg::RegKey;
        let (key, _) = RegKey::predef(HKEY_LOCAL_MACHINE).create_subkey(RUN_KEY)?;
        key.set_value(RUN_VALUE, &format!("\"{}\"", tray_exe.display()))?;
        Ok(())
    }

    pub fn unregister_tray_autostart() -> anyhow::Result<()> {
        use winreg::enums::{HKEY_LOCAL_MACHINE, KEY_SET_VALUE};
        use winreg::RegKey;
        match RegKey::predef(HKEY_LOCAL_MACHINE).open_subkey_with_flags(RUN_KEY, KEY_SET_VALUE) {
            Ok(key) => match key.delete_value(RUN_VALUE) {
                Ok(()) => Ok(()),
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
                Err(e) => Err(e.into()),
            },
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(e.into()),
        }
    }

    /// Lanza el tray para la sesión actual (sin esperar al próximo inicio de
    /// sesión), desacoplado de la consola desde la que corre `install`: si el
    /// usuario la cierra, el tray sigue.
    pub fn launch_tray(tray_exe: &Path) {
        use std::os::windows::process::CommandExt;
        const DETACHED_PROCESS: u32 = 0x0000_0008;
        const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
        if let Err(e) = std::process::Command::new(tray_exe)
            .creation_flags(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
            .spawn()
        {
            warn!(error = %e, "no se pudo lanzar el tray");
        }
    }

    /// Suelta la consola del proceso actual (para `agent.exe tray` lanzado a
    /// mano desde una consola: cerrarla no debe cerrar el tray).
    pub fn detach_console() {
        unsafe {
            windows_sys::Win32::System::Console::FreeConsole();
        }
    }

    /// Señala el evento con nombre que los trays abiertos miran cada 5 s.
    pub fn quit_trays() {
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{OpenEventW, ResetEvent, SetEvent, EVENT_MODIFY_STATE};
        let name: Vec<u16> = crate::tray::QUIT_EVENT.encode_utf16().chain(std::iter::once(0)).collect();
        let h = unsafe { OpenEventW(EVENT_MODIFY_STATE, 0, name.as_ptr()) };
        if h.is_null() {
            return; // no hay ningún tray abierto
        }
        unsafe {
            SetEvent(h);
            std::thread::sleep(Duration::from_millis(6000));
            ResetEvent(h);
            CloseHandle(h);
        }
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
    pub fn stop_if_running() -> anyhow::Result<bool> {
        Ok(false)
    }
    pub fn register_tray_autostart(_exe: &Path) -> anyhow::Result<()> {
        anyhow::bail!("solo Windows")
    }
    pub fn unregister_tray_autostart() -> anyhow::Result<()> {
        Ok(())
    }
    pub fn launch_tray(_exe: &Path) {}
    pub fn detach_console() {}
    pub fn quit_trays() {}
}

/// Nombre del ejecutable del tray (sin consola) dentro del directorio de datos.
pub const TRAY_EXE_NAME: &str = "agent-tray.exe";

/// Crea `tray_exe` a partir de `agent_exe`: mismo binario con el subsistema PE
/// cambiado a GUI, así Windows no le abre una consola al iniciar sesión. Si al
/// lado de `agent_exe` ya hay un `agent-tray.exe` (build firmado), se copia ese.
pub fn make_tray_exe(agent_exe: &Path, tray_exe: &Path) -> anyhow::Result<()> {
    let sibling = agent_exe.with_file_name(TRAY_EXE_NAME);
    if sibling.exists() && sibling != tray_exe {
        std::fs::copy(&sibling, tray_exe)?;
        return Ok(());
    }
    let mut bytes = std::fs::read(agent_exe)?;
    patch_pe_subsystem_gui(&mut bytes)?;
    std::fs::write(tray_exe, bytes)?;
    Ok(())
}

const IMAGE_SUBSYSTEM_WINDOWS_GUI: u16 = 2;
const IMAGE_SUBSYSTEM_WINDOWS_CUI: u16 = 3;

/// Cambia el campo `Subsystem` de la cabecera opcional PE de consola a GUI.
/// Es un `u16` en el offset 68 de la cabecera opcional, tanto en PE32 como en PE32+.
pub fn patch_pe_subsystem_gui(bytes: &mut [u8]) -> anyhow::Result<()> {
    let u16_at = |b: &[u8], i: usize| -> anyhow::Result<u16> {
        Ok(u16::from_le_bytes(b.get(i..i + 2).ok_or_else(|| anyhow::anyhow!("PE truncado"))?.try_into()?))
    };
    let u32_at = |b: &[u8], i: usize| -> anyhow::Result<u32> {
        Ok(u32::from_le_bytes(b.get(i..i + 4).ok_or_else(|| anyhow::anyhow!("PE truncado"))?.try_into()?))
    };
    if bytes.get(0..2) != Some(b"MZ") {
        anyhow::bail!("no es un ejecutable PE (falta MZ)");
    }
    let pe = u32_at(bytes, 0x3C)? as usize;
    if bytes.get(pe..pe + 4) != Some(b"PE\0\0") {
        anyhow::bail!("no es un ejecutable PE (falta la firma PE)");
    }
    let opt = pe + 4 + 20;
    let magic = u16_at(bytes, opt)?;
    if magic != 0x10b && magic != 0x20b {
        anyhow::bail!("cabecera opcional PE desconocida: 0x{magic:x}");
    }
    let sub = opt + 68;
    let current = u16_at(bytes, sub)?;
    if current != IMAGE_SUBSYSTEM_WINDOWS_CUI && current != IMAGE_SUBSYSTEM_WINDOWS_GUI {
        anyhow::bail!("subsistema inesperado: {current}");
    }
    bytes[sub..sub + 2].copy_from_slice(&IMAGE_SUBSYSTEM_WINDOWS_GUI.to_le_bytes());
    Ok(())
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
    fn patches_subsystem_in_a_minimal_pe() {
        // MZ + e_lfanew=0x40 + "PE\0\0" + COFF(20) + opcional PE32+ con Subsystem=3 en +68.
        let mut b = vec![0u8; 0x40 + 4 + 20 + 96];
        b[0] = b'M';
        b[1] = b'Z';
        b[0x3C..0x40].copy_from_slice(&0x40u32.to_le_bytes());
        b[0x40..0x44].copy_from_slice(b"PE\0\0");
        let opt = 0x40 + 24;
        b[opt..opt + 2].copy_from_slice(&0x20bu16.to_le_bytes());
        b[opt + 68..opt + 70].copy_from_slice(&3u16.to_le_bytes());
        patch_pe_subsystem_gui(&mut b).unwrap();
        assert_eq!(u16::from_le_bytes([b[opt + 68], b[opt + 69]]), 2);
        // Idempotente y rechaza basura.
        patch_pe_subsystem_gui(&mut b).unwrap();
        assert!(patch_pe_subsystem_gui(&mut [0u8; 10]).is_err());
        b[opt + 68] = 9;
        assert!(patch_pe_subsystem_gui(&mut b).is_err());
    }

    #[test]
    fn data_dir_defaults_to_programdata() {
        let d = resolve_data_dir(None);
        assert!(d.ends_with("RemediaAgent"), "{}", d.display());
        assert_eq!(resolve_data_dir(Some(PathBuf::from("x"))), PathBuf::from("x"));
    }
}
