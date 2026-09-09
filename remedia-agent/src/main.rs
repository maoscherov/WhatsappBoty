use anyhow::Context;
use clap::{Parser, Subcommand};
use remedia_agent::catalog::state::{
    META_CATALOG_COUNT, META_ERP_STATUS, META_ERP_VERSION, META_LAST_FULL_MANIFEST,
    META_LAST_HEARTBEAT, META_LAST_SYNC_ERROR, META_LAST_SYNC_OK,
};
use remedia_agent::catalog::State;
use remedia_agent::config::{Config, ErpConfig, LogConfig, CONFIG_FILE, STATE_FILE};
use remedia_agent::service::{self, win};
use remedia_agent::{logging, AGENT_VERSION};
use std::path::PathBuf;
use tokio_util::sync::CancellationToken;

#[derive(Parser)]
#[command(name = "agent", version, about = "Remedia Agent: conector ObServer Gestión → Remedia")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Escribe agent.toml, registra el servicio RemediaAgent (arranque automático) y lo inicia
    Install {
        /// Token de la sucursal emitido por Remedia
        #[arg(long)]
        token: String,
        /// Base de la API del ERP, p. ej. http://192.168.1.156:60064
        #[arg(long)]
        erp: String,
        /// Identificador de la sucursal, p. ej. farmacia-xxx
        #[arg(long)]
        branch: String,
        /// Base de la API de Remedia, solo el servidor (sin /bo/...)
        #[arg(long, default_value = "https://cerca.remedia.ar")]
        remedia: String,
        /// Directorio de datos (agent.toml, state.sqlite, logs). Default: C:\ProgramData\RemediaAgent
        #[arg(long)]
        data_dir: Option<PathBuf>,
    },
    /// Detiene y elimina el servicio RemediaAgent (no borra los datos)
    Uninstall,
    /// Corre el agente en foreground con logs en consola (debug)
    Run {
        #[arg(long)]
        data_dir: Option<PathBuf>,
    },
    /// Ejecuta un ciclo de sync (pendientes + delta) y termina
    SyncNow {
        #[arg(long)]
        data_dir: Option<PathBuf>,
    },
    /// Muestra el estado local: catálogo, pendientes, último sync y heartbeat
    Status {
        #[arg(long)]
        data_dir: Option<PathBuf>,
    },
    /// Icono en la bandeja del sistema: estado, latencias y configuración
    Tray,
    /// Punto de entrada usado por el Service Control Manager (no invocar a mano)
    #[command(hide = true)]
    Service {
        #[arg(long)]
        data_dir: Option<PathBuf>,
    },
}

fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();
    match cli.cmd {
        Cmd::Install { token, erp, branch, remedia, data_dir } => {
            let data_dir = service::resolve_data_dir(data_dir);
            install(&data_dir, token, erp, branch, remedia)
        }
        Cmd::Uninstall => {
            win::quit_trays();
            if let Err(e) = win::unregister_tray_autostart() {
                eprintln!("Aviso: no se pudo quitar el autoarranque del tray: {e}");
            }
            win::uninstall().context("desinstalando el servicio")?;
            println!("Servicio {} eliminado.", service::SERVICE_NAME);
            Ok(())
        }
        Cmd::Run { data_dir } => {
            let data_dir = service::resolve_data_dir(data_dir);
            let cfg = Config::load(&data_dir.join(CONFIG_FILE))?;
            let _guard = logging::init(Some(&cfg.log.dir), true)?;
            let rt = tokio::runtime::Runtime::new()?;
            rt.block_on(async {
                let shutdown = CancellationToken::new();
                let sd = shutdown.clone();
                tokio::spawn(async move {
                    let _ = tokio::signal::ctrl_c().await;
                    tracing::info!("Ctrl+C recibido");
                    sd.cancel();
                });
                service::run_agent(data_dir, shutdown).await
            })
        }
        Cmd::Service { data_dir } => win::run_as_service(service::resolve_data_dir(data_dir)),
        Cmd::Tray => remedia_agent::tray::run_tray(),
        Cmd::SyncNow { data_dir } => {
            let data_dir = service::resolve_data_dir(data_dir);
            let _guard = logging::init(None, true)?;
            let rt = tokio::runtime::Runtime::new()?;
            rt.block_on(async {
                let engine = service::build_engine(&data_dir)?;
                let report = engine.run_once().await?;
                println!(
                    "ERP: {} | leídos: {} | cambiados: {} | lotes enviados: {} | encolados: {} | pendientes enviados: {}",
                    report.erp_status, report.fetched, report.changed, report.sent_batches,
                    report.queued_batches, report.flushed_batches
                );
                Ok(())
            })
        }
        Cmd::Status { data_dir } => status(&service::resolve_data_dir(data_dir)),
    }
}

fn install(data_dir: &std::path::Path, token: String, erp: String, branch: String, remedia: String) -> anyhow::Result<()> {
    for (what, u) in [("--remedia", &remedia), ("--erp", &erp)] {
        if let Err(e) = remedia_agent::runtime::validate_url(u) {
            anyhow::bail!("{what}: {e}");
        }
    }
    let remedia = remedia.trim_end_matches('/').to_string();
    let erp = erp.trim_end_matches('/').to_string();
    let cfg = Config {
        branch_id: branch,
        remedia_url: remedia,
        token,
        heartbeat_interval_secs: 300,
        erp: ErpConfig {
            kind: "observer".into(),
            base_url: erp,
            sync_interval_secs: 900,
            max_concurrency: 4,
            request_timeout_secs: 30,
            daily_id_scan: false,
            id_scan_max: 100_300,
        },
        log: LogConfig { dir: data_dir.join("logs") },
    };
    // Validación temprana: que el toml resultante sea el que después se carga.
    Config::from_toml(&toml::to_string(&cfg)?)?;
    let cfg_path = data_dir.join(CONFIG_FILE);
    cfg.write(&cfg_path)?;
    println!("Config escrita en {}", cfg_path.display());

    // El servicio apunta a una copia del exe dentro del directorio de datos,
    // así no depende de dónde se descargó el instalador.
    let current = std::env::current_exe()?;
    let target = data_dir.join("agent.exe");
    if current.canonicalize().ok() != target.canonicalize().ok() {
        std::fs::copy(&current, &target)
            .with_context(|| format!("copiando {} a {}", current.display(), target.display()))?;
    }

    win::install(data_dir, &target).context("registrando el servicio (¿consola como administrador?)")?;
    println!("Servicio {} registrado e iniciado (v{AGENT_VERSION}).", service::SERVICE_NAME);
    println!("Logs en {}", cfg.log.dir.display());
    match win::register_tray_autostart(&target) {
        Ok(()) => {
            win::launch_tray(&target);
            println!("Icono de bandeja registrado para todos los usuarios y abierto en esta sesión.");
        }
        Err(e) => eprintln!("Aviso: no se pudo registrar el icono de bandeja: {e}"),
    }
    Ok(())
}

fn status(data_dir: &std::path::Path) -> anyhow::Result<()> {
    // Con el servicio corriendo, el pipe da los datos vivos (métricas incluidas).
    if let Ok(resp) = remedia_agent::ipc::client::call_blocking(&remedia_agent::ipc::Request::Status) {
        if let Some(report) = resp.status {
            println!("remedia-agent v{AGENT_VERSION} — servicio corriendo\n");
            print!("{}", remedia_agent::tray::state::detail_text(Some(&report), chrono::Local::now()));
            return Ok(());
        }
    }
    println!("(servicio detenido: se muestra el último estado guardado)\n");
    let cfg_path = data_dir.join(CONFIG_FILE);
    let state_path = data_dir.join(STATE_FILE);
    println!("remedia-agent v{AGENT_VERSION}");
    println!("data dir:        {}", data_dir.display());
    match Config::load(&cfg_path) {
        Ok(c) => {
            println!("sucursal:        {}", c.branch_id);
            println!("ERP:             {} ({})", c.erp.base_url, c.erp.kind);
            println!("Remedia:         {}", c.remedia_url);
        }
        Err(e) => println!("config:          no se pudo leer ({e})"),
    }
    if !state_path.exists() {
        println!("estado:          todavía no hay state.sqlite (el agente nunca corrió)");
        return Ok(());
    }
    let state = State::open(&state_path)?;
    let get = |k: &str| state.get_meta(k).ok().flatten().unwrap_or_else(|| "-".into());
    println!("ERP status:      {}", get(META_ERP_STATUS));
    println!("ERP versión:     {}", get(META_ERP_VERSION));
    println!("catálogo (ERP):  {}", get(META_CATALOG_COUNT));
    println!("items en estado: {}", state.count_items()?);
    println!("lotes pendientes:{}", state.pending_count()?);
    println!("último sync OK:  {}", get(META_LAST_SYNC_OK));
    println!("último error:    {}", get(META_LAST_SYNC_ERROR));
    println!("último manifest: {}", get(META_LAST_FULL_MANIFEST));
    println!("último heartbeat:{}", get(META_LAST_HEARTBEAT));
    Ok(())
}
