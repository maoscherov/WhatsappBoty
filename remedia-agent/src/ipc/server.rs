//! Servidor del named pipe (lado servicio).

#[cfg(windows)]
pub use imp::*;

#[cfg(windows)]
mod imp {
    use crate::ipc::{Request, Response, IO_TIMEOUT_SECS, MAX_MESSAGE, PIPE_NAME, PIPE_SDDL};
    use crate::runtime::Runtime;
    use std::sync::Arc;
    use std::time::Duration;
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
    use tokio::net::windows::named_pipe::{NamedPipeServer, ServerOptions};
    use tokio_util::sync::CancellationToken;
    use tracing::{debug, error, warn};
    use windows_sys::Win32::Foundation::LocalFree;
    use windows_sys::Win32::Security::Authorization::{
        ConvertStringSecurityDescriptorToSecurityDescriptorW, SDDL_REVISION_1,
    };
    use windows_sys::Win32::Security::SECURITY_ATTRIBUTES;

    /// `SECURITY_ATTRIBUTES` construidas desde un SDDL. Mantiene vivo el
    /// descriptor mientras el pipe lo necesite.
    pub struct SecurityAttributes {
        attrs: SECURITY_ATTRIBUTES,
    }

    unsafe impl Send for SecurityAttributes {}
    unsafe impl Sync for SecurityAttributes {}

    impl SecurityAttributes {
        pub fn from_sddl(sddl: &str) -> anyhow::Result<SecurityAttributes> {
            let wide: Vec<u16> = sddl.encode_utf16().chain(std::iter::once(0)).collect();
            let mut psd = std::ptr::null_mut();
            let ok = unsafe {
                ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    wide.as_ptr(),
                    SDDL_REVISION_1,
                    &mut psd,
                    std::ptr::null_mut(),
                )
            };
            if ok == 0 || psd.is_null() {
                anyhow::bail!("SDDL inválido: {}", std::io::Error::last_os_error());
            }
            Ok(SecurityAttributes {
                attrs: SECURITY_ATTRIBUTES {
                    nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
                    lpSecurityDescriptor: psd,
                    bInheritHandle: 0,
                },
            })
        }

        fn as_ptr(&self) -> *mut std::ffi::c_void {
            &self.attrs as *const SECURITY_ATTRIBUTES as *mut std::ffi::c_void
        }
    }

    impl Drop for SecurityAttributes {
        fn drop(&mut self) {
            if !self.attrs.lpSecurityDescriptor.is_null() {
                unsafe { LocalFree(self.attrs.lpSecurityDescriptor) };
            }
        }
    }

    fn create_instance(name: &str, sa: &SecurityAttributes, first: bool) -> std::io::Result<NamedPipeServer> {
        let mut opts = ServerOptions::new();
        opts.first_pipe_instance(first);
        // SAFETY: `sa` apunta a SECURITY_ATTRIBUTES válidas mientras dure `sa`.
        unsafe { opts.create_with_security_attributes_raw(name, sa.as_ptr()) }
    }

    pub async fn serve(rt: Arc<Runtime>, shutdown: CancellationToken) -> anyhow::Result<()> {
        serve_on(PIPE_NAME, rt, shutdown).await
    }

    /// Acepta conexiones hasta que `shutdown` se cancele. Cada conexión se
    /// atiende en su propia tarea.
    pub async fn serve_on(name: &str, rt: Arc<Runtime>, shutdown: CancellationToken) -> anyhow::Result<()> {
        let sa = Arc::new(SecurityAttributes::from_sddl(PIPE_SDDL)?);
        let mut server = create_instance(name, &sa, true)
            .map_err(|e| anyhow::anyhow!("no se pudo crear el pipe {name}: {e}"))?;
        debug!(name, "pipe IPC escuchando");
        loop {
            tokio::select! {
                _ = shutdown.cancelled() => return Ok(()),
                r = server.connect() => {
                    if let Err(e) = r {
                        warn!(error = %e, "pipe: connect falló");
                        tokio::time::sleep(Duration::from_millis(200)).await;
                        continue;
                    }
                    let conn = server;
                    server = match create_instance(name, &sa, false) {
                        Ok(s) => s,
                        Err(e) => {
                            error!(error = %e, "pipe: no se pudo crear la siguiente instancia");
                            tokio::time::sleep(Duration::from_millis(500)).await;
                            create_instance(name, &sa, false)?
                        }
                    };
                    let rt = Arc::clone(&rt);
                    tokio::spawn(async move {
                        if let Err(e) = handle_conn(conn, rt).await {
                            debug!(error = %e, "pipe: conexión terminó con error");
                        }
                    });
                }
            }
        }
    }

    async fn handle_conn(pipe: NamedPipeServer, rt: Arc<Runtime>) -> anyhow::Result<()> {
        let mut reader = BufReader::new(pipe);
        let mut line = String::new();
        let n = tokio::time::timeout(Duration::from_secs(IO_TIMEOUT_SECS), reader.read_line(&mut line)).await??;
        if n == 0 {
            return Ok(());
        }
        if line.len() > MAX_MESSAGE {
            anyhow::bail!("mensaje demasiado largo");
        }
        let response = match serde_json::from_str::<Request>(line.trim()) {
            Ok(req) => {
                debug!(?req, "pipe: request");
                rt.handle(req).await
            }
            Err(e) => Response::err(format!("Pedido no reconocido: {e}")),
        };
        let mut out = serde_json::to_string(&response)?;
        out.push('\n');
        let mut pipe = reader.into_inner();
        tokio::time::timeout(Duration::from_secs(IO_TIMEOUT_SECS), pipe.write_all(out.as_bytes())).await??;
        pipe.flush().await?;
        // Dejar que el cliente lea antes de desconectar.
        let _ = tokio::time::timeout(Duration::from_secs(2), async {
            let mut sink = [0u8; 1];
            let _ = tokio::io::AsyncReadExt::read(&mut pipe, &mut sink).await;
        })
        .await;
        let _ = pipe.disconnect();
        Ok(())
    }
}

#[cfg(not(windows))]
pub async fn serve(_rt: std::sync::Arc<crate::runtime::Runtime>, _shutdown: tokio_util::sync::CancellationToken) -> anyhow::Result<()> {
    anyhow::bail!("el pipe IPC solo existe en Windows")
}
