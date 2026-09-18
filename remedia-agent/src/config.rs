//! Configuración del agente (`agent.toml`).

use anyhow::Context;
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

pub const CONFIG_FILE: &str = "agent.toml";
pub const STATE_FILE: &str = "state.sqlite";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    pub branch_id: String,
    pub remedia_url: String,
    pub token: String,
    #[serde(default = "d_heartbeat")]
    pub heartbeat_interval_secs: u64,
    pub erp: ErpConfig,
    #[serde(default)]
    pub log: LogConfig,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ErpConfig {
    #[serde(default = "d_kind")]
    pub kind: String,
    pub base_url: String,
    #[serde(default = "d_sync_interval")]
    pub sync_interval_secs: u64,
    #[serde(default = "d_concurrency")]
    pub max_concurrency: usize,
    #[serde(default = "d_timeout")]
    pub request_timeout_secs: u64,
    /// Barrido diario por rango de `idProducto` (ver spec §1, PENDIENTE del lote).
    #[serde(default)]
    pub daily_id_scan: bool,
    #[serde(default = "d_id_scan_max")]
    pub id_scan_max: i64,
    /// "Pase de verdad" en cada ciclo: precio y stock se releen de los
    /// endpoints en vivo (`codigosBarras` / `{id}`) porque el lote los trae en
    /// cero (hallazgo 11/9). Apagarlo vuelve al dato del lote tal cual.
    #[serde(default = "d_true")]
    pub live_enrich: bool,
    /// Pase de verdad SELECTIVO (0.3.2): por ciclo se releen solo los productos
    /// con stock o precio en la última lectura buena y los nunca vistos; el
    /// barrido completo, una vez por día a `live_full_hour`. Baja la carga
    /// sobre el ERP ~10 veces en horario de atención.
    #[serde(default = "d_true")]
    pub live_selective: bool,
    /// Hora local (0-23) del barrido completo diario.
    #[serde(default = "d_live_full_hour")]
    pub live_full_hour: u8,
    /// Pausa entre requests al ERP durante el pase de verdad (ms).
    #[serde(default = "d_live_pause_ms")]
    pub live_pause_ms: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LogConfig {
    #[serde(default = "d_log_dir")]
    pub dir: PathBuf,
}

impl Default for LogConfig {
    fn default() -> Self {
        LogConfig { dir: d_log_dir() }
    }
}

/// Defaults conservadores (0.3.3, tras el incidente del 15/9): un request a la
/// vez, 500 ms entre requests del pase de verdad y un ciclo cada 30 minutos.
/// Observer atiende el mostrador; el agente no puede competirle.
pub const DEFAULT_SYNC_INTERVAL_SECS: u64 = 1800;
pub const DEFAULT_MAX_CONCURRENCY: usize = 1;
pub const DEFAULT_LIVE_PAUSE_MS: u64 = 500;

impl ErpConfig {
    /// Al actualizar se conserva el ajuste fino de la farmacia, salvo los
    /// valores que son exactamente los defaults agresivos de 0.3.2 y
    /// anteriores (900 s / 4 hilos / 50 ms): esos nadie los eligió, los
    /// escribió el instalador, y se migran a los nuevos.
    pub fn migrar_defaults_viejos(&mut self) {
        if self.sync_interval_secs == 900 {
            self.sync_interval_secs = DEFAULT_SYNC_INTERVAL_SECS;
        }
        if self.max_concurrency == 4 {
            self.max_concurrency = DEFAULT_MAX_CONCURRENCY;
        }
        if self.live_pause_ms == 50 {
            self.live_pause_ms = DEFAULT_LIVE_PAUSE_MS;
        }
    }
}

fn d_heartbeat() -> u64 {
    300
}
fn d_kind() -> String {
    "observer".into()
}
fn d_sync_interval() -> u64 {
    DEFAULT_SYNC_INTERVAL_SECS
}
fn d_concurrency() -> usize {
    DEFAULT_MAX_CONCURRENCY
}
fn d_timeout() -> u64 {
    30
}
fn d_id_scan_max() -> i64 {
    100_300
}
fn d_true() -> bool {
    true
}
fn d_live_full_hour() -> u8 {
    3
}
fn d_live_pause_ms() -> u64 {
    DEFAULT_LIVE_PAUSE_MS
}
fn d_log_dir() -> PathBuf {
    Config::default_data_dir().join("logs")
}

impl Config {
    /// `C:\ProgramData\RemediaAgent` (o `%PROGRAMDATA%\RemediaAgent` si está definido).
    pub fn default_data_dir() -> PathBuf {
        let base = std::env::var_os("PROGRAMDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(r"C:\ProgramData"));
        base.join("RemediaAgent")
    }

    pub fn from_toml(s: &str) -> anyhow::Result<Config> {
        let cfg: Config = toml::from_str(s).context("agent.toml inválido")?;
        anyhow::ensure!(!cfg.branch_id.trim().is_empty(), "branch_id vacío");
        anyhow::ensure!(!cfg.token.trim().is_empty(), "token vacío");
        anyhow::ensure!(cfg.erp.max_concurrency >= 1, "erp.max_concurrency debe ser >= 1");
        Ok(cfg)
    }

    pub fn load(path: &Path) -> anyhow::Result<Config> {
        let s = std::fs::read_to_string(path)
            .with_context(|| format!("no se pudo leer {}", path.display()))?;
        Self::from_toml(&s)
    }

    pub fn write(&self, path: &Path) -> anyhow::Result<()> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("no se pudo crear {}", parent.display()))?;
        }
        let s = toml::to_string_pretty(self).context("serializando config")?;
        std::fs::write(path, s).with_context(|| format!("escribiendo {}", path.display()))
    }

    pub fn sync_interval(&self) -> std::time::Duration {
        std::time::Duration::from_secs(self.erp.sync_interval_secs.max(30))
    }
    pub fn heartbeat_interval(&self) -> std::time::Duration {
        std::time::Duration::from_secs(self.heartbeat_interval_secs.max(30))
    }
    pub fn request_timeout(&self) -> std::time::Duration {
        std::time::Duration::from_secs(self.erp.request_timeout_secs.max(1))
    }
}

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
        assert_eq!(c.erp.kind, "observer");
        assert_eq!(c.erp.sync_interval_secs, 1800);
        assert_eq!(c.erp.max_concurrency, 1);
        assert_eq!(c.erp.request_timeout_secs, 30);
        assert!(!c.erp.daily_id_scan);
        assert_eq!(c.erp.id_scan_max, 100_300);
        assert!(c.erp.live_enrich);
        assert!(c.erp.live_selective);
        assert_eq!(c.erp.live_full_hour, 3);
        assert_eq!(c.erp.live_pause_ms, 500);
        assert_eq!(c.heartbeat_interval_secs, 300);
        assert_eq!(c.log.dir, PathBuf::from(r"C:\ProgramData\RemediaAgent\logs"));
    }

    #[test]
    fn migra_defaults_viejos_y_respeta_lo_elegido() {
        let s = SAMPLE.replace(
            "base_url = \"http://192.168.1.156:60064\"",
            "base_url = \"http://192.168.1.156:60064\"
sync_interval_secs = 900
max_concurrency = 4
live_pause_ms = 50",
        );
        let mut c = Config::from_toml(&s).unwrap();
        c.erp.migrar_defaults_viejos();
        assert_eq!((c.erp.sync_interval_secs, c.erp.max_concurrency, c.erp.live_pause_ms), (1800, 1, 500));

        let s = SAMPLE.replace(
            "base_url = \"http://192.168.1.156:60064\"",
            "base_url = \"http://192.168.1.156:60064\"
sync_interval_secs = 600
max_concurrency = 2
live_pause_ms = 1000",
        );
        let mut c = Config::from_toml(&s).unwrap();
        c.erp.migrar_defaults_viejos();
        assert_eq!((c.erp.sync_interval_secs, c.erp.max_concurrency, c.erp.live_pause_ms), (600, 2, 1000));
    }

    #[test]
    fn live_enrich_can_be_disabled() {
        let s = SAMPLE.replace(
            "base_url = \"http://192.168.1.156:60064\"",
            "base_url = \"http://192.168.1.156:60064\"\nlive_enrich = false",
        );
        assert!(!Config::from_toml(&s).unwrap().erp.live_enrich);
    }

    #[test]
    fn log_section_is_optional() {
        let s = SAMPLE.split("[log]").next().unwrap();
        let c = Config::from_toml(s).unwrap();
        assert!(c.log.dir.ends_with("logs"));
    }

    #[test]
    fn rejects_empty_token() {
        assert!(Config::from_toml(&SAMPLE.replace("\"abc\"", "\"\"")).is_err());
    }

    #[test]
    fn roundtrip_write() {
        let c = Config::from_toml(SAMPLE).unwrap();
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("sub").join(CONFIG_FILE);
        c.write(&p).unwrap();
        let c2 = Config::load(&p).unwrap();
        assert_eq!(c2.erp.base_url, c.erp.base_url);
        assert_eq!(c2.token, "abc");
    }
}
