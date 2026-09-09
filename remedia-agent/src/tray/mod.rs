//! Icono de bandeja (`agent.exe tray`). Proceso del usuario que habla con el
//! servicio por el named pipe. Ver spec `2026-09-09-remedia-agent-tray-design.md`.

pub mod state;

#[cfg(windows)]
mod config_window;
#[cfg(windows)]
mod detail_window;

#[cfg(windows)]
pub use imp::run_tray;

#[cfg(not(windows))]
pub fn run_tray() -> anyhow::Result<()> {
    anyhow::bail!("el tray solo existe en Windows")
}

/// Nombre del mutex de instancia única.
pub const SINGLE_INSTANCE_MUTEX: &str = r"Local\RemediaAgentTray";
/// Evento con nombre que `uninstall` señala para cerrar los trays abiertos.
pub const QUIT_EVENT: &str = r"Global\RemediaAgentTrayQuit";

#[cfg(windows)]
mod imp {
    use super::config_window::ConfigWindow;
    use super::detail_window::DetailWindow;
    use super::state::{self, IconState};
    use super::{QUIT_EVENT, SINGLE_INSTANCE_MUTEX};
    use crate::ipc::client::call_blocking;
    use crate::ipc::{Request, Response, StatusReport};
    use native_windows_gui as nwg;
    use std::cell::{Cell, RefCell};
    use std::collections::VecDeque;
    use std::rc::Rc;
    use std::sync::{Arc, Mutex};
    use std::time::Duration;
    use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, ERROR_ALREADY_EXISTS, HANDLE, WAIT_OBJECT_0};
    use windows_sys::Win32::System::Threading::{CreateEventW, CreateMutexW, WaitForSingleObject};

    const REFRESH: Duration = Duration::from_secs(5);
    const ICON_OK: &[u8] = include_bytes!("../../assets/tray_ok.ico");
    const ICON_WARN: &[u8] = include_bytes!("../../assets/tray_warn.ico");
    const ICON_ERR: &[u8] = include_bytes!("../../assets/tray_err.ico");
    const ICON_DOWN: &[u8] = include_bytes!("../../assets/tray_down.ico");

    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub enum JobKind {
        Refresh,
        SyncNow,
        TestErp,
        ConfigTestRemedia,
        ConfigTestErp,
        ConfigSave,
    }

    pub struct JobResult {
        pub kind: JobKind,
        pub result: anyhow::Result<Response>,
    }

    /// Ítems del menú contextual. Se reconstruye en cada apertura porque
    /// `MenuItem` no permite cambiar el texto una vez creado.
    struct TrayMenu {
        _menu: nwg::Menu,
        _status: Vec<nwg::MenuItem>,
        _seps: Vec<nwg::MenuSeparator>,
        sync_now: nwg::MenuItem,
        test_erp: nwg::MenuItem,
        detail: nwg::MenuItem,
        config: nwg::MenuItem,
        logs: nwg::MenuItem,
        exit: nwg::MenuItem,
    }

    pub struct App {
        pub window: nwg::MessageWindow,
        icons: [nwg::Icon; 4],
        pub tray: nwg::TrayNotification,
        timer: nwg::AnimationTimer,
        notice: nwg::Notice,
        menu: RefCell<Option<TrayMenu>>,
        pub report: RefCell<Option<StatusReport>>,
        current_icon: Cell<Option<IconState>>,
        jobs: Arc<Mutex<VecDeque<JobResult>>>,
        refreshing: Cell<bool>,
        pub config_win: RefCell<Option<ConfigWindow>>,
        detail_win: RefCell<Option<DetailWindow>>,
        quit_event: HANDLE,
    }

    fn to_wide(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
    }

    /// `Some(handle)` si somos la primera instancia; `None` si ya hay un tray.
    fn single_instance() -> Option<HANDLE> {
        let name = to_wide(SINGLE_INSTANCE_MUTEX);
        let h = unsafe { CreateMutexW(std::ptr::null(), 0, name.as_ptr()) };
        if h.is_null() {
            return Some(h);
        }
        if unsafe { GetLastError() } == ERROR_ALREADY_EXISTS {
            unsafe { CloseHandle(h) };
            return None;
        }
        Some(h)
    }

    fn quit_event() -> HANDLE {
        let name = to_wide(QUIT_EVENT);
        unsafe { CreateEventW(std::ptr::null(), 1, 0, name.as_ptr()) }
    }

    fn quit_requested(h: HANDLE) -> bool {
        !h.is_null() && unsafe { WaitForSingleObject(h, 0) } == WAIT_OBJECT_0
    }

    pub fn run_tray() -> anyhow::Result<()> {
        let Some(_mutex) = single_instance() else {
            return Ok(());
        };
        nwg::init().map_err(|e| anyhow::anyhow!("nwg init: {e}"))?;
        let _ = nwg::Font::set_global_family("Segoe UI");
        let app = App::build().map_err(|e| anyhow::anyhow!("tray: {e}"))?;
        let app = Rc::new(app);
        App::bind(&app);
        App::spawn_job(&app, JobKind::Refresh, Request::Status);
        nwg::dispatch_thread_events();
        Ok(())
    }

    impl App {
        fn build() -> Result<App, nwg::NwgError> {
            let mut window = nwg::MessageWindow::default();
            nwg::MessageWindow::builder().build(&mut window)?;

            let icons = [
                nwg::Icon::from_bin(ICON_OK)?,
                nwg::Icon::from_bin(ICON_WARN)?,
                nwg::Icon::from_bin(ICON_ERR)?,
                nwg::Icon::from_bin(ICON_DOWN)?,
            ];

            let mut tray = nwg::TrayNotification::default();
            nwg::TrayNotification::builder()
                .parent(&window)
                .icon(Some(&icons[3]))
                .tip(Some("Remedia · conectando con el servicio…"))
                .build(&mut tray)?;

            let mut timer = nwg::AnimationTimer::default();
            nwg::AnimationTimer::builder()
                .parent(&window)
                .interval(REFRESH)
                .active(true)
                .build(&mut timer)?;

            let mut notice = nwg::Notice::default();
            nwg::Notice::builder().parent(&window).build(&mut notice)?;

            Ok(App {
                window,
                icons,
                tray,
                timer,
                notice,
                menu: RefCell::new(None),
                report: RefCell::new(None),
                current_icon: Cell::new(None),
                jobs: Arc::new(Mutex::new(VecDeque::new())),
                refreshing: Cell::new(false),
                config_win: RefCell::new(None),
                detail_win: RefCell::new(None),
                quit_event: quit_event(),
            })
        }

        fn bind(app: &Rc<App>) {
            use nwg::Event as E;
            let weak = Rc::downgrade(app);
            let handler = move |evt, _data, handle: nwg::ControlHandle| {
                let Some(app) = weak.upgrade() else { return };
                match evt {
                    E::OnContextMenu if handle == app.tray => App::show_menu(&app),
                    E::OnTimerTick if handle == app.timer => App::on_tick(&app),
                    E::OnNotice if handle == app.notice => App::on_notice(&app),
                    E::OnMenuItemSelected => App::on_menu(&app, handle),
                    _ => {}
                }
            };
            // El handler vive hasta que termina el proceso (el tray no se "cierra").
            let _ = nwg::full_bind_event_handler(&app.window.handle, handler);
        }

        // ---- jobs en hilos ---------------------------------------------------

        pub fn spawn_job(app: &Rc<App>, kind: JobKind, req: Request) {
            if kind == JobKind::Refresh {
                if app.refreshing.get() {
                    return;
                }
                app.refreshing.set(true);
            }
            let jobs = Arc::clone(&app.jobs);
            let sender = app.notice.sender();
            std::thread::spawn(move || {
                let result = call_blocking(&req);
                jobs.lock().unwrap_or_else(|e| e.into_inner()).push_back(JobResult { kind, result });
                sender.notice();
            });
        }

        fn on_tick(app: &Rc<App>) {
            if quit_requested(app.quit_event) {
                nwg::stop_thread_dispatch();
                return;
            }
            App::spawn_job(app, JobKind::Refresh, Request::Status);
        }

        fn on_notice(app: &Rc<App>) {
            let drained: Vec<JobResult> = {
                let mut q = app.jobs.lock().unwrap_or_else(|e| e.into_inner());
                q.drain(..).collect()
            };
            for job in drained {
                match job.kind {
                    JobKind::Refresh => {
                        app.refreshing.set(false);
                        let report = match job.result {
                            Ok(r) => r.status,
                            Err(_) => None,
                        };
                        *app.report.borrow_mut() = report;
                        App::refresh_icon(app);
                    }
                    JobKind::SyncNow => match job.result {
                        Ok(r) if r.ok => app.balloon("Sincronización pedida", "El agente va a sincronizar en unos segundos."),
                        Ok(r) => app.balloon("No se pudo pedir la sincronización", &r.error.unwrap_or_default()),
                        Err(e) => app.balloon("No se pudo pedir la sincronización", &e.to_string()),
                    },
                    JobKind::TestErp => App::show_test_erp(app, job.result),
                    JobKind::ConfigTestRemedia | JobKind::ConfigTestErp | JobKind::ConfigSave => {
                        let refresh = {
                            let win = app.config_win.borrow();
                            match win.as_ref() {
                                Some(w) => w.on_job(job.kind, job.result),
                                None => false,
                            }
                        };
                        if refresh {
                            App::spawn_job(app, JobKind::Refresh, Request::Status);
                        }
                    }
                }
            }
        }

        fn refresh_icon(app: &Rc<App>) {
            let now = chrono::Local::now();
            let report = app.report.borrow();
            let st = state::icon_state(report.as_ref(), now);
            if app.current_icon.get() != Some(st) {
                let idx = match st {
                    IconState::Ok => 0,
                    IconState::Warn => 1,
                    IconState::Err => 2,
                    IconState::Down => 3,
                };
                app.tray.set_icon(&app.icons[idx]);
                app.current_icon.set(Some(st));
            }
            app.tray.set_tip(&state::tooltip(report.as_ref(), now));
            if let Some(w) = app.detail_win.borrow().as_ref() {
                w.update(&state::detail_text(report.as_ref(), now));
            }
        }

        fn balloon(&self, title: &str, text: &str) {
            let flags = nwg::TrayNotificationFlags::USER_ICON | nwg::TrayNotificationFlags::LARGE_ICON;
            let idx = match self.current_icon.get().unwrap_or(IconState::Down) {
                IconState::Ok => 0,
                IconState::Warn => 1,
                IconState::Err => 2,
                IconState::Down => 3,
            };
            self.tray.show(text, Some(title), Some(flags), Some(&self.icons[idx]));
        }

        fn show_test_erp(app: &Rc<App>, result: anyhow::Result<Response>) {
            match result {
                Ok(r) if r.ok => nwg::modal_info_message(
                    &app.window,
                    "Conexión con el ERP",
                    &format!(
                        "El ERP respondió en {}.\n\nLote 1: {} productos, {} lotes en total.",
                        state::fmt_ms(r.ms),
                        r.productos.unwrap_or(0),
                        r.cantidad_lotes.unwrap_or(0)
                    ),
                ),
                Ok(r) => nwg::modal_error_message(&app.window, "Conexión con el ERP", &r.error.unwrap_or_default()),
                Err(e) => nwg::modal_error_message(&app.window, "Conexión con el ERP", &e.to_string()),
            };
        }

        // ---- menú -------------------------------------------------------------

        fn build_menu(&self) -> Result<TrayMenu, nwg::NwgError> {
            let now = chrono::Local::now();
            let report = self.report.borrow();
            let lines = state::menu_lines(report.as_ref(), now);
            let running = report.is_some();
            drop(report);

            let mut menu = nwg::Menu::default();
            nwg::Menu::builder().popup(true).parent(&self.window).build(&mut menu)?;
            let mut status = Vec::new();
            for l in lines {
                let mut it = nwg::MenuItem::default();
                nwg::MenuItem::builder().text(&l).disabled(true).parent(&menu).build(&mut it)?;
                status.push(it);
            }
            let mut seps = Vec::new();
            let mut sep = nwg::MenuSeparator::default();
            nwg::MenuSeparator::builder().parent(&menu).build(&mut sep)?;
            seps.push(sep);

            let item = |text: &str, enabled: bool| -> Result<nwg::MenuItem, nwg::NwgError> {
                let mut it = nwg::MenuItem::default();
                nwg::MenuItem::builder().text(text).disabled(!enabled).parent(&menu).build(&mut it)?;
                Ok(it)
            };
            let sync_now = item("Sincronizar ahora", running)?;
            let test_erp = item("Probar conexión con el ERP", running)?;
            let detail = item("Ver detalle…", true)?;
            let config = item("Configuración…", running)?;
            let logs = item("Abrir carpeta de logs", running)?;

            let mut sep2 = nwg::MenuSeparator::default();
            nwg::MenuSeparator::builder().parent(&menu).build(&mut sep2)?;
            seps.push(sep2);
            let exit = item("Salir del icono (el servicio sigue)", true)?;

            Ok(TrayMenu { _menu: menu, _status: status, _seps: seps, sync_now, test_erp, detail, config, logs, exit })
        }

        fn show_menu(app: &Rc<App>) {
            match app.build_menu() {
                Ok(m) => {
                    *app.menu.borrow_mut() = Some(m);
                }
                Err(e) => {
                    eprintln!("menú: {e}");
                    return;
                }
            }
            let (x, y) = nwg::GlobalCursor::position();
            // `popup` corre su propio loop de mensajes: no mantener el borrow.
            let handle = app.menu.borrow().as_ref().map(|m| m._menu.handle);
            if let Some(h) = handle {
                let menu = nwg::Menu { handle: h };
                menu.popup(x, y);
                std::mem::forget(menu); // el dueño real sigue siendo TrayMenu
            }
        }

        fn on_menu(app: &Rc<App>, handle: nwg::ControlHandle) {
            enum Action {
                SyncNow,
                TestErp,
                Detail,
                Config,
                Logs,
                Exit,
            }
            let action = {
                let m = app.menu.borrow();
                let Some(m) = m.as_ref() else { return };
                if handle == m.sync_now {
                    Action::SyncNow
                } else if handle == m.test_erp {
                    Action::TestErp
                } else if handle == m.detail {
                    Action::Detail
                } else if handle == m.config {
                    Action::Config
                } else if handle == m.logs {
                    Action::Logs
                } else if handle == m.exit {
                    Action::Exit
                } else {
                    return;
                }
            };
            match action {
                Action::SyncNow => App::spawn_job(app, JobKind::SyncNow, Request::SyncNow),
                Action::TestErp => App::spawn_job(app, JobKind::TestErp, Request::TestErp { url: None }),
                Action::Detail => App::show_detail(app),
                Action::Config => App::show_config(app),
                Action::Logs => {
                    let dir = app.report.borrow().as_ref().map(|r| r.log_dir.clone());
                    if let Some(dir) = dir {
                        let _ = std::process::Command::new("explorer.exe").arg(dir).spawn();
                    }
                }
                Action::Exit => nwg::stop_thread_dispatch(),
            }
        }

        fn show_detail(app: &Rc<App>) {
            let now = chrono::Local::now();
            let text = state::detail_text(app.report.borrow().as_ref(), now);
            let mut slot = app.detail_win.borrow_mut();
            if slot.is_none() {
                match DetailWindow::build(&app.icons[0]) {
                    Ok(w) => *slot = Some(w),
                    Err(e) => {
                        nwg::modal_error_message(&app.window, "Remedia Agent", &format!("No se pudo abrir la ventana: {e}"));
                        return;
                    }
                }
            }
            slot.as_ref().unwrap().show(&text);
        }

        fn show_config(app: &Rc<App>) {
            let report = app.report.borrow().clone();
            let Some(report) = report else { return };
            let mut slot = app.config_win.borrow_mut();
            if slot.is_none() {
                match ConfigWindow::build(app, &app.icons[0]) {
                    Ok(w) => *slot = Some(w),
                    Err(e) => {
                        nwg::modal_error_message(&app.window, "Remedia Agent", &format!("No se pudo abrir la ventana: {e}"));
                        return;
                    }
                }
            }
            slot.as_ref().unwrap().show(&report);
        }
    }
}
