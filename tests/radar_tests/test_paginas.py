"""
Pantallas (decisión 12): HTML estático sin scripts ni estilos inline, un único
JS que escribe solo con textContent, y CSP estricta (§7: escapar todo y CSP).
"""
import pathlib
import re

from app.radar.constantes import TENANT_KIS
from app.radar.routers.paginas import CSP

from .helpers import crear_usuario, entrar, escenario_vinculable

ESTATICOS = pathlib.Path(__file__).resolve().parents[2] / "app" / "radar" / "static"
IDS_CLIENTE = {"error", "panel", "texto-consentimiento", "parametros", "version", "acepto", "btn-consentir",
               "paso-consentimiento", "paso-vincular", "full-sync", "btn-generar", "estado-texto", "qr", "cuenta",
               "reinicios", "btn-reiniciar", "telefono", "btn-codigo", "codigo", "aviso", "btn-desconectar",
               "btn-borrar"}


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
    assert (await cliente.get("/radar/consola")).status_code == 401
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
    assert usados and usados <= _ids_html("consola.html"), usados - _ids_html("consola.html")


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
