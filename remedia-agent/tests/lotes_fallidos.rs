//! 0.3.5: un lote que el ERP no puede servir (500) no aborta el ciclo; sus
//! productos se recuperan por id (incidente 28/9, lote 11 con 4 productos rotos).

mod common;

use remedia_agent::catalog::{State, SyncEngine};
use remedia_agent::config::Config;
use remedia_agent::erp::build_adapter;
use remedia_agent::erp::model::{LoteResponse, ProductoDTO};
use remedia_agent::erp::observer::ObserverAdapter;
use remedia_agent::erp::{ErpAdapter, ErpError};
use remedia_agent::metrics::Metrics;
use remedia_agent::remedia::client::CatalogBatch;
use remedia_agent::remedia::RemediaClient;
use std::sync::Arc;
use std::time::Duration;
use wiremock::matchers::{method, path, path_regex};
use wiremock::{Mock, MockServer, ResponseTemplate};

fn prod(id: i64) -> ProductoDTO {
    let mut p = common::lote1().productos[0].clone();
    p.id_producto = id;
    p.codigo_barras = vec![];
    p.descripcion = format!("PRODUCTO {id}");
    p.stock_sucursal = 3.0;
    p
}

fn lote(total: u32, ids: std::ops::RangeInclusive<i64>) -> LoteResponse {
    LoteResponse { cantidad_lotes: total, productos: ids.map(prod).collect() }
}

async fn lote_ok(s: &MockServer, n: u32, l: &LoteResponse) {
    Mock::given(method("GET")).and(path(format!("/api/productos/lote/{n}")))
        .respond_with(ResponseTemplate::new(200).set_body_json(l)).mount(s).await;
}

async fn lote_500(s: &MockServer, n: u32) {
    Mock::given(method("GET")).and(path(format!("/api/productos/lote/{n}")))
        .respond_with(ResponseTemplate::new(500).set_body_string("error interno")).mount(s).await;
}

async fn id_ok(s: &MockServer, ids: std::ops::RangeInclusive<i64>) {
    for id in ids {
        Mock::given(method("GET")).and(path(format!("/api/productos/{id}")))
            .respond_with(ResponseTemplate::new(200).set_body_json(prod(id))).mount(s).await;
    }
}

async fn id_500(s: &MockServer, id: i64) {
    Mock::given(method("GET")).and(path(format!("/api/productos/{id}")))
        .respond_with(ResponseTemplate::new(500)).mount(s).await;
}

/// Lo que no se montó antes: lote fuera de rango (400) e id inexistente (404).
async fn fallbacks(s: &MockServer) {
    Mock::given(method("GET")).and(path_regex(r"^/api/productos/lote/\d+$"))
        .respond_with(ResponseTemplate::new(400).set_body_json(serde_json::json!({"Message": "fin"})))
        .mount(s).await;
    Mock::given(method("GET")).and(path_regex(r"^/api/productos/\d+$"))
        .respond_with(ResponseTemplate::new(404)).mount(s).await;
}

fn adapter(uri: &str) -> ObserverAdapter {
    ObserverAdapter::new(uri, Duration::from_secs(5))
}

fn engine(erp_uri: &str, remedia_uri: &str) -> SyncEngine {
    let toml = format!(
        "branch_id = \"farmacia-test\"\nremedia_url = \"{remedia_uri}\"\ntoken = \"tok\"\n[erp]\nbase_url = \"{erp_uri}\"\nlive_pause_ms = 0\nrequest_timeout_secs = 5\n"
    );
    let cfg = Arc::new(Config::from_toml(&toml).unwrap());
    let erp = build_adapter(&cfg.erp, Duration::from_secs(5)).unwrap();
    let remedia = Arc::new(RemediaClient::new(&cfg.remedia_url, &cfg.token, Duration::from_secs(5)));
    SyncEngine::new(erp, remedia, Arc::new(State::open_in_memory().unwrap()), cfg, Metrics::new())
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
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({}))).mount(&s).await;
    Mock::given(method("POST")).and(path("/v1/sync/full-manifest"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "resend": [], "deactivated": 0 }))).mount(&s).await;
    s
}

async fn received_items(s: &MockServer) -> Vec<String> {
    s.received_requests().await.unwrap().iter()
        .filter(|r| r.url.path() == "/v1/sync/catalog")
        .flat_map(|r| serde_json::from_slice::<CatalogBatch>(&r.body).unwrap().items)
        .map(|i| i.external_id)
        .collect()
}

/// Lotes 1 y 3 bien (ids 1-5 y 11-15), lote 2 con 500. El hueco 6..=10: tres
/// ids sanos, uno inexistente y uno que también da 500.
async fn erp_lote_2_roto() -> MockServer {
    let s = MockServer::start().await;
    lote_ok(&s, 1, &lote(3, 1..=5)).await;
    lote_500(&s, 2).await;
    lote_ok(&s, 3, &lote(3, 11..=15)).await;
    id_ok(&s, 6..=8).await;
    // el 9 cae al 404 del fallback
    id_500(&s, 10).await;
    id_ok(&s, 1..=5).await;
    id_ok(&s, 11..=15).await;
    fallbacks(&s).await;
    s
}

#[tokio::test]
async fn lote_intermedio_con_500_se_recupera_por_id() {
    let s = erp_lote_2_roto().await;
    let r = adapter(&s.uri()).fetch_all().await.unwrap();
    let mut ids: Vec<i64> = r.productos.iter().map(|p| p.id_producto).collect();
    ids.sort();
    assert_eq!(ids, vec![1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 13, 14, 15]);
    assert_eq!(r.lotes_fallidos, vec![2]);
    assert_eq!(r.ids_rotos, vec![10]);
    assert_eq!(r.lotes, 2, "solo cuentan los lotes leídos bien");
    // Se recorrió exactamente el hueco (6..=10), no más.
    let pedidos: Vec<String> = s.received_requests().await.unwrap().iter()
        .map(|r| r.url.path().to_string())
        .filter(|p| !p.contains("/lote/"))
        .collect();
    assert_eq!(pedidos.len(), 5, "{pedidos:?}");
}

#[tokio::test]
async fn ciclo_con_lote_roto_no_aborta_y_envia_delta() {
    let erp = erp_lote_2_roto().await;
    let remedia = remedia_ok().await;
    let e = engine(&erp.uri(), &remedia.uri());

    let r = e.run_once().await.unwrap();
    assert_eq!(r.erp_status, "ok");
    assert_eq!(r.fetched, 13);
    assert_eq!(r.changed, 13);
    assert_eq!(r.sent_batches, 1);
    let mut items = received_items(&remedia).await;
    items.sort_by_key(|i| i.parse::<i64>().unwrap());
    assert_eq!(items, ["1", "2", "3", "4", "5", "6", "7", "8", "11", "12", "13", "14", "15"]);

    let hb = e.build_heartbeat().unwrap();
    assert_eq!(hb.erp_lotes_fallidos, Some(vec![2]));
    assert_eq!(hb.erp_productos_rotos, Some(vec![10]));
}

#[tokio::test]
async fn ultimo_lote_roto_corta_tras_300_404_seguidos() {
    let s = MockServer::start().await;
    lote_ok(&s, 1, &lote(2, 1..=3)).await;
    lote_500(&s, 2).await;
    id_ok(&s, 4..=6).await; // vivos en el hueco; del 7 en adelante, 404
    fallbacks(&s).await;
    let r = adapter(&s.uri()).fetch_all().await.unwrap();
    assert_eq!(r.productos.len(), 6);
    assert_eq!(r.lotes_fallidos, vec![2]);
    assert!(r.ids_rotos.is_empty());
    let max_id = s.received_requests().await.unwrap().iter()
        .filter_map(|r| r.url.path().strip_prefix("/api/productos/").and_then(|x| x.parse::<i64>().ok()))
        .max().unwrap();
    assert_eq!(max_id, 306, "6 vivos + 300 404 seguidos");
}

#[tokio::test]
async fn lote_1_con_500_sigue_siendo_error() {
    let s = MockServer::start().await;
    lote_500(&s, 1).await;
    fallbacks(&s).await;
    let r = adapter(&s.uri()).fetch_all().await;
    assert!(matches!(r, Err(ErpError::Http(500, _))), "{r:?}");
}

#[tokio::test]
async fn erp_no_autorizado_al_recuperar_propaga_el_error() {
    let s = MockServer::start().await;
    lote_ok(&s, 1, &lote(3, 1..=5)).await;
    lote_500(&s, 2).await;
    lote_ok(&s, 3, &lote(3, 11..=15)).await;
    Mock::given(method("GET")).and(path("/api/productos/6"))
        .respond_with(ResponseTemplate::new(401)).mount(&s).await;
    fallbacks(&s).await;
    let r = adapter(&s.uri()).fetch_all().await;
    assert!(matches!(r, Err(ErpError::NotAuthorized)), "{r:?}");
}

#[tokio::test]
async fn full_manifest_incluye_rotos_y_extras_con_hash_conocido() {
    let erp = erp_lote_2_roto().await;
    let remedia = remedia_ok().await;
    let e = engine(&erp.uri(), &remedia.uri());
    // El 10 (roto hoy) y el 999 (del sondeo por CB, no está en ningún lote) ya
    // se habían enviado antes; el 555 nunca se envió.
    e.state.upsert_hashes(&[("10".into(), "h10".into()), ("999".into(), "h999".into())]).unwrap();
    e.state.extras_cb_add(&[("7790001".into(), 999), ("7790002".into(), 555)]).unwrap();

    e.full_manifest().await.unwrap();

    let req = remedia.received_requests().await.unwrap().into_iter()
        .find(|r| r.url.path() == "/v1/sync/full-manifest").unwrap();
    let m: serde_json::Value = serde_json::from_slice(&req.body).unwrap();
    let mut ids: Vec<i64> = m["items"].as_array().unwrap().iter()
        .map(|i| i["external_id"].as_str().unwrap().parse().unwrap()).collect();
    ids.sort();
    assert_eq!(ids, vec![1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14, 15, 999]);
    let hash_10 = m["items"].as_array().unwrap().iter()
        .find(|i| i["external_id"] == "10").unwrap()["hash"].as_str().unwrap().to_string();
    assert_eq!(hash_10, "h10", "va con el hash anterior");
    // y no se poda del estado local
    let known = e.state.known_hashes().unwrap();
    assert!(known.contains_key("10") && known.contains_key("999"));
}

#[tokio::test]
async fn heartbeat_lleva_los_campos_nuevos() {
    let erp = common::mock_erp(1).await;
    let remedia = remedia_ok().await;
    let e = engine(&erp.uri(), &remedia.uri());

    // Antes de la primera lectura no se sabe nada: los campos no viajan.
    e.send_heartbeat().await.unwrap();
    // Con una lectura sana viajan vacíos (así el servidor limpia lo anterior).
    e.run_once().await.unwrap();
    e.send_heartbeat().await.unwrap();

    let hbs: Vec<serde_json::Value> = remedia.received_requests().await.unwrap().iter()
        .filter(|r| r.url.path() == "/v1/sync/heartbeat")
        .map(|r| serde_json::from_slice(&r.body).unwrap())
        .collect();
    assert_eq!(hbs.len(), 2);
    assert!(hbs[0].get("erp_productos_rotos").is_none());
    assert!(hbs[0].get("erp_lotes_fallidos").is_none());
    assert!(hbs[0].get("sondeo").is_none());
    assert_eq!(hbs[1]["erp_productos_rotos"], serde_json::json!([]));
    assert_eq!(hbs[1]["erp_lotes_fallidos"], serde_json::json!([]));
    assert!(hbs[1].get("sondeo").is_none(), "nunca corrió el sondeo");
}
