//! Adapter para ObServer Gestión (`ServiciosGestion.exe`, Web API self-hosted en `:60064`).

use super::model::{LoteResponse, ProductoDTO};
use super::{ErpAdapter, ErpError};
use async_trait::async_trait;
use reqwest::{header, Client, Response, StatusCode};
use std::time::Duration;
use tracing::{debug, warn};

pub struct ObserverAdapter {
    client: Client,
    base_url: String,
}

impl ObserverAdapter {
    pub fn new(base_url: &str, timeout: Duration) -> ObserverAdapter {
        let mut headers = header::HeaderMap::new();
        headers.insert(header::ACCEPT, header::HeaderValue::from_static("application/json"));
        let client = Client::builder()
            .default_headers(headers)
            .timeout(timeout)
            .connect_timeout(Duration::from_secs(5))
            .no_proxy()
            .build()
            .expect("reqwest client");
        ObserverAdapter {
            client,
            base_url: base_url.trim_end_matches('/').to_string(),
        }
    }

    fn url(&self, path: &str) -> String {
        format!("{}{}", self.base_url, path)
    }

    /// Mapea errores de transporte y el 401 de `API_Productos`. Deja pasar el
    /// resto de los status para que cada endpoint decida (400 fin de lotes, 404).
    async fn send(&self, req: reqwest::RequestBuilder) -> Result<Response, ErpError> {
        let resp = req.send().await.map_err(|e| {
            if e.is_connect() || e.is_timeout() || e.is_request() {
                ErpError::Unreachable(e.to_string())
            } else {
                ErpError::Http(0, e.to_string())
            }
        })?;
        if resp.status() == StatusCode::UNAUTHORIZED {
            return Err(ErpError::NotAuthorized);
        }
        Ok(resp)
    }

    async fn fetch_lote(&self, n: u32) -> Result<Option<LoteResponse>, ErpError> {
        let resp = self
            .send(self.client.get(self.url(&format!("/api/productos/lote/{n}"))))
            .await?;
        match resp.status() {
            StatusCode::BAD_REQUEST => {
                let body = resp.text().await.unwrap_or_default();
                warn!(lote = n, body = %truncate(&body), "lote fuera de rango, fin del catálogo");
                Ok(None)
            }
            s if s.is_success() => resp
                .json::<LoteResponse>()
                .await
                .map(Some)
                .map_err(|e| ErpError::Decode(format!("lote {n}: {e}"))),
            s => Err(ErpError::Http(s.as_u16(), resp.text().await.unwrap_or_default())),
        }
    }
}

fn truncate(s: &str) -> String {
    s.chars().take(200).collect()
}

#[async_trait]
impl ErpAdapter for ObserverAdapter {
    /// Itera `1..=cantidadLotes`. El total lo fija el lote 1 de este ciclo;
    /// un 400 antes de llegar corta el barrido.
    async fn fetch_all(&self) -> Result<Vec<ProductoDTO>, ErpError> {
        let Some(first) = self.fetch_lote(1).await? else {
            return Ok(Vec::new());
        };
        let total = first.cantidad_lotes.max(1);
        debug!(total_lotes = total, productos = first.productos.len(), "lote 1");
        let mut out = first.productos;
        for n in 2..=total {
            match self.fetch_lote(n).await? {
                Some(lote) => {
                    debug!(lote = n, productos = lote.productos.len(), "lote leído");
                    out.extend(lote.productos);
                }
                None => break,
            }
        }
        Ok(out)
    }

    async fn lookup_by_barcodes(&self, barcodes: &[String]) -> Result<Vec<ProductoDTO>, ErpError> {
        if barcodes.is_empty() {
            return Ok(Vec::new());
        }
        let resp = self
            .send(
                self.client
                    .post(self.url("/api/productos/codigosBarras"))
                    .json(&barcodes),
            )
            .await?;
        match resp.status() {
            StatusCode::NOT_FOUND => Ok(Vec::new()),
            s if s.is_success() => resp
                .json::<Vec<ProductoDTO>>()
                .await
                .map_err(|e| ErpError::Decode(format!("codigosBarras: {e}"))),
            s => Err(ErpError::Http(s.as_u16(), resp.text().await.unwrap_or_default())),
        }
    }

    async fn lookup_by_id(&self, id: i64) -> Result<Option<ProductoDTO>, ErpError> {
        let resp = self
            .send(self.client.get(self.url(&format!("/api/productos/{id}"))))
            .await?;
        match resp.status() {
            StatusCode::NOT_FOUND => Ok(None),
            s if s.is_success() => resp
                .json::<ProductoDTO>()
                .await
                .map(Some)
                .map_err(|e| ErpError::Decode(format!("producto {id}: {e}"))),
            s => Err(ErpError::Http(s.as_u16(), resp.text().await.unwrap_or_default())),
        }
    }
}
