//! Lógica pura del tray: color del icono, textos del menú, tooltip y detalle.
//! Sin UI, para poder testearla.

use crate::ipc::StatusReport;
use chrono::{DateTime, Local};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IconState {
    Ok,
    Warn,
    Err,
    Down,
}

/// Sin sync OK hace más de esto → amarillo.
pub const STALE_SYNC_MINUTES: i64 = 60;

pub fn icon_state(r: Option<&StatusReport>, now: DateTime<Local>) -> IconState {
    let Some(r) = r else { return IconState::Down };
    if matches!(r.erp_status.as_str(), "no_autorizado" | "inalcanzable" | "error") {
        return IconState::Err;
    }
    if r.last_error.as_deref().is_some_and(|e| !e.is_empty()) {
        return IconState::Err;
    }
    if !r.ws_connected || r.pending_batches > 0 {
        return IconState::Warn;
    }
    match minutes_since(r.last_sync_ok_at.as_deref(), now) {
        Some(m) if m <= STALE_SYNC_MINUTES => IconState::Ok,
        _ => IconState::Warn,
    }
}

pub fn minutes_since(ts: Option<&str>, now: DateTime<Local>) -> Option<i64> {
    let t = DateTime::parse_from_rfc3339(ts?).ok()?;
    Some(now.signed_duration_since(t.with_timezone(&Local)).num_minutes().max(0))
}

/// "hace 3 min", "hace 2 h", "hace 1 día", "nunca".
pub fn fmt_ago(ts: Option<&str>, now: DateTime<Local>) -> String {
    match minutes_since(ts, now) {
        None => "nunca".into(),
        Some(0) => "recién".into(),
        Some(m) if m < 60 => format!("hace {m} min"),
        Some(m) if m < 24 * 60 => format!("hace {} h", m / 60),
        Some(m) => format!("hace {} día{}", m / 1440, if m / 1440 == 1 { "" } else { "s" }),
    }
}

/// "302 ms", "14,8 s", "–".
pub fn fmt_ms(ms: Option<u64>) -> String {
    match ms {
        None => "–".into(),
        Some(v) if v < 1000 => format!("{v} ms"),
        Some(v) => format!("{},{} s", v / 1000, (v % 1000) / 100),
    }
}

pub fn hora_local(ts: Option<&str>) -> String {
    ts.and_then(|s| DateTime::parse_from_rfc3339(s).ok())
        .map(|t| t.with_timezone(&Local).format("%H:%M").to_string())
        .unwrap_or_else(|| "–".into())
}

pub fn erp_status_text(status: &str) -> &'static str {
    match status {
        "ok" => "ok",
        "no_autorizado" => "no autorizado (API de productos deshabilitada en ObServer)",
        "inalcanzable" => "sin conexión",
        "error" => "con error",
        _ => "sin datos todavía",
    }
}

pub fn fmt_count(n: Option<u64>) -> String {
    match n {
        None => "–".into(),
        Some(v) => {
            let s = v.to_string();
            let mut out = String::new();
            for (i, c) in s.chars().enumerate() {
                if i > 0 && (s.len() - i) % 3 == 0 {
                    out.push('.');
                }
                out.push(c);
            }
            out
        }
    }
}

/// Líneas de estado que van arriba del menú (ítems deshabilitados).
pub fn menu_lines(r: Option<&StatusReport>, now: DateTime<Local>) -> Vec<String> {
    let Some(r) = r else {
        return vec![
            format!("Remedia Agent {}", crate::AGENT_VERSION),
            "Servicio detenido".into(),
        ];
    };
    let mut lines = vec![format!("Remedia Agent {} — {}", r.agent_version, r.branch_id)];
    lines.push(format!(
        "ERP: {} ({} la última lectura)",
        erp_status_text(&r.erp_status),
        fmt_ms(r.metrics.erp_fetch_ms)
    ));
    let cambios = match r.last_sync_changed {
        Some(c) => format!(" ({c} cambios)"),
        None => String::new(),
    };
    lines.push(format!(
        "Remedia: {} · último sync {}{}",
        if r.ws_connected { "conectado" } else { "desconectado" },
        fmt_ago(r.last_sync_ok_at.as_deref(), now),
        cambios
    ));
    lines.push(format!("Pendientes de envío: {}", r.pending_batches));
    if let Some(e) = r.last_error.as_deref().filter(|e| !e.is_empty()) {
        lines.push(format!("Último error: {}", truncate(e, 70)));
    }
    lines
}

pub fn tooltip(r: Option<&StatusReport>, now: DateTime<Local>) -> String {
    let t = match r {
        None => "Remedia · servicio detenido".to_string(),
        Some(r) => format!(
            "Remedia · ERP {} · sync {}",
            erp_status_text(&r.erp_status).split(' ').next().unwrap_or(""),
            fmt_ago(r.last_sync_ok_at.as_deref(), now)
        ),
    };
    truncate(&t, 120)
}

pub fn detail_text(r: Option<&StatusReport>, now: DateTime<Local>) -> String {
    let Some(r) = r else {
        return "El servicio Remedia Agent no está corriendo.\r\n\r\nSi acaba de instalarse, esperá un minuto. Si no, avisá a soporte.".into();
    };
    let m = &r.metrics;
    let mut s = String::new();
    let mut line = |k: &str, v: String| {
        s.push_str(&format!("{k:<28}{v}\r\n"));
    };
    line("Versión del agente", r.agent_version.clone());
    line("Sucursal", r.branch_id.clone());
    line("Servicio activo", fmt_uptime(m.uptime_secs));
    line("", String::new());
    line("ERP", r.erp_url.clone());
    line("Estado del ERP", erp_status_text(&r.erp_status).to_string());
    line("Versión del ERP", r.erp_version.clone().unwrap_or_else(|| "–".into()));
    line("Lectura completa", fmt_ms(m.erp_fetch_ms));
    line("Promedio por lote", fmt_ms(m.erp_lote_avg_ms));
    line("Productos leídos", fmt_count(r.last_sync_fetched));
    line("", String::new());
    line("Remedia", r.remedia_url.clone());
    line("Websocket", if r.ws_connected { "conectado".into() } else { "desconectado".into() });
    line("Reconexiones", m.ws_reconnects.to_string());
    line("Último sync OK", format!("{} ({})", hora_local(r.last_sync_ok_at.as_deref()), fmt_ago(r.last_sync_ok_at.as_deref(), now)));
    line("Último intento", format!("{} ({})", hora_local(r.last_sync_at.as_deref()), fmt_ago(r.last_sync_at.as_deref(), now)));
    line("Cambios enviados", fmt_count(r.last_sync_changed));
    line("Ciclo completo", fmt_ms(m.sync_total_ms));
    line("Envío por lote", fmt_ms(m.remedia_push_avg_ms));
    line("Lotes pendientes", r.pending_batches.to_string());
    line("Heartbeat", format!("{} · {}", fmt_ago(r.last_heartbeat_at.as_deref(), now), fmt_ms(m.heartbeat_ms)));
    line("Consulta en vivo", format!("último {} · promedio {}", fmt_ms(m.lookup_last_ms), fmt_ms(m.lookup_avg_ms)));
    line("", String::new());
    line("Último error", r.last_error.clone().filter(|e| !e.is_empty()).unwrap_or_else(|| "ninguno".into()));
    line("Config", r.config_path.clone());
    line("Logs", r.log_dir.clone());
    s
}

pub fn fmt_uptime(secs: u64) -> String {
    let (d, h, m) = (secs / 86400, (secs % 86400) / 3600, (secs % 3600) / 60);
    if d > 0 {
        format!("{d} d {h} h")
    } else if h > 0 {
        format!("{h} h {m} min")
    } else {
        format!("{m} min")
    }
}

pub fn truncate(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        s.to_string()
    } else {
        let cut: String = s.chars().take(max - 1).collect();
        format!("{cut}…")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::metrics::MetricsSnapshot;

    fn report(now: DateTime<Local>) -> StatusReport {
        StatusReport {
            agent_version: "0.2.0".into(),
            branch_id: "farmacia-x".into(),
            erp_status: "ok".into(),
            ws_connected: true,
            last_sync_ok_at: Some((now - chrono::Duration::minutes(3)).to_rfc3339()),
            last_sync_changed: Some(37),
            pending_batches: 0,
            metrics: MetricsSnapshot { erp_fetch_ms: Some(14_820), ..Default::default() },
            ..Default::default()
        }
    }

    #[test]
    fn icon_states() {
        let now = Local::now();
        assert_eq!(icon_state(None, now), IconState::Down);
        let r = report(now);
        assert_eq!(icon_state(Some(&r), now), IconState::Ok);
        let mut e = report(now);
        e.erp_status = "no_autorizado".into();
        assert_eq!(icon_state(Some(&e), now), IconState::Err);
        let mut e = report(now);
        e.last_error = Some("Remedia HTTP 401".into());
        assert_eq!(icon_state(Some(&e), now), IconState::Err);
        let mut w = report(now);
        w.pending_batches = 2;
        assert_eq!(icon_state(Some(&w), now), IconState::Warn);
        let mut w = report(now);
        w.ws_connected = false;
        assert_eq!(icon_state(Some(&w), now), IconState::Warn);
        let mut w = report(now);
        w.last_sync_ok_at = Some((now - chrono::Duration::hours(3)).to_rfc3339());
        assert_eq!(icon_state(Some(&w), now), IconState::Warn);
        let mut w = report(now);
        w.last_sync_ok_at = None;
        assert_eq!(icon_state(Some(&w), now), IconState::Warn);
    }

    #[test]
    fn formatting() {
        let now = Local::now();
        assert_eq!(fmt_ms(None), "–");
        assert_eq!(fmt_ms(Some(302)), "302 ms");
        assert_eq!(fmt_ms(Some(14_820)), "14,8 s");
        assert_eq!(fmt_ago(None, now), "nunca");
        assert_eq!(fmt_ago(Some(&(now - chrono::Duration::minutes(3)).to_rfc3339()), now), "hace 3 min");
        assert_eq!(fmt_ago(Some(&(now - chrono::Duration::hours(2)).to_rfc3339()), now), "hace 2 h");
        assert_eq!(fmt_ago(Some(&(now - chrono::Duration::days(2)).to_rfc3339()), now), "hace 2 días");
        assert_eq!(fmt_ago(Some("basura"), now), "nunca");
        assert_eq!(fmt_count(Some(54235)), "54.235");
        assert_eq!(fmt_count(Some(999)), "999");
        assert_eq!(fmt_uptime(90_000), "1 d 1 h");
        assert_eq!(truncate("abcdef", 4), "abc…");
    }

    #[test]
    fn menu_lines_and_tooltip() {
        let now = Local::now();
        let r = report(now);
        let lines = menu_lines(Some(&r), now);
        assert_eq!(lines[0], "Remedia Agent 0.2.0 — farmacia-x");
        assert_eq!(lines[1], "ERP: ok (14,8 s la última lectura)");
        assert_eq!(lines[2], "Remedia: conectado · último sync hace 3 min (37 cambios)");
        assert_eq!(lines[3], "Pendientes de envío: 0");
        assert_eq!(lines.len(), 4);
        assert_eq!(menu_lines(None, now)[1], "Servicio detenido");
        assert_eq!(tooltip(Some(&r), now), "Remedia · ERP ok · sync hace 3 min");
        assert!(tooltip(None, now).contains("detenido"));
        let d = detail_text(Some(&r), now);
        assert!(d.contains("Lectura completa"));
        assert!(d.contains("14,8 s"));
    }
}
