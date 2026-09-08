mod common;

use remedia_agent::catalog::state::{META_ERP_STATUS, META_LAST_FULL_MANIFEST, META_LAST_SYNC_OK};
use remedia_agent::catalog::{State, SyncEngine};
use remedia_agent::config::Config;
use remedia_agent::erp::build_adapter;
use remedia_agent::remedia::client::CatalogBatch;
use remedia_agent::remedia::RemediaClient;
use std::sync::Arc;
use std::time::Duration;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

fn config(erp_uri: &str, remedia_uri: &str, extra: &str) -> Arc<Config> {
    let toml = format!(
        r#"
branch_id = "farmacia-test"
remedia_url = "{remedia_uri}"
token = "tok"
[erp]
kind = "observer"
base_url = "{erp_uri}"
request_timeout_secs = 5
{extra}
"#
    );
    Arc::new(Config::from_toml(&toml).unwrap())
}

fn engine(erp_uri: &str, remedia_uri: &str, extra: &str) -> SyncEngine {
    let cfg = config(erp_uri, remedia_uri, extra);
    let erp = build_adapter(&cfg.erp, Duration::from_secs(5)).unwrap();
    let remedia = Arc::new(RemediaClient::new(&cfg.remedia_url, &cfg.token, Duration::from_secs(5)));
    let state = Arc::new(State::open_in_memory().unwrap());
    SyncEngine::new(erp, remedia, state, cfg)
}

async fn remedia_ok() -> MockServer {
    let s = MockServer::start().await;
    Mock::given(method("POST")).and(path("/v1/sync/catalog"))
        .respond_with(|req: &wiremock::Request| {
            let b: CatalogBatch = serde_json::from_slice(&req.body).unwrap();
            ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "received": b.items.len(), "upserted": b.items.len(), "unchanged": 0 }))
        })
        .mount(&s).await;
    Mock::given(method("POST")).and(path("/v1/sync/heartbeat"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({})))
        .mount(&s).await;
    s
}

/// Lotes recibidos por `/v1/sync/catalog`, en orden.
async fn received_batches(s: &MockServer) -> Vec<CatalogBatch> {
    s.received_requests().await.unwrap().iter()
        .filter(|r| r.url.path() == "/v1/sync/catalog")
        .map(|r| serde_json::from_slice(&r.body).unwrap())
        .collect()
}

#[tokio::test]
async fn first_run_sends_everything_second_run_sends_nothing() {
    let erp = common::mock_erp(1).await;
    let remedia = remedia_ok().await;
    let e = engine(&erp.uri(), &remedia.uri(), "");
    let n = common::lote1().productos.len();

    let r1 = e.run_once().await.unwrap();
    assert_eq!(r1.erp_status, "ok");
    assert_eq!(r1.fetched, n);
    assert_eq!(r1.changed, n);
    assert_eq!(r1.sent_batches, 1);
    assert_eq!(r1.queued_batches, 0);
    assert_eq!(e.state.count_items().unwrap() as usize, n);
    assert!(e.state.get_meta(META_LAST_SYNC_OK).unwrap().is_some());

    let batches = received_batches(&remedia).await;
    assert_eq!(batches.len(), 1);
    assert_eq!(batches[0].mode, "delta");
    assert_eq!(batches[0].branch_id, "farmacia-test");
    assert_eq!(batches[0].source, "observer-gestion");
    assert_eq!((batches[0].batch, batches[0].total_batches), (1, 1));
    assert_eq!(batches[0].schema_version, 1);

    let r2 = e.run_once().await.unwrap();
    assert_eq!(r2.changed, 0);
    assert_eq!(r2.sent_batches, 0);
    assert_eq!(received_batches(&remedia).await.len(), 1);
}

#[tokio::test]
async fn stock_change_produces_delta_of_one() {
    let remedia = remedia_ok().await;
    let erp1 = common::mock_erp(1).await;
    let e = engine(&erp1.uri(), &remedia.uri(), "");
    e.run_once().await.unwrap();
    drop(erp1);

    // Mismo catálogo, 7454 ahora con stock 5. Nuevo mock ERP en otra URL:
    // reconstruimos el engine reutilizando el estado.
    let erp2 = common::mock_erp_with(1, |l| {
        l.productos.iter_mut().find(|p| p.id_producto == 7454).unwrap().stock_sucursal = 5.0;
    }).await;
    let cfg = config(&erp2.uri(), &remedia.uri(), "");
    let e2 = SyncEngine::new(
        build_adapter(&cfg.erp, Duration::from_secs(5)).unwrap(),
        Arc::clone(&e.remedia),
        Arc::clone(&e.state),
        cfg,
    );
    let r = e2.run_once().await.unwrap();
    assert_eq!(r.changed, 1);
    let batches = received_batches(&remedia).await;
    let last = batches.last().unwrap();
    assert_eq!(last.items.len(), 1);
    assert_eq!(last.items[0].external_id, "7454");
    assert_eq!(last.items[0].stock, 5);
    assert_eq!(last.items[0].name, "CLARITROMICINA RICHET 500 mg COM x 8");
}

#[tokio::test]
async fn remedia_down_queues_and_keeps_state_unchanged_then_flushes() {
    let erp = common::mock_erp(1).await;
    let remedia = MockServer::start().await;
    let down = Mock::given(method("POST")).and(path("/v1/sync/catalog"))
        .respond_with(ResponseTemplate::new(503));
    let guard = remedia.register_as_scoped(down).await;
    let e = engine(&erp.uri(), &remedia.uri(), "");

    let r = e.run_once().await.unwrap();
    assert_eq!(r.sent_batches, 0);
    assert_eq!(r.queued_batches, 1);
    assert_eq!(e.state.pending_count().unwrap(), 1);
    assert_eq!(e.state.count_items().unwrap(), 0);
    assert!(e.state.get_meta(META_LAST_SYNC_OK).unwrap().is_none());
    let pending = e.state.due_pending(i64::MAX).unwrap();
    assert_eq!(pending[0].attempts, 1);

    // No está vencido todavía: flush no manda nada.
    assert_eq!(e.flush_pending().await.unwrap(), 0);

    // Remedia vuelve. Forzamos el vencimiento y sincronizamos de nuevo.
    drop(guard);
    Mock::given(method("POST")).and(path("/v1/sync/catalog"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "received": 12, "upserted": 12, "unchanged": 0 })))
        .mount(&remedia).await;
    e.state.mark_failed(pending[0].id, 0).unwrap();
    let r2 = e.run_once().await.unwrap();
    assert_eq!(r2.flushed_batches, 1);
    assert_eq!(r2.changed, 0, "el lote encolado ya cubría todo el catálogo");
    assert_eq!(e.state.pending_count().unwrap(), 0);
    assert_eq!(e.state.count_items().unwrap() as usize, common::lote1().productos.len());
    assert!(e.state.get_meta(META_LAST_SYNC_OK).unwrap().is_some());
}

#[tokio::test]
async fn erp_401_sets_no_autorizado_and_sends_nothing() {
    let erp = MockServer::start().await;
    Mock::given(method("GET")).and(path("/api/productos/lote/1"))
        .respond_with(ResponseTemplate::new(401).set_body_string("No autorizado para obtener información de productos."))
        .mount(&erp).await;
    let remedia = remedia_ok().await;
    let e = engine(&erp.uri(), &remedia.uri(), "");
    let r = e.run_once().await.unwrap();
    assert_eq!(r.erp_status, "no_autorizado");
    assert_eq!(r.changed, 0);
    assert_eq!(e.state.get_meta(META_ERP_STATUS).unwrap().as_deref(), Some("no_autorizado"));
    assert!(received_batches(&remedia).await.is_empty());

    let hb = e.build_heartbeat().unwrap();
    assert_eq!(hb.erp_status, "no_autorizado");
    assert_eq!(hb.pending_batches, 0);
    e.send_heartbeat().await.unwrap();
}

#[tokio::test]
async fn erp_unreachable_sets_inalcanzable() {
    let remedia = remedia_ok().await;
    let e = engine("http://127.0.0.1:1", &remedia.uri(), "");
    let r = e.run_once().await.unwrap();
    assert_eq!(r.erp_status, "inalcanzable");
    assert_eq!(e.build_heartbeat().unwrap().erp_status, "inalcanzable");
}

#[tokio::test]
async fn full_manifest_resends_mismatched_and_prunes_missing() {
    let erp = common::mock_erp(1).await;
    let remedia = remedia_ok().await;
    Mock::given(method("POST")).and(path("/v1/sync/full-manifest"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "resend": ["7454", "20000"], "deactivated": 0 })))
        .mount(&remedia).await;
    let e = engine(&erp.uri(), &remedia.uri(), "");
    e.run_once().await.unwrap();
    // Simulamos un producto que Remedia conoce pero el ERP ya no tiene.
    e.state.upsert_hashes(&[("424242".into(), "zzz".into())]).unwrap();

    let resent = e.full_manifest().await.unwrap();
    assert_eq!(resent, 2);
    let batches = received_batches(&remedia).await;
    let last = batches.last().unwrap();
    assert_eq!(last.mode, "full");
    let mut ids: Vec<&str> = last.items.iter().map(|i| i.external_id.as_str()).collect();
    ids.sort();
    assert_eq!(ids, vec!["20000", "7454"]);

    let manifest_req = remedia.received_requests().await.unwrap().into_iter()
        .find(|r| r.url.path() == "/v1/sync/full-manifest").unwrap();
    let m: serde_json::Value = serde_json::from_slice(&manifest_req.body).unwrap();
    assert_eq!(m["items"].as_array().unwrap().len(), common::lote1().productos.len());
    assert!(m["items"][0].get("name").is_none(), "el manifiesto no lleva payload completo");

    assert!(!e.state.known_hashes().unwrap().contains_key("424242"), "se poda lo que ya no está");
    assert!(e.state.get_meta(META_LAST_FULL_MANIFEST).unwrap().is_some());
}

#[tokio::test]
async fn full_manifest_failure_does_not_mark_done() {
    let erp = common::mock_erp(1).await;
    let remedia = remedia_ok().await;
    Mock::given(method("POST")).and(path("/v1/sync/full-manifest"))
        .respond_with(ResponseTemplate::new(500)).mount(&remedia).await;
    let e = engine(&erp.uri(), &remedia.uri(), "");
    assert!(e.full_manifest().await.is_err());
    assert!(e.state.get_meta(META_LAST_FULL_MANIFEST).unwrap().is_none());
}

#[tokio::test]
async fn daily_id_scan_is_off_by_default_and_finds_products_when_on() {
    let erp = common::mock_erp(1).await;
    let remedia = remedia_ok().await;
    let e = engine(&erp.uri(), &remedia.uri(), "");
    assert!(e.daily_id_scan().await.unwrap().is_empty());

    let e = engine(&erp.uri(), &remedia.uri(), "daily_id_scan = true\nid_scan_max = 7460\nmax_concurrency = 4");
    let found = e.daily_id_scan().await.unwrap();
    let mut ids: Vec<i64> = found.iter().map(|p| p.id_producto).collect();
    ids.sort();
    assert_eq!(ids, vec![7454, 7455]);

    // El lote gana sobre el barrido ante el mismo id; el barrido no agrega nada
    // que el lote ya traiga, así que el conteo no cambia.
    let r = e.run_once_with(found).await.unwrap();
    assert_eq!(r.fetched, common::lote1().productos.len());
}

#[tokio::test]
async fn batches_are_split_at_500() {
    let erp = common::mock_erp_with(1, |l| {
        let base = l.productos[0].clone();
        for i in 0..1100 {
            let mut p = base.clone();
            p.id_producto = 100_000 + i;
            p.codigo_barras = vec![];
            l.productos.push(p);
        }
    }).await;
    let remedia = remedia_ok().await;
    let e = engine(&erp.uri(), &remedia.uri(), "");
    let r = e.run_once().await.unwrap();
    assert_eq!(r.sent_batches, 3);
    let batches = received_batches(&remedia).await;
    assert_eq!(batches.iter().map(|b| b.items.len()).collect::<Vec<_>>(), vec![500, 500, r.fetched - 1000]);
    assert!(batches.iter().all(|b| b.total_batches == 3));
}
