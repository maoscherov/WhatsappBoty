//! tracing: stdout (modo foreground) + archivo rotativo diario, 7 archivos máximo.

use std::path::Path;
use tracing_appender::non_blocking::WorkerGuard;
use tracing_appender::rolling::{RollingFileAppender, Rotation};
use tracing_subscriber::{fmt, layer::SubscriberExt, util::SubscriberInitExt, EnvFilter};

pub const MAX_LOG_FILES: usize = 7;

/// Inicializa el subscriber global. Devuelve el guard del appender: hay que
/// mantenerlo vivo hasta el final del proceso para que se vacíe el buffer.
pub fn init(dir: Option<&Path>, to_stdout: bool) -> anyhow::Result<Option<WorkerGuard>> {
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    let registry = tracing_subscriber::registry().with(filter);

    let stdout_layer = to_stdout.then(|| fmt::layer().with_target(false));

    let (file_layer, guard) = match dir {
        Some(dir) => {
            std::fs::create_dir_all(dir)?;
            let appender = RollingFileAppender::builder()
                .rotation(Rotation::DAILY)
                .max_log_files(MAX_LOG_FILES)
                .filename_prefix("agent")
                .filename_suffix("log")
                .build(dir)?;
            let (writer, guard) = tracing_appender::non_blocking(appender);
            let layer = fmt::layer()
                .with_ansi(false)
                .with_target(false)
                .with_writer(writer);
            (Some(layer), Some(guard))
        }
        None => (None, None),
    };

    registry.with(stdout_layer).with(file_layer).try_init()?;
    Ok(guard)
}
