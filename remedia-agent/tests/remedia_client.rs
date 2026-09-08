use remedia_agent::remedia::client::{
    now_rfc3339, CatalogBatch, FullManifest, Heartbeat, ManifestEntry, RemediaClient, RemediaError,
    SCHEMA_VERSION, SOURCE_OBSERVER,
};
use std::time::Duration;
use wiremock::matchers::{header, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

fn batch() -> CatalogBatch {
    CatalogBatch {
        schema_version: SCHEMA_VERSION,
        branch_id: "farmacia-test".into(),
        source: SOURCE_OBSERVER.into(),
        mode: "delta".into(),
        batch: 1,
        total_batches: 1,
        generated_at: now_rfc3339(),
        items: vec![],
    }
}

#[tokio::test]
async fn push_catalog_sends_bearer_and_parses_response() {
    let s = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/sync/catalog"))
        .and(header("authorization", "Bearer tok"))
        .and(header("content-type", "application/json"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "received": 500, "upserted": 37, "unchanged": 463
        })))
        .expect(1)
        .mount(&s)
        .await;
    let c = RemediaClient::new(&s.uri(), "tok", Duration::from_secs(5));
    let r = c.push_catalog(&batch()).await.unwrap();
    assert_eq!((r.received, r.upserted, r.unchanged), (500, 37, 463));
}

#[tokio::test]
async fn server_error_is_http_with_body() {
    let s = MockServer::start().await;
    Mock::given(method("POST")).and(path("/v1/sync/catalog"))
        .respond_with(ResponseTemplate::new(503).set_body_string("maintenance"))
        .mount(&s).await;
    let c = RemediaClient::new(&s.uri(), "tok", Duration::from_secs(5));
    match c.push_catalog(&batch()).await {
        Err(RemediaError::Http(503, body)) => assert_eq!(body, "maintenance"),
        other => panic!("{other:?}"),
    }
}

#[tokio::test]
async fn unreachable_is_unreachable() {
    let c = RemediaClient::new("http://127.0.0.1:1", "tok", Duration::from_secs(2));
    assert!(matches!(c.push_catalog(&batch()).await, Err(RemediaError::Unreachable(_))));
}

#[tokio::test]
async fn full_manifest_and_heartbeat() {
    let s = MockServer::start().await;
    Mock::given(method("POST")).and(path("/v1/sync/full-manifest"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "resend": ["7454"], "deactivated": 3 })))
        .mount(&s).await;
    Mock::given(method("POST")).and(path("/v1/sync/heartbeat"))
        .respond_with(ResponseTemplate::new(204)).expect(1).mount(&s).await;
    let c = RemediaClient::new(&s.uri(), "tok", Duration::from_secs(5));
    let r = c.full_manifest(&FullManifest {
        branch_id: "b".into(),
        generated_at: now_rfc3339(),
        items: vec![ManifestEntry { external_id: "7454".into(), hash: "x".into() }],
    }).await.unwrap();
    assert_eq!(r.resend, vec!["7454"]);
    assert_eq!(r.deactivated, 3);
    c.heartbeat(&Heartbeat {
        branch_id: "b".into(),
        agent_version: "0.1.0".into(),
        erp_version: None,
        erp_status: "ok".into(),
        last_sync_ok_at: None,
        catalog_count: 0,
        pending_batches: 0,
    }).await.unwrap();
}

#[test]
fn generated_at_has_offset() {
    let t = now_rfc3339();
    assert!(t.len() >= 25, "{t}");
    assert!(t.ends_with('Z') || t[19..].contains('+') || t[19..].contains('-'), "{t}");
}
