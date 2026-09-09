//! Cliente del named pipe (tray y `agent.exe status`).

use crate::ipc::{Request, Response};

/// Error legible cuando el servicio no está corriendo.
pub const SERVICE_DOWN: &str = "El servicio Remedia Agent no está corriendo";

#[cfg(windows)]
pub use imp::*;

#[cfg(windows)]
mod imp {
    use super::*;
    use crate::ipc::{IO_TIMEOUT_SECS, MAX_MESSAGE, PIPE_NAME};
    use std::time::Duration;
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
    use tokio::net::windows::named_pipe::{ClientOptions, NamedPipeClient};

    const ERROR_PIPE_BUSY: i32 = 231;
    const ERROR_FILE_NOT_FOUND: i32 = 2;
    const ERROR_ACCESS_DENIED: i32 = 5;

    async fn open(name: &str) -> anyhow::Result<NamedPipeClient> {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
        loop {
            match ClientOptions::new().open(name) {
                Ok(c) => return Ok(c),
                Err(e) if e.raw_os_error() == Some(ERROR_PIPE_BUSY) && tokio::time::Instant::now() < deadline => {
                    tokio::time::sleep(Duration::from_millis(50)).await;
                }
                Err(e) if e.raw_os_error() == Some(ERROR_FILE_NOT_FOUND) => anyhow::bail!("{SERVICE_DOWN}"),
                Err(e) if e.raw_os_error() == Some(ERROR_ACCESS_DENIED) => {
                    anyhow::bail!("Sin permiso para hablar con el servicio (pipe {name})")
                }
                Err(e) => anyhow::bail!("No se pudo abrir el pipe {name}: {e}"),
            }
        }
    }

    pub async fn call(req: &Request) -> anyhow::Result<Response> {
        call_on(PIPE_NAME, req).await
    }

    pub async fn call_on(name: &str, req: &Request) -> anyhow::Result<Response> {
        let mut pipe = open(name).await?;
        let mut line = serde_json::to_string(req)?;
        line.push('\n');
        tokio::time::timeout(Duration::from_secs(IO_TIMEOUT_SECS), pipe.write_all(line.as_bytes())).await??;
        pipe.flush().await?;
        let mut reader = BufReader::new(pipe);
        let mut resp = String::new();
        let n = tokio::time::timeout(Duration::from_secs(IO_TIMEOUT_SECS * 2), reader.read_line(&mut resp)).await??;
        if n == 0 {
            anyhow::bail!("El servicio cerró la conexión sin responder");
        }
        if resp.len() > MAX_MESSAGE {
            anyhow::bail!("respuesta demasiado larga");
        }
        let parsed: Response = serde_json::from_str(resp.trim())?;
        // Aviso de "leí todo" para que el servidor desconecte.
        let mut pipe = reader.into_inner();
        let _ = pipe.write_all(b"\n").await;
        Ok(parsed)
    }

    /// Para el tray (sin runtime tokio propio).
    pub fn call_blocking(req: &Request) -> anyhow::Result<Response> {
        let rt = tokio::runtime::Builder::new_current_thread().enable_all().build()?;
        rt.block_on(call(req))
    }
}

#[cfg(not(windows))]
pub async fn call(_req: &Request) -> anyhow::Result<Response> {
    anyhow::bail!("{SERVICE_DOWN}")
}

#[cfg(not(windows))]
pub fn call_blocking(_req: &Request) -> anyhow::Result<Response> {
    anyhow::bail!("{SERVICE_DOWN}")
}
