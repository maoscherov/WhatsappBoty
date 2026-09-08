mod common;

use remedia_agent::erp::observer::ObserverAdapter;
use remedia_agent::erp::{ErpAdapter, ErpError};
use std::time::Duration;
use wiremock::matchers::{body_json, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

fn adapter(uri: &str) -> ObserverAdapter {
    ObserverAdapter::new(uri, Duration::from_secs(5))
}

#[tokio::test]
async fn fetch_all_reads_all_lotes_and_stops_on_400() {
    let s = common::mock_erp(1).await;
    let p = adapter(&s.uri()).fetch_all().await.unwrap();
    assert_eq!(p.len(), common::lote1().productos.len());
    assert!(p.iter().any(|x| x.id_producto == 7454));
}

#[tokio::test]
async fn fetch_all_concatenates_lotes() {
    let s = common::mock_erp(3).await;
    let p = adapter(&s.uri()).fetch_all().await.unwrap();
    assert_eq!(p.len(), common::lote1().productos.len() * 3);
}

#[tokio::test]
async fn cantidad_lotes_from_first_lote_is_used_even_if_later_lotes_differ() {
    let s = MockServer::start().await;
    let mut l1 = common::lote1();
    l1.cantidad_lotes = 2;
    let mut l2 = common::lote1();
    l2.cantidad_lotes = 5;
    Mock::given(method("GET")).and(path("/api/productos/lote/1"))
        .respond_with(ResponseTemplate::new(200).set_body_json(&l1)).mount(&s).await;
    Mock::given(method("GET")).and(path("/api/productos/lote/2"))
        .respond_with(ResponseTemplate::new(200).set_body_json(&l2)).mount(&s).await;
    Mock::given(method("GET")).and(path("/api/productos/lote/3"))
        .respond_with(ResponseTemplate::new(200).set_body_json(&l2)).expect(0).mount(&s).await;
    let p = adapter(&s.uri()).fetch_all().await.unwrap();
    assert_eq!(p.len(), common::lote1().productos.len() * 2);
}

#[tokio::test]
async fn early_400_ends_the_scan() {
    let s = MockServer::start().await;
    let mut l1 = common::lote1();
    l1.cantidad_lotes = 4;
    Mock::given(method("GET")).and(path("/api/productos/lote/1"))
        .respond_with(ResponseTemplate::new(200).set_body_json(&l1)).mount(&s).await;
    Mock::given(method("GET")).and(path("/api/productos/lote/2"))
        .respond_with(ResponseTemplate::new(400).set_body_json(serde_json::json!({
            "Message": "El numeroLote=2 no genera un conjunto de productos"}))).mount(&s).await;
    let p = adapter(&s.uri()).fetch_all().await.unwrap();
    assert_eq!(p.len(), common::lote1().productos.len());
}

#[tokio::test]
async fn fetch_all_401_is_not_authorized() {
    let s = MockServer::start().await;
    Mock::given(method("GET")).and(path("/api/productos/lote/1"))
        .respond_with(ResponseTemplate::new(401)
            .set_body_string("No autorizado para obtener información de productos."))
        .mount(&s).await;
    let r = adapter(&s.uri()).fetch_all().await;
    assert!(matches!(r, Err(ErpError::NotAuthorized)), "{r:?}");
}

#[tokio::test]
async fn server_error_is_http() {
    let s = MockServer::start().await;
    Mock::given(method("GET")).and(path("/api/productos/lote/1"))
        .respond_with(ResponseTemplate::new(500).set_body_string("boom")).mount(&s).await;
    let r = adapter(&s.uri()).fetch_all().await;
    assert!(matches!(r, Err(ErpError::Http(500, _))), "{r:?}");
}

#[tokio::test]
async fn unreachable_erp() {
    let a = ObserverAdapter::new("http://127.0.0.1:1", Duration::from_secs(2));
    let r = a.fetch_all().await;
    assert!(matches!(r, Err(ErpError::Unreachable(_))), "{r:?}");
    assert_eq!(r.unwrap_err().status_label(), "inalcanzable");
}

#[tokio::test]
async fn lookup_by_barcodes_posts_list() {
    let s = MockServer::start().await;
    let lote = common::lote1();
    let found: Vec<_> = lote.productos.iter().take(2).cloned().collect();
    Mock::given(method("POST")).and(path("/api/productos/codigosBarras"))
        .and(body_json(serde_json::json!(["7795336085205", "7795336085212"])))
        .respond_with(ResponseTemplate::new(200).set_body_json(&found)).mount(&s).await;
    let r = adapter(&s.uri())
        .lookup_by_barcodes(&["7795336085205".to_string(), "7795336085212".to_string()])
        .await.unwrap();
    assert_eq!(r.len(), 2);
}

#[tokio::test]
async fn lookup_by_barcodes_404_is_empty_and_empty_input_skips_request() {
    let s = MockServer::start().await;
    Mock::given(method("POST")).and(path("/api/productos/codigosBarras"))
        .respond_with(ResponseTemplate::new(404)).expect(1).mount(&s).await;
    let a = adapter(&s.uri());
    assert!(a.lookup_by_barcodes(&["nope".to_string()]).await.unwrap().is_empty());
    assert!(a.lookup_by_barcodes(&[]).await.unwrap().is_empty());
}

#[tokio::test]
async fn lookup_by_id_404_is_none_and_hit_is_some() {
    let s = common::mock_erp(1).await;
    let a = adapter(&s.uri());
    assert!(a.lookup_by_id(999_999).await.unwrap().is_none());
    let p = a.lookup_by_id(7454).await.unwrap().unwrap();
    assert_eq!(p.descripcion, "CLARITROMICINA RICHET 500 mg COM x    8");
}

#[tokio::test]
async fn build_adapter_rejects_unknown_kind() {
    let cfg = remedia_agent::config::ErpConfig {
        kind: "sap".into(),
        base_url: "http://x".into(),
        sync_interval_secs: 900,
        max_concurrency: 4,
        request_timeout_secs: 30,
        daily_id_scan: false,
        id_scan_max: 100,
    };
    assert!(remedia_agent::erp::build_adapter(&cfg, Duration::from_secs(1)).is_err());
}
