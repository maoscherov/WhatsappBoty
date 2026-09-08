use clap::{Parser, Subcommand};
use std::path::PathBuf;

#[derive(Parser)]
#[command(name = "agent", version, about = "Remedia Agent: conector ObServer Gestión → Remedia")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Escribe agent.toml, registra el servicio RemediaAgent y lo inicia
    Install {
        #[arg(long)]
        token: String,
        #[arg(long)]
        erp: String,
        #[arg(long)]
        branch: String,
        #[arg(long, default_value = "https://api.remedia.ar")]
        remedia: String,
        #[arg(long)]
        data_dir: Option<PathBuf>,
    },
    /// Detiene y elimina el servicio RemediaAgent
    Uninstall,
    /// Corre el agente en foreground (debug)
    Run {
        #[arg(long)]
        data_dir: Option<PathBuf>,
    },
    /// Ejecuta un ciclo de sync y termina
    SyncNow {
        #[arg(long)]
        data_dir: Option<PathBuf>,
    },
    /// Muestra el estado local (state.sqlite y último heartbeat)
    Status {
        #[arg(long)]
        data_dir: Option<PathBuf>,
    },
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
        _ => anyhow::bail!("no implementado todavía"),
    }
}
