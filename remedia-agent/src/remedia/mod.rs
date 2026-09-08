//! Cliente hacia Remedia: HTTP (`/v1/sync/*`) y websocket saliente (`/v1/agent/ws`).

pub mod client;

pub use client::{RemediaClient, RemediaError};
