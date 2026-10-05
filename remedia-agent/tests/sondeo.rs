//! 0.3.5: sondeo diario por código de barras. El listado por lotes de Observer
//! omite productos que existen (SESAREN XR, 28/9); `codigosBarras` los encuentra.

mod common;

use remedia_agent::catalog::{State, SyncEngine};
use remedia_agent::config::Config;
use remedia_agent::erp::build_adapter;
use remedia_agent::erp::model::{LoteResponse, ProductoDTO};
use remedia_agent::metrics::Metrics;
use remedia_agent::remedia::client::CatalogBatch;
use remedia_agent::remedia::RemediaClient;
use std::sync::Arc;
use std::time::Duration;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

fn prod(id: i64, cbs: &[&str]) -> ProductoDTO {
    let mut p = common::lote1().productos[0].clone();
    p.id_producto = id;
    p.codigo_barras = cbs.iter().map(|c| c.to_string()).collect();
    p.descripcion = format!("PRODUCTO {id}");
    p.stock_sucursal = 4.0;
    p
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

/// Remedia con los endpoints de sync; `faltantes` = respuesta de codigos-faltantes.
async fn remedia(faltantes: ResponseTemplate) -> MockServer {
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
    Mock::given(method("GET")).and(path("/v1/sync/codigos-faltantes"))
        .respond_with(faltantes).mount(&s).await;
    s
}

/// ERP cuyo lote lista solo 1 y 2; `en_erp` son los productos que existen y
/// `codigosBarras`/`{id}` encuentran pero el lote omite.
async fn erp(en_erp: Vec<ProductoDTO>) -> MockServer {
    let s = MockServer::start().await;
    mount_erp(&s, en_erp).await;
    s
}

async fn mount_erp(s: &MockServer, en_erp: Vec<ProductoDTO>) {
    let lote = LoteResponse { cantidad_lotes: 1, productos: vec![prod(1, &[]), prod(2, &[])] };
    common::mount_lote(s, &lote).await;
    let mut todos = lote.clone();
    todos.productos.extend(en_erp);
    common::mount_lookups(s, &todos).await;
}

async fn batch_ids(s: &MockServer) -> Vec<String> {
    s.received_requests().await.unwrap().iter()
        .filter(|r| r.url.path() == "/v1/sync/catalog")
        .flat_map(|r| serde_json::from_slice::<CatalogBatch>(&r.body).unwrap().items)
        .map(|i| i.external_id)
        .collect()
}

#[tokio::test]
async fn sondeo_encuentra_extras_y_los_sincroniza_en_los_ciclos_siguientes() {
    let faltantes = ResponseTemplate::new(200).set_body_json(serde_json::json!({
        "codigos": ["7795345013084", "7795345013091", "7795345013107", "abc", ""] }));
    let remedia = remedia(faltantes).await;
    // Existen dos de los tres códigos (el segundo producto tiene además otro CB);
    // el tercero no está en el ERP.
    let erp = erp(vec![
        prod(12521, &["7795345013084"]),
        prod(12522, &["7795345013091", "7795345019999"]),
    ]).await;
    let e = engine(&erp.uri(), &remedia.uri());

    assert!(e.sondeo_cb().await.unwrap());
    let extras = e.state.extras_cb().unwrap();
    assert_eq!(extras.len(), 3, "{extras:?}");
    assert_eq!(extras["7795345013084"], 12521);
    assert_eq!(extras["7795345019999"], 12522);
    let hb = e.build_heartbeat().unwrap();
    let s = hb.sondeo.expect("resumen del sondeo");
    assert_eq!((s.consultados, s.encontrados, s.activos), (3, 2, 2));

    // Un segundo sondeo no vuelve a pedir lo ya descubierto.
    assert!(e.sondeo_cb().await.unwrap());
    assert_eq!(e.build_heartbeat().unwrap().sondeo.unwrap().consultados, 1);

    // Ciclo siguiente (el sondeo ya corrió hoy, no toca): los extras se
    // releen y se envían junto con el lote.
    remedia_agent::service::run_cycle(&e).await;
    let mut ids = batch_ids(&remedia).await;
    ids.sort();
    assert_eq!(ids, vec!["1", "12521", "12522", "2"]);
    // El full-manifest de ese ciclo no deja que el servidor los desactive.
    let m = remedia.received_requests().await.unwrap().into_iter()
        .find(|r| r.url.path() == "/v1/sync/full-manifest").unwrap();
    let m: serde_json::Value = serde_json::from_slice(&m.body).unwrap();
    let en_manifiesto: Vec<&str> = m["items"].as_array().unwrap().iter()
        .map(|i| i["external_id"].as_str().unwrap()).collect();
    assert!(en_manifiesto.contains(&"12521") && en_manifiesto.contains(&"12522"), "{en_manifiesto:?}");

    // El 12522 desaparece del ERP: sus CB se olvidan; el 12521 sigue.
    erp.reset().await;
    mount_erp(&erp, vec![prod(12521, &["7795345013084"])]).await;
    let extra = e.extras_cb_lookup().await.unwrap();
    assert_eq!(extra.iter().map(|p| p.id_producto).collect::<Vec<_>>(), vec![12521]);
    let extras = e.state.extras_cb().unwrap();
    assert_eq!(extras.keys().collect::<Vec<_>>(), vec!["7795345013084"]);
}

#[tokio::test]
async fn erp_caido_conserva_los_extras() {
    let remedia = remedia(ResponseTemplate::new(200).set_body_json(serde_json::json!({ "codigos": [] }))).await;
    let e = engine("http://127.0.0.1:1", &remedia.uri());
    e.state.extras_cb_add(&[("7795345013084".into(), 12521)]).unwrap();
    assert!(e.extras_cb_lookup().await.unwrap().is_empty());
    assert_eq!(e.state.extras_cb().unwrap().len(), 1);
}

#[tokio::test]
async fn codigos_faltantes_404_es_lista_vacia() {
    let remedia = remedia(ResponseTemplate::new(404)).await;
    let c = RemediaClient::new(&remedia.uri(), "tok", Duration::from_secs(5));
    assert!(c.codigos_faltantes().await.unwrap().is_empty());

    // El sondeo con un servidor viejo no rompe nada.
    let erp = erp(vec![]).await;
    let e = engine(&erp.uri(), &remedia.uri());
    assert!(e.sondeo_cb().await.unwrap());
    assert!(e.state.extras_cb().unwrap().is_empty());
    assert_eq!(e.build_heartbeat().unwrap().sondeo.unwrap().consultados, 0);
}

#[tokio::test]
async fn codigos_faltantes_error_de_servidor_no_registra_corrida() {
    let remedia = remedia(ResponseTemplate::new(500)).await;
    let c = RemediaClient::new(&remedia.uri(), "tok", Duration::from_secs(5));
    assert!(c.codigos_faltantes().await.is_err());
    let erp = erp(vec![]).await;
    let e = engine(&erp.uri(), &remedia.uri());
    assert!(!e.sondeo_cb().await.unwrap());
    assert!(e.build_heartbeat().unwrap().sondeo.is_none());
}

#[tokio::test]
async fn sondeo_respeta_sondeo_max_y_manda_la_autenticacion() {
    let faltantes = ResponseTemplate::new(200).set_body_json(serde_json::json!({
        "codigos": ["7795345013084", "7795345013091", "7795345013107"] }));
    let remedia = remedia(faltantes).await;
    let erp = erp(vec![]).await;
    let mut cfg_e = engine(&erp.uri(), &remedia.uri());
    let mut cfg = (*cfg_e.cfg).clone();
    cfg.erp.sondeo_max = 2;
    cfg_e = SyncEngine::new(cfg_e.erp(), cfg_e.remedia(), Arc::clone(&cfg_e.state), Arc::new(cfg), Metrics::new());
    cfg_e.sondeo_cb().await.unwrap();
    assert_eq!(cfg_e.build_heartbeat().unwrap().sondeo.unwrap().consultados, 2);
    let req = remedia.received_requests().await.unwrap().into_iter()
        .find(|r| r.url.path() == "/v1/sync/codigos-faltantes").unwrap();
    assert_eq!(req.headers.get("authorization").unwrap().to_str().unwrap(), "Bearer tok");
}
