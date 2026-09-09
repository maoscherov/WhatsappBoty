mod common;

use remedia_agent::config::{Config, CONFIG_FILE};
use remedia_agent::ipc::Request;
use remedia_agent::runtime::Runtime;
use std::sync::Arc;
use tokio_util::sync::CancellationToken;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

async fn remedia(status: u16) -> MockServer {
    let s = MockServer::start().await;
    Mock::given(method("POST")).and(path("/v1/sync/heartbeat"))
        .respond_with(ResponseTemplate::new(status)).mount(&s).await;
    s
}

fn write_config(dir: &std::path::Path, erp: &str, remedia: &str) -> Config {
    let toml = format!(
        "branch_id = \"farmacia-test\"\nremedia_url = \"{remedia}\"\ntoken = \"tok\"\n[erp]\nbase_url = \"{erp}\"\nrequest_timeout_secs = 3\n[log]\ndir = '{}'\n",
        dir.join("logs").display().to_string().replace('\\', "/")
    );
    let cfg = Config::from_toml(&toml).unwrap();
    cfg.write(&dir.join(CONFIG_FILE)).unwrap();
    cfg
}

fn build(dir: &std::path::Path) -> (Arc<Runtime>, tokio::sync::mpsc::Receiver<()>) {
    Runtime::build(dir, CancellationToken::new()).unwrap()
}

#[tokio::test]
async fn status_and_sync_now() {
    let erp = common::mock_erp(1).await;
    let rem = remedia(204).await;
    let dir = tempfile::tempdir().unwrap();
    write_config(dir.path(), &erp.uri(), &rem.uri());
    let (rt, mut rx) = build(dir.path());

    let r = rt.handle(Request::Status).await;
    let st = r.status.unwrap();
    assert_eq!(st.branch_id, "farmacia-test");
    assert_eq!(st.erp_status, "desconocido");
    assert_eq!(st.erp_url, erp.uri());
    assert!(!st.ws_connected);

    let r = rt.handle(Request::SyncNow).await;
    assert!(r.ok);
    rx.try_recv().unwrap();

    rt.engine.run_once().await.unwrap();
    let st = rt.handle(Request::Status).await.status.unwrap();
    assert_eq!(st.erp_status, "ok");
    assert_eq!(st.last_sync_fetched, Some(common::lote1().productos.len() as u64));
    assert!(st.metrics.erp_fetch_ms.is_some());
}

#[tokio::test]
async fn test_erp_reports_ms_and_products() {
    let erp = common::mock_erp(1).await;
    let rem = remedia(204).await;
    let dir = tempfile::tempdir().unwrap();
    write_config(dir.path(), &erp.uri(), &rem.uri());
    let (rt, _rx) = build(dir.path());

    let r = rt.handle(Request::TestErp { url: None }).await;
    assert!(r.ok, "{r:?}");
    assert_eq!(r.productos, Some(common::lote1().productos.len()));
    assert_eq!(r.cantidad_lotes, Some(1));
    assert!(r.ms.is_some());

    let r = rt.handle(Request::TestErp { url: Some("http://127.0.0.1:1".into()) }).await;
    assert!(!r.ok);
    assert_eq!(r.status_label.as_deref(), Some("inalcanzable"));
    assert!(r.error.unwrap().contains("No se pudo conectar"));

    let r = rt.handle(Request::TestErp { url: Some("no-es-url".into()) }).await;
    assert!(!r.ok);
}

#[tokio::test]
async fn set_config_rejects_bad_token_and_keeps_file() {
    let erp = common::mock_erp(1).await;
    let rem = remedia(401).await;
    let dir = tempfile::tempdir().unwrap();
    write_config(dir.path(), &erp.uri(), &rem.uri());
    let (rt, _rx) = build(dir.path());

    let r = rt.handle(Request::SetConfig { token: Some("malo".into()), erp_url: None, remedia_url: None }).await;
    assert!(!r.ok);
    assert!(r.error.unwrap().contains("rechazó el token"), "mensaje para humanos");
    let on_disk = Config::load(&dir.path().join(CONFIG_FILE)).unwrap();
    assert_eq!(on_disk.token, "tok");
    assert_eq!(rt.config().token, "tok");
}

#[tokio::test]
async fn set_config_applies_new_remedia_and_token_hot() {
    let erp = common::mock_erp(1).await;
    let rem1 = remedia(204).await;
    let rem2 = remedia(204).await;
    let dir = tempfile::tempdir().unwrap();
    write_config(dir.path(), &erp.uri(), &rem1.uri());
    let (rt, _rx) = build(dir.path());

    let r = rt.handle(Request::SetConfig {
        token: Some("nuevo".into()),
        erp_url: None,
        remedia_url: Some(format!("{}/", rem2.uri())),
    }).await;
    assert!(r.ok, "{r:?}");
    assert!(r.warnings.is_empty());

    let on_disk = Config::load(&dir.path().join(CONFIG_FILE)).unwrap();
    assert_eq!(on_disk.token, "nuevo");
    assert_eq!(on_disk.remedia_url, rem2.uri());
    assert_eq!(rt.engine.remedia().base_url(), rem2.uri());

    // El heartbeat siguiente va al Remedia nuevo con el token nuevo.
    rt.engine.send_heartbeat().await.unwrap();
    let reqs = rem2.received_requests().await.unwrap();
    let hb = reqs.iter().rev().find(|r| r.url.path() == "/v1/sync/heartbeat").unwrap();
    assert_eq!(hb.headers.get("authorization").unwrap().to_str().unwrap(), "Bearer nuevo");
}

#[tokio::test]
async fn set_config_saves_unreachable_erp_with_warning() {
    let erp = common::mock_erp(1).await;
    let rem = remedia(204).await;
    let dir = tempfile::tempdir().unwrap();
    write_config(dir.path(), &erp.uri(), &rem.uri());
    let (rt, _rx) = build(dir.path());

    let r = rt.handle(Request::SetConfig { token: None, erp_url: Some("http://127.0.0.1:1".into()), remedia_url: None }).await;
    assert!(r.ok, "{r:?}");
    assert_eq!(r.warnings.len(), 1);
    assert!(r.warnings[0].contains("no respondió"));
    let on_disk = Config::load(&dir.path().join(CONFIG_FILE)).unwrap();
    assert_eq!(on_disk.erp.base_url, "http://127.0.0.1:1");
    assert_eq!(rt.config().erp.base_url, "http://127.0.0.1:1");

    // El engine ya usa el adapter nuevo: el próximo ciclo reporta inalcanzable.
    let rep = rt.engine.run_once().await.unwrap();
    assert_eq!(rep.erp_status, "inalcanzable");

    let r = rt.handle(Request::SetConfig { token: None, erp_url: None, remedia_url: None }).await;
    assert!(!r.ok);
}
