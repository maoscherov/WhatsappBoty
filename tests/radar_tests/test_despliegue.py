"""
Detalles de despliegue de Radar: el host de uvicorn configurable en el Dockerfile (Decisión 9) y
`Cache-Control: no-store` en lo que responden /radar/api/* y /radar/admin/* (el estado del vínculo, el número de la
línea y las listas no tienen que quedar en la caché del navegador ni en la de un intermediario).
"""
import json
import pathlib
import re
import uuid

from fastapi import Response
from httpx import ASGITransport, AsyncClient

from app.radar.app import crear_app_radar
from app.radar.constantes import TENANT_KIS

from .helpers import crear_usuario, entrar, escenario_vinculable, vincular_de_prueba
from .waha_falso import PNG

RAIZ = pathlib.Path(__file__).resolve().parents[2]
# Una sola cabecera, no dos: el QR ya trae su no-store y el middleware no la repite.
NO_STORE = ["no-store"]


# ── Dockerfile y documentación ─────────────────────────────────────────────────────────────────────────────

def _comando_del_dockerfile() -> str:
    cmds = re.findall(r"^CMD\s+(\[.*\])\s*$", (RAIZ / "Dockerfile").read_text(encoding="utf-8"), re.M)
    assert len(cmds) == 1, "el Dockerfile tiene que tener un único CMD"
    forma_exec = json.loads(cmds[0])
    assert forma_exec[:2] == ["sh", "-c"] and len(forma_exec) == 3        # sin shell no se expande ${...}
    return forma_exec[2]


def test_el_dockerfile_escucha_en_uvicorn_host_y_sigue_en_0000_por_defecto():
    comando = _comando_del_dockerfile()
    assert comando.startswith("uvicorn app.main:app ")
    # Un único --host, con el default de siempre: sin UVICORN_HOST (o vacía) el bot y Radar escuchan en 0.0.0.0.
    assert re.findall(r"--host\s+(\S+)", comando) == ["${UVICORN_HOST:-0.0.0.0}"]
    assert re.findall(r"--port\s+(\S+)", comando) == ["${PORT:-8000}"]


def test_la_tabla_de_despliegue_documenta_uvicorn_host():
    doc = (RAIZ / "docs" / "radar-despliegue.md").read_text(encoding="utf-8")
    fila = next((f for f in doc.splitlines() if f.startswith("| `UVICORN_HOST` |")), "")
    assert "`0.0.0.0`" in fila and "`::`" in fila and "IPv6" in fila and "red privada" in fila


# ── Cache-Control: no-store en la API ──────────────────────────────────────────────────────────────────────

async def _admin(cliente, ctx):
    uid = await crear_usuario(ctx.db, TENANT_KIS, "admin@keepitsimple.com.ar", "admin")
    await entrar(cliente, ctx, TENANT_KIS, uid, "admin")
    return uid


def _vinculo_admin(esc, sufijo=""):
    return f"/radar/admin/tenants/{esc['tenant_id']}/lineas/{esc['line_id']}/vinculo{sufijo}"


def _descripcion(r) -> str:
    return f"{r.request.method} {r.request.url.path} -> {r.status_code}"


async def test_la_api_del_admin_responde_no_store(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    await vincular_de_prueba(ctx_waha, waha, esc)
    await _admin(cliente, ctx_waha)
    lineas = await cliente.get("/radar/admin/consola/lineas")
    estado = await cliente.get(_vinculo_admin(esc))
    # traen justo lo que no debe quedar en una caché: el número de la línea y el estado de su vínculo
    assert lineas.status_code == 200 and lineas.json()[0]["numero"] == "…4567"
    assert estado.status_code == 200 and estado.json()["estado"] == "vinculado"
    for r in (lineas, estado):
        assert r.headers.get_list("cache-control") == NO_STORE, _descripcion(r)


async def test_la_api_del_dueno_responde_no_store(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    await vincular_de_prueba(ctx_waha, waha, esc, working=False)
    await entrar(cliente, ctx_waha, esc["tenant_id"], esc["dueno_id"], "dueno")
    vinculo = f"/radar/api/lineas/{esc['line_id']}/vinculo"
    for ruta in ("/radar/api/yo", "/radar/api/lineas", vinculo, vinculo + "/qr"):
        r = await cliente.get(ruta)
        assert r.status_code == 200, _descripcion(r)
        assert r.headers.get_list("cache-control") == NO_STORE, _descripcion(r)


async def test_el_qr_sigue_con_su_no_store_sin_repetirlo(cliente, ctx_waha, waha):
    esc = await escenario_vinculable(ctx_waha)
    await vincular_de_prueba(ctx_waha, waha, esc, working=False)
    await _admin(cliente, ctx_waha)
    qr = await cliente.get(_vinculo_admin(esc, "/qr"))
    assert qr.status_code == 200 and qr.content == PNG and qr.headers["content-type"] == "image/png"
    assert qr.headers.get_list("cache-control") == NO_STORE          # el del propio QR, no uno más del middleware


async def test_no_pisa_el_cache_control_que_la_respuesta_ya_trae(radar_ctx):
    app = crear_app_radar(radar_ctx.settings, contexto=radar_ctx)

    @app.get("/radar/admin/prueba-con-su-cache")
    async def con_su_cache():
        return Response("x", headers={"Cache-Control": "private, max-age=60"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        r = await c.get("/radar/admin/prueba-con-su-cache")
    assert r.status_code == 200 and r.headers.get_list("cache-control") == ["private, max-age=60"]


async def test_los_errores_de_la_api_tambien_van_sin_cache(cliente, ctx_waha):
    esc = await escenario_vinculable(ctx_waha)         # sin vínculo todavía: no hay QR que pedir
    errores = [(401, await cliente.get("/radar/api/yo")),
               (401, await cliente.get("/radar/admin/consola/lineas"))]
    await entrar(cliente, ctx_waha, esc["tenant_id"], esc["dueno_id"], "dueno")
    errores.append((403, await cliente.get("/radar/admin/consola/lineas")))
    await _admin(cliente, ctx_waha)
    errores += [
        (404, await cliente.get(f"/radar/admin/tenants/{uuid.uuid4()}/lineas/{esc['line_id']}/vinculo")),
        (404, await cliente.get("/radar/admin/ruta-que-no-existe")),
        (409, await cliente.get(_vinculo_admin(esc, "/qr"))),
        (422, await cliente.get("/radar/admin/consola/lineas?estado=otro")),       # la validación de FastAPI
        (422, await cliente.post("/radar/admin/workers", json={})),                # el 422 propio de workers_admin
    ]
    for esperado, r in errores:
        assert r.status_code == esperado, _descripcion(r)
        assert r.headers.get_list("cache-control") == NO_STORE, _descripcion(r)


async def test_fuera_de_la_api_las_respuestas_quedan_como_estaban(cliente):
    """El middleware mira solo /radar/api/ y /radar/admin/, con la barra: lo demás conserva sus cabeceras."""
    health = await cliente.get("/health")
    assert health.status_code == 200 and "cache-control" not in health.headers
    webhook = await cliente.post("/webhook/waha", content=b"{}")                    # sin firma: 401 como siempre
    assert webhook.status_code == 401 and "cache-control" not in webhook.headers
    assert (await cliente.get("/radar/estaticos/radar.js")).headers.get_list("cache-control") == ["no-cache"]
    assert (await cliente.get("/radar/login")).headers.get_list("cache-control") == NO_STORE      # el de la página
    # un prefijo parecido no cuenta: /radar/administrador no es /radar/admin/
    for ruta in ("/radar/administrador", "/radar/apiary"):
        r = await cliente.get(ruta)
        assert r.status_code == 404 and "cache-control" not in r.headers, ruta
