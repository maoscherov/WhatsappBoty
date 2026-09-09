//! Ventana "Configuración…": URL de Remedia, URL del ERP y token.

use super::imp::{App, JobKind};
use super::state;
use crate::ipc::{Request, Response, StatusReport};
use native_windows_gui as nwg;
use std::cell::RefCell;
use std::rc::{Rc, Weak};

pub struct ConfigWindow {
    window: nwg::Window,
    remedia_url: nwg::TextInput,
    erp_url: nwg::TextInput,
    token: nwg::TextInput,
    status: nwg::Label,
    test_btn: nwg::Button,
    save_btn: nwg::Button,
    cancel_btn: nwg::Button,
    _labels: Vec<nwg::Label>,
    _handler: RefCell<Option<nwg::EventHandler>>,
}

const HINT: &str = "Las direcciones son solo el servidor (sin /bo/... ni /api/...).\r\nProbá antes de guardar. Si el ERP está apagado, igual se puede guardar.";

const CONFIRM: &str ="Vas a cambiar la configuración del agente.\n\nSi un dato es incorrecto, la farmacia deja de sincronizar con Remedia. Este cambio normalmente lo indica soporte.\n\n¿Continuar?";

impl ConfigWindow {
    pub fn build(app: &Rc<App>, icon: &nwg::Icon) -> Result<ConfigWindow, nwg::NwgError> {
        let mut window = nwg::Window::default();
        // Medidas holgadas: con escala de pantalla al 125 % los textos crecen
        // pero las posiciones no.
        nwg::Window::builder()
            .size((640, 330))
            .position((300, 300))
            .title("Remedia Agent — Configuración")
            .icon(Some(icon))
            .flags(nwg::WindowFlags::WINDOW)
            .build(&mut window)?;

        let mut labels = Vec::new();
        let mut label = |text: &str, y: i32| -> Result<(), nwg::NwgError> {
            let mut l = nwg::Label::default();
            nwg::Label::builder().text(text).position((16, y)).size((200, 26)).parent(&window).build(&mut l)?;
            labels.push(l);
            Ok(())
        };
        label("Dirección de Remedia", 20)?;
        label("Dirección del ERP", 62)?;
        label("Token de la sucursal", 104)?;

        let mut remedia_url = nwg::TextInput::default();
        nwg::TextInput::builder()
            .position((220, 16))
            .size((400, 28))
            .placeholder_text(Some("https://cerca.remedia.ar (solo el servidor)"))
            .parent(&window)
            .build(&mut remedia_url)?;
        let mut erp_url = nwg::TextInput::default();
        nwg::TextInput::builder()
            .position((220, 58))
            .size((400, 28))
            .placeholder_text(Some("http://192.168.1.156:60064"))
            .parent(&window)
            .build(&mut erp_url)?;
        let mut token = nwg::TextInput::default();
        nwg::TextInput::builder()
            .position((220, 100))
            .size((400, 28))
            .password(Some('•'))
            .placeholder_text(Some("vacío = no cambiar"))
            .parent(&window)
            .build(&mut token)?;

        let mut status = nwg::Label::default();
        nwg::Label::builder()
            .text(HINT)
            .position((16, 146))
            .size((604, 110))
            .parent(&window)
            .build(&mut status)?;

        let mut test_btn = nwg::Button::default();
        nwg::Button::builder().text("Probar").position((260, 270)).size((110, 36)).parent(&window).build(&mut test_btn)?;
        let mut save_btn = nwg::Button::default();
        nwg::Button::builder().text("Guardar").position((385, 270)).size((110, 36)).parent(&window).build(&mut save_btn)?;
        let mut cancel_btn = nwg::Button::default();
        nwg::Button::builder().text("Cancelar").position((510, 270)).size((110, 36)).parent(&window).build(&mut cancel_btn)?;

        let win = ConfigWindow {
            window,
            remedia_url,
            erp_url,
            token,
            status,
            test_btn,
            save_btn,
            cancel_btn,
            _labels: labels,
            _handler: RefCell::new(None),
        };
        win.bind(Rc::downgrade(app));
        Ok(win)
    }

    fn bind(&self, app: Weak<App>) {
        use nwg::Event as E;
        let window_handle = self.window.handle;
        let test = self.test_btn.handle;
        let save = self.save_btn.handle;
        let cancel = self.cancel_btn.handle;
        let handler = nwg::full_bind_event_handler(&self.window.handle, move |evt, data, handle| {
            let Some(app) = app.upgrade() else { return };
            match evt {
                E::OnWindowClose if handle == window_handle => {
                    if let nwg::EventData::OnWindowClose(d) = data {
                        d.close(false);
                    }
                    if let Some(w) = app.config_win.borrow().as_ref() {
                        w.window.set_visible(false);
                    }
                }
                E::OnButtonClick if handle == cancel => {
                    if let Some(w) = app.config_win.borrow().as_ref() {
                        w.window.set_visible(false);
                    }
                }
                E::OnButtonClick if handle == test => {
                    let reqs = app.config_win.borrow().as_ref().map(|w| w.test_requests());
                    if let Some((rem, erp)) = reqs {
                        if let Some(w) = app.config_win.borrow().as_ref() {
                            w.status.set_text("Probando…");
                            w.set_busy(true);
                        }
                        App::spawn_job(&app, JobKind::ConfigTestRemedia, rem);
                        App::spawn_job(&app, JobKind::ConfigTestErp, erp);
                    }
                }
                E::OnButtonClick if handle == save => {
                    let req = app.config_win.borrow().as_ref().map(|w| w.save_request());
                    let Some(req) = req else { return };
                    let params = nwg::MessageParams {
                        title: "Confirmar cambio",
                        content: CONFIRM,
                        buttons: nwg::MessageButtons::YesNo,
                        icons: nwg::MessageIcons::Warning,
                    };
                    if nwg::modal_message(window_handle, &params) != nwg::MessageChoice::Yes {
                        return;
                    }
                    if let Some(w) = app.config_win.borrow().as_ref() {
                        w.status.set_text("Guardando…");
                        w.set_busy(true);
                    }
                    App::spawn_job(&app, JobKind::ConfigSave, req);
                }
                _ => {}
            }
        });
        *self._handler.borrow_mut() = Some(handler);
    }

    pub fn show(&self, report: &StatusReport) {
        self.remedia_url.set_text(&report.remedia_url);
        self.erp_url.set_text(&report.erp_url);
        self.token.set_text("");
        self.status.set_text(HINT);
        self.set_busy(false);
        self.window.set_visible(true);
        self.window.set_focus();
    }

    fn set_busy(&self, busy: bool) {
        self.test_btn.set_enabled(!busy);
        self.save_btn.set_enabled(!busy);
    }

    fn opt(s: String) -> Option<String> {
        let t = s.trim().to_string();
        if t.is_empty() {
            None
        } else {
            Some(t)
        }
    }

    fn test_requests(&self) -> (Request, Request) {
        (
            Request::TestRemedia { url: Self::opt(self.remedia_url.text()), token: Self::opt(self.token.text()) },
            Request::TestErp { url: Self::opt(self.erp_url.text()) },
        )
    }

    fn save_request(&self) -> Request {
        Request::SetConfig {
            token: Self::opt(self.token.text()),
            erp_url: Self::opt(self.erp_url.text()),
            remedia_url: Self::opt(self.remedia_url.text()),
        }
    }

    /// Resultado de un job de esta ventana. Devuelve `true` si hay que
    /// refrescar el estado del tray.
    pub fn on_job(&self, kind: JobKind, result: anyhow::Result<Response>) -> bool {
        match kind {
            JobKind::ConfigTestRemedia | JobKind::ConfigTestErp => {
                let who = if kind == JobKind::ConfigTestRemedia { "Remedia" } else { "ERP" };
                let line = match result {
                    Ok(r) if r.ok => format!("{who}: OK ({})", state::fmt_ms(r.ms)),
                    Ok(r) => format!("{who}: {}", r.error.unwrap_or_default()),
                    Err(e) => format!("{who}: {e}"),
                };
                let prev = self.status.text();
                let prev = if prev.starts_with("Probando") { String::new() } else { prev };
                let text = if prev.is_empty() { line } else { format!("{prev}\r\n{line}") };
                self.status.set_text(&text);
                self.set_busy(false);
                false
            }
            JobKind::ConfigSave => {
                self.set_busy(false);
                match result {
                    Ok(r) if r.ok => {
                        let msg = if r.warnings.is_empty() {
                            "Configuración guardada y aplicada.".to_string()
                        } else {
                            format!("Configuración guardada con avisos:\n\n{}", r.warnings.join("\n"))
                        };
                        nwg::modal_info_message(&self.window, "Configuración", &msg);
                        self.window.set_visible(false);
                    }
                    Ok(r) => {
                        let e = r.error.unwrap_or_default();
                        self.status.set_text(&format!("No se guardó: {e}"));
                        nwg::modal_error_message(&self.window, "Configuración", &format!("No se guardó.\n\n{e}"));
                    }
                    Err(e) => {
                        self.status.set_text(&format!("No se guardó: {e}"));
                        nwg::modal_error_message(&self.window, "Configuración", &format!("No se guardó.\n\n{e}"));
                    }
                }
                true
            }
            _ => false,
        }
    }
}
