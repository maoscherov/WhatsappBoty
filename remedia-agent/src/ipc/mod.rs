//! Protocolo entre el tray (proceso del usuario) y el servicio, por named pipe
//! local. Una request por conexión: una línea JSON de ida, una de vuelta.

pub mod client;
pub mod server;

use crate::metrics::MetricsSnapshot;
use serde::{Deserialize, Serialize};

pub const PIPE_NAME: &str = r"\\.\pipe\RemediaAgent";
/// SYSTEM y administradores: todo. Usuarios autenticados: lectura/escritura.
pub const PIPE_SDDL: &str = "D:(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGW;;;AU)";
pub const MAX_MESSAGE: usize = 64 * 1024;
pub const IO_TIMEOUT_SECS: u64 = 15;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "cmd", rename_all = "snake_case")]
pub enum Request {
    Status,
    SyncNow,
    TestErp {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        url: Option<String>,
    },
    TestRemedia {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        url: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        token: Option<String>,
    },
    SetConfig {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        token: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        erp_url: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        remedia_url: Option<String>,
    },
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct Response {
    pub ok: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub warnings: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ms: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub productos: Option<usize>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cantidad_lotes: Option<u32>,
    /// Etiqueta de `erp_status` cuando una prueba del ERP falla.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub status_label: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub status: Option<StatusReport>,
}

impl Response {
    pub fn ok() -> Response {
        Response { ok: true, ..Default::default() }
    }
    pub fn err(msg: impl Into<String>) -> Response {
        Response { ok: false, error: Some(msg.into()), ..Default::default() }
    }
    pub fn status(report: StatusReport) -> Response {
        Response { ok: true, status: Some(report), ..Default::default() }
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct StatusReport {
    pub agent_version: String,
    pub branch_id: String,
    pub erp_url: String,
    pub remedia_url: String,
    pub config_path: String,
    pub log_dir: String,
    /// ok | no_autorizado | inalcanzable | error | desconocido
    pub erp_status: String,
    pub erp_version: Option<String>,
    pub ws_connected: bool,
    pub last_sync_ok_at: Option<String>,
    pub last_sync_at: Option<String>,
    pub last_sync_fetched: Option<u64>,
    pub last_sync_changed: Option<u64>,
    pub catalog_count: Option<u64>,
    pub pending_batches: u64,
    pub last_heartbeat_at: Option<String>,
    pub last_error: Option<String>,
    pub metrics: MetricsSnapshot,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn requests_roundtrip() {
        let reqs = vec![
            Request::Status,
            Request::SyncNow,
            Request::TestErp { url: None },
            Request::TestErp { url: Some("http://x:60064".into()) },
            Request::TestRemedia { url: Some("https://r".into()), token: Some("t".into()) },
            Request::SetConfig { token: Some("t".into()), erp_url: None, remedia_url: None },
        ];
        for r in reqs {
            let s = serde_json::to_string(&r).unwrap();
            assert!(!s.contains('\n'));
            let back: Request = serde_json::from_str(&s).unwrap();
            assert_eq!(back, r);
        }
        assert_eq!(serde_json::to_string(&Request::Status).unwrap(), r#"{"cmd":"status"}"#);
        assert_eq!(
            serde_json::from_str::<Request>(r#"{"cmd":"test_erp"}"#).unwrap(),
            Request::TestErp { url: None }
        );
        assert!(serde_json::from_str::<Request>(r#"{"cmd":"reboot"}"#).is_err());
    }

    #[test]
    fn response_shapes() {
        let ok = Response::ok();
        assert_eq!(serde_json::to_string(&ok).unwrap(), r#"{"ok":true}"#);
        let e = Response::err("boom");
        let v: serde_json::Value = serde_json::to_value(&e).unwrap();
        assert_eq!(v["ok"], false);
        assert_eq!(v["error"], "boom");
        let st = Response::status(StatusReport { branch_id: "b".into(), ..Default::default() });
        let back: Response = serde_json::from_str(&serde_json::to_string(&st).unwrap()).unwrap();
        assert_eq!(back.status.unwrap().branch_id, "b");
    }
}
