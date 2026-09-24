"use strict";
// Radar: Consola KIS (C1, C2, C4) y P2/P3 del dueño. Sin dependencias.
// Todo dato del servidor se escribe con textContent o setAttribute; nunca se arma HTML.
(function () {
  const modo = document.body.dataset.modo;            // "consola" | "cliente"
  const $ = (id) => document.getElementById(id);
  const REFRESCO_ESTADO_MS = 3000;
  const REFRESCO_LINEAS_MS = 15000;
  const RENUEVA_QR_S = 15;
  let actual = null;                                  // {tenant_id, line_id, nombre}
  let sondeo = null;
  let qrDesde = 0;

  const MENSAJES = {
    // Sin promesa de aviso: en este tramo nada avisa cuando se libera lugar.
    sin_capacidad: "En este momento no hay lugar para una conexión nueva. Probá de nuevo más tarde o escribinos.",
    tenant_inexistente: "El cliente no existe.",
    sin_vinculo: "Esta línea no tiene una conexión.",
    sin_consentimiento: "Falta el consentimiento de esta línea.",
    consentimiento_no_aceptado: "Hay que marcar la aceptación del texto.",
    restriccion_activa: "La cuenta tiene una restricción activa: no se puede volver a vincular.",
    sin_reinicios: "Se agotaron los reintentos. Escribinos y te ayudamos.",
    codigo_no_disponible: "No se pudo generar el código. Seguí con el QR.",
    telefono_invalido: "Revisá el número: formato internacional, por ejemplo +54 9 341 123 4567.",
    vinculo_activo: "Esta línea ya tiene una conexión en curso.",
    config_no_coincide: "No pudimos preparar la conexión segura. Ya avisamos al equipo.",
    waha_error: "El servidor de conexión no respondió. Probá de nuevo en unos minutos.",
    waha_sin_configurar: "El servidor de conexión no está configurado.",
    falta_confirmacion: "Falta confirmar la acción.",
    confirmacion_incorrecta: "El nombre no coincide con el de la línea.",
    sin_dueno: "La cuenta no tiene un dueño cargado.",
    linea_invalida: "El link no indica una línea válida.",
  };
  const TEXTOS_ESTADO = {
    sin_vinculo: "Sin conexión.",
    creando: "Preparando conexión segura…",
    esperando_qr: "Escaneá el código con el teléfono de la línea.",
    vinculado: "Conectado.",
    caido: "Tu WhatsApp se desconectó.",
    cerrando: "Desconectando…",
    cerrado: "Desconectado. El tablero y lo ya importado se conservan según la configuración de la línea.",
    abortado: "No se pudo preparar la conexión.",
  };

  function mostrarError(e) {
    $("error").textContent = e ? (MENSAJES[e.message] || ("Error: " + e.message)) : "";
  }

  function base() {
    if (modo === "consola") {
      return "/radar/admin/tenants/" + encodeURIComponent(actual.tenant_id) + "/lineas/" + encodeURIComponent(actual.line_id);
    }
    return "/radar/api/lineas/" + encodeURIComponent(actual.line_id);
  }

  async function pedir(metodo, url, cuerpo) {
    const opciones = { method: metodo, credentials: "same-origin", headers: {} };
    if (cuerpo !== undefined) {
      opciones.headers["Content-Type"] = "application/json";
      opciones.body = JSON.stringify(cuerpo);
    }
    const r = await fetch(url, opciones);
    let datos = null;
    try { datos = await r.json(); } catch (e) { datos = null; }
    if (!r.ok) {
      const d = datos && datos.detail;
      throw new Error((d && d.error) || (typeof d === "string" ? d : "error_" + r.status));
    }
    return datos;
  }

  async function accion(fn) {
    try { await fn(); mostrarError(null); } catch (e) { mostrarError(e); }
  }

  function enlazar(id, fn) {
    const el = $(id);
    if (el) el.addEventListener("click", () => accion(fn));
  }

  // ---- P2: consentimiento (asistido en la Consola, propio en la pantalla del dueño)
  async function cargarConsentimiento() {
    const url = modo === "consola" ? base() + "/consentimiento-asistido/texto" : base() + "/vinculo/texto-consentimiento";
    const t = await pedir("GET", url);
    $("texto-consentimiento").textContent = t.texto;
    $("parametros").textContent = "Parámetros de esta línea: " + JSON.stringify(t.parametros_linea);
    $("version").value = t.version;
  }

  async function consentir() {
    if (!$("acepto").checked) throw new Error("consentimiento_no_aceptado");
    if (modo === "consola") {
      const r = await pedir("POST", base() + "/consentimiento-asistido", {
        version_texto: $("version").value, titular_leyo_y_acepto: true,
        modo: $("modo").value, nombre: $("nombre").value.trim(),
      });
      $("aviso").textContent = r.copia_enviada
        ? "Consentimiento registrado. Se envió una copia al dueño."
        : "Consentimiento registrado. La copia por email no salió: reenviala a mano.";
    } else {
      await pedir("POST", base() + "/consentimientos", { version_texto: $("version").value, acepta: true, titular: true });
    }
    await refrescarEstado();
  }

  // ---- P3: estado del vínculo, QR, código
  function diaMes(iso) {
    // "DD/MM" en hora de Argentina (Estados especiales: "se desconectó el DD/MM").
    return new Date(iso).toLocaleDateString("es-AR", {
      day: "2-digit", month: "2-digit", timeZone: "America/Argentina/Buenos_Aires",
    });
  }

  function mostrarEstado(e) {
    actual.nombre = e.linea_nombre;
    let texto = TEXTOS_ESTADO[e.estado] || e.estado;
    if (e.estado === "caido" && e.caido_desde) {
      texto = "Tu WhatsApp se desconectó el " + diaMes(e.caido_desde) + ". Tocá «Reconectar».";
    }
    if (e.estado === "esperando_qr") texto = "Abrí WhatsApp → Dispositivos vinculados → Vincular un dispositivo → Escaneá.";
    if (e.estado === "vinculado" && e.numero) {
      texto = "Conectado: " + e.numero + ". ¿Es esta la línea del negocio? Si no lo es, usá «Desconectar y borrar todo».";
    }
    if (e.qr_vencido) texto = e.reinicios_restantes > 0 ? "El código venció. Generá uno nuevo." : MENSAJES.sin_reinicios;
    if (e.passkey) texto = "WhatsApp pide una verificación adicional que todavía no soportamos desde acá. Escribinos y te ayudamos.";
    if (e.restriccion_activa) texto += " " + MENSAJES.restriccion_activa;
    $("estado-texto").textContent = texto;

    const puedeVincular = ["sin_vinculo", "cerrado", "abortado", "caido"].includes(e.estado) && !e.restriccion_activa;
    $("paso-consentimiento").hidden = !(puedeVincular && !e.tiene_consentimiento);
    $("paso-vincular").hidden = !(puedeVincular && e.tiene_consentimiento);
    $("btn-generar").textContent = e.estado === "caido" ? "Reconectar" : "Generar QR";

    const conQr = e.qr_disponible === true;
    $("qr").hidden = !conQr;
    $("cuenta").hidden = !conQr;
    if (conQr && (!$("qr").getAttribute("src") || Date.now() - qrDesde > RENUEVA_QR_S * 1000)) {
      $("qr").setAttribute("src", base() + "/vinculo/qr?r=" + Date.now());
      qrDesde = Date.now();
    }
    if (!conQr) $("qr").removeAttribute("src");
    $("btn-reiniciar").hidden = !(e.qr_vencido && e.reinicios_restantes > 0);
    $("reinicios").textContent = e.estado === "esperando_qr" ? "Reintentos disponibles: " + e.reinicios_restantes : "";
    $("btn-desconectar").hidden = !["creando", "esperando_qr", "vinculado", "caido"].includes(e.estado);
  }

  async function refrescarEstado() {
    const e = await pedir("GET", base() + "/vinculo");
    mostrarEstado(e);
    if (["sin_vinculo", "cerrado", "abortado"].includes(e.estado) && sondeo) {
      clearInterval(sondeo);
      sondeo = null;
    }
    return e;
  }

  function iniciarSondeo() {
    if (sondeo) clearInterval(sondeo);
    sondeo = setInterval(() => { refrescarEstado().catch(mostrarError); }, REFRESCO_ESTADO_MS);
  }

  setInterval(() => {
    const cuenta = $("cuenta");
    if (cuenta && !cuenta.hidden) {
      const resta = Math.max(0, RENUEVA_QR_S - Math.floor((Date.now() - qrDesde) / 1000));
      cuenta.textContent = "El código se renueva solo en " + resta + " s.";
    }
  }, 1000);

  async function generar() {
    mostrarEstado(await pedir("POST", base() + "/vinculo", { full_sync: $("full-sync").checked }));
    iniciarSondeo();
  }

  async function reiniciar() {
    mostrarEstado(await pedir("POST", base() + "/vinculo/reiniciar-qr", {}));
  }

  async function pedirCodigo() {
    const r = await pedir("POST", base() + "/vinculo/codigo", { telefono: $("telefono").value.trim() });
    $("codigo").textContent = "Código: " + r.codigo + ". En el teléfono: Dispositivos vinculados → Vincular con número de teléfono.";
  }

  async function desconectar() {
    if (!window.confirm("¿Desconectar esta línea de WhatsApp? El tablero y lo ya importado se conservan.")) return;
    await pedir("POST", base() + "/vinculo/desconectar", { confirmar: true });
    $("aviso").textContent = "Desconexión pedida: la sesión se borra en unos minutos.";
    await refrescarEstado();
    iniciarSondeo();
  }

  async function borrarTodo() {
    if (!window.confirm("Desconectar y borrar todo: se borra la conexión y se pide borrar los datos de esta línea. ¿Seguir?")) return;
    const nombre = window.prompt("Para confirmar, escribí el nombre de la línea: " + actual.nombre);
    if (nombre === null) return;
    await pedir("POST", base() + "/vinculo/desconectar-y-borrar", { confirmar: true, nombre_linea: nombre });
    $("aviso").textContent = "Pedido registrado: la conexión se borra en minutos; el borrado de los datos de la línea queda en curso.";
    await refrescarEstado();
    iniciarSondeo();
  }

  // ---- C1 y C4 (solo Consola)
  const COLUMNAS = ["semaforo", "tenant_nombre", "line_nombre", "numero", "line_estado", "waha_status", "observado_hasta",
                    "ultimo_mensaje", "sincronizacion", "huecos", "salud", "worker", "gasto_ia_mes"];

  function celda(valor) {
    const td = document.createElement("td");
    td.textContent = (valor === null || valor === undefined || valor === "") ? "—" : String(valor);
    return td;
  }

  function pintarTabla(filas) {
    const cuerpo = $("lineas");
    cuerpo.replaceChildren();
    for (const f of filas) {
      const tr = document.createElement("tr");
      tr.className = "semaforo-" + f.semaforo;
      for (const c of COLUMNAS) tr.appendChild(celda(f[c]));
      const td = document.createElement("td");
      const boton = document.createElement("button");
      boton.type = "button";
      boton.textContent = "Operar";
      boton.addEventListener("click", () => abrirPanel(f));
      td.appendChild(boton);
      tr.appendChild(td);
      cuerpo.appendChild(tr);
    }
  }

  function llenarClientes(filas) {
    const sel = $("filtro-cliente");
    if (sel.options.length > 1) return;
    const vistos = new Map();
    for (const f of filas) vistos.set(f.tenant_id, f.tenant_nombre);
    for (const [id, nombre] of vistos) {
      const op = document.createElement("option");
      op.value = id;
      op.textContent = nombre;
      sel.appendChild(op);
    }
  }

  async function cargarLineas() {
    const p = new URLSearchParams();
    if ($("filtro-estado").value) p.set("estado", $("filtro-estado").value);
    if ($("filtro-cliente").value) p.set("tenant_id", $("filtro-cliente").value);
    const filas = await pedir("GET", "/radar/admin/consola/lineas" + (p.toString() ? "?" + p.toString() : ""));
    pintarTabla(filas);
    llenarClientes(filas);
  }

  function abrirPanel(f) {
    actual = { tenant_id: f.tenant_id, line_id: f.line_id, nombre: f.line_nombre };
    $("panel").hidden = false;
    $("panel-titulo").textContent = f.tenant_nombre + " — " + f.line_nombre;
    $("codigo").textContent = "";
    $("aviso").textContent = "";
    $("qr").removeAttribute("src");
    qrDesde = 0;
    cargarConsentimiento().catch(mostrarError);
    refrescarEstado().then(iniciarSondeo).catch(mostrarError);
  }

  async function marcarRestriccion() {
    const valor = $("restriccion-hasta").value;
    mostrarEstado(await pedir("PUT", base() + "/vinculo/restriccion", { hasta: valor ? new Date(valor).toISOString() : null }));
  }

  async function levantarRestriccion() {
    mostrarEstado(await pedir("DELETE", base() + "/vinculo/restriccion"));
  }

  async function cerrarPanel() {
    $("panel").hidden = true;
    if (sondeo) clearInterval(sondeo);
    sondeo = null;
    actual = null;
  }

  // ---- arranque
  enlazar("btn-consentir", consentir);
  enlazar("btn-generar", generar);
  enlazar("btn-reiniciar", reiniciar);
  enlazar("btn-codigo", pedirCodigo);
  enlazar("btn-desconectar", desconectar);
  enlazar("btn-borrar", borrarTodo);

  if (modo === "consola") {
    enlazar("btn-restriccion", marcarRestriccion);
    enlazar("btn-levantar", levantarRestriccion);
    enlazar("btn-refrescar", cargarLineas);
    enlazar("btn-cerrar-panel", cerrarPanel);
    $("filtro-estado").addEventListener("change", () => accion(cargarLineas));
    $("filtro-cliente").addEventListener("change", () => accion(cargarLineas));
    accion(cargarLineas);
    setInterval(() => { cargarLineas().catch(mostrarError); }, REFRESCO_LINEAS_MS);
  } else {
    const linea = new URLSearchParams(window.location.search).get("linea") || "";
    if (!/^[0-9a-f-]{36}$/.test(linea)) {
      mostrarError(new Error("linea_invalida"));
      return;
    }
    actual = { line_id: linea, nombre: "" };
    cargarConsentimiento().catch(mostrarError);
    refrescarEstado().then(iniciarSondeo).catch(mostrarError);
  }
})();
