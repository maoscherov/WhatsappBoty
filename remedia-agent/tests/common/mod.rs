//! Helpers compartidos por los tests de integración: mock del ERP con wiremock.

#![allow(dead_code)]

use remedia_agent::erp::model::LoteResponse;
use wiremock::matchers::{method, path, path_regex};
use wiremock::{Mock, MockServer, ResponseTemplate};

pub fn fixture(name: &str) -> String {
    let p = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures")
        .join(name);
    std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("fixture {}: {e}", p.display()))
}

pub fn lote1() -> LoteResponse {
    serde_json::from_str(&fixture("lote1.json")).expect("lote1.json parsea")
}

/// Mock del ERP con el lote 1 tal cual el fixture y `cantidadLotes` forzado.
pub async fn mock_erp(cantidad_lotes: u32) -> MockServer {
    mock_erp_with(cantidad_lotes, |_| {}).await
}

/// Igual que `mock_erp` pero permite mutar el lote antes de montarlo.
/// Monta también `GET /api/productos/{id}` y `POST /api/productos/codigosBarras`
/// a partir de los productos del lote (404 si no están).
pub async fn mock_erp_with(cantidad_lotes: u32, f: impl FnOnce(&mut LoteResponse)) -> MockServer {
    let mut lote = lote1();
    lote.cantidad_lotes = cantidad_lotes;
    f(&mut lote);
    let server = MockServer::start().await;
    mount_lote(&server, &lote).await;
    mount_lookups(&server, &lote).await;
    server
}

pub async fn mount_lote(server: &MockServer, lote: &LoteResponse) {
    // Lotes 1..=cantidad devuelven el mismo contenido (los tests miden por lote 1).
    for n in 1..=lote.cantidad_lotes.max(1) {
        Mock::given(method("GET"))
            .and(path(format!("/api/productos/lote/{n}")))
            .respond_with(ResponseTemplate::new(200).set_body_json(lote))
            .mount(server)
            .await;
    }
    Mock::given(method("GET"))
        .and(path_regex(r"^/api/productos/lote/\d+$"))
        .respond_with(ResponseTemplate::new(400).set_body_json(serde_json::json!({
            "Message": "El numeroLote=N no genera un conjunto de productos"
        })))
        .mount(server)
        .await;
}

pub async fn mount_lookups(server: &MockServer, lote: &LoteResponse) {
    for p in &lote.productos {
        Mock::given(method("GET"))
            .and(path(format!("/api/productos/{}", p.id_producto)))
            .respond_with(ResponseTemplate::new(200).set_body_json(p))
            .mount(server)
            .await;
    }
    Mock::given(method("GET"))
        .and(path_regex(r"^/api/productos/\d+$"))
        .respond_with(ResponseTemplate::new(404))
        .mount(server)
        .await;

    let productos = lote.productos.clone();
    Mock::given(method("POST"))
        .and(path("/api/productos/codigosBarras"))
        .respond_with(move |req: &wiremock::Request| {
            let asked: Vec<String> = serde_json::from_slice(&req.body).unwrap_or_default();
            let found: Vec<_> = productos
                .iter()
                .filter(|p| p.codigo_barras.iter().any(|cb| asked.contains(cb)))
                .cloned()
                .collect();
            if found.is_empty() {
                ResponseTemplate::new(404)
            } else {
                ResponseTemplate::new(200).set_body_json(found)
            }
        })
        .mount(server)
        .await;
}
