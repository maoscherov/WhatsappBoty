//! Ventana "Ver detalle…": todo el estado en texto, con botón Copiar.

use native_windows_gui as nwg;
use std::cell::RefCell;

pub struct DetailWindow {
    window: nwg::Window,
    text: nwg::TextBox,
    copy_btn: nwg::Button,
    close_btn: nwg::Button,
    _handler: RefCell<Option<nwg::EventHandler>>,
}

impl DetailWindow {
    pub fn build(icon: &nwg::Icon) -> Result<DetailWindow, nwg::NwgError> {
        let mut window = nwg::Window::default();
        nwg::Window::builder()
            .size((560, 520))
            .position((320, 200))
            .title("Remedia Agent — Detalle")
            .icon(Some(icon))
            .flags(nwg::WindowFlags::WINDOW)
            .build(&mut window)?;

        let mut text = nwg::TextBox::default();
        nwg::TextBox::builder()
            .position((12, 12))
            .size((536, 450))
            .readonly(true)
            .flags(nwg::TextBoxFlags::VISIBLE | nwg::TextBoxFlags::VSCROLL | nwg::TextBoxFlags::AUTOVSCROLL)
            .parent(&window)
            .build(&mut text)?;
        // `Font` no libera el handle al soltarse: el control lo sigue usando.
        let mut mono = nwg::Font::default();
        if nwg::Font::builder().family("Consolas").size(15).build(&mut mono).is_ok() {
            text.set_font(Some(&mono));
        }

        let mut copy_btn = nwg::Button::default();
        nwg::Button::builder().text("Copiar").position((338, 472)).size((100, 32)).parent(&window).build(&mut copy_btn)?;
        let mut close_btn = nwg::Button::default();
        nwg::Button::builder().text("Cerrar").position((448, 472)).size((100, 32)).parent(&window).build(&mut close_btn)?;

        let win = DetailWindow { window, text, copy_btn, close_btn, _handler: RefCell::new(None) };
        win.bind();
        Ok(win)
    }

    fn bind(&self) {
        use nwg::Event as E;
        let window_handle = self.window.handle;
        let text_handle = self.text.handle;
        let copy = self.copy_btn.handle;
        let close = self.close_btn.handle;
        let handler = nwg::full_bind_event_handler(&self.window.handle, move |evt, data, handle| match evt {
            E::OnWindowClose if handle == window_handle => {
                if let nwg::EventData::OnWindowClose(d) = data {
                    d.close(false);
                }
                let w = nwg::Window { handle: window_handle };
                w.set_visible(false);
                std::mem::forget(w);
            }
            E::OnButtonClick if handle == close => {
                let w = nwg::Window { handle: window_handle };
                w.set_visible(false);
                std::mem::forget(w);
            }
            E::OnButtonClick if handle == copy => {
                let t = nwg::TextBox { handle: text_handle };
                nwg::Clipboard::set_data_text(window_handle, &t.text());
                std::mem::forget(t);
            }
            _ => {}
        });
        *self._handler.borrow_mut() = Some(handler);
    }

    pub fn show(&self, text: &str) {
        self.text.set_text(text);
        self.window.set_visible(true);
        self.window.set_focus();
    }

    /// Refresca el texto solo si la ventana está visible.
    pub fn update(&self, text: &str) {
        if self.window.visible() {
            self.text.set_text(text);
        }
    }
}
