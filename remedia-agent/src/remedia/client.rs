//! Cliente HTTP hacia Remedia y tipos del contrato (`/v1/sync/*`).

use crate::catalog::CatalogItem;
use reqwest::{header, Client, StatusCode};
use serde::{Deserialize, Serialize};
use std::time::Duration;

pub const SCHEMA_VERSION: u32 = 1;
pub const SOURCE_OBSERVER: &str = "observer-gestion";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CatalogBatch {
    pub schema_version: u32,
    pub branch_id: String,
    pub source: String,
    /// `"delta"` o `"full"`.
    pub mode: String,
    pub batch: u32,
    pub total_batches: u32,
    pub generated_at: String,
    pub items: Vec<CatalogItem>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct SyncResponse {
    #[serde(default)]
    pub received: u64,
    #[serde(default)]
    pub upserted: u64,
    #[serde(default)]
    pub unchanged: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ManifestEntry {
    pub external_id: String,
    pub hash: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct FullManifest {
    pub branch_id: String,
    pub generated_at: String,
    pub items: Vec<ManifestEntry>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct FullManifestResponse {
    /// `external_id` cuyo hash no coincide en Remedia: hay que reenviarlos completos.
    #[serde(default)]
    pub resend: Vec<String>,
    #[serde(default)]
    pub deactivated: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Heartbeat {
    pub branch_id: String,
    pub agent_version: String,
    pub erp_version: Option<String>,
    /// `ok | no_autorizado | inalcanzable | error`
    pub erp_status: String,
    pub last_sync_ok_at: Option<String>,
    pub catalog_count: i64,
    pub pending_batches: i64,
}

#[derive(Debug, thiserror::Error)]
pub enum RemediaError {
    #[error("Remedia inalcanzable: {0}")]
    Unreachable(String),
    #[error("Remedia HTTP {0}: {1}")]
    Http(u16, String),
    #[error("Remedia respuesta inválida: {0}")]
    Decode(String),
}

pub struct RemediaClient {
    client: Client,
    base_url: String,
}

/// Fecha local con offset (`2026-09-08T03:12:00-03:00`).
pub fn now_rfc3339() -> String {
    chrono::Local::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, false)
}

impl RemediaClient {
    pub fn new(base_url: &str, token: &str, timeout: Duration) -> RemediaClient {
        let mut headers = header::HeaderMap::new();
        let mut auth = header::HeaderValue::from_str(&format!("Bearer {token}"))
            .expect("token con caracteres inválidos para header");
        auth.set_sensitive(true);
        headers.insert(header::AUTHORIZATION, auth);
        headers.insert(header::ACCEPT, header::HeaderValue::from_static("application/json"));
        headers.insert(
            header::USER_AGENT,
            header::HeaderValue::from_str(&format!("remedia-agent/{}", crate::AGENT_VERSION)).unwrap(),
        );
        let client = Client::builder()
            .default_headers(headers)
            .timeout(timeout)
            .connect_timeout(Duration::from_secs(10))
            .build()
            .expect("reqwest client");
        RemediaClient {
            client,
            base_url: base_url.trim_end_matches('/').to_string(),
        }
    }

    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    /// `wss://<remedia>/v1/agent/ws` (o `ws://` si la base es `http://`).
    pub fn ws_url(&self) -> String {
        let base = if let Some(rest) = self.base_url.strip_prefix("https://") {
            format!("wss://{rest}")
        } else if let Some(rest) = self.base_url.strip_prefix("http://") {
            format!("ws://{rest}")
        } else {
            format!("wss://{}", self.base_url)
        };
        format!("{base}/v1/agent/ws")
    }

    async fn post<B: Serialize, R: for<'de> Deserialize<'de> + Default>(
        &self,
        path: &str,
        body: &B,
    ) -> Result<R, RemediaError> {
        let resp = self
            .client
            .post(format!("{}{}", self.base_url, path))
            .json(body)
            .send()
            .await
            .map_err(|e| RemediaError::Unreachable(e.to_string()))?;
        let status = resp.status();
        if !status.is_success() {
            let text = resp.text().await.unwrap_or_default();
            return Err(RemediaError::Http(status.as_u16(), text.chars().take(300).collect()));
        }
        if status == StatusCode::NO_CONTENT {
            return Ok(R::default());
        }
        let bytes = resp.bytes().await.map_err(|e| RemediaError::Decode(e.to_string()))?;
        if bytes.is_empty() {
            return Ok(R::default());
        }
        serde_json::from_slice(&bytes).map_err(|e| RemediaError::Decode(e.to_string()))
    }

    pub async fn push_catalog(&self, batch: &CatalogBatch) -> Result<SyncResponse, RemediaError> {
        self.post("/v1/sync/catalog", batch).await
    }

    pub async fn full_manifest(&self, m: &FullManifest) -> Result<FullManifestResponse, RemediaError> {
        self.post("/v1/sync/full-manifest", m).await
    }

    pub async fn heartbeat(&self, h: &Heartbeat) -> Result<(), RemediaError> {
        let _: serde_json::Value = self.post("/v1/sync/heartbeat", h).await?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ws_url_from_https_and_http() {
        let c = RemediaClient::new("https://api.remedia.ar/", "t", Duration::from_secs(1));
        assert_eq!(c.ws_url(), "wss://api.remedia.ar/v1/agent/ws");
        let c = RemediaClient::new("http://localhost:8000", "t", Duration::from_secs(1));
        assert_eq!(c.ws_url(), "ws://localhost:8000/v1/agent/ws");
    }
}
