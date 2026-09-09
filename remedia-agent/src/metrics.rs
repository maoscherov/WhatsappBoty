//! Latencias y contadores del agente. Se exponen por el pipe (tray), en
//! `agent.exe status` y en el heartbeat hacia Remedia.

use serde::{Deserialize, Serialize};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

/// Peso de la media móvil exponencial del lookup en vivo.
pub const LOOKUP_EMA_ALPHA: f64 = 0.2;

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct MetricsSnapshot {
    /// Última lectura completa del catálogo del ERP (`fetch_all`).
    pub erp_fetch_ms: Option<u64>,
    /// Promedio por lote en esa lectura.
    pub erp_lote_avg_ms: Option<u64>,
    /// Promedio por lote enviado a Remedia en el último ciclo con envíos.
    pub remedia_push_avg_ms: Option<u64>,
    /// Ciclo completo de sync (pendientes + fetch + diff + push).
    pub sync_total_ms: Option<u64>,
    /// Último lookup en vivo contra el ERP.
    pub lookup_last_ms: Option<u64>,
    /// Media móvil de los lookups (α = 0.2).
    pub lookup_avg_ms: Option<u64>,
    pub heartbeat_ms: Option<u64>,
    /// Reconexiones del websocket desde el arranque del servicio.
    pub ws_reconnects: u64,
    pub ws_connected: bool,
    pub uptime_secs: u64,
}

#[derive(Default)]
struct Inner {
    erp_fetch_ms: Option<u64>,
    erp_lote_avg_ms: Option<u64>,
    remedia_push_avg_ms: Option<u64>,
    sync_total_ms: Option<u64>,
    lookup_last_ms: Option<u64>,
    lookup_avg_ms: Option<f64>,
    heartbeat_ms: Option<u64>,
}

pub struct Metrics {
    inner: Mutex<Inner>,
    ws_connected: AtomicBool,
    ws_reconnects: AtomicU64,
    started: Instant,
}

impl Metrics {
    pub fn new() -> Arc<Metrics> {
        Arc::new(Metrics {
            inner: Mutex::new(Inner::default()),
            ws_connected: AtomicBool::new(false),
            ws_reconnects: AtomicU64::new(0),
            started: Instant::now(),
        })
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }

    pub fn record_fetch(&self, ms: u64, lotes: u32) {
        let mut i = self.lock();
        i.erp_fetch_ms = Some(ms);
        i.erp_lote_avg_ms = Some(if lotes == 0 { ms } else { ms / lotes as u64 });
    }

    pub fn record_push_avg(&self, ms: u64) {
        self.lock().remedia_push_avg_ms = Some(ms);
    }

    pub fn record_sync_total(&self, ms: u64) {
        self.lock().sync_total_ms = Some(ms);
    }

    pub fn record_lookup(&self, ms: u64) {
        let mut i = self.lock();
        i.lookup_last_ms = Some(ms);
        i.lookup_avg_ms = Some(match i.lookup_avg_ms {
            None => ms as f64,
            Some(avg) => avg * (1.0 - LOOKUP_EMA_ALPHA) + ms as f64 * LOOKUP_EMA_ALPHA,
        });
    }

    pub fn record_heartbeat(&self, ms: u64) {
        self.lock().heartbeat_ms = Some(ms);
    }

    pub fn set_ws_connected(&self, connected: bool) {
        self.ws_connected.store(connected, Ordering::Relaxed);
    }

    pub fn ws_connected(&self) -> bool {
        self.ws_connected.load(Ordering::Relaxed)
    }

    pub fn inc_ws_reconnects(&self) {
        self.ws_reconnects.fetch_add(1, Ordering::Relaxed);
    }

    pub fn snapshot(&self) -> MetricsSnapshot {
        let i = self.lock();
        MetricsSnapshot {
            erp_fetch_ms: i.erp_fetch_ms,
            erp_lote_avg_ms: i.erp_lote_avg_ms,
            remedia_push_avg_ms: i.remedia_push_avg_ms,
            sync_total_ms: i.sync_total_ms,
            lookup_last_ms: i.lookup_last_ms,
            lookup_avg_ms: i.lookup_avg_ms.map(|v| v.round() as u64),
            heartbeat_ms: i.heartbeat_ms,
            ws_reconnects: self.ws_reconnects.load(Ordering::Relaxed),
            ws_connected: self.ws_connected(),
            uptime_secs: self.started.elapsed().as_secs(),
        }
    }
}

pub fn elapsed_ms(since: Instant) -> u64 {
    since.elapsed().as_millis() as u64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn snapshot_starts_empty() {
        let m = Metrics::new();
        let s = m.snapshot();
        assert_eq!(s, MetricsSnapshot { uptime_secs: s.uptime_secs, ..Default::default() });
        assert!(!s.ws_connected);
    }

    #[test]
    fn fetch_avg_per_lote() {
        let m = Metrics::new();
        m.record_fetch(14_820, 49);
        let s = m.snapshot();
        assert_eq!(s.erp_fetch_ms, Some(14_820));
        assert_eq!(s.erp_lote_avg_ms, Some(302));
        m.record_fetch(500, 0);
        assert_eq!(m.snapshot().erp_lote_avg_ms, Some(500));
    }

    #[test]
    fn lookup_moving_average() {
        let m = Metrics::new();
        m.record_lookup(100);
        assert_eq!(m.snapshot().lookup_avg_ms, Some(100));
        m.record_lookup(200);
        // 100 * 0.8 + 200 * 0.2 = 120
        assert_eq!(m.snapshot().lookup_avg_ms, Some(120));
        assert_eq!(m.snapshot().lookup_last_ms, Some(200));
    }

    #[test]
    fn ws_flags() {
        let m = Metrics::new();
        m.set_ws_connected(true);
        m.inc_ws_reconnects();
        m.inc_ws_reconnects();
        let s = m.snapshot();
        assert!(s.ws_connected);
        assert_eq!(s.ws_reconnects, 2);
    }

    #[test]
    fn snapshot_roundtrips_json() {
        let m = Metrics::new();
        m.record_sync_total(15_400);
        let s = m.snapshot();
        let back: MetricsSnapshot = serde_json::from_str(&serde_json::to_string(&s).unwrap()).unwrap();
        assert_eq!(back, s);
    }
}
