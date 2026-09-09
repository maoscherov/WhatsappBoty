#![cfg(windows)]
mod common;

use remedia_agent::config::{Config, CONFIG_FILE};
use remedia_agent::ipc::client::call_on;
use remedia_agent::ipc::server::serve_on;
use remedia_agent::ipc::{Request, Response};
use remedia_agent::runtime::Runtime;
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio_util::sync::CancellationToken;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

fn pipe_name() -> String {
    format!(r"\\.\pipe\RemediaAgentTest-{}-{}", std::process::id(), rand_suffix())
}

fn rand_suffix() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos() as u64
}

async fn setup() -> (std::sync::Arc<Runtime>, tokio::sync::mpsc::Receiver<()>, String, CancellationToken, tempfile::TempDir, MockServer, MockServer) {
    let erp = common::mock_erp(1).await;
    let rem = MockServer::start().await;
    Mock::given(method("POST")).and(path("/v1/sync/heartbeat"))
        .respond_with(ResponseTemplate::new(204)).mount(&rem).await;
    let dir = tempfile::tempdir().unwrap();
    let toml = format!(
        "branch_id = \"farmacia-test\"\nremedia_url = \"{}\"\ntoken = \"tok\"\n[erp]\nbase_url = \"{}\"\n[log]\ndir = '{}'\n",
        rem.uri(), erp.uri(), dir.path().join("logs").display().to_string().replace('\\', "/")
    );
    Config::from_toml(&toml).unwrap().write(&dir.path().join(CONFIG_FILE)).unwrap();
    let shutdown = CancellationToken::new();
    let (rt, rx) = Runtime::build(dir.path(), shutdown.clone()).unwrap();
    let name = pipe_name();
    let rt2 = std::sync::Arc::clone(&rt);
    let n2 = name.clone();
    let sd = shutdown.clone();
    tokio::spawn(async move { serve_on(&n2, rt2, sd).await.unwrap() });
    tokio::time::sleep(Duration::from_millis(100)).await;
    (rt, rx, name, shutdown, dir, erp, rem)
}

#[tokio::test]
async fn status_over_real_pipe() {
    let (_rt, _rx, name, shutdown, _dir, _erp, _rem) = setup().await;
    let r = call_on(&name, &Request::Status).await.unwrap();
    assert!(r.ok);
    assert_eq!(r.status.unwrap().branch_id, "farmacia-test");
    // Varias conexiones seguidas: el server recrea la instancia cada vez.
    for _ in 0..3 {
        assert!(call_on(&name, &Request::Status).await.unwrap().ok);
    }
    shutdown.cancel();
}

#[tokio::test]
async fn sync_now_over_pipe_triggers_channel() {
    let (_rt, mut rx, name, shutdown, _dir, _erp, _rem) = setup().await;
    let r = call_on(&name, &Request::SyncNow).await.unwrap();
    assert!(r.ok);
    tokio::time::timeout(Duration::from_secs(2), rx.recv()).await.unwrap().unwrap();
    shutdown.cancel();
}

#[tokio::test]
async fn invalid_request_gets_error_response() {
    let (_rt, _rx, name, shutdown, _dir, _erp, _rem) = setup().await;
    let pipe = tokio::net::windows::named_pipe::ClientOptions::new().open(&name).unwrap();
    let mut reader = BufReader::new(pipe);
    reader.get_mut().write_all(b"{\"cmd\":\"reboot\"}\n").await.unwrap();
    let mut line = String::new();
    reader.read_line(&mut line).await.unwrap();
    let r: Response = serde_json::from_str(line.trim()).unwrap();
    assert!(!r.ok);
    assert!(r.error.unwrap().contains("no reconocido"));
    shutdown.cancel();
}

#[tokio::test]
async fn concurrent_clients() {
    let (_rt, _rx, name, shutdown, _dir, _erp, _rem) = setup().await;
    let mut set = tokio::task::JoinSet::new();
    for _ in 0..8 {
        let n = name.clone();
        set.spawn(async move { call_on(&n, &Request::Status).await.unwrap().ok });
    }
    while let Some(r) = set.join_next().await {
        assert!(r.unwrap());
    }
    shutdown.cancel();
}

#[tokio::test]
async fn missing_pipe_is_service_down() {
    let err = call_on(r"\\.\pipe\RemediaAgentNoExiste", &Request::Status).await.unwrap_err();
    assert!(err.to_string().contains("no está corriendo"), "{err}");
}
