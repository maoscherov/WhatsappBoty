"""
Pantallas (decisión 12): HTML estático sin scripts ni estilos inline, un único
JS que escribe solo con textContent, y CSP estricta (§7: escapar todo y CSP).
"""
import pathlib
import re
import uuid

from app.radar.auth import COOKIE
from app.radar.constantes import TENANT_KIS
from app.radar.routers.paginas import CSP

from .helpers import (DOMINIO_COOKIE, como_superusuario, crear_linea_directa, crear_tenant_directo, crear_usuario,
                      entrar, escenario_vinculable)

ESTATICOS = pathlib.Path(__file__).resolve().parents[2] / "app" / "radar" / "static"
IDS_CLIENTE = {"error", "panel", "texto-consentimiento", "parametros", "version", "acepto", "btn-consentir",
               "paso-consentimiento", "paso-vincular", "full-sync", "btn-generar", "estado-texto", "qr", "cuenta",
               "reinicios", "btn-reiniciar", "telefono", "btn-codigo", "codigo", "aviso", "btn-desconectar",
               "btn-borrar"}
# Los que solo usa el modo "login" (login.html); `error` y `aviso` los comparte con las otras dos pantallas.
IDS_LOGIN = {"form-login", "email", "btn-entrar"}
# La sección "Alta" de la Consola: cliente nuevo con su primera línea, línea nueva y reenvío de invitación.
IDS_ALTA = {"alta-nombre", "alta-rubro", "alta-dueno-email", "alta-dueno-nombre", "alta-linea-nombre",
            "alta-propuesta", "btn-alta", "linea-cliente", "linea-nombre", "btn-agregar-linea", "inv-cliente",
            "inv-email", "btn-invitar", "alta-aviso"}


def _ids_html(nombre: str) -> set[str]:
    return set(re.findall(r'\bid="([a-z0-9-]+)"', (ESTATICOS / nombre).read_text(encoding="utf-8")))


def _sin_inline(html: str) -> None:
    assert re.search(r"<script(?![^>]*\bsrc=)", html) is None
    assert all(c.strip() == "" for c in re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S))
    assert '<script src="/radar/estaticos/radar.js" defer></script>' in html
    assert " style=" not in html and "<style" not in html
    assert re.search(r"\son[a-z]+\s*=", html) is None


async def _admin(cliente, ctx):
    uid = await crear_usuario(ctx.db, TENANT_KIS, "admin@keepitsimple.com.ar", "admin")
    await entrar(cliente, ctx, TENANT_KIS, uid, "admin")
    return uid


async def test_consola_solo_para_admins(cliente, radar_ctx):
    # sin sesión no hay 401 sino la redirección al login: test_pantallas_sin_sesion_van_al_login
    esc = await escenario_vinculable(radar_ctx)
    await entrar(cliente, radar_ctx, esc["tenant_id"], esc["dueno_id"], "dueno")
    assert (await cliente.get("/radar/consola")).status_code == 403
    await _admin(cliente, radar_ctx)
    assert (await cliente.get("/radar/consola")).status_code == 200


async def test_consola_con_csp_estricta_y_sin_inline(cliente, radar_ctx):
    await _admin(cliente, radar_ctx)
    r = await cliente.get("/radar/consola")
    assert r.headers["content-security-policy"] == CSP
    assert "unsafe-inline" not in CSP and "default-src 'none'" in CSP
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    _sin_inline(r.text)


async def test_abrir_la_consola_queda_auditado(cliente, radar_ctx):
    uid = await _admin(cliente, radar_ctx)
    await cliente.get("/radar/consola")
    async with radar_ctx.db.tenant_tx(TENANT_KIS) as con:
        n = await con.fetchval("SELECT count(*) FROM access_audit_log WHERE accion = 'consola_abierta' "
                               "AND actor_user_id = $1", uid)
    assert n == 1


async def test_conectar_solo_para_el_dueno(cliente, radar_ctx):
    esc = await escenario_vinculable(radar_ctx)
    gestor = await crear_usuario(radar_ctx.db, esc["tenant_id"], "gestor@cliente.com", "gestor")
    await entrar(cliente, radar_ctx, esc["tenant_id"], gestor, "gestor")
    assert (await cliente.get("/radar/conectar")).status_code == 403
    await entrar(cliente, radar_ctx, esc["tenant_id"], esc["dueno_id"], "dueno")
    r = await cliente.get(f"/radar/conectar?linea={esc['line_id']}")
    assert r.status_code == 200 and r.headers["content-security-policy"] == CSP
    _sin_inline(r.text)


async def test_login_es_una_pagina_sin_inline(cliente):
    r = await cliente.get("/radar/login")
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == CSP and r.headers["cache-control"] == "no-store"
    _sin_inline(r.text)
    for fragmento in ('id="email"', 'type="email"', 'autocomplete="email"', 'id="btn-entrar"', 'id="aviso"',
                      'id="error"', 'data-modo="login"', "<title>Radar — Entrar</title>"):
        assert fragmento in r.text, fragmento
    # no trae nada de la consola ni del cliente, salvo los dos mensajes que comparten las tres pantallas
    assert _ids_html("login.html") & (_ids_html("consola.html") | _ids_html("conectar.html")) <= {"error", "aviso"}


PANTALLAS = ("/radar/consola", "/radar/conectar", "/radar/inicio")


async def _todas_van_al_login(cliente) -> None:
    for ruta in PANTALLAS:
        r = await cliente.get(ruta, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/radar/login", ruta


async def test_pantallas_sin_sesion_van_al_login(cliente):
    await _todas_van_al_login(cliente)
    # el destino es fijo: nada de lo que viene en el pedido llega a la redirección
    r = await cliente.get("/radar/inicio?next=https://ajeno.example&linea=1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/radar/login"


async def test_pantallas_con_sesion_invalida_van_al_login(cliente, radar_ctx):
    """Cookie adulterada o sesión revocada: igual que sin cookie, no un 401 JSON en el navegador."""
    uid = await _admin(cliente, radar_ctx)
    assert (await cliente.get("/radar/consola")).status_code == 200          # guarda: la cookie viaja
    valida = cliente.cookies.get(COOKIE)
    adulterada = valida[:-1] + ("0" if valida[-1] != "0" else "1")
    cliente.cookies.set(COOKIE, adulterada, domain=DOMINIO_COOKIE, path="/radar")
    await _todas_van_al_login(cliente)

    await entrar(cliente, radar_ctx, TENANT_KIS, uid, "admin")
    revocada = cliente.cookies.get(COOKIE)
    assert (await cliente.post("/radar/logout")).status_code == 200          # revoca la sesión
    cliente.cookies.set(COOKIE, revocada, domain=DOMINIO_COOKIE, path="/radar")   # y la cookie vieja vuelve a viajar
    await _todas_van_al_login(cliente)


async def test_api_sin_sesion_sigue_en_401(cliente):
    for ruta in ("/radar/api/yo", "/radar/admin/consola/lineas"):
        assert (await cliente.get(ruta)).status_code == 401, ruta


async def test_inicio_admin_va_a_la_consola(cliente, radar_ctx):
    await _admin(cliente, radar_ctx)
    r = await cliente.get("/radar/inicio", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/radar/consola"
    # el aterrizaje no audita: la apertura de la consola la registra la propia consola, una sola vez
    async with radar_ctx.db.tenant_tx(TENANT_KIS) as con:
        assert await con.fetchval("SELECT count(*) FROM access_audit_log WHERE accion = 'consola_abierta'") == 0
    assert (await cliente.get("/radar/consola")).status_code == 200
    async with radar_ctx.db.tenant_tx(TENANT_KIS) as con:
        assert await con.fetchval("SELECT count(*) FROM access_audit_log WHERE accion = 'consola_abierta'") == 1


async def test_inicio_dueno_va_a_conectar_su_linea(cliente, radar_ctx):
    esc = await escenario_vinculable(radar_ctx)
    await entrar(cliente, radar_ctx, esc["tenant_id"], esc["dueno_id"], "dueno")
    r = await cliente.get("/radar/inicio", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/radar/conectar?linea={esc['line_id']}"
    # la línea sale de la base, nunca del pedido
    r = await cliente.get("/radar/inicio?linea=otra&next=https://ajeno.example", follow_redirects=False)
    assert r.headers["location"] == f"/radar/conectar?linea={esc['line_id']}"


async def test_inicio_dueno_va_a_la_linea_mas_antigua_que_no_esta_de_baja(cliente, radar_ctx, radar_urls):
    t = await crear_tenant_directo(radar_ctx.db, "Farmacia B")
    d = await crear_usuario(radar_ctx.db, t, "dueno@b.com", "dueno")
    baja = await crear_linea_directa(radar_ctx.db, t, "Cerrada")
    await crear_linea_directa(radar_ctx.db, t, "A reciente")
    antigua = await crear_linea_directa(radar_ctx.db, t, "Z antigua")
    async with radar_ctx.db.tenant_tx(t) as con:               # el CHECK pide estado y de_baja_at a la vez
        await con.execute("UPDATE lines SET estado = 'de_baja', de_baja_at = now() WHERE id = $1", baja)
    # created_at no lo puede escribir radar_app. Orden por antigüedad: baja, antigua, reciente; ni el orden de
    # alta ni el alfabético dan "antigua", y la más vieja de todas (la de baja) no cuenta.
    envejecer = "UPDATE lines SET created_at = now() - $2::int * interval '1 day' WHERE id = $1"
    await como_superusuario(radar_urls, envejecer, baja, 3)
    await como_superusuario(radar_urls, envejecer, antigua, 2)
    await entrar(cliente, radar_ctx, t, d, "dueno")
    r = await cliente.get("/radar/inicio", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/radar/conectar?linea={antigua}"


async def test_inicio_dueno_con_lineas_permitidas_va_a_una_de_las_suyas(cliente, radar_ctx):
    t = await crear_tenant_directo(radar_ctx.db, "Farmacia B")
    await crear_linea_directa(radar_ctx.db, t, "Primera")
    segunda = await crear_linea_directa(radar_ctx.db, t, "Segunda")
    d = await crear_usuario(radar_ctx.db, t, "dueno@b.com", "dueno", lineas_permitidas=[segunda])
    await entrar(cliente, radar_ctx, t, d, "dueno")
    r = await cliente.get("/radar/inicio", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/radar/conectar?linea={segunda}"


async def test_inicio_dueno_sin_lineas_va_a_conectar_sin_linea(cliente, radar_ctx):
    t = await crear_tenant_directo(radar_ctx.db, "Farmacia C")
    d = await crear_usuario(radar_ctx.db, t, "dueno@c.com", "dueno")
    unica = await crear_linea_directa(radar_ctx.db, t, "Cerrada")
    async with radar_ctx.db.tenant_tx(t) as con:               # la única línea, dada de baja
        await con.execute("UPDATE lines SET estado = 'de_baja', de_baja_at = now() WHERE id = $1", unica)
    await entrar(cliente, radar_ctx, t, d, "dueno")
    r = await cliente.get("/radar/inicio", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/radar/conectar"


async def test_inicio_lector_va_a_yo(cliente, radar_ctx):
    esc = await escenario_vinculable(radar_ctx)
    lector = await crear_usuario(radar_ctx.db, esc["tenant_id"], "lector@cliente.com", "lector")
    await entrar(cliente, radar_ctx, esc["tenant_id"], lector, "lector")
    r = await cliente.get("/radar/inicio", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/radar/api/yo"


async def test_inicio_gestor_y_soporte_tambien_van_a_yo(cliente, radar_ctx):
    """Todo rol que no es admin ni dueño: su pantalla llega con el tablero (tramo 4); mientras, /radar/api/yo."""
    esc = await escenario_vinculable(radar_ctx)
    gestor = await crear_usuario(radar_ctx.db, esc["tenant_id"], "gestor@cliente.com", "gestor")
    await entrar(cliente, radar_ctx, esc["tenant_id"], gestor, "gestor")
    r = await cliente.get("/radar/inicio", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/radar/api/yo"
    # sesión de soporte: un admin de KIS con una sesión en el tenant del cliente
    admin = await crear_usuario(radar_ctx.db, TENANT_KIS, "admin@keepitsimple.com.ar", "admin")
    await entrar(cliente, radar_ctx, esc["tenant_id"], admin, "soporte")
    r = await cliente.get("/radar/inicio", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/radar/api/yo"


async def test_raiz_redirige_a_inicio(cliente):
    for ruta in ("/", "/radar"):
        r = await cliente.get(ruta, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/radar/inicio", ruta


def test_consola_y_conectar_tienen_salir():
    for pagina in ("consola.html", "conectar.html"):
        assert 'id="btn-salir"' in (ESTATICOS / pagina).read_text(encoding="utf-8"), pagina
    assert "btn-salir" not in _ids_html("login.html")          # sin sesión no hay de qué salir


def test_consola_tiene_alta():
    html = (ESTATICOS / "consola.html").read_text(encoding="utf-8")
    _sin_inline(html)
    ids = re.findall(r'\bid="([a-z0-9-]+)"', html)
    assert IDS_ALTA <= set(ids), IDS_ALTA - set(ids)
    assert len(ids) == len(set(ids)), "ids repetidos"           # tres formularios parecidos: un id copiado se cuela fácil
    # la sección va arriba de las líneas; el rubro lleva el mismo formato que exige el servidor (^[a-z_]{1,40}$)
    alta = re.search(r'<section\b[^>]*\bid="alta"', html)
    assert alta and alta.start() < html.index("<h2>Líneas</h2>")
    assert 'pattern="[a-z_]{1,40}"' in html and "minúsculas y guion bajo, ej.: farmacia" in html
    # los tres selects de cliente: el de siempre más los dos del alta
    for id_ in ("filtro-cliente", "linea-cliente", "inv-cliente"):
        assert re.search(rf'<select\b[^>]*\bid="{id_}"', html), id_
    assert IDS_ALTA.isdisjoint(_ids_html("conectar.html"))      # el dueño no ve el alta


def test_el_js_pide_el_link_y_cierra_la_sesion_por_la_api():
    js = (ESTATICOS / "radar.js").read_text(encoding="utf-8")
    assert "Si el email está registrado, te mandamos un link para entrar. Revisá tu correo." in js
    assert '"/radar/login"' in js and '"/radar/logout"' in js
    assert 'window.location.assign("/radar/login")' in js      # también si el logout falla


def test_el_js_del_alta_avisa_lo_que_paso_con_la_invitacion():
    js = (ESTATICOS / "radar.js").read_text(encoding="utf-8")
    # el alta ya está hecha aunque el mail no salga: el aviso sale del flag que contesta el servidor, no se supone
    assert "r.invitacion_enviada" in js and "r.enviada" in js
    assert "Cliente creado. Invitación al dueño: enviada." in js
    assert "no salió: reenviala desde «Reenviar invitación»." in js
    assert "Invitación enviada." in js and "No salió: revisá el email o el correo saliente." in js
    # lo que devuelven estos endpoints se lee en castellano: el 422 propio de FastAPI y los 404 con texto
    # (como claves de MENSAJES: "error_422:" y no solo el nombre, que también aparece en un comentario)
    for clave in ("error_422:", '"tenant inexistente":', '"usuario inexistente en este tenant":'):
        assert clave in js, clave


def test_el_js_del_alta_usa_los_endpoints_que_ya_existen():
    js = (ESTATICOS / "radar.js").read_text(encoding="utf-8")
    assert re.search(r'pedir\("GET",\s*"/radar/admin/tenants"\)', js)        # los tres selects: todos los clientes
    assert re.search(r'pedir\("POST",\s*"/radar/admin/tenants",', js)        # cliente nuevo
    assert '"/radar/admin/propuesta?rubro="' in js                          # perfil y parámetros del rubro
    assert re.search(r'"/radar/admin/tenants/"\s*\+[^;]*\+\s*"/lineas"', js)
    assert re.search(r'"/radar/admin/tenants/"\s*\+[^;]*\+\s*"/invitaciones"', js)


def test_la_propuesta_del_alta_no_presenta_la_retencion_de_la_fuente_como_aplicada():
    """Un rubro sensible propone retencion_fuente_dias = 7, pero el alta crea la línea sin plazo (0) hasta que llegue
    el almacén purgable (tramo 6). Es la pantalla con la que se da de alta la primera farmacia: ese 7 no puede salir
    como un parámetro propuesto más (test_la_linea_del_alta_de_un_rubro_sensible_se_crea_sin_plazo)."""
    js = (ESTATICOS / "radar.js").read_text(encoding="utf-8")
    propuesta = js[js.index("async function mostrarPropuesta("):js.index("async function crearCliente(")]
    # "Parámetros propuestos" son los del cliente; la parte de línea ya no entra en esa lista
    assert "Object.entries(p.parametros_tenant)" in propuesta and "p.parametros_linea }" not in propuesta
    assert '"Perfil de datos: "' in propuesta and '" · Parámetros propuestos: "' in propuesta
    # va aparte, dicha como lo que es
    assert "p.parametros_linea.retencion_fuente_dias" in propuesta
    assert 'Retención de la fuente propuesta: "' in propuesta
    assert '" días — todavía no se aplica (llega con el almacén purgable): la línea se crea sin plazo."' in propuesta
    assert "textContent" in propuesta and "innerHTML" not in propuesta


async def test_la_linea_del_alta_de_un_rubro_sensible_se_crea_sin_plazo(cliente, radar_ctx):
    """Lo que dice la pantalla del alta, del lado del servidor: propone 7 días y la línea queda en 0 (sin plazo)."""
    await _admin(cliente, radar_ctx)
    r = await cliente.get("/radar/admin/propuesta", params={"rubro": "farmacia"})
    assert r.json()["parametros_linea"] == {"retencion_fuente_dias": 7}
    r = await cliente.post("/radar/admin/tenants", json={       # el cuerpo que manda la Consola: sin parámetros
        "nombre": "Farmacia del Centro", "rubro": "farmacia",
        "dueno": {"email": "dueno@farmacia.com", "nombre": "Ana"}, "linea": {"nombre": "Local centro"}})
    assert r.status_code == 201, r.text
    async with radar_ctx.db.tenant_tx(uuid.UUID(r.json()["tenant_id"])) as con:
        dias = await con.fetchval("SELECT retencion_fuente_dias FROM lines WHERE id = $1", uuid.UUID(r.json()["line_id"]))
    assert dias == 0


def test_una_sesion_cerrada_lleva_al_login_y_no_repite_el_error():
    """Con «Salir» en otra pestaña, o con la sesión vencida, el refresco de 15 s repetía para siempre el error del 401
    («Error: sesión inválida o vencida»). Un 401 lleva al login, una sola vez, y desde ahí la página no pide nada más."""
    js = (ESTATICOS / "radar.js").read_text(encoding="utf-8")
    pedir = js[js.index("async function pedir("):js.index("async function accion(")]
    assert re.search(r'if \(r\.status === 401\) \{\s*alLogin = true;\s*window\.location\.assign\("/radar/login"\);'
                     r'\s*throw new Error\("sesion_cerrada"\);', pedir)
    # antes del fetch: con la sesión ya cerrada, ni el sondeo de 3 s ni el refresco de 15 s salen a la red
    assert pedir.index('if (alLogin) throw new Error("sesion_cerrada");') < pedir.index("await fetch(")
    # un aviso que se lee, no «Error: sesion_cerrada»
    assert re.search(r'\bsesion_cerrada: "[^"]+",', js)


def test_el_refresco_automatico_no_vuelve_a_pedir_los_clientes():
    """GET /radar/admin/tenants deja una fila en access_audit_log por pedido: cada 15 s serían miles por día.
    Los selects se rearman al cargar, al refrescar a mano y tras un alta, no con la tabla."""
    js = (ESTATICOS / "radar.js").read_text(encoding="utf-8")
    fin = js.index("REFRESCO_LINEAS_MS)")                     # el único setInterval que usa ese período
    automatico = js[js.rindex("setInterval(", 0, fin):fin]
    assert "cargarLineas()" in automatico
    assert "cargarClientes" not in automatico and "refrescar()" not in automatico


def test_el_js_no_arma_html():
    js = (ESTATICOS / "radar.js").read_text(encoding="utf-8")
    for prohibido in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert prohibido not in js, prohibido
    assert "textContent" in js


async def test_estaticos_con_tipo_y_nosniff(cliente):
    js = await cliente.get("/radar/estaticos/radar.js")
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
    assert js.headers["x-content-type-options"] == "nosniff"
    css = await cliente.get("/radar/estaticos/radar.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert (await cliente.get("/radar/estaticos/consola.html")).status_code == 404
    assert (await cliente.get("/radar/estaticos/..%2Fapp.py")).status_code == 404


def test_los_ids_que_usa_el_js_existen_en_la_consola():
    js = (ESTATICOS / "radar.js").read_text(encoding="utf-8")
    usados = set(re.findall(r'(?:\$|enlazar)\("([a-z0-9-]+)"', js))
    # el modo login tiene su propia pantalla: sus ids van en login.html y los demás siguen en la consola
    de_consola = usados - IDS_LOGIN
    assert de_consola and de_consola <= _ids_html("consola.html"), de_consola - _ids_html("consola.html")
    assert IDS_LOGIN <= usados, IDS_LOGIN - usados
    assert IDS_LOGIN <= _ids_html("login.html"), IDS_LOGIN - _ids_html("login.html")


def test_la_pantalla_del_dueno_tiene_lo_que_usa_el_modo_cliente():
    js = (ESTATICOS / "radar.js").read_text(encoding="utf-8")
    assert IDS_CLIENTE <= set(re.findall(r'(?:\$|enlazar)\("([a-z0-9-]+)"', js))
    assert IDS_CLIENTE <= _ids_html("conectar.html"), IDS_CLIENTE - _ids_html("conectar.html")
    assert {"filtro-estado", "lineas", "restriccion-hasta"}.isdisjoint(_ids_html("conectar.html"))


def test_textos_del_js_no_prometen_lo_que_el_codigo_no_hace():
    js = (ESTATICOS / "radar.js").read_text(encoding="utf-8")
    assert "te avisamos por email" not in js          # sin_capacidad: nada avisa cuando se libera lugar
    assert "e.caido_desde" in js and "se desconectó el " in js       # caída con fecha DD/MM
    assert "r.copia_enviada" in js                    # la copia del consentimiento puede no haber salido
