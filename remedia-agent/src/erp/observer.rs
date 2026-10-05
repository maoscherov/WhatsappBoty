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

/// Tope de ids a recorrer por hueco y de 404 seguidos para cortar cuando el
/// lote fallido fue el último (no hay lote siguiente que marque dónde termina).
pub const RECUPERO_MAX_HUECO: i64 = 20_000;
pub const RECUPERO_COLA: i64 = 5_000;
pub const RECUPERO_404_SEGUIDOS: u32 = 300;

fn rango_ids(p: &[ProductoDTO]) -> Option<(i64, i64)> {
    let min = p.iter().map(|x| x.id_producto).min()?;
    let max = p.iter().map(|x| x.id_producto).max()?;
    Some((min, max))
}

impl ObserverAdapter {
    /// Lee de a uno los ids `lo..=hi` (`hi = None`: no hay lote siguiente, se
    /// recorren hasta 5.000 y se corta tras 300 404 seguidos). Agrega lo
    /// encontrado a `out` y devuelve `(recuperados, ids_rotos)`. Los productos
    /// vienen de la consulta individual, o sea con precio y stock reales.
    async fn recuperar_hueco(
        &self,
        lo: i64,
        hi: Option<i64>,
        lotes: &[u32],
        out: &mut Vec<ProductoDTO>,
    ) -> Result<(usize, Vec<i64>), ErpError> {
        let (hi, es_cola) = match hi {
            Some(h) => (h, false),
            None => (lo + RECUPERO_COLA, true),
        };
        let mut hi = hi;
        if hi - lo + 1 > RECUPERO_MAX_HUECO {
            warn!(?lotes, lo, hi, "hueco demasiado grande, se recorren solo {RECUPERO_MAX_HUECO} ids");
            hi = lo + RECUPERO_MAX_HUECO - 1;
        }
        let mut recuperados = 0usize;
        let mut rotos = Vec::new();
        let mut seguidos_404 = 0u32;
        let mut id = lo;
        while id <= hi {
            match self.lookup_by_id(id).await {
                Ok(Some(p)) => {
                    out.push(p);
                    recuperados += 1;
                    seguidos_404 = 0;
                }
                Ok(None) => {
                    seguidos_404 += 1;
                    if es_cola && seguidos_404 >= RECUPERO_404_SEGUIDOS {
                        break;
                    }
                }
                Err(e @ (ErpError::Unreachable(_) | ErpError::NotAuthorized)) => return Err(e),
                Err(_) => {
                    rotos.push(id);
                    seguidos_404 = 0;
                }
            }
            id += 1;
        }
        warn!(?lotes, rango = %format!("{lo}..={hi}"), recuperados, rotos = ?rotos, "lote recuperado por id");
        Ok((recuperados, rotos))
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
    ///
    /// Un lote que da 500 o viene ilegible NO aborta el ciclo (0.3.5, incidente
    /// 28/9: 4 productos rotos hacían fallar el lote 11 entero y nada se
    /// actualizaba por horas): se anota y, al final, sus productos se recuperan
    /// de a uno por id dentro del hueco que dejan los lotes vecinos (los lotes
    /// van ordenados por idProducto). Los ids que también fallan solos quedan en
    /// `ids_rotos`. Sin lote 1 no se conoce el total, ahí sí se propaga el error.
    async fn fetch_all(&self) -> Result<FetchResult, ErpError> {
        let Some(first) = self.fetch_lote(1).await? else {
            return Ok(FetchResult::default());
        };
        let total = first.cantidad_lotes.max(1);
        debug!(total_lotes = total, productos = first.productos.len(), "lote 1");
        // (n, rango de idProducto) de cada lote leído bien; None si vino vacío.
        let mut leidos: Vec<(u32, Option<(i64, i64)>)> = vec![(1, rango_ids(&first.productos))];
        let mut out = first.productos;
        let mut lotes = 1;
        let mut lotes_fallidos: Vec<u32> = Vec::new();
        for n in 2..=total {
            match self.fetch_lote(n).await {
                Ok(Some(lote)) => {
                    debug!(lote = n, productos = lote.productos.len(), "lote leído");
                    leidos.push((n, rango_ids(&lote.productos)));
                    out.extend(lote.productos);
                    lotes += 1;
                }
                Ok(None) => break,
                Err(e @ (ErpError::Unreachable(_) | ErpError::NotAuthorized)) => return Err(e),
                Err(e @ (ErpError::Decode(_) | ErpError::Http(500..=599, _) | ErpError::Http(0, _))) => {
                    warn!(lote = n, error = %e, "el ERP no pudo servir el lote, se recupera producto por producto");
                    lotes_fallidos.push(n);
                }
                Err(e) => return Err(e),
            }
        }

        let mut ids_rotos: Vec<i64> = Vec::new();
        if !lotes_fallidos.is_empty() {
            // Lotes fallidos contiguos comparten el mismo hueco: se recorre una vez.
            let mut huecos: Vec<((i64, Option<i64>), Vec<u32>)> = Vec::new();
            for &n in &lotes_fallidos {
                let lo = leidos.iter().rev()
                    .filter(|(m, _)| *m < n)
                    .find_map(|(_, r)| r.map(|(_, max)| max + 1))
                    .unwrap_or(1);
                let hi = leidos.iter()
                    .filter(|(m, _)| *m > n)
                    .find_map(|(_, r)| r.map(|(min, _)| min - 1));
                match huecos.iter_mut().find(|(k, _)| *k == (lo, hi)) {
                    Some((_, ns)) => ns.push(n),
                    None => huecos.push(((lo, hi), vec![n])),
                }
            }
            let mut recuperados_total = 0usize;
            for ((lo, hi), ns) in huecos {
                let (recuperados, rotos) = self.recuperar_hueco(lo, hi, &ns, &mut out).await?;
                recuperados_total += recuperados;
                ids_rotos.extend(rotos);
            }
            warn!(lotes = ?lotes_fallidos, recuperados = recuperados_total, rotos = ids_rotos.len(),
                  "lotes con error recuperados por id");
        }
        Ok(FetchResult { productos: out, lotes, lotes_fallidos, ids_rotos })
    }

    /// "Probar conexión": lee el lote 1 y, si falla con error HTTP, prueba la
    /// consulta individual para distinguir "ERP caído" de "API de lotes rota"
    /// (incidente 15/9: el lote daba 500 y las consultas por id andaban — el
    /// remedio es reiniciar ServiciosGestion, no el agente).
    async fn probe(&self) -> Result<Probe, ErpError> {
        match self.fetch_lote(1).await {
            Ok(Some(l)) => Ok(Probe { productos: l.productos.len(), cantidad_lotes: l.cantidad_lotes }),
            Ok(None) => Ok(Probe::default()),
            Err(e @ (ErpError::Unreachable(_) | ErpError::NotAuthorized)) => Err(e),
            Err(e) => {
                // ¿Responde la consulta individual? 200 o 404 = el endpoint vive.
                let mut individual_ok = false;
                for id in [1i64, 2, 3] {
                    match self.lookup_by_id(id).await {
                        Ok(_) => {
                            individual_ok = true;
                            break;
                        }
                        Err(ErpError::Unreachable(_)) | Err(ErpError::NotAuthorized) => break,
                        Err(_) => continue,
                    }
                }
                let detalle = if individual_ok {
                    "La API de LOTES de Observer falla pero las consultas individuales responden: \
                     hay que reiniciar el servicio ServiciosGestion en el servidor de Observer. \
                     El agente no puede sincronizar hasta entonces."
                } else {
                    "Observer no responde ni por lotes ni por consulta individual: el servicio \
                     ServiciosGestion está caído o a medio arrancar."
                };
                Err(match e {
                    ErpError::Http(s, body) => ErpError::Http(s, format!("{detalle} ({body})")),
                    ErpError::Decode(m) => ErpError::Decode(format!("{detalle} ({m})")),
                    otro => otro,
                })
            }
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
