"use strict";
// Radar: login, Consola KIS (C1, C2, C4) y P2/P3 del dueño. Sin dependencias.
// Todo dato del servidor se escribe con textContent o setAttribute; nunca se arma HTML.
(function () {
  const modo = document.body.dataset.modo;            // "login" | "consola" | "cliente"
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
    pedido_no_enviado: "No pudimos enviar el pedido. Revisá tu conexión y probá de nuevo.",
    // /radar/admin: el 422 propio de FastAPI llega sin texto (error_422) y los 404 traen un texto suelto. Los 422 que
    // sí traen texto (email inválido, valor fuera de rango…) se muestran tal cual.
    error_422: "Revisá los datos: algún campo está vacío o no tiene el formato pedido.",
    "tenant inexistente": "El cliente no existe.",
    "usuario inexistente en este tenant": "Ese email no corresponde a ningún usuario de este cliente.",
    // Servidores WAHA: la clave se prueba contra el propio WAHA antes de guardarla.
    clave_incorrecta: "WAHA rechazó la clave.",
    waha_no_responde: "No se pudo conectar con ese servidor WAHA.",
    motor_distinto: "El servidor WAHA usa otro motor.",
    worker_duplicado: "Ya hay un servidor con ese nombre.",
    worker_inexistente: "El servidor no existe.",
    disco_invalido: "Escribí los GB usados como un número, por ejemplo 3,5.",
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

  // El error va al párrafo #error de arriba, salvo que la acción pida el suyo (la sección de servidores está abajo de
  // todo y un mensaje allá arriba no se vería).
  function mostrarError(e, destino) {
    const lugar = destino ? $(destino) : $("error");
    lugar.textContent = e ? (MENSAJES[e.message] || ("Error: " + e.message)) : "";
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

  async function accion(fn, destino) {
    try { await fn(); mostrarError(null, destino); } catch (e) { mostrarError(e, destino); }
  }

  function enlazar(id, fn) {
    const el = $(id);
    if (el) el.addEventListener("click", () => accion(fn));
  }

  // ---- Sesión: pedir el link (login) y salir (consola y pantalla del dueño)
  async function pedirLink() {
    $("aviso").textContent = "";
    $("btn-entrar").disabled = true;                  // un doble clic gastaría dos de los 3 pedidos por ventana
    try {
      await pedir("POST", "/radar/login", { email: $("email").value });
      // El mismo aviso exista o no el email: el servidor tampoco lo revela (siempre 202).
      $("aviso").textContent = "Si el email está registrado, te mandamos un link para entrar. Revisá tu correo.";
    } catch (e) {
      throw new Error("pedido_no_enviado");
    } finally {
      $("btn-entrar").disabled = false;
    }
  }

  async function salir() {
    try { await pedir("POST", "/radar/logout"); } catch (e) { /* sesión ya vencida o red caída: se sale igual */ }
    window.location.assign("/radar/login");
  }

  if (modo === "login") {
    // Pantalla aparte: no usa nada de lo que sigue. El envío nativo lo bloquea la CSP
    // (form-action 'none'), así que el formulario lo manda este JS.
    $("form-login").addEventListener("submit", (ev) => { ev.preventDefault(); accion(pedirLink); });
    return;
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

  // Las tres listas de clientes (filtro, alta de línea, reenvío de invitación) salen de /tenants, que trae todos los
  // clientes y no solo los que ya tienen líneas, y se rearman enteras. La primera opción ("Todos" / "Elegí un
  // cliente") es la del HTML; cada lista conserva el cliente elegido mientras ese cliente exista.
  function llenarClientes(clientes) {
    for (const sel of [$("filtro-cliente"), $("linea-cliente"), $("inv-cliente")]) {
      const elegido = sel.value;
      const opciones = clientes.map((c) => {
        const op = document.createElement("option");
        op.value = c.id;
        op.textContent = c.nombre;
        return op;
      });
      sel.replaceChildren(sel.options[0], ...opciones);
      sel.value = clientes.some((c) => c.id === elegido) ? elegido : "";
    }
  }

  async function cargarClientes() {
    llenarClientes(await pedir("GET", "/radar/admin/tenants"));
  }

  async function cargarLineas() {
    const p = new URLSearchParams();
    if ($("filtro-estado").value) p.set("estado", $("filtro-estado").value);
    if ($("filtro-cliente").value) p.set("tenant_id", $("filtro-cliente").value);
    const filas = await pedir("GET", "/radar/admin/consola/lineas" + (p.toString() ? "?" + p.toString() : ""));
    pintarTabla(filas);
  }

  // Al abrir la pantalla, con «Refrescar» y tras un alta. Primero los clientes: si el del filtro ya no existe, el
  // filtro vuelve a «Todos» antes de pedir las líneas. El refresco automático de la tabla no pasa por acá porque
  // cada GET /tenants deja una fila en el log de auditoría. Los servidores van al final: si esa lista falla, lo
  // demás ya cargó.
  async function refrescar() {
    await cargarClientes();
    await cargarLineas();
    await cargarWorkers();
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

  // ---- Alta (solo Consola): cliente nuevo con su primera línea, línea nueva y reenvío de invitación
  // Perfil de datos y parámetros que el servidor propone para el rubro. Solo informa: el alta manda el rubro y es el
  // servidor quien arma el perfil y los parámetros.
  async function mostrarPropuesta() {
    const rubro = $("alta-rubro").value.trim();
    $("alta-propuesta").textContent = "";
    if (!/^[a-z_]{1,40}$/.test(rubro)) return;          // el servidor exige este formato: otro sería un 422
    const p = await pedir("GET", "/radar/admin/propuesta?rubro=" + encodeURIComponent(rubro));
    if ($("alta-rubro").value.trim() !== rubro) return;  // el rubro cambió mientras tanto: esta respuesta ya no vale
    const parametros = Object.entries({ ...p.parametros_tenant, ...p.parametros_linea }).map(([k, v]) => k + " = " + v);
    $("alta-propuesta").textContent = "Perfil de datos: " + p.perfil_de_datos + " · Parámetros propuestos: " +
      (parametros.length ? parametros.join(", ") : "ninguno");
  }

  async function crearCliente() {
    $("alta-aviso").textContent = "";
    // El servidor devuelve solo los ids: los nombres para el título del panel se leen antes de limpiar el formulario.
    const tenantNombre = $("alta-nombre").value.trim();
    const lineaNombre = $("alta-linea-nombre").value.trim();
    const r = await pedir("POST", "/radar/admin/tenants", {
      nombre: tenantNombre,
      rubro: $("alta-rubro").value.trim(),
      dueno: { email: $("alta-dueno-email").value.trim(), nombre: $("alta-dueno-nombre").value.trim() },
      linea: { nombre: lineaNombre },
    });
    // El cliente ya existe aunque el mail no haya salido: se avisa lo que pasó y el formulario se limpia antes de
    // refrescar nada, así ni un mail caído ni un refresco que falle dejan el formulario lleno para crear otro igual.
    $("alta-aviso").textContent = r.invitacion_enviada
      ? "Cliente creado. Invitación al dueño: enviada."
      : "Cliente creado. Invitación al dueño: no salió: reenviala desde «Reenviar invitación».";
    $("form-alta").reset();
    $("alta-propuesta").textContent = "";
    abrirPanel({ tenant_id: r.tenant_id, line_id: r.line_id, tenant_nombre: tenantNombre, line_nombre: lineaNombre });
    await refrescar();
  }

  async function agregarLinea() {
    $("alta-aviso").textContent = "";
    await pedir("POST", "/radar/admin/tenants/" + encodeURIComponent($("linea-cliente").value) + "/lineas",
                { nombre: $("linea-nombre").value.trim() });
    $("alta-aviso").textContent = "Línea agregada.";
    $("linea-nombre").value = "";
    await refrescar();
  }

  async function reenviarInvitacion() {
    $("alta-aviso").textContent = "";
    const r = await pedir("POST", "/radar/admin/tenants/" + encodeURIComponent($("inv-cliente").value) + "/invitaciones",
                          { email: $("inv-email").value.trim() });
    $("alta-aviso").textContent = r.enviada ? "Invitación enviada." : "No salió: revisá el email o el correo saliente.";
    if (r.enviada) $("inv-email").value = "";            // si no salió, el email queda para reintentar
  }

  // ---- Servidores WAHA (solo Consola): registrar uno, reemplazar su clave y cargar el disco usado
  // La clave admin se escribe en un campo de contraseña, viaja una sola vez y el campo se vacía al terminar, salga como
  // salga. Ninguna pantalla la muestra: el servidor solo dice si está cargada.
  function llenarWorkers(workers) {
    const sel = $("worker-reemplazo");
    const elegido = sel.value;
    const opciones = workers.map((w) => {
      const op = document.createElement("option");
      op.value = w.id;
      op.textContent = w.nombre;
      return op;
    });
    sel.replaceChildren(sel.options[0], ...opciones);
    sel.value = workers.some((w) => w.id === elegido) ? elegido : "";
  }

  function pintarWorkers(workers) {
    const cuerpo = $("workers");
    cuerpo.replaceChildren();
    for (const w of workers) {
      const tr = document.createElement("tr");
      const valores = [w.nombre, w.engine, w.base_url, w.sesiones + "/" + w.max_sesiones,
                       w.disco_usado_gb + "/" + w.disco_max_gb + " GB", w.activo ? "sí" : "no",
                       w.clave_cargada ? "sí" : "no"];
      for (const v of valores) tr.appendChild(celda(v));
      const td = document.createElement("td");
      const boton = document.createElement("button");
      boton.type = "button";
      boton.textContent = "Disco";
      boton.addEventListener("click", () => accion(() => actualizarDisco(w), "worker-error"));
      td.appendChild(boton);
      tr.appendChild(td);
      cuerpo.appendChild(tr);
    }
    llenarWorkers(workers);
  }

  async function cargarWorkers() {
    pintarWorkers(await pedir("GET", "/radar/admin/workers"));
  }

  // Probar la clave es un pedido a otro servidor: si no contesta, tarda hasta el timeout (20 s por defecto).
  const PROBANDO = "Probando la clave contra el servidor…";

  async function registrarWorker() {
    $("worker-aviso").textContent = PROBANDO;
    const nombre = $("worker-nombre").value.trim();
    let r;
    try {
      r = await pedir("POST", "/radar/admin/workers", {
        nombre: nombre,
        base_url: $("worker-url").value.trim(),
        engine: $("worker-motor").value,
        max_sesiones: Number($("worker-max").value),
        disco_max_gb: Number($("worker-disco").value),
        admin_key: $("worker-clave").value.trim(),
      });
    } finally {
      $("worker-clave").value = "";                      // ni siquiera si WAHA la rechazó queda en la página
      $("worker-aviso").textContent = "";
    }
    // Ya está registrado: el formulario se limpia antes de refrescar, así un refresco que falle no deja el
    // formulario lleno para registrar el mismo nombre otra vez (daría el 409).
    $("form-worker").reset();
    $("worker-aviso").textContent = "Servidor " + nombre + " registrado: WAHA " + (r.version || "sin versión") +
      ", motor " + r.engine + ".";
    await cargarWorkers();
  }

  async function reemplazarClave() {
    $("worker-aviso").textContent = PROBANDO;
    const sel = $("worker-reemplazo");
    const nombre = sel.selectedOptions[0].textContent;
    let r;
    try {
      r = await pedir("PUT", "/radar/admin/workers/" + encodeURIComponent(sel.value) + "/clave",
                      { admin_key: $("worker-clave-nueva").value.trim() });
    } finally {
      $("worker-clave-nueva").value = "";
      $("worker-aviso").textContent = "";
    }
    $("worker-aviso").textContent = "Clave de " + nombre + " reemplazada: WAHA " + (r.version || "sin versión") +
      ", motor " + r.engine + ".";
    await cargarWorkers();
  }

  async function actualizarDisco(w) {
    const dato = window.prompt("GB usados en el disco de " + w.nombre + " (el total es de " + w.disco_max_gb + " GB):",
                               String(w.disco_usado_gb));
    if (dato === null) return;                           // canceló
    const usado = Number(dato.trim().replace(",", "."));   // «3,5» y «3.5»
    if (dato.trim() === "" || !Number.isFinite(usado) || usado < 0) throw new Error("disco_invalido");
    await pedir("PUT", "/radar/admin/workers/" + encodeURIComponent(w.id) + "/disco", { usado_gb: usado });
    $("worker-aviso").textContent = "Disco de " + w.nombre + " actualizado: " + usado + " GB usados.";
    await cargarWorkers();
  }

  // El envío nativo de un formulario lo bloquea la CSP (form-action 'none'), así que lo manda este JS, como en el
  // login. Un doble clic duplicaría un cliente que no se puede borrar: el botón queda inactivo mientras viaja el pedido.
  function alEnviar(form, boton, fn, destino) {
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      boton.disabled = true;
      accion(fn, destino).finally(() => { boton.disabled = false; });
    });
  }

  // ---- arranque
  enlazar("btn-salir", salir);
  enlazar("btn-consentir", consentir);
  enlazar("btn-generar", generar);
  enlazar("btn-reiniciar", reiniciar);
  enlazar("btn-codigo", pedirCodigo);
  enlazar("btn-desconectar", desconectar);
  enlazar("btn-borrar", borrarTodo);

  if (modo === "consola") {
    enlazar("btn-restriccion", marcarRestriccion);
    enlazar("btn-levantar", levantarRestriccion);
    enlazar("btn-refrescar", refrescar);
    enlazar("btn-cerrar-panel", cerrarPanel);
    alEnviar($("form-alta"), $("btn-alta"), crearCliente);
    alEnviar($("form-linea"), $("btn-agregar-linea"), agregarLinea);
    alEnviar($("form-invitacion"), $("btn-invitar"), reenviarInvitacion);
    alEnviar($("form-worker"), $("btn-worker"), registrarWorker, "worker-error");
    alEnviar($("form-worker-clave"), $("btn-worker-clave"), reemplazarClave, "worker-error");
    $("alta-rubro").addEventListener("change", () => accion(mostrarPropuesta));
    $("filtro-estado").addEventListener("change", () => accion(cargarLineas));
    $("filtro-cliente").addEventListener("change", () => accion(cargarLineas));
    accion(refrescar);
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
