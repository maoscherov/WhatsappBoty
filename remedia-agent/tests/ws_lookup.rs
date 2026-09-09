mod common;

use futures_util::{SinkExt, StreamExt};
use remedia_agent::config::Config;
use remedia_agent::erp::build_adapter;
use remedia_agent::metrics::Metrics;
use remedia_agent::remedia::ws::{handle_lookup, run_ws};
use std::sync::Arc;
use std::time::Duration;
use tokio::net::TcpListener;
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::handshake::server::{Request, Response};
use tokio_tungstenite::tungstenite::Message;
use tokio_util::sync::CancellationToken;

fn config(erp_uri: &str) -> Arc<Config> {
    Arc::new(
        Config::from_toml(&format!(
            "branch_id = \"farmacia-test\"\nremedia_url = \"http://unused\"\ntoken = \"tok\"\n[erp]\nbase_url = \"{erp_uri}\"\n"
        ))
        .unwrap(),
    )
}

/// Servidor WS de prueba: acepta una conexión verificando el Bearer y devuelve
/// el stream para que el test converse con el agente.
async fn ws_server() -> (String, tokio::task::JoinHandle<tokio_tungstenite::WebSocketStream<tokio::net::TcpStream>>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let handle = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        tokio_tungstenite::accept_hdr_async(stream, |req: &Request, resp: Response| {
            let auth = req.headers().get("authorization").and_then(|v| v.to_str().ok());
            assert_eq!(auth, Some("Bearer tok"), "falta el Bearer en el handshake");
            assert_eq!(req.uri().path(), "/v1/agent/ws");
            Ok(resp)
        })
        .await
        .unwrap()
    });
    (format!("ws://{addr}/v1/agent/ws"), handle)
}

async fn next_json(ws: &mut tokio_tungstenite::WebSocketStream<tokio::net::TcpStream>) -> serde_json::Value {
    loop {
        let msg = tokio::time::timeout(Duration::from_secs(10), ws.next()).await
            .expect("timeout esperando mensaje").expect("stream cerrado").unwrap();
        if let Message::Text(t) = msg {
            return serde_json::from_str(&t).unwrap();
        }
    }
}

#[tokio::test]
async fn lookup_over_ws_returns_items_and_missing() {
    let erp = common::mock_erp(1).await;
    let cfg = config(&erp.uri());
    let adapter = build_adapter(&cfg.erp, Duration::from_secs(5)).unwrap();
    let (url, server) = ws_server().await;
    let (tx, mut rx) = mpsc::channel(1);
    let shutdown = CancellationToken::new();
    let agent = tokio::spawn(run_ws(url, "tok".into(), adapter, tx, Arc::clone(&cfg), Metrics::new(), shutdown.clone()));

    let mut ws = server.await.unwrap();
    let hello = next_json(&mut ws).await;
    assert_eq!(hello["op"], "hello");
    assert_eq!(hello["branch_id"], "farmacia-test");

    ws.send(Message::Text(
        r#"{"op":"lookup","req_id":"abc","barcodes":["7795336085205","0000"],"ids":[20000,999999]}"#.into(),
    )).await.unwrap();
    let res = next_json(&mut ws).await;
    assert_eq!(res["op"], "lookup_result");
    assert_eq!(res["req_id"], "abc");
    let mut ids: Vec<&str> = res["items"].as_array().unwrap().iter().map(|i| i["external_id"].as_str().unwrap()).collect();
    ids.sort();
    assert_eq!(ids, vec!["20000", "7454"]);
    let mut missing: Vec<&str> = res["missing"].as_array().unwrap().iter().map(|m| m.as_str().unwrap()).collect();
    missing.sort();
    assert_eq!(missing, vec!["0000", "999999"]);

    ws.send(Message::Text(r#"{"op":"sync_now"}"#.into())).await.unwrap();
    tokio::time::timeout(Duration::from_secs(5), rx.recv()).await.expect("sync_now no llegó").unwrap();

    // ping del servidor → pong del agente
    ws.send(Message::Text(r#"{"op":"ping"}"#.into())).await.unwrap();
    assert_eq!(next_json(&mut ws).await["op"], "pong");

    shutdown.cancel();
    tokio::time::timeout(Duration::from_secs(5), agent).await.unwrap().unwrap();
}

#[tokio::test]
async fn reconnects_after_server_closes() {
    let erp = common::mock_erp(1).await;
    let cfg = config(&erp.uri());
    let adapter = build_adapter(&cfg.erp, Duration::from_secs(5)).unwrap();

    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}/v1/agent/ws", listener.local_addr().unwrap());
    let (tx, _rx) = mpsc::channel(1);
    let shutdown = CancellationToken::new();
    let agent = tokio::spawn(run_ws(url, "tok".into(), adapter, tx, cfg, Metrics::new(), shutdown.clone()));

    // Primera conexión: la cerramos enseguida.
    let (s1, _) = listener.accept().await.unwrap();
    let mut ws1 = tokio_tungstenite::accept_async(s1).await.unwrap();
    let _ = next_json(&mut ws1).await; // hello
    ws1.close(None).await.unwrap();
    drop(ws1);

    // Segunda conexión: debe llegar sola (backoff mínimo 5 s).
    let (s2, _) = tokio::time::timeout(Duration::from_secs(20), listener.accept()).await
        .expect("el agente no reconectó").unwrap();
    let mut ws2 = tokio_tungstenite::accept_async(s2).await.unwrap();
    assert_eq!(next_json(&mut ws2).await["op"], "hello");

    shutdown.cancel();
    tokio::time::timeout(Duration::from_secs(5), agent).await.unwrap().unwrap();
}

#[tokio::test]
async fn handle_lookup_times_out_per_request() {
    let erp = wiremock::MockServer::start().await;
    wiremock::Mock::given(wiremock::matchers::method("POST"))
        .respond_with(wiremock::ResponseTemplate::new(200).set_body_json(serde_json::json!([])).set_delay(Duration::from_secs(3)))
        .mount(&erp).await;
    let cfg = config(&erp.uri());
    let adapter = build_adapter(&cfg.erp, Duration::from_secs(5)).unwrap();
    let t0 = std::time::Instant::now();
    let (items, missing) = handle_lookup(adapter.as_ref(), vec!["1".into()], vec![], Duration::from_millis(300), 4).await;
    assert!(items.is_empty());
    assert_eq!(missing, vec!["1"]);
    assert!(t0.elapsed() < Duration::from_secs(2), "{:?}", t0.elapsed());
}
