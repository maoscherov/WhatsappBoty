//! remedia-agent: conector de catálogo ObServer Gestión → Remedia.
//!
//! Todo el tráfico es saliente. La lógica pura (normalización, hash, diff)
//! vive separada del I/O para poder testearla sin red.

pub mod catalog;
pub mod config;
pub mod erp;
pub mod ipc;
pub mod logging;
pub mod metrics;
pub mod remedia;
pub mod runtime;
pub mod service;
pub mod tray;

pub const AGENT_VERSION: &str = env!("CARGO_PKG_VERSION");
