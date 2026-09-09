//! remedia-agent: conector de catálogo ObServer Gestión → Remedia.
//!
//! Todo el tráfico es saliente. La lógica pura (normalización, hash, diff)
//! vive separada del I/O para poder testearla sin red.

pub mod catalog;
pub mod config;
pub mod erp;
pub mod logging;
pub mod metrics;
pub mod remedia;
pub mod service;

pub const AGENT_VERSION: &str = env!("CARGO_PKG_VERSION");
