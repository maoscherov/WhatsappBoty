//! Adapter para ObServer Gestión (`ServiciosGestion.exe`, Web API self-hosted en `:60064`).

use super::model::{LoteResponse, ProductoDTO};
use super::{ErpAdapter, ErpError, FetchResult, Probe};
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
            s if s.is_success() => {
                let body = resp.text().await.map_err(|e| ErpError::Decode(format!("lote {n}: {e}")))?;
                decode_json::<LoteResponse>(&body, &format!("lote {n}")).map(Some)
            }
            s => Err(ErpError::Http(s.as_u16(), resp.text().await.unwrap_or_default())),
        }
    }
}

fn truncate(s: &str) -> String {
    s.chars().take(200).collect()
}

/// Parsea el cuerpo y, si falla, arma un error con línea/columna, el
/// fragmento alrededor del problema y guarda la respuesta cruda en un archivo
/// para poder diagnosticar el formato real del ERP.
pub fn decode_json<T: serde::de::DeserializeOwned>(body: &str, what: &str) -> Result<T, ErpError> {
    match serde_json::from_str::<T>(body) {
        Ok(v) => Ok(v),
        Err(e) => {
            let snippet = snippet_at(body, e.line(), e.column());
            let dump = dump_body(body, what);
            Err(ErpError::Decode(format!(
                "{what}: {e}. Fragmento: «{snippet}».{}",
                dump.map(|p| format!(" Respuesta guardada en {}", p.display())).unwrap_or_default()
            )))
        }
    }
}

fn snippet_at(body: &str, line: usize, column: usize) -> String {
    let mut offset = 0;
    for (i, l) in body.split('\n').enumerate() {
        if i + 1 == line {
            offset += column.saturating_sub(1).min(l.len());
            break;
        }
        offset += l.len() + 1;
    }
    let start = body[..offset.min(body.len())]
        .char_indices()
        .rev()
        .nth(80)
        .map(|(i, _)| i)
        .unwrap_or(0);
    let end = body[offset.min(body.len())..]
        .char_indices()
        .nth(80)
        .map(|(i, _)| offset + i)
        .unwrap_or(body.len());
    body[start..end].replace(['\n', '\r'], " ")
}

fn dump_body(body: &str, what: &str) -> Option<std::path::PathBuf> {
    let name: String = what.chars().map(|c| if c.is_alphanumeric() { c } else { '_' }).collect();
    let path = std::env::temp_dir().join(format!("remedia-agent-erp-{name}.json"));
    std::fs::write(&path, body).ok().map(|_| path)
}

#[async_trait]
impl ErpAdapter for ObserverAdapter {
    /// Itera `1..=cantidadLotes`. El total lo fija el lote 1 de este ciclo;
    /// un 400 antes de llegar corta el barrido.
    async fn fetch_all(&self) -> Result<FetchResult, ErpError> {
        let Some(first) = self.fetch_lote(1).await? else {
            return Ok(FetchResult::default());
        };
        let total = first.cantidad_lotes.max(1);
        debug!(total_lotes = total, productos = first.productos.len(), "lote 1");
        let mut out = first.productos;
        let mut lotes = 1;
        for n in 2..=total {
            match self.fetch_lote(n).await? {
                Some(lote) => {
                    debug!(lote = n, productos = lote.productos.len(), "lote leído");
                    out.extend(lote.productos);
                    lotes += 1;
                }
                None => break,
            }
        }
        Ok(FetchResult { productos: out, lotes })
    }

    async fn probe(&self) -> Result<Probe, ErpError> {
        match self.fetch_lote(1).await? {
            Some(l) => Ok(Probe { productos: l.productos.len(), cantidad_lotes: l.cantidad_lotes }),
            None => Ok(Probe::default()),
        }
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
            s if s.is_success() => {
                let body = resp.text().await.map_err(|e| ErpError::Decode(format!("codigosBarras: {e}")))?;
                decode_json::<Vec<ProductoDTO>>(&body, "codigosBarras")
            }
            s => Err(ErpError::Http(s.as_u16(), resp.text().await.unwrap_or_default())),
        }
    }

    async fn lookup_by_id(&self, id: i64) -> Result<Option<ProductoDTO>, ErpError> {
        let resp = self
            .send(self.client.get(self.url(&format!("/api/productos/{id}"))))
            .await?;
        match resp.status() {
            StatusCode::NOT_FOUND => Ok(None),
            s if s.is_success() => {
                let body = resp.text().await.map_err(|e| ErpError::Decode(format!("producto {id}: {e}")))?;
                decode_json::<ProductoDTO>(&body, &format!("producto {id}")).map(Some)
            }
            s => Err(ErpError::Http(s.as_u16(), resp.text().await.unwrap_or_default())),
        }
    }
}
