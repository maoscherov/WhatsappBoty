//! Websocket saliente hacia Remedia (`/v1/agent/ws`): lookups de stock/precio
//! en tiempo real contra el ERP y disparo de `sync_now`. Reconexión con backoff.

use crate::catalog::CatalogItem;
use crate::config::Config;
use crate::erp::ErpAdapter;
use futures_util::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::Message;
use tokio_util::sync::CancellationToken;
use tracing::{debug, info, warn};

pub const LOOKUP_TIMEOUT: Duration = Duration::from_secs(3);
pub const PING_INTERVAL: Duration = Duration::from_secs(30);
pub const RECONNECT_MIN: Duration = Duration::from_secs(5);
pub const RECONNECT_MAX: Duration = Duration::from_secs(300);
/// Máximo de CB por request al ERP (`POST /api/productos/codigosBarras`).
pub const BARCODES_PER_REQUEST: usize = 20;

#[derive(Debug, Deserialize, PartialEq)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum Inbound {
    Lookup {
        req_id: String,
        #[serde(default)]
        barcodes: Vec<String>,
        #[serde(default)]
        ids: Vec<i64>,
    },
    SyncNow,
    Ping,
    Pong,
}

#[derive(Debug, Serialize, PartialEq)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum Outbound {
    Hello {
        branch_id: String,
        agent_version: String,
    },
    LookupResult {
        req_id: String,
        items: Vec<CatalogItem>,
        missing: Vec<String>,
    },
    Ping,
    Pong,
}

/// Resuelve `barcodes` (en tandas de hasta 20) e `ids` (en paralelo acotado)
/// contra el ERP con timeout por request. `missing` lista lo que no se encontró
/// o falló, con los ids como string.
pub async fn handle_lookup(
    erp: &dyn ErpAdapter,
    barcodes: Vec<String>,
    ids: Vec<i64>,
    timeout: Duration,
    max_concurrency: usize,
) -> (Vec<CatalogItem>, Vec<String>) {
    let mut items: Vec<CatalogItem> = Vec::new();
    let mut missing: Vec<String> = Vec::new();

    for chunk in barcodes.chunks(BARCODES_PER_REQUEST) {
        let found = match tokio::time::timeout(timeout, erp.lookup_by_barcodes(chunk)).await {
            Ok(Ok(v)) => v,
            Ok(Err(e)) => {
                warn!(error = %e, "lookup por CB falló");
                Vec::new()
            }
            Err(_) => {
                warn!("lookup por CB: timeout de {timeout:?}");
                Vec::new()
            }
        };
        for cb in chunk {
            if !found.iter().any(|p| p.codigo_barras.iter().any(|c| c == cb)) {
                missing.push(cb.clone());
            }
        }
        items.extend(found.iter().map(CatalogItem::from_dto));
    }

    // Lookups por id en paralelo acotado por semáforo. Se usa `join_all` sobre
    // futures no-'static para no exigir `Arc` en el trait object.
    let sem = Arc::new(tokio::sync::Semaphore::new(max_concurrency.max(1)));
    let erp_ref: &dyn ErpAdapter = erp;
    let futs = ids.iter().map(|&id| {
        let sem = Arc::clone(&sem);
        async move {
            let _p = sem.acquire_owned().await.expect("semaphore");
            let r = tokio::time::timeout(timeout, erp_ref.lookup_by_id(id)).await;
            (id, r)
        }
    });
    let results = futures_util::future::join_all(futs).await;
    for (id, r) in results {
        match r {
            Ok(Ok(Some(p))) => {
                let it = CatalogItem::from_dto(&p);
                if !items.iter().any(|x| x.external_id == it.external_id) {
                    items.push(it);
                }
            }
            Ok(Ok(None)) => missing.push(id.to_string()),
            Ok(Err(e)) => {
                warn!(id, error = %e, "lookup por id falló");
                missing.push(id.to_string());
            }
            Err(_) => {
                warn!(id, "lookup por id: timeout");
                missing.push(id.to_string());
            }
        }
    }
    (items, missing)
}

/// Loop de conexión. Termina solo cuando `shutdown` se cancela.
pub async fn run_ws(
    url: String,
    token: String,
    erp: Arc<dyn ErpAdapter>,
    sync_now: mpsc::Sender<()>,
    cfg: Arc<Config>,
    shutdown: CancellationToken,
) {
    let mut backoff = RECONNECT_MIN;
    loop {
        if shutdown.is_cancelled() {
            return;
        }
        match connect_and_serve(&url, &token, &erp, &sync_now, &cfg, &shutdown).await {
            Ok(()) => {
                if shutdown.is_cancelled() {
                    return;
                }
                info!("websocket cerrado por el servidor, reconectando");
                backoff = RECONNECT_MIN;
            }
            Err(e) => {
                warn!(error = %e, retry_in = ?backoff, "websocket caído");
            }
        }
        tokio::select! {
            _ = shutdown.cancelled() => return,
            _ = tokio::time::sleep(backoff) => {}
        }
        backoff = (backoff * 2).min(RECONNECT_MAX);
    }
}

async fn connect_and_serve(
    url: &str,
    token: &str,
    erp: &Arc<dyn ErpAdapter>,
    sync_now: &mpsc::Sender<()>,
    cfg: &Arc<Config>,
    shutdown: &CancellationToken,
) -> anyhow::Result<()> {
    let mut req = url.into_client_request()?;
    req.headers_mut().insert(
        http::header::AUTHORIZATION,
        http::HeaderValue::from_str(&format!("Bearer {token}"))?,
    );
    let (ws, _) = tokio::time::timeout(Duration::from_secs(15), tokio_tungstenite::connect_async(req))
        .await
        .map_err(|_| anyhow::anyhow!("timeout conectando"))??;
    info!(url, "websocket conectado");
    let (mut tx, mut rx) = ws.split();

    let hello = Outbound::Hello {
        branch_id: cfg.branch_id.clone(),
        agent_version: crate::AGENT_VERSION.to_string(),
    };
    tx.send(Message::Text(serde_json::to_string(&hello)?)).await?;

    let mut ping = tokio::time::interval(PING_INTERVAL);
    ping.tick().await; // el primer tick es inmediato
    let mut awaiting_pong: u8 = 0;

    loop {
        tokio::select! {
            _ = shutdown.cancelled() => {
                let _ = tx.send(Message::Close(None)).await;
                return Ok(());
            }
            _ = ping.tick() => {
                if awaiting_pong >= 2 {
                    anyhow::bail!("sin pong en {:?}", PING_INTERVAL * 2);
                }
                awaiting_pong += 1;
                tx.send(Message::Text(serde_json::to_string(&Outbound::Ping)?)).await?;
            }
            msg = rx.next() => {
                let Some(msg) = msg else { return Ok(()) };
                match msg? {
                    Message::Text(text) => {
                        let inbound: Inbound = match serde_json::from_str(&text) {
                            Ok(i) => i,
                            Err(e) => {
                                warn!(error = %e, msg = %text.chars().take(200).collect::<String>(), "mensaje ws no reconocido");
                                continue;
                            }
                        };
                        match inbound {
                            Inbound::Pong => { awaiting_pong = 0; }
                            Inbound::Ping => {
                                tx.send(Message::Text(serde_json::to_string(&Outbound::Pong)?)).await?;
                            }
                            Inbound::SyncNow => {
                                info!("sync_now recibido por websocket");
                                let _ = sync_now.try_send(());
                            }
                            Inbound::Lookup { req_id, barcodes, ids } => {
                                debug!(req_id, barcodes = barcodes.len(), ids = ids.len(), "lookup");
                                let (items, missing) = handle_lookup(
                                    erp.as_ref(), barcodes, ids, LOOKUP_TIMEOUT, cfg.erp.max_concurrency,
                                ).await;
                                let out = Outbound::LookupResult { req_id, items, missing };
                                tx.send(Message::Text(serde_json::to_string(&out)?)).await?;
                            }
                        }
                    }
                    Message::Ping(payload) => { tx.send(Message::Pong(payload)).await?; }
                    Message::Pong(_) => { awaiting_pong = 0; }
                    Message::Close(_) => return Ok(()),
                    Message::Binary(_) | Message::Frame(_) => {}
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_inbound_ops() {
        let l: Inbound = serde_json::from_str(r#"{"op":"lookup","req_id":"abc","barcodes":["779"],"ids":[123]}"#).unwrap();
        assert_eq!(l, Inbound::Lookup { req_id: "abc".into(), barcodes: vec!["779".into()], ids: vec![123] });
        let l: Inbound = serde_json::from_str(r#"{"op":"lookup","req_id":"x"}"#).unwrap();
        assert!(matches!(l, Inbound::Lookup { ids, barcodes, .. } if ids.is_empty() && barcodes.is_empty()));
        assert_eq!(serde_json::from_str::<Inbound>(r#"{"op":"sync_now"}"#).unwrap(), Inbound::SyncNow);
        assert_eq!(serde_json::from_str::<Inbound>(r#"{"op":"pong"}"#).unwrap(), Inbound::Pong);
        assert!(serde_json::from_str::<Inbound>(r#"{"op":"nope"}"#).is_err());
    }

    #[test]
    fn serializes_outbound_ops() {
        assert_eq!(serde_json::to_string(&Outbound::Ping).unwrap(), r#"{"op":"ping"}"#);
        let v = serde_json::to_value(Outbound::LookupResult { req_id: "a".into(), items: vec![], missing: vec!["1".into()] }).unwrap();
        assert_eq!(v["op"], "lookup_result");
        assert_eq!(v["missing"][0], "1");
    }
}
