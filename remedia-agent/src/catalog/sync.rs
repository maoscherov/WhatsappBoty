//! Motor de sync: fetch del ERP → normalizar → diff contra `state.sqlite` →
//! push a Remedia (o cola local con backoff) → full-manifest diario → heartbeat.

use crate::catalog::state::{
    State, KIND_CATALOG, META_CATALOG_COUNT, META_ERP_STATUS, META_ERP_VERSION,
    META_LAST_FULL_MANIFEST, META_LAST_HEARTBEAT, META_LAST_HEARTBEAT_ERROR, META_LAST_SYNC_AT,
    META_LAST_SYNC_CHANGED, META_LAST_SYNC_ERROR, META_LAST_SYNC_FETCHED, META_LAST_SYNC_OK,
    META_LAST_FULL_LIVE, META_METRICS_JSON,
};
use crate::catalog::CatalogItem;
use crate::config::Config;
use crate::erp::model::ProductoDTO;
use crate::erp::{ErpAdapter, ErpError};
use crate::metrics::{elapsed_ms, Metrics};
use crate::remedia::client::{
    now_rfc3339, CatalogBatch, FullManifest, Heartbeat, ManifestEntry, SCHEMA_VERSION,
    SOURCE_OBSERVER,
};
use crate::remedia::{RemediaClient, RemediaError};
use std::collections::{HashMap, HashSet};
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant};
use tracing::{error, info, warn};

pub const BATCH_SIZE: usize = 500;
pub const BACKOFF_MIN_SECS: u64 = 30;
pub const BACKOFF_MAX_SECS: u64 = 600;
pub const ID_SCAN_RETRIES: u32 = 3;
/// Códigos de barras por llamada a `POST /api/productos/codigosBarras`.
pub const LIVE_CB_CHUNK: usize = 20;

/// `min(30 · 2^attempts, 600)` segundos.
pub fn backoff_secs(attempts: u32) -> u64 {
    BACKOFF_MIN_SECS
        .saturating_mul(1u64 << attempts.min(10))
        .min(BACKOFF_MAX_SECS)
}

/// Nuevos + hash distinto. `known` es `external_id → hash` del estado local.
pub fn compute_delta<'a>(
    items: &'a [CatalogItem],
    known: &HashMap<String, String>,
) -> Vec<&'a CatalogItem> {
    items
        .iter()
        .filter(|it| known.get(&it.external_id) != Some(&it.hash))
        .collect()
}

#[derive(Debug, Default, Clone, PartialEq)]
pub struct SyncReport {
    pub erp_status: String,
    pub fetched: usize,
    pub changed: usize,
    pub sent_batches: usize,
    pub queued_batches: usize,
    pub flushed_batches: usize,
    /// Productos cuyo dato en vivo no se pudo leer: NO se envían (el servidor
    /// conserva el último valor bueno). Incidente 15/9: se mandaban los ceros
    /// del lote y pisaban precio/stock reales.
    pub sin_verificar: usize,
    /// Omitidos por el pase selectivo (no se releyeron a propósito).
    pub omitidos: usize,
}

/// Resultado del pase de verdad: lo verificado (o sin nada que verificar) y
/// lo que quedó sin poder leerse en vivo — o se omitió a propósito (pase
/// selectivo). Ninguno de los `sin_verificar` se envía.
#[derive(Debug, Default)]
pub struct EnrichOutcome {
    pub productos: Vec<ProductoDTO>,
    pub sin_verificar: Vec<ProductoDTO>,
    /// Cuántos de `sin_verificar` se omitieron por selección (no por falla).
    pub omitidos: usize,
    /// Fue un barrido completo (todos los productos consultados en vivo).
    pub completo: bool,
}

/// Qué productos relee el pase de verdad en este ciclo.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum LiveModo {
    /// Todos (primera vez, `live_selective = false`, o la corrida diaria).
    Completo,
    /// Solo los activos (stock o precio > 0 en la última lectura) y los nunca
    /// vistos. El resto queda como estaba en el servidor.
    Selectivo,
}

/// Parte los productos del lote en (a verificar, omitidos) según el modo.
pub fn seleccionar(
    productos: Vec<ProductoDTO>,
    modo: LiveModo,
    activos: &HashSet<i64>,
    vistos: &HashSet<i64>,
) -> (Vec<ProductoDTO>, Vec<ProductoDTO>) {
    if modo == LiveModo::Completo {
        return (productos, Vec::new());
    }
    let mut a_verificar = Vec::new();
    let mut omitidos = Vec::new();
    for p in productos {
        if activos.contains(&p.id_producto) || !vistos.contains(&p.id_producto) {
            a_verificar.push(p);
        } else {
            omitidos.push(p);
        }
    }
    (a_verificar, omitidos)
}

/// ¿Toca el barrido completo? Sin estado previo, con la selección apagada, o
/// a la hora configurada si hoy todavía no se hizo.
pub fn modo_para(cfg: &Config, ahora: chrono::DateTime<chrono::Local>, last_full: Option<&str>,
                 vistos: usize) -> LiveModo {
    if !cfg.erp.live_selective || vistos == 0 {
        return LiveModo::Completo;
    }
    use chrono::{Datelike, Timelike};
    let hoy_hecho = last_full
        .and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok())
        .map(|d| d.with_timezone(&chrono::Local).ordinal() == ahora.ordinal()
                 && d.with_timezone(&chrono::Local).year() == ahora.year())
        .unwrap_or(false);
    if ahora.hour() as u8 == cfg.erp.live_full_hour && !hoy_hecho {
        LiveModo::Completo
    } else {
        LiveModo::Selectivo
    }
}

/// El adapter del ERP y el cliente de Remedia son intercambiables en caliente
/// (el tray puede cambiar URLs y token sin reiniciar el servicio).
pub struct SyncEngine {
    erp: RwLock<Arc<dyn ErpAdapter>>,
    remedia: RwLock<Arc<RemediaClient>>,
    pub state: Arc<State>,
    pub cfg: Arc<Config>,
    pub metrics: Arc<Metrics>,
}

impl SyncEngine {
    pub fn new(
        erp: Arc<dyn ErpAdapter>,
        remedia: Arc<RemediaClient>,
        state: Arc<State>,
        cfg: Arc<Config>,
        metrics: Arc<Metrics>,
    ) -> SyncEngine {
        SyncEngine {
            erp: RwLock::new(erp),
            remedia: RwLock::new(remedia),
            state,
            cfg,
            metrics,
        }
    }

    pub fn erp(&self) -> Arc<dyn ErpAdapter> {
        self.erp.read().unwrap_or_else(|e| e.into_inner()).clone()
    }

    pub fn remedia(&self) -> Arc<RemediaClient> {
        self.remedia.read().unwrap_or_else(|e| e.into_inner()).clone()
    }

    pub fn set_erp(&self, erp: Arc<dyn ErpAdapter>) {
        *self.erp.write().unwrap_or_else(|e| e.into_inner()) = erp;
    }

    pub fn set_remedia(&self, remedia: Arc<RemediaClient>) {
        *self.remedia.write().unwrap_or_else(|e| e.into_inner()) = remedia;
    }

    /// Un ciclo completo. Nunca falla por el ERP o Remedia caídos: eso queda en
    /// el reporte y en `meta`. Solo devuelve `Err` ante fallas del estado local.
    pub async fn run_once(&self) -> anyhow::Result<SyncReport> {
        self.run_once_with(Vec::new()).await
    }

    /// Igual que `run_once`, uniendo `extra` (p. ej. el barrido diario por ID)
    /// a los productos del lote. El lote tiene prioridad ante el mismo `idProducto`.
    pub async fn run_once_with(&self, extra: Vec<ProductoDTO>) -> anyhow::Result<SyncReport> {
        let t_total = Instant::now();
        let result = self.run_cycle(extra).await;
        self.metrics.record_sync_total(elapsed_ms(t_total));
        self.persist_metrics();
        self.state.set_meta(META_LAST_SYNC_AT, &now_rfc3339())?;
        result
    }

    async fn run_cycle(&self, extra: Vec<ProductoDTO>) -> anyhow::Result<SyncReport> {
        // Primero lo que quedó pendiente: mantiene el orden de los envíos.
        let flushed_batches = self.flush_pending().await?;
        let mut report = SyncReport { flushed_batches, ..Default::default() };

        let erp = self.erp();
        let t_fetch = Instant::now();
        let fetched = match erp.fetch_all().await {
            Ok(v) => {
                self.metrics.record_fetch(elapsed_ms(t_fetch), v.lotes);
                self.state.set_meta(META_ERP_STATUS, "ok")?;
                self.state.set_meta(META_LAST_SYNC_ERROR, "")?;
                v
            }
            Err(e) => {
                let label = e.status_label();
                warn!(error = %e, "no se pudo leer el catálogo del ERP");
                self.state.set_meta(META_ERP_STATUS, label)?;
                self.state.set_meta(META_LAST_SYNC_ERROR, &e.to_string())?;
                report.erp_status = label.to_string();
                return Ok(report);
            }
        };
        if let Some(v) = erp.version().await {
            self.state.set_meta(META_ERP_VERSION, &v)?;
        }
        report.erp_status = "ok".into();

        let outcome = self.enrich_live(fetched.productos).await;
        report.sin_verificar = outcome.sin_verificar.len() - outcome.omitidos;
        report.omitidos = outcome.omitidos;
        let items = merge_items(&outcome.productos, &extra);
        report.fetched = items.len() + outcome.sin_verificar.len();

        let known = self.state.known_hashes()?;
        let delta: Vec<CatalogItem> = compute_delta(&items, &known).into_iter().cloned().collect();
        report.changed = delta.len();
        info!(fetched = report.fetched, changed = delta.len(), sin_verificar = report.sin_verificar,
              omitidos = report.omitidos, lotes = fetched.lotes, "catálogo leído");

        let (sent, queued) = self.push_items(delta, "delta").await?;
        report.sent_batches = sent;
        report.queued_batches = queued;

        self.state.set_meta(META_CATALOG_COUNT, &items.len().to_string())?;
        self.state.set_meta(META_LAST_SYNC_FETCHED, &report.fetched.to_string())?;
        self.state.set_meta(META_LAST_SYNC_CHANGED, &report.changed.to_string())?;
        if queued == 0 {
            self.state.set_meta(META_LAST_SYNC_OK, &now_rfc3339())?;
        }
        Ok(report)
    }

    fn persist_metrics(&self) {
        if let Ok(json) = serde_json::to_string(&self.metrics.snapshot()) {
            let _ = self.state.set_meta(META_METRICS_JSON, &json);
        }
    }

    /// Envía `items` en lotes de `BATCH_SIZE`. Los que Remedia acepta actualizan
    /// el estado; los que fallan van a la cola. Devuelve `(enviados, encolados)`.
    async fn push_items(&self, items: Vec<CatalogItem>, mode: &str) -> anyhow::Result<(usize, usize)> {
        if items.is_empty() {
            return Ok((0, 0));
        }
        let remedia = self.remedia();
        let total = items.len().div_ceil(BATCH_SIZE) as u32;
        let generated_at = now_rfc3339();
        let mut sent = 0;
        let mut queued = 0;
        let mut push_ms_sum = 0u64;
        for (i, chunk) in items.chunks(BATCH_SIZE).enumerate() {
            let batch = CatalogBatch {
                schema_version: SCHEMA_VERSION,
                branch_id: self.cfg.branch_id.clone(),
                source: SOURCE_OBSERVER.into(),
                mode: mode.into(),
                batch: i as u32 + 1,
                total_batches: total,
                generated_at: generated_at.clone(),
                items: chunk.to_vec(),
            };
            let t = Instant::now();
            match remedia.push_catalog(&batch).await {
                Ok(resp) => {
                    push_ms_sum += elapsed_ms(t);
                    info!(batch = batch.batch, total, received = resp.received, upserted = resp.upserted, "lote enviado");
                    self.state.upsert_hashes(&hashes_of(&batch.items))?;
                    sent += 1;
                }
                Err(e) => {
                    warn!(batch = batch.batch, total, error = %e, "Remedia no aceptó el lote, se encola");
                    let id = self.state.enqueue(KIND_CATALOG, &serde_json::to_string(&batch)?)?;
                    self.state.mark_failed(id, crate::catalog::state::now_unix() + backoff_secs(0) as i64)?;
                    queued += 1;
                }
            }
        }
        if sent > 0 {
            self.metrics.record_push_avg(push_ms_sum / sent as u64);
        }
        Ok((sent, queued))
    }

    /// Reintenta los lotes vencidos de la cola. Devuelve cuántos se enviaron.
    pub async fn flush_pending(&self) -> anyhow::Result<usize> {
        let now = crate::catalog::state::now_unix();
        let due = self.state.due_pending(now)?;
        let remedia = self.remedia();
        let mut flushed = 0;
        for p in due {
            let batch: CatalogBatch = match serde_json::from_str(&p.payload) {
                Ok(b) => b,
                Err(e) => {
                    error!(id = p.id, error = %e, "lote pendiente corrupto, se descarta");
                    self.state.remove_pending(p.id)?;
                    continue;
                }
            };
            match remedia.push_catalog(&batch).await {
                Ok(_) => {
                    self.state.upsert_hashes(&hashes_of(&batch.items))?;
                    self.state.remove_pending(p.id)?;
                    flushed += 1;
                    info!(id = p.id, attempts = p.attempts, "lote pendiente enviado");
                }
                Err(e) => {
                    let next = now + backoff_secs(p.attempts) as i64;
                    self.state.mark_failed(p.id, next)?;
                    warn!(id = p.id, attempts = p.attempts + 1, retry_in = next - now, error = %e, "lote pendiente sigue fallando");
                    if matches!(e, RemediaError::Unreachable(_)) {
                        break;
                    }
                }
            }
        }
        Ok(flushed)
    }

    /// Manifiesto completo (`external_id` + hash de lo que el ERP tiene hoy).
    /// Remedia desactiva lo ausente y pide reenviar los hashes que no coinciden.
    /// También poda del estado local lo que ya no está en el ERP.
    /// Devuelve la cantidad de productos reenviados.
    pub async fn full_manifest(&self) -> anyhow::Result<usize> {
        let fetched = self.erp().fetch_all().await.map_err(|e| {
            let _ = self.state.set_meta(META_ERP_STATUS, e.status_label());
            anyhow::anyhow!("full-manifest: {e}")
        })?;
        // Mismos datos (y hashes) que el delta: si no, el manifiesto pediría
        // reenviar todo lo corregido en cada corrida.
        let outcome = self.enrich_live(fetched.productos).await;
        let items = merge_items(&outcome.productos, &[]);
        let known = self.state.known_hashes()?;
        // Lo que no se pudo verificar en vivo va al manifiesto con su hash
        // ANTERIOR (así el servidor no lo desactiva ni pide reenviarlo); si
        // nunca se envió, se omite y entrará en un ciclo sano.
        let mut entries: Vec<ManifestEntry> = items
            .iter()
            .map(|i| ManifestEntry { external_id: i.external_id.clone(), hash: i.hash.clone() })
            .collect();
        for p in &outcome.sin_verificar {
            let id = p.id_producto.to_string();
            if let Some(h) = known.get(&id) {
                entries.push(ManifestEntry { external_id: id, hash: h.clone() });
            }
        }
        let current: HashSet<String> = entries.iter().map(|e| e.external_id.clone()).collect();

        let manifest = FullManifest {
            branch_id: self.cfg.branch_id.clone(),
            generated_at: now_rfc3339(),
            items: entries,
        };
        let resp = self
            .remedia()
            .full_manifest(&manifest)
            .await
            .map_err(|e| anyhow::anyhow!("full-manifest: {e}"))?;

        let stale: Vec<String> = known
            .into_keys()
            .filter(|id| !current.contains(id))
            .collect();
        if !stale.is_empty() {
            info!(count = stale.len(), "productos que ya no están en el ERP, se podan del estado local");
            self.state.remove_items(&stale)?;
        }

        let resend: HashSet<&str> = resp.resend.iter().map(String::as_str).collect();
        let to_send: Vec<CatalogItem> = items
            .into_iter()
            .filter(|i| resend.contains(i.external_id.as_str()))
            .collect();
        let resent = to_send.len();
        info!(resend = resent, deactivated = resp.deactivated, "full-manifest aceptado");
        let (_, queued) = self.push_items(to_send, "full").await?;
        if queued == 0 {
            self.state.set_meta(META_LAST_FULL_MANIFEST, &now_rfc3339())?;
        }
        Ok(resent)
    }

    pub fn build_heartbeat(&self) -> anyhow::Result<Heartbeat> {
        let get = |k: &str| self.state.get_meta(k);
        Ok(Heartbeat {
            branch_id: self.cfg.branch_id.clone(),
            agent_version: crate::AGENT_VERSION.to_string(),
            erp_version: get(META_ERP_VERSION)?,
            erp_status: get(META_ERP_STATUS)?.unwrap_or_else(|| "inalcanzable".into()),
            last_sync_ok_at: get(META_LAST_SYNC_OK)?,
            catalog_count: get(META_CATALOG_COUNT)?.and_then(|v| v.parse().ok()).unwrap_or(0),
            pending_batches: self.state.pending_count()?,
            metrics: Some(self.metrics.snapshot()),
        })
    }

    pub async fn send_heartbeat(&self) -> anyhow::Result<()> {
        let hb = self.build_heartbeat()?;
        let t = Instant::now();
        match self.remedia().heartbeat(&hb).await {
            Ok(()) => {
                self.metrics.record_heartbeat(elapsed_ms(t));
                self.state.set_meta(META_LAST_HEARTBEAT, &now_rfc3339())?;
                self.state.set_meta(META_LAST_HEARTBEAT_ERROR, "")?;
                Ok(())
            }
            Err(e) => {
                self.state.set_meta(META_LAST_HEARTBEAT_ERROR, &e.to_string())?;
                Err(anyhow::anyhow!("heartbeat: {e}"))
            }
        }
    }

    /// "Pase de verdad" (hallazgo 11/9): `GET /api/productos/lote/{n}` devuelve
    /// `stockSucursal = 0` y `precio = 0` para productos que la consulta
    /// individual trae bien (Aveno solar: lote 0/0, en vivo 2 y $32.409). El lote
    /// sirve para saber QUÉ productos existen; precio y stock se leen de los
    /// endpoints en vivo: `codigosBarras` de a 20 (barato) y `{id}` para los que
    /// no tienen CB o comparten CB con otro (el ERP devuelve uno solo por CB).
    ///
    /// Con concurrencia 4 y ~50 ms por request: ~2.400 + ~6.300 llamadas ≈ 2-4
    /// min por ciclo, dentro del intervalo de 15. Best-effort: lo que falla
    /// conserva el dato del lote; ERP inalcanzable o 401 aborta y devuelve el
    /// lote tal cual. `erp.live_enrich = false` lo apaga.
    pub async fn enrich_live(&self, productos: Vec<ProductoDTO>) -> EnrichOutcome {
        if !self.cfg.erp.live_enrich || productos.is_empty() {
            return EnrichOutcome { productos, ..Default::default() };
        }
        let t = Instant::now();

        // Modo: completo o selectivo (0.3.2), según estado y hora.
        let vistos = self.state.live_vistos().unwrap_or_default();
        let activos = self.state.live_activos().unwrap_or_default();
        let last_full = self.state.get_meta(META_LAST_FULL_LIVE).ok().flatten();
        let modo = modo_para(&self.cfg, chrono::Local::now(), last_full.as_deref(), vistos.len());
        let total = productos.len();
        let (productos, omitidos_v) = seleccionar(productos, modo, &activos, &vistos);
        let omitidos = omitidos_v.len();
        info!(?modo, a_verificar = productos.len(), omitidos, total, "pase de verdad: selección");

        let pausa = Duration::from_millis(self.cfg.erp.live_pause_ms);
        let erp = self.erp();
        let sem = Arc::new(tokio::sync::Semaphore::new(self.cfg.erp.max_concurrency.max(1)));
        let mut live: HashMap<i64, ProductoDTO> = HashMap::with_capacity(productos.len());
        let mut cb_fallidos = 0usize;
        let mut abortado = false;

        // 1) Por código de barras, de a LIVE_CB_CHUNK, con bisección: una tanda
        //    que el ERP rechaza (500) se parte en mitades hasta aislar el CB
        //    que rompe, así casi todo se resuelve por la vía barata.
        let cbs: Vec<String> = productos.iter().flat_map(|p| p.codigo_barras.iter().cloned()).collect();
        let mut tasks = tokio::task::JoinSet::new();
        for chunk in cbs.chunks(LIVE_CB_CHUNK) {
            let erp = Arc::clone(&erp);
            let sem = Arc::clone(&sem);
            let chunk = chunk.to_vec();
            tasks.spawn(async move {
                let _permit = sem.acquire_owned().await.expect("semaphore");
                let r = lookup_cb_bisect(erp.as_ref(), chunk).await;
                tokio::time::sleep(pausa).await;   // no saturar el ERP
                r
            });
        }
        let cb_calls = tasks.len();
        while let Some(res) = tasks.join_next().await {
            match res {
                Ok(Ok((found, fallidos))) => {
                    cb_fallidos += fallidos.len();
                    for p in found {
                        live.insert(p.id_producto, p);
                    }
                }
                Ok(Err(e)) => {
                    // Unreachable / NotAuthorized: el ERP no está. Nada de lo
                    // que falte se verifica → nada de eso se envía.
                    tasks.abort_all();
                    warn!(error = %e, "pase de verdad abortado (CB): lo no verificado no se envía");
                    abortado = true;
                    break;
                }
                Err(_) => cb_fallidos += 1,
            }
        }

        // 2) Los que no aparecieron (sin CB, CB compartido o tanda rota): por id.
        let faltan: Vec<i64> = productos
            .iter()
            .map(|p| p.id_producto)
            .filter(|id| !live.contains_key(id))
            .collect();
        let id_calls = if abortado { 0 } else { faltan.len() };
        let mut id_fallidos: HashSet<i64> = HashSet::new();
        if abortado {
            id_fallidos.extend(faltan.iter().copied());
        } else {
            let mut tasks = tokio::task::JoinSet::new();
            for id in faltan {
                let erp = Arc::clone(&erp);
                let sem = Arc::clone(&sem);
                tasks.spawn(async move {
                    let _permit = sem.acquire_owned().await.expect("semaphore");
                    let r = erp.lookup_by_id(id).await;
                    tokio::time::sleep(pausa).await;   // no saturar el ERP
                    (id, r)
                });
            }
            let mut pendientes: HashSet<i64> = HashSet::new();
            while let Some(res) = tasks.join_next().await {
                match res {
                    Ok((_, Ok(Some(p)))) => {
                        live.insert(p.id_producto, p);
                    }
                    Ok((_, Ok(None))) => {}   // el ERP dice que no existe: queda el lote
                    Ok((id, Err(e @ (ErpError::Unreachable(_) | ErpError::NotAuthorized)))) => {
                        tasks.abort_all();
                        warn!(error = %e, "pase de verdad abortado (id): lo no verificado no se envía");
                        id_fallidos.insert(id);
                        abortado = true;
                        break;
                    }
                    Ok((id, Err(_))) => {
                        id_fallidos.insert(id);
                    }
                    Err(_) => {}
                }
                let _ = &mut pendientes;
            }
            if abortado {
                // Todo lo que no llegó a verificarse queda como no verificado.
                for p in &productos {
                    if !live.contains_key(&p.id_producto) {
                        id_fallidos.insert(p.id_producto);
                    }
                }
            }
        }

        let (out, corregidos, mut sin_verificar) = apply_live(productos, &live, &id_fallidos);
        if !sin_verificar.is_empty() {
            warn!(
                fallidos = sin_verificar.len(),
                "pase de verdad: productos sin dato en vivo NO se envían (el servidor conserva el último bueno)"
            );
        }
        // Estado "live": qué se vio y qué está activo (stock o precio > 0).
        let marcas: Vec<(i64, bool)> = live
            .values()
            .map(|p| (p.id_producto, p.stock_sucursal > 0.0 || p.precio > rust_decimal::Decimal::ZERO))
            .collect();
        if let Err(e) = self.state.live_marcar(&marcas) {
            warn!(error = %e, "no se pudo guardar el estado live");
        }
        let completo = modo == LiveModo::Completo && !abortado && id_fallidos.is_empty();
        if completo {
            let _ = self.state.set_meta(META_LAST_FULL_LIVE, &now_rfc3339());
        }
        info!(
            ?modo, cb_calls, id_calls, en_vivo = live.len(), corregidos, cb_fallidos,
            fallidos = sin_verificar.len(), omitidos, ms = elapsed_ms(t), "pase de verdad terminado"
        );
        sin_verificar.extend(omitidos_v);
        EnrichOutcome { productos: out, sin_verificar, omitidos, completo }
    }

    /// Barrido `GET /api/productos/{id}` por rango `1..=id_scan_max` con
    /// concurrencia acotada. Vacío si `daily_id_scan` está apagado.
    /// Aborta ante ERP inalcanzable o 401; los 404 (huecos) se ignoran.
    pub async fn daily_id_scan(&self) -> anyhow::Result<Vec<ProductoDTO>> {
        if !self.cfg.erp.daily_id_scan {
            return Ok(Vec::new());
        }
        let max = self.cfg.erp.id_scan_max.max(1);
        let sem = Arc::new(tokio::sync::Semaphore::new(self.cfg.erp.max_concurrency.max(1)));
        let erp_shared = self.erp();
        let mut tasks = tokio::task::JoinSet::new();
        for id in 1..=max {
            let erp = Arc::clone(&erp_shared);
            let sem = Arc::clone(&sem);
            tasks.spawn(async move {
                let _permit = sem.acquire_owned().await.expect("semaphore");
                let mut last_err = None;
                for _ in 0..ID_SCAN_RETRIES {
                    match erp.lookup_by_id(id).await {
                        Ok(p) => return Ok(p),
                        Err(e @ (ErpError::Unreachable(_) | ErpError::NotAuthorized)) => return Err(e),
                        Err(e) => last_err = Some(e),
                    }
                }
                Err(last_err.expect("al menos un intento"))
            });
        }
        let mut found = Vec::new();
        let mut failed = 0usize;
        while let Some(res) = tasks.join_next().await {
            match res? {
                Ok(Some(p)) => found.push(p),
                Ok(None) => {}
                Err(e @ (ErpError::Unreachable(_) | ErpError::NotAuthorized)) => {
                    tasks.abort_all();
                    self.state.set_meta(META_ERP_STATUS, e.status_label())?;
                    anyhow::bail!("barrido por ID abortado: {e}");
                }
                Err(e) => {
                    failed += 1;
                    warn!(error = %e, "barrido por ID: producto omitido tras reintentos");
                }
            }
        }
        info!(found = found.len(), failed, max, "barrido por ID terminado");
        Ok(found)
    }
}

/// Reemplaza cada DTO del lote por su versión en vivo (mismo `idProducto`).
/// Devuelve `(productos, corregidos, sin_verificar)`: los que están en
/// `fallidos` (su lectura en vivo falló) se separan — no se envían, porque
/// mandarlos con los ceros del lote pisaría el dato bueno del servidor
/// (incidente 15/9).
pub fn apply_live(
    productos: Vec<ProductoDTO>,
    live: &HashMap<i64, ProductoDTO>,
    fallidos: &HashSet<i64>,
) -> (Vec<ProductoDTO>, usize, Vec<ProductoDTO>) {
    let mut corregidos = 0usize;
    let mut out = Vec::with_capacity(productos.len());
    let mut sin_verificar = Vec::new();
    for p in productos {
        match live.get(&p.id_producto) {
            Some(v) => {
                if v.stock_sucursal != p.stock_sucursal || v.precio != p.precio {
                    corregidos += 1;
                }
                out.push(v.clone());
            }
            None if fallidos.contains(&p.id_producto) => sin_verificar.push(p),
            None => out.push(p),
        }
    }
    (out, corregidos, sin_verificar)
}

/// Lookup por CB con bisección: si el ERP rechaza la tanda (HTTP 5xx / decode),
/// se parte en mitades hasta aislar los CB que rompen. Devuelve
/// `(encontrados, cbs_fallidos)`. Unreachable / NotAuthorized se propagan.
pub fn lookup_cb_bisect<'a>(
    erp: &'a dyn ErpAdapter,
    chunk: Vec<String>,
) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(Vec<ProductoDTO>, Vec<String>), ErpError>> + Send + 'a>> {
    Box::pin(async move {
        match erp.lookup_by_barcodes(&chunk).await {
            Ok(found) => Ok((found, Vec::new())),
            Err(e @ (ErpError::Unreachable(_) | ErpError::NotAuthorized)) => Err(e),
            Err(e) => {
                if chunk.len() <= 1 {
                    warn!(cb = ?chunk.first(), error = %e, "CB rechazado por el ERP");
                    return Ok((Vec::new(), chunk));
                }
                let mitad = chunk.len() / 2;
                let (a, b) = chunk.split_at(mitad);
                let (fa, xa) = lookup_cb_bisect(erp, a.to_vec()).await?;
                let (fb, xb) = lookup_cb_bisect(erp, b.to_vec()).await?;
                let mut found = fa;
                found.extend(fb);
                let mut fallidos = xa;
                fallidos.extend(xb);
                Ok((found, fallidos))
            }
        }
    })
}

fn hashes_of(items: &[CatalogItem]) -> Vec<(String, String)> {
    items.iter().map(|i| (i.external_id.clone(), i.hash.clone())).collect()
}

/// Normaliza y une lote + extra, sin duplicados por `external_id` (gana el lote).
fn merge_items(lote: &[ProductoDTO], extra: &[ProductoDTO]) -> Vec<CatalogItem> {
    let mut by_id: HashMap<String, CatalogItem> = HashMap::with_capacity(lote.len() + extra.len());
    for dto in extra.iter().chain(lote.iter()) {
        let it = CatalogItem::from_dto(dto);
        by_id.insert(it.external_id.clone(), it);
    }
    let mut out: Vec<CatalogItem> = by_id.into_values().collect();
    out.sort_by(|a, b| a.external_id.len().cmp(&b.external_id.len()).then(a.external_id.cmp(&b.external_id)));
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_grows_and_caps() {
        assert_eq!(backoff_secs(0), 30);
        assert_eq!(backoff_secs(1), 60);
        assert_eq!(backoff_secs(2), 120);
        assert_eq!(backoff_secs(4), 480);
        assert_eq!(backoff_secs(5), 600);
        assert_eq!(backoff_secs(50), 600);
    }

    #[test]
    fn apply_live_replaces_by_id_and_counts_corrections() {
        use rust_decimal::Decimal;
        let dto = |id: i64, stock: f64, precio: i64| ProductoDTO {
            id_producto: id,
            troquel: 0,
            codigo_barras: vec![],
            descripcion: format!("P{id}"),
            stock_sucursal: stock,
            precio: Decimal::new(precio, 0),
            categoria: String::new(),
            rubro: String::new(),
            subrubro: String::new(),
            forma_farmaceutica: None,
            acciones_terapeuticas: vec![],
            laboratorio: None,
            nombres_drogas: None,
            ofertas: vec![],
            es_visible_en_venta: true,
            visibles_mismo_cb: None,
            baja: false,
        };
        // Lote: todo en cero (bug real del ERP). En vivo: 1 con datos, 2 igual,
        // 3 ausente (el ERP dijo que no existe → queda el lote), 4 FALLÓ.
        let lote = vec![dto(1, 0.0, 0), dto(2, 0.0, 0), dto(3, 0.0, 0), dto(4, 0.0, 0)];
        let live: HashMap<i64, ProductoDTO> = [(1, dto(1, 2.0, 32409)), (2, dto(2, 0.0, 0))].into();
        let fallidos: HashSet<i64> = [4].into();
        let (out, corregidos, sin_verificar) = apply_live(lote, &live, &fallidos);
        assert_eq!(corregidos, 1);
        assert_eq!(out.len(), 3);
        assert_eq!(out[0].stock_sucursal, 2.0);
        assert_eq!(out[0].precio, Decimal::new(32409, 0));
        assert_eq!(out[2].stock_sucursal, 0.0); // sin dato en vivo pero verificado ausente: queda el lote
        // El 4 no se envía: mandarlo en cero pisaría el dato bueno del servidor (15/9).
        assert_eq!(sin_verificar.len(), 1);
        assert_eq!(sin_verificar[0].id_producto, 4);
    }

    #[test]
    fn seleccion_selectiva_relee_activos_y_nunca_vistos() {
        let dto = |id: i64| ProductoDTO {
            id_producto: id, troquel: 0, codigo_barras: vec![], descripcion: format!("P{id}"),
            stock_sucursal: 0.0, precio: rust_decimal::Decimal::ZERO, categoria: String::new(),
            rubro: String::new(), subrubro: String::new(), forma_farmaceutica: None,
            acciones_terapeuticas: vec![], laboratorio: None, nombres_drogas: None, ofertas: vec![],
            es_visible_en_venta: true, visibles_mismo_cb: None, baja: false,
        };
        let lote = vec![dto(1), dto(2), dto(3), dto(4)];
        let activos: HashSet<i64> = [1].into();          // tuvo stock/precio
        let vistos: HashSet<i64> = [1, 2, 3].into();     // 4 es nuevo
        let (ver, omit) = seleccionar(lote.clone(), LiveModo::Selectivo, &activos, &vistos);
        assert_eq!(ver.iter().map(|p| p.id_producto).collect::<Vec<_>>(), vec![1, 4]);
        assert_eq!(omit.iter().map(|p| p.id_producto).collect::<Vec<_>>(), vec![2, 3]);
        let (ver, omit) = seleccionar(lote, LiveModo::Completo, &activos, &vistos);
        assert_eq!(ver.len(), 4);
        assert!(omit.is_empty());
    }

    #[test]
    fn modo_completo_sin_estado_o_a_la_hora() {
        use chrono::TimeZone;
        let mut cfg = Config::from_toml(
            "branch_id = \"b\"\nremedia_url = \"https://r\"\ntoken = \"t\"\n[erp]\nbase_url = \"http://e\"\n",
        ).unwrap();
        let ahora = chrono::Local.with_ymd_and_hms(2026, 9, 16, 3, 10, 0).unwrap();
        assert_eq!(modo_para(&cfg, ahora, None, 0), LiveModo::Completo);          // sin estado
        assert_eq!(modo_para(&cfg, ahora, None, 100), LiveModo::Completo);        // 3 AM, nunca hecho
        let hoy = chrono::Local.with_ymd_and_hms(2026, 9, 16, 3, 2, 0).unwrap().to_rfc3339();
        assert_eq!(modo_para(&cfg, ahora, Some(&hoy), 100), LiveModo::Selectivo); // ya hecho hoy
        let tarde = chrono::Local.with_ymd_and_hms(2026, 9, 16, 15, 0, 0).unwrap();
        assert_eq!(modo_para(&cfg, tarde, None, 100), LiveModo::Selectivo);       // otra hora
        cfg.erp.live_selective = false;
        assert_eq!(modo_para(&cfg, tarde, Some(&hoy), 100), LiveModo::Completo);  // apagado
    }

    #[test]
    fn delta_is_new_plus_changed() {
        let mk = |id: &str, hash: &str| CatalogItem {
            external_id: id.into(),
            hash: hash.into(),
            barcodes: vec![],
            troquel: None,
            name: String::new(),
            brand: None,
            drug: None,
            form: None,
            category: String::new(),
            rubro: String::new(),
            subrubro: String::new(),
            therapeutic_actions: vec![],
            price: None,
            stock: 0,
            visible: true,
            active: true,
        };
        let items = vec![mk("1", "a"), mk("2", "b"), mk("3", "c")];
        let known: HashMap<String, String> = [("1".to_string(), "a".to_string()), ("2".to_string(), "x".to_string())].into();
        let d = compute_delta(&items, &known);
        let ids: Vec<&str> = d.iter().map(|i| i.external_id.as_str()).collect();
        assert_eq!(ids, vec!["2", "3"]);
    }
}
