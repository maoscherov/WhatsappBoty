//! Acceso al ERP. Un segundo ERP = otro archivo en este módulo que implemente `ErpAdapter`.

pub mod model;
pub mod observer;

use crate::config::ErpConfig;
use async_trait::async_trait;
use model::ProductoDTO;
use std::sync::Arc;

#[derive(Debug, thiserror::Error)]
pub enum ErpError {
    #[error("ERP inalcanzable: {0}")]
    Unreachable(String),
    #[error("ERP no autorizado (flag API_Productos deshabilitado)")]
    NotAuthorized,
    #[error("ERP HTTP {0}: {1}")]
    Http(u16, String),
    #[error("ERP respuesta inválida: {0}")]
    Decode(String),
}

impl ErpError {
    /// Valor de `erp_status` para el heartbeat.
    pub fn status_label(&self) -> &'static str {
        match self {
            ErpError::Unreachable(_) => "inalcanzable",
            ErpError::NotAuthorized => "no_autorizado",
            ErpError::Http(..) | ErpError::Decode(_) => "error",
        }
    }
}

/// Resultado de `fetch_all`: productos y cuántos lotes se leyeron (para medir
/// el tiempo por lote).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct FetchResult {
    pub productos: Vec<ProductoDTO>,
    pub lotes: u32,
}

#[async_trait]
pub trait ErpAdapter: Send + Sync {
    /// Catálogo completo (sync por lotes).
    async fn fetch_all(&self) -> Result<FetchResult, ErpError>;
    /// Lookup en vivo por lista de códigos de barras (hasta ~20 por llamada).
    async fn lookup_by_barcodes(&self, barcodes: &[String]) -> Result<Vec<ProductoDTO>, ErpError>;
    /// Lookup en vivo por `idProducto`. `None` si no existe.
    async fn lookup_by_id(&self, id: i64) -> Result<Option<ProductoDTO>, ErpError>;
    /// Versión del ERP si el adapter la conoce (va al heartbeat).
    async fn version(&self) -> Option<String> {
        None
    }
}

pub fn build_adapter(cfg: &ErpConfig, timeout: std::time::Duration) -> anyhow::Result<Arc<dyn ErpAdapter>> {
    match cfg.kind.as_str() {
        "observer" => Ok(Arc::new(observer::ObserverAdapter::new(&cfg.base_url, timeout))),
        other => anyhow::bail!("erp.kind desconocido: {other} (soportados: observer)"),
    }
}
