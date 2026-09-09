# remedia-agent — Tray de estado y configuración

Icono en la bandeja del sistema de la PC de la farmacia que muestra el estado del
agente (`remedia-agent`, servicio de Windows) y permite cambiar token, URL del ERP
y URL de Remedia sin consola ni permisos de administrador.

Complementa [`2026-09-08-remedia-agent-design.md`](2026-09-08-remedia-agent-design.md).
Fecha: 2026-09-09.

---

## 1. Decisiones acordadas

| # | Decisión | Por qué |
|---|---|---|
| T1 | **Mismo `agent.exe`, subcomando `tray`.** | Un solo archivo para distribuir. |
| T2 | **Tray y servicio hablan por un named pipe local** `\\.\pipe\RemediaAgent`. | El servicio corre en sesión 0 y no puede dibujar UI. Un pipe no es una conexión de red: se mantiene "nada entrante". |
| T3 | **Cambios de configuración sin admin.** El tray manda `set_config` por el pipe; el servicio valida, escribe `agent.toml` y lo aplica en caliente. | El personal no debe tocar `C:\ProgramData` ni reiniciar servicios. |
| T4 | **Puede mirarlo el personal de la farmacia.** Textos simples en castellano; "Configuración…" pide confirmación antes de guardar. | Lo pidió Mariano. |
| T5 | **Arranca solo para todos los usuarios de la PC** (`HKLM\...\Run`). | La PC de la farmacia se comparte. |
| T6 | **El servicio mide latencias** (`Metrics`) y las expone por el pipe, en `status` y en el heartbeat. | Hoy no se mide nada; "milisegundos" fue un pedido explícito. |
| T7 | **Stack: `native-windows-gui`** (tray, menú, ventana de configuración, diálogos). | Rust puro, solo Windows, que es nuestro único target. |
| T8 | Guardar una URL de ERP que no responde **se permite con aviso**; guardar un token o URL de Remedia que no autentica **se rechaza**. | El ERP puede estar apagado legítimamente; un token malo deja al agente mudo. |

---

## 2. Named pipe: protocolo

- Nombre: `\\.\pipe\RemediaAgent`. Lo crea el servicio (también `agent.exe run`).
- Seguridad: DACL explícita por SDDL `D:(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGW;;;AU)`:
  SYSTEM y administradores todo, **usuarios autenticados lectura/escritura**. Sin
  esto el DACL por defecto no deja escribir a usuarios comunes.
- Una request por conexión: el cliente escribe una línea JSON, el servidor responde
  una línea JSON y cierra. Timeout de 10 s por lado (`test_*` puede tardar).
- Modo `message` del pipe, máximo 64 KB por mensaje.

```jsonc
// → { "cmd": "status" }
// ← StatusReport (ver §3)

// → { "cmd": "sync_now" }
// ← { "ok": true }

// → { "cmd": "test_erp", "url": "http://192.168.1.156:60064" }   // url opcional: default la configurada
// ← { "ok": true, "ms": 312, "productos": 1107, "cantidad_lotes": 49 }
// ← { "ok": false, "error": "ERP no autorizado (flag API_Productos deshabilitado)", "status": "no_autorizado" }

// → { "cmd": "test_remedia", "url": "https://api.remedia.ar", "token": "..." }  // ambos opcionales
// ← { "ok": true, "ms": 180 }
// ← { "ok": false, "error": "Remedia HTTP 401: token inválido" }

// → { "cmd": "set_config", "token": "...", "erp_url": "...", "remedia_url": "..." }  // todos opcionales
// ← { "ok": true, "warnings": ["El ERP no respondió en http://...: se guardó igual"] }
// ← { "ok": false, "error": "Remedia rechazó el token (HTTP 401)" }
```

`set_config`:
1. Valida sintaxis: URLs `http(s)://`, token no vacío.
2. Si cambia `token` o `remedia_url`: heartbeat de prueba contra la nueva
   combinación. Falla → `ok: false`, no se guarda nada.
3. Si cambia `erp_url`: `GET /lote/1` de prueba. Falla → se guarda con `warning`.
4. Escribe `agent.toml` (`Config::write`), reemplaza en caliente el adapter del
   ERP y el cliente de Remedia (`ArcSwap`), y reinicia la tarea del websocket con
   la nueva URL/token. El loop de sync toma los nuevos objetos en el próximo ciclo.
5. Loguea `config cambiada desde el tray: campos=[token, erp_url]` sin valores.

Errores del pipe (JSON inválido, comando desconocido) → `{ "ok": false, "error": "..." }`.

---

## 3. `StatusReport` y métricas

```jsonc
{
  "agent_version": "0.2.0",
  "branch_id": "farmacia-mutual",
  "erp_url": "http://192.168.1.156:60064",
  "remedia_url": "https://api.remedia.ar",
  "config_path": "C:\\ProgramData\\RemediaAgent\\agent.toml",
  "log_dir": "C:\\ProgramData\\RemediaAgent\\logs",
  "erp_status": "ok",                 // ok | no_autorizado | inalcanzable | error | desconocido
  "erp_version": null,
  "ws_connected": true,
  "last_sync_ok_at": "2026-09-09T10:15:00-03:00",
  "last_sync_at": "2026-09-09T10:15:00-03:00",
  "last_sync_fetched": 54235,
  "last_sync_changed": 37,
  "catalog_count": 54235,
  "pending_batches": 0,
  "last_heartbeat_at": "...",
  "last_error": null,
  "metrics": {
    "erp_fetch_ms": 14820,            // último fetch_all completo
    "erp_lote_avg_ms": 302,           // promedio por lote en ese fetch
    "remedia_push_avg_ms": 240,       // promedio por lote enviado en el último ciclo
    "sync_total_ms": 15400,
    "lookup_last_ms": 85,             // último lookup en vivo (ERP)
    "lookup_avg_ms": 90,              // media móvil (α = 0.2)
    "heartbeat_ms": 180,
    "ws_reconnects": 2,               // desde el arranque
    "uptime_secs": 86400
  }
}
```

`Metrics` (`src/metrics.rs`): `Arc<Metrics>` compartido, `Mutex<Inner>` con los
campos de arriba, `AtomicBool ws_connected`, `AtomicU64 ws_reconnects`. Métodos
`record_fetch(ms, lotes)`, `record_push(ms)`, `record_sync_total(ms)`,
`record_lookup(ms)`, `record_heartbeat(ms)`, `snapshot() -> MetricsSnapshot`.
Al final de cada ciclo se persiste `snapshot()` como JSON en `meta.metrics_json`
para que `agent.exe status` lo muestre aunque el servicio esté detenido. El
heartbeat suma un campo `metrics: MetricsSnapshot` (Remedia lo ignora hasta que
lo use; Pydantic descarta campos extra).

Para medir por lote, `ErpAdapter::fetch_all` pasa a devolver `FetchResult
{ productos, lotes: u32 }`; el engine mide el total y divide.

---

## 4. Tray (`agent.exe tray`)

- Proceso del usuario. Icono en la bandeja con **tres estados de color**:
  verde (ERP ok, websocket conectado, sin pendientes), amarillo (pendientes,
  websocket caído o servicio sin responder hace < 2 min), rojo (ERP no
  autorizado/inalcanzable, o servicio sin responder). Iconos `.ico` embebidos
  (`assets/tray_ok.ico`, `tray_warn.ico`, `tray_err.ico`, generados en el repo).
- Refresco: `status` por el pipe cada 5 s (timer). Si el pipe no existe →
  "Servicio detenido".
- Tooltip: `Remedia · ERP ok · sincronizado hace 3 min`.
- Menú (clic derecho), ítems deshabilitados como texto de estado:

```
Remedia Agent 0.2.0 — farmacia-mutual
ERP: ok (14,8 s la última lectura completa)
Remedia: conectado · último sync 10:15 (37 cambios)
Pendientes: 0
───────────
Sincronizar ahora
Probar conexión con el ERP
Ver detalle…
Configuración…
Abrir carpeta de logs
───────────
Salir
```

- **Ver detalle…**: ventana con texto de solo lectura (todo el `StatusReport`
  formateado, métricas en ms) y botón "Copiar".
- **Configuración…**: ventana con tres campos — URL de Remedia, URL del ERP,
  Token (campo tipo contraseña, vacío = no cambiar) — botones "Probar" (llama
  `test_remedia` / `test_erp` con lo tipeado y muestra ms o error) y "Guardar".
  Guardar pide confirmación: "Vas a cambiar la configuración del agente. Si el
  dato es incorrecto la farmacia deja de sincronizar. ¿Continuar?". Resultado
  con `MessageBox`; warnings del ERP se muestran como aviso, no como error.
- **Sincronizar ahora**: `sync_now`, aviso "Sincronización pedida" (globo).
- **Probar conexión con el ERP**: `test_erp` con la URL actual; muestra ms y
  productos del lote 1, o el error en castellano.
- "Salir" cierra solo el tray; el servicio sigue. Vuelve al iniciar sesión.
- Una sola instancia: mutex con nombre `Local\RemediaAgentTray`.

Textos orientados a personal no técnico: "no autorizado" se explica como "el
ERP tiene deshabilitada la API de productos (configuración de ObServer)".

---

## 5. Instalación

- `agent.exe install` además: escribe `HKLM\Software\Microsoft\Windows\CurrentVersion\Run\RemediaAgentTray = "C:\ProgramData\RemediaAgent\agent.exe" tray` y lanza el tray para el usuario actual.
- `agent.exe uninstall` borra la clave y cierra el tray si está corriendo (evento con nombre `Global\RemediaAgentTrayQuit`).
- `agent.exe run` (foreground) también abre el pipe, para probar el tray sin instalar.

---

## 6. Estructura

```
remedia-agent/
├── assets/tray_ok.ico, tray_warn.ico, tray_err.ico
├── src/
│   ├── metrics.rs         # Metrics + MetricsSnapshot
│   ├── ipc/
│   │   ├── mod.rs         # protocolo: Request, Response, StatusReport (serde)
│   │   ├── server.rs      # named pipe server (tokio), dispatch a Runtime
│   │   └── client.rs      # call(Request) -> Response, usado por tray y por `status`
│   ├── runtime.rs         # Runtime: config + ArcSwap<adapter>, ArcSwap<RemediaClient>, engine, metrics, ws task; set_config/test_*
│   ├── service.rs         # run_agent usa Runtime; install/uninstall + Run key + tray
│   └── tray/
│       ├── mod.rs         # main loop nwg, icono, menú, timer
│       ├── config_window.rs
│       └── detail_window.rs
└── tests/
    ├── ipc_pipe.rs        # server+client reales por pipe: status, sync_now, set_config con mock Remedia/ERP
    └── metrics.rs
```

Crates nuevos: `native-windows-gui` + `native-windows-derive`, `arc-swap`,
`winreg`, `windows-sys` (SDDL → SECURITY_ATTRIBUTES, mutex/evento con nombre).
`tokio` suma la feature `net` (ya está) para `tokio::net::windows::named_pipe`.

---

## 7. Casos

- Servicio detenido → tray en rojo "Servicio detenido"; las acciones muestran "El servicio no está corriendo".
- Dos trays (dos sesiones) → cada uno habla con el mismo pipe; ambos ven lo mismo.
- `set_config` mientras corre un sync → el ciclo en curso termina con los objetos viejos; el siguiente usa los nuevos. El websocket se reconecta de inmediato.
- Token rotado desde el panel de Remedia → heartbeat 401 → `erp_status` sigue `ok` pero `last_error` dice "Remedia HTTP 401" y el icono pasa a rojo; el tray sugiere "Configuración…".
- Usuario sin permisos sobre el pipe (DACL) → error claro "Sin permiso para hablar con el servicio"; no debería pasar con el SDDL de §2.
- `agent.exe status` por consola pasa a usar el pipe si está disponible (datos vivos) y cae a `state.sqlite` si no.

---

## 8. Tests

- Unit: serde del protocolo; `Metrics` (media móvil, snapshot); mapeo de color del icono a partir de `StatusReport` (`fn icon_state(&StatusReport) -> IconState`, puro).
- Integración (Windows): levantar `ipc::server` con un `Runtime` de prueba (mock ERP y Remedia con wiremock), conectar con `ipc::client`: `status` devuelve `branch_id`; `sync_now` dispara el canal; `test_erp` devuelve ms y productos; `set_config` con token inválido → `ok:false` y `agent.toml` intacto; con token válido → `agent.toml` actualizado y `RemediaClient` reemplazado (el siguiente heartbeat va a la URL nueva).
- El tray no se testea automáticamente; verificación manual: arrancar `agent.exe run --data-dir X` y `agent.exe tray --data-dir X` y recorrer el menú.
