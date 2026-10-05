"""
/radar/admin/workers (Consola KIS): registrar un servidor WAHA, reemplazar su clave y cargar el disco usado.

La clave admin se prueba contra el propio WAHA (GET /api/server/version) antes de guardarse, va directo al
SecretStore y de ahí no sale: ni a un log, ni a la base, ni a una respuesta, ni a un mensaje de error.
"""
import base64
import json
import logging
import pathlib
import re
import uuid

import pytest

from app.radar.constantes import TENANT_KIS
from app.radar.routers.workers_admin import ClaveIn, WorkerIn
from app.radar.workers import nombre_clave_admin

from .helpers import crear_link_directo, crear_usuario, crear_worker_directo, entrar, escenario_vinculable
from .test_paginas import ESTATICOS, _sin_inline

URL = "/radar/admin/workers"
CLAVE = "clave-admin-larga-0123456789"
CLAVE_B = "clave-admin-nueva-9876543210"
URL_WAHA = "http://waha-1.interno:3000"
CAMPOS_LISTA = {"id", "nombre", "base_url", "engine", "max_sesiones", "sesiones", "disco_max_gb", "disco_usado_gb",
                "activo", "clave_cargada"}
IDS_WORKERS = {"workers", "worker-nombre", "worker-url", "worker-motor", "worker-max", "worker-disco", "worker-clave",
               "btn-worker", "worker-reemplazo", "worker-clave-nueva", "btn-worker-clave"}
# Los dos párrafos de la sección: el aviso de lo que salió bien y el error propio (el de arriba de la página no se ve
# desde un formulario que está abajo de todo), más los dos formularios.
IDS_WORKERS_EXTRA = {"worker-aviso", "worker-error", "form-worker", "form-worker-clave"}


@pytest.fixture
async def ctx(radar_ctx, waha):
    """radar_ctx con el WAHA falso como transporte. Sin workers: cada test registra los suyos."""
    radar_ctx.waha_transport = waha.transporte()
    return radar_ctx


async def _admin(cliente, ctx) -> uuid.UUID:
    uid = await crear_usuario(ctx.db, TENANT_KIS, "admin@keepitsimple.com.ar", "admin")
    await entrar(cliente, ctx, TENANT_KIS, uid, "admin")
    return uid


def _alta(**cambios) -> dict:
    return {"nombre": "w1", "base_url": URL_WAHA, "engine": "NOWEB", "max_sesiones": 50, "disco_max_gb": 20,
            "admin_key": CLAVE, **cambios}


async def _registrar(cliente, **cambios) -> str:
    r = await cliente.post(URL, json=_alta(**cambios))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _claves_guardadas(ctx) -> list[str]:
    """Un archivo por clave admin en el SecretStore de los tests (FileSecretStore)."""
    return sorted(p.name for p in pathlib.Path(ctx.settings.secrets_dir).glob("waha_admin__*"))


async def _filas(ctx, sql: str, *args) -> list:
    async with ctx.db.tenant_tx(TENANT_KIS) as con:
        return await con.fetch(sql, *args)


async def _auditadas(ctx, accion: str) -> list:
    return await _filas(ctx, "SELECT actor_user_id, actor_rol, tipo_objeto, objeto_id, detalle::text AS detalle "
                             "FROM access_audit_log WHERE accion = $1", accion)


# --- acceso ------------------------------------------------------------------------------------------------------

async def test_solo_admin_sin_sesion_401_y_dueno_403(cliente, ctx, waha):
    wid = await crear_worker_directo(ctx.db)
    pedidos = [("GET", URL, None), ("POST", URL, _alta()), ("PUT", f"{URL}/{wid}/clave", {"admin_key": CLAVE}),
               ("PUT", f"{URL}/{wid}/disco", {"usado_gb": 1})]

    async def todos_dan(codigo: int) -> None:
        for metodo, ruta, cuerpo in pedidos:
            r = await cliente.request(metodo, ruta, json=cuerpo)
            assert r.status_code == codigo, (metodo, ruta, r.status_code)

    await todos_dan(401)
    esc = await escenario_vinculable(ctx)
    await entrar(cliente, ctx, esc["tenant_id"], esc["dueno_id"], "dueno")
    await todos_dan(403)
    assert waha.llamadas == [] and _claves_guardadas(ctx) == []         # ni una prueba contra WAHA ni una clave


# --- listar ------------------------------------------------------------------------------------------------------

async def test_listar_muestra_el_estado_y_nunca_la_clave(cliente, ctx):
    await _admin(cliente, ctx)
    sin_clave = await crear_worker_directo(ctx.db, "a-sin-clave", engine="GOWS", max_sesiones=10, disco_max_gb=5)
    wid = await _registrar(cliente)
    esc = await escenario_vinculable(ctx)
    await crear_link_directo(ctx.db, esc["tenant_id"], esc["line_id"], uuid.UUID(wid), esc["consent_id"],
                             estado="vinculado")

    r = await cliente.get(URL)
    assert r.status_code == 200
    assert [w["nombre"] for w in r.json()] == ["a-sin-clave", "w1"]               # por nombre, como la tabla
    assert all(set(w) == CAMPOS_LISTA for w in r.json())                           # ningún campo de más: ni la clave
    por_nombre = {w["nombre"]: w for w in r.json()}
    assert por_nombre["w1"] == {"id": wid, "nombre": "w1", "base_url": URL_WAHA, "engine": "NOWEB",
                                "max_sesiones": 50, "sesiones": 1, "disco_max_gb": 20.0, "disco_usado_gb": 0.0,
                                "activo": True, "clave_cargada": True}
    assert por_nombre["a-sin-clave"]["id"] == str(sin_clave) and por_nombre["a-sin-clave"]["engine"] == "GOWS"
    assert por_nombre["a-sin-clave"]["clave_cargada"] is False
    assert CLAVE not in r.text and base64.b64encode(CLAVE.encode()).decode() not in r.text


async def test_listar_no_audita(cliente, ctx):
    """Es una lectura que la Consola repite; abrirla ya queda auditado (consola_abierta)."""
    await _admin(cliente, ctx)
    await cliente.get(URL)
    assert await _filas(ctx, "SELECT 1 FROM access_audit_log") == []


# --- registrar ---------------------------------------------------------------------------------------------------

async def test_registrar_prueba_la_clave_la_guarda_y_audita(cliente, ctx, waha):
    admin = await _admin(cliente, ctx)
    r = await cliente.post(URL, json=_alta())
    assert r.status_code == 201
    cuerpo = r.json()
    assert set(cuerpo) == {"id", "nombre", "version", "engine"}
    assert (cuerpo["nombre"], cuerpo["version"], cuerpo["engine"]) == ("w1", "2026.8.2", "NOWEB")
    wid = uuid.UUID(cuerpo["id"])
    # la prueba fue un solo GET /api/server/version, al servidor indicado y con la clave en la cabecera
    assert waha.llamadas == ["GET /api/server/version"]
    assert waha.pedidos_version == [(f"{URL_WAHA}/api/server/version", CLAVE)]
    # la clave quedó en el SecretStore, y es la única
    assert ctx.secretos.get(nombre_clave_admin(wid)) == CLAVE.encode()
    assert _claves_guardadas(ctx) == [f"waha_admin__{wid}.b64"]
    # la fila, sin clave; y la auditoría con el admin como actor
    [fila] = await _filas(ctx, "SELECT * FROM waha_workers")
    assert (fila["nombre"], fila["base_url"], fila["engine"], fila["max_sesiones"], float(fila["disco_max_gb"]),
            float(fila["disco_usado_gb"]), fila["activo"]) == ("w1", URL_WAHA, "NOWEB", 50, 20.0, 0.0, True)
    assert CLAVE not in str(dict(fila))
    [a] = await _auditadas(ctx, "worker_registrado")
    assert (a["actor_user_id"], a["actor_rol"], a["tipo_objeto"], a["objeto_id"], json.loads(a["detalle"])) == \
           (admin, "admin", "waha_worker", wid, {})
    assert [w["nombre"] for w in (await cliente.get(URL)).json()] == ["w1"]


async def test_registrar_con_el_motor_pedido(cliente, ctx, waha):
    await _admin(cliente, ctx)
    waha.version_engine = "GOWS"
    r = await cliente.post(URL, json=_alta(engine="GOWS"))
    assert r.status_code == 201 and r.json()["engine"] == "GOWS"
    assert [w["engine"] for w in (await cliente.get(URL)).json()] == ["GOWS"]


@pytest.mark.parametrize("max_sesiones,disco_max_gb", [(1, 0.01), (500, 100000)])
async def test_registrar_en_los_limites_de_lo_permitido(cliente, ctx, max_sesiones, disco_max_gb):
    await _admin(cliente, ctx)
    await _registrar(cliente, max_sesiones=max_sesiones, disco_max_gb=disco_max_gb)
    [w] = (await cliente.get(URL)).json()
    assert (w["max_sesiones"], w["disco_max_gb"]) == (max_sesiones, disco_max_gb)


# (atributo del WAHA falso, valor, código que ve el navegador). Ninguno guarda nada.
FALLAS = [
    ("version_status", 401, "clave_incorrecta"),
    ("version_status", 403, "clave_incorrecta"),
    ("version_status", 404, "waha_no_responde"),
    ("version_status", 502, "waha_no_responde"),
    ("version_sin_red", True, "waha_no_responde"),
    ("version_crudo", b"<html>esto no es WAHA</html>", "waha_no_responde"),     # 200 que no es JSON
    ("version_crudo", b'["NOWEB"]', "waha_no_responde"),                         # JSON que no es un objeto
    ("version_crudo", b'"NOWEB"', "waha_no_responde"),
    ("version_crudo", b"null", "waha_no_responde"),
    ("version_engine", "GOWS", "motor_distinto"),
    ("version_engine", "WEBJS", "motor_distinto"),                                # el motor por defecto de WAHA
]


@pytest.mark.parametrize("atributo,valor,codigo", FALLAS)
async def test_si_waha_no_acepta_la_clave_no_se_guarda_nada(cliente, ctx, waha, atributo, valor, codigo):
    await _admin(cliente, ctx)
    setattr(waha, atributo, valor)
    r = await cliente.post(URL, json=_alta())
    assert r.status_code == 422 and r.json() == {"detail": {"error": codigo}}      # el código y nada más
    assert await _filas(ctx, "SELECT 1 FROM waha_workers") == []
    assert _claves_guardadas(ctx) == []
    assert await _auditadas(ctx, "worker_registrado") == []


async def test_nombre_repetido_es_409_y_no_pisa_la_clave_del_existente(cliente, ctx):
    await _admin(cliente, ctx)
    primero = await _registrar(cliente)
    r = await cliente.post(URL, json=_alta(base_url="http://otro.interno:3000", admin_key=CLAVE_B))
    assert r.status_code == 409 and r.json() == {"detail": {"error": "worker_duplicado"}}
    assert ctx.secretos.get(nombre_clave_admin(uuid.UUID(primero))) == CLAVE.encode()
    assert _claves_guardadas(ctx) == [f"waha_admin__{primero}.b64"]                # ni una clave huérfana
    assert [w["base_url"] for w in (await cliente.get(URL)).json()] == [URL_WAHA]
    assert len(await _auditadas(ctx, "worker_registrado")) == 1


# --- validación --------------------------------------------------------------------------------------------------

INVALIDOS = [
    ("nombre", "W1"), ("nombre", "con espacio"), ("nombre", ""), ("nombre", "a" * 41), ("nombre", "w1\n"),
    ("nombre", "ñandú"),
    ("base_url", "waha.interno:3000"), ("base_url", "ftp://waha.interno"), ("base_url", "http://"),
    ("base_url", "http://ho st"), ("base_url", "http://waha.interno\n"), ("base_url", "http://waha.interno?x=1"),
    ("base_url", "http://waha.interno#f"), ("base_url", "http://waha.interno:abc"), ("base_url", "http://" + "a" * 300),
    ("max_sesiones", 0), ("max_sesiones", 501), ("max_sesiones", -5), ("max_sesiones", 2.5),
    ("max_sesiones", "muchas"),
    ("disco_max_gb", 0), ("disco_max_gb", -1), ("disco_max_gb", 0.001), ("disco_max_gb", 1_000_000),
    ("disco_max_gb", "grande"),
    ("engine", "WEBJS"), ("engine", "noweb"),
    ("admin_key", "x" * 15), ("admin_key", ""), ("admin_key", "x" * 257),
]


@pytest.mark.parametrize("campo,valor", INVALIDOS)
async def test_un_dato_invalido_es_422_y_no_llega_a_waha(cliente, ctx, waha, campo, valor):
    await _admin(cliente, ctx)
    r = await cliente.post(URL, json=_alta(**{campo: valor}))
    assert r.status_code == 422, r.text
    assert [e["loc"] for e in r.json()["detail"]] == [["body", campo]]            # solo ese campo
    assert waha.llamadas == [] and _claves_guardadas(ctx) == []
    assert await _filas(ctx, "SELECT 1 FROM waha_workers") == []


@pytest.mark.parametrize("url", ["http://usuario:secreto@waha.interno:3000", "http://usuario@waha.interno"])
async def test_base_url_con_credenciales_se_rechaza_y_no_vuelve(cliente, ctx, waha, url):
    """La lista devuelve base_url: una credencial ahí se vería en la Consola. La clave viaja solo en X-Api-Key."""
    await _admin(cliente, ctx)
    r = await cliente.post(URL, json=_alta(base_url=url))
    assert r.status_code == 422 and [e["loc"] for e in r.json()["detail"]] == [["body", "base_url"]]
    assert "usuario" not in r.text and "secreto" not in r.text
    assert waha.llamadas == []


@pytest.mark.parametrize("numero", ["NaN", "Infinity", "-Infinity"])
async def test_disco_no_acepta_nan_ni_infinito(cliente, ctx, waha, numero):
    await _admin(cliente, ctx)
    crudo = json.dumps(_alta(disco_max_gb="@@")).replace('"@@"', numero)         # json.dumps no sabe emitirlos
    r = await cliente.post(URL, content=crudo.encode(), headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and waha.llamadas == []


@pytest.mark.parametrize("clave", ["clave-con-ñ-0123456789", "clave\ncon-salto-0123456", "clave\tcon-tab-0123456789",
                                   " empieza-con-un-espacio-0123", "termina-con-un-espacio-0123456 ",
                                   CLAVE + "\n"])                         # el salto de línea que trae un copiar y pegar
async def test_una_clave_que_no_cabe_en_una_cabecera_es_clave_incorrecta(cliente, ctx, waha, clave):
    """No sale a la red: httpx lanzaría un UnicodeEncodeError cuyo mensaje trae un carácter de la clave."""
    await _admin(cliente, ctx)
    r = await cliente.post(URL, json=_alta(admin_key=clave))
    assert r.status_code == 422 and r.json() == {"detail": {"error": "clave_incorrecta"}}
    wid = await crear_worker_directo(ctx.db, "otro")
    r = await cliente.put(f"{URL}/{wid}/clave", json={"admin_key": clave})
    assert r.status_code == 422 and r.json() == {"detail": {"error": "clave_incorrecta"}}
    assert waha.llamadas == [] and _claves_guardadas(ctx) == []


async def test_una_clave_con_espacios_en_el_medio_es_valida(cliente, ctx, waha):
    """WAHA toma la clave como un texto cualquiera: una frase de paso es válida y se manda tal cual."""
    await _admin(cliente, ctx)
    frase = "una frase de paso con espacios 123"
    wid = await _registrar(cliente, admin_key=frase)
    assert waha.pedidos_version == [(f"{URL_WAHA}/api/server/version", frase)]
    assert ctx.secretos.get(nombre_clave_admin(uuid.UUID(wid))) == frase.encode()


SECRETO = "secreto-que-no-debe-volver-0123"


async def test_un_422_no_repite_lo_que_se_mando(cliente, ctx):
    """El 422 de FastAPI repite el valor inválido en `input` (la clave si es corta) y, si falta un campo, el cuerpo
    entero. Estas rutas contestan solo qué campo falló y por qué tipo de error."""
    await _admin(cliente, ctx)
    wid = await crear_worker_directo(ctx.db)
    sin_nombre = {k: v for k, v in _alta(admin_key=SECRETO).items() if k != "nombre"}
    json_ct = {"Content-Type": "application/json"}
    pedidos = [
        ("POST", URL, {"json": _alta(admin_key=SECRETO[:15])}),                           # clave corta
        ("POST", URL, {"json": sin_nombre}),                                              # falta un campo
        ("POST", URL, {"json": _alta(admin_key=SECRETO, max_sesiones=[SECRETO])}),        # tipo equivocado
        ("POST", URL, {"json": _alta(admin_key=SECRETO, nombre=SECRETO + "!")}),          # formato inválido
        ("POST", URL, {"json": _alta(admin_key=SECRETO, base_url="x" + SECRETO)}),
        ("POST", URL, {"json": [SECRETO]}),                                               # no es un objeto
        ("POST", URL, {"content": ('{"admin_key": "' + SECRETO).encode(), "headers": json_ct}),   # JSON roto
        ("POST", URL, {"content": json.dumps(sin_nombre).encode()}),                      # sin Content-Type
        ("PUT", f"{URL}/{wid}/clave", {"json": {"admin_key": SECRETO[:15]}}),
        ("PUT", f"{URL}/{wid}/clave", {"json": {"clave": SECRETO}}),
        ("PUT", f"{URL}/{wid}/clave", {"json": [SECRETO]}),
        ("PUT", f"{URL}/{wid}/clave", {"content": ('{"admin_key": "' + SECRETO).encode(), "headers": json_ct}),
        ("PUT", f"{URL}/{wid}/disco", {"json": {"usado_gb": SECRETO}}),
    ]
    for metodo, ruta, args in pedidos:
        r = await cliente.request(metodo, ruta, **args)
        assert r.status_code == 422, (metodo, ruta, r.status_code, r.text)
        assert SECRETO[:15] not in r.text, (metodo, ruta)
        detalle = r.json()["detail"]
        assert detalle and all(set(e) == {"loc", "type"} for e in detalle), (metodo, ruta, detalle)


def test_los_modelos_no_muestran_la_clave_en_el_repr():
    for modelo in (WorkerIn(**_alta()), ClaveIn(admin_key=CLAVE)):
        for texto in (repr(modelo), str(modelo), modelo.model_dump_json()):
            assert CLAVE not in texto


# --- reemplazar la clave -----------------------------------------------------------------------------------------

async def test_reemplazar_prueba_la_clave_nueva_contra_el_servidor_del_worker(cliente, ctx, waha):
    admin = await _admin(cliente, ctx)
    wid = await _registrar(cliente, base_url="http://waha-b.interno:4000")
    waha.llamadas.clear()
    waha.pedidos_version.clear()
    r = await cliente.put(f"{URL}/{wid}/clave", json={"admin_key": CLAVE_B})
    assert r.status_code == 200 and r.json() == {"id": wid, "version": "2026.8.2", "engine": "NOWEB"}
    assert waha.pedidos_version == [("http://waha-b.interno:4000/api/server/version", CLAVE_B)]
    assert ctx.secretos.get(nombre_clave_admin(uuid.UUID(wid))) == CLAVE_B.encode()
    assert _claves_guardadas(ctx) == [f"waha_admin__{wid}.b64"]
    [a] = await _auditadas(ctx, "worker_clave_reemplazada")
    assert (a["actor_user_id"], a["actor_rol"], a["tipo_objeto"], str(a["objeto_id"]), json.loads(a["detalle"])) == \
           (admin, "admin", "waha_worker", wid, {})
    assert len(await _auditadas(ctx, "worker_registrado")) == 1                    # el alta sigue siendo una sola


async def test_reemplazar_verifica_el_motor_del_propio_worker(cliente, ctx, waha):
    await _admin(cliente, ctx)
    waha.version_engine = "GOWS"
    wid = await _registrar(cliente, engine="GOWS")
    r = await cliente.put(f"{URL}/{wid}/clave", json={"admin_key": CLAVE_B})
    assert r.status_code == 200 and r.json()["engine"] == "GOWS"
    waha.version_engine = "NOWEB"                         # el servidor ahora contesta con otro motor
    r = await cliente.put(f"{URL}/{wid}/clave", json={"admin_key": CLAVE})
    assert r.status_code == 422 and r.json() == {"detail": {"error": "motor_distinto"}}
    assert ctx.secretos.get(nombre_clave_admin(uuid.UUID(wid))) == CLAVE_B.encode()


@pytest.mark.parametrize("atributo,valor,codigo", FALLAS)
async def test_un_reemplazo_rechazado_no_cambia_la_clave(cliente, ctx, waha, atributo, valor, codigo):
    await _admin(cliente, ctx)
    wid = await _registrar(cliente)
    setattr(waha, atributo, valor)
    r = await cliente.put(f"{URL}/{wid}/clave", json={"admin_key": CLAVE_B})
    assert r.status_code == 422 and r.json() == {"detail": {"error": codigo}}
    assert ctx.secretos.get(nombre_clave_admin(uuid.UUID(wid))) == CLAVE.encode()
    assert await _auditadas(ctx, "worker_clave_reemplazada") == []


async def test_reemplazar_carga_la_clave_de_un_worker_que_no_la_tenia(cliente, ctx):
    await _admin(cliente, ctx)
    wid = await crear_worker_directo(ctx.db)
    assert [w["clave_cargada"] for w in (await cliente.get(URL)).json()] == [False]
    r = await cliente.put(f"{URL}/{wid}/clave", json={"admin_key": CLAVE_B})
    assert r.status_code == 200
    assert [w["clave_cargada"] for w in (await cliente.get(URL)).json()] == [True]
    assert ctx.secretos.get(nombre_clave_admin(wid)) == CLAVE_B.encode()


async def test_reemplazar_la_clave_de_un_worker_que_no_existe_es_404(cliente, ctx, waha):
    await _admin(cliente, ctx)
    r = await cliente.put(f"{URL}/{uuid.uuid4()}/clave", json={"admin_key": CLAVE})
    assert r.status_code == 404 and r.json() == {"detail": {"error": "worker_inexistente"}}
    assert waha.llamadas == [] and _claves_guardadas(ctx) == []


# --- disco usado -------------------------------------------------------------------------------------------------

async def test_actualizar_el_disco_usado_y_auditar(cliente, ctx, waha):
    admin = await _admin(cliente, ctx)
    wid = await crear_worker_directo(ctx.db, disco_max_gb=10)
    r = await cliente.put(f"{URL}/{wid}/disco", json={"usado_gb": 3.5})
    assert r.status_code == 200 and r.json() == {"id": str(wid)}
    assert [w["disco_usado_gb"] for w in (await cliente.get(URL)).json()] == [3.5]
    [a] = await _auditadas(ctx, "worker_disco_actualizado")
    assert (a["actor_user_id"], a["actor_rol"], a["tipo_objeto"], a["objeto_id"], json.loads(a["detalle"])) == \
           (admin, "admin", "waha_worker", wid, {})
    assert (await cliente.put(f"{URL}/{wid}/disco", json={"usado_gb": 0})).status_code == 200      # disco vacío
    assert waha.llamadas == []                    # es un dato que carga el admin: no se le pregunta a WAHA


@pytest.mark.parametrize("cuerpo", [{"usado_gb": -1}, {"usado_gb": "mucho"}, {"usado_gb": 1_000_000}, {}, [3]])
async def test_disco_invalido_es_422_y_no_cambia_nada(cliente, ctx, cuerpo):
    await _admin(cliente, ctx)
    wid = await crear_worker_directo(ctx.db)
    await cliente.put(f"{URL}/{wid}/disco", json={"usado_gb": 2})
    r = await cliente.put(f"{URL}/{wid}/disco", json=cuerpo)
    assert r.status_code == 422
    assert [w["disco_usado_gb"] for w in (await cliente.get(URL)).json()] == [2.0]
    assert len(await _auditadas(ctx, "worker_disco_actualizado")) == 1


async def test_actualizar_el_disco_de_un_worker_que_no_existe_es_404(cliente, ctx):
    await _admin(cliente, ctx)
    r = await cliente.put(f"{URL}/{uuid.uuid4()}/disco", json={"usado_gb": 1})
    assert r.status_code == 404 and r.json() == {"detail": {"error": "worker_inexistente"}}
    assert await _auditadas(ctx, "worker_disco_actualizado") == []


# --- la clave no se filtra ---------------------------------------------------------------------------------------

async def test_la_clave_no_se_filtra_a_logs_salida_respuestas_ni_base(cliente, ctx, waha, caplog, capsys):
    await _admin(cliente, ctx)
    respuestas = []
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("app.radar.workers").debug("canario")        # el captor está vivo: se vería lo que loguee
        respuestas.append(await cliente.post(URL, json=_alta()))                                   # 201
        wid = respuestas[0].json()["id"]
        respuestas.append(await cliente.get(URL))                                                  # 200
        respuestas.append(await cliente.put(f"{URL}/{wid}/clave", json={"admin_key": CLAVE_B}))    # 200
        respuestas.append(await cliente.put(f"{URL}/{wid}/disco", json={"usado_gb": 2}))           # 200
        waha.version_status = 401
        respuestas.append(await cliente.put(f"{URL}/{wid}/clave", json={"admin_key": CLAVE}))      # 422 incorrecta
        respuestas.append(await cliente.post(URL, json=_alta(nombre="w2")))                        # 422 incorrecta
        waha.version_status, waha.version_sin_red = 200, True
        respuestas.append(await cliente.post(URL, json=_alta(nombre="w3")))                        # 422 no responde
        waha.version_sin_red = False
        respuestas.append(await cliente.post(URL, json=_alta()))                                   # 409 duplicado
        respuestas.append(await cliente.post(URL, json=_alta(admin_key=CLAVE[:10])))               # 422 de validación
        respuestas.append(await cliente.post(URL, json=_alta(admin_key=CLAVE + "\n")))             # 422 incorrecta
    assert [r.status_code for r in respuestas] == [201, 200, 200, 200, 422, 422, 422, 409, 422, 422]
    assert "canario" in caplog.text

    secretos = [CLAVE, CLAVE_B, CLAVE[:10]]
    secretos += [base64.b64encode(s.encode()).decode() for s in secretos]      # así queda en el archivo del SecretStore
    salida = capsys.readouterr()
    donde = {
        "logs": caplog.text,
        "stdout y stderr": salida.out + salida.err,
        "respuestas": "\n".join(f"{r.headers}\n{r.text}" for r in respuestas),
        "access_audit_log": "\n".join(f["t"] for f in await _filas(ctx, "SELECT row_to_json(a)::text AS t "
                                                                        "FROM access_audit_log a")),
        "waha_workers": "\n".join(f["t"] for f in await _filas(ctx, "SELECT row_to_json(w)::text AS t "
                                                                    "FROM waha_workers w")),
    }
    assert donde["access_audit_log"] and donde["waha_workers"]                      # la prueba mira algo
    for lugar, texto in donde.items():
        for secreto in secretos:
            assert secreto not in texto, (lugar, secreto[:6])


# --- la pantalla -------------------------------------------------------------------------------------------------

def test_consola_tiene_servidores_waha():
    html = (ESTATICOS / "consola.html").read_text(encoding="utf-8")
    _sin_inline(html)
    ids = re.findall(r'\bid="([a-z0-9-]+)"', html)
    assert IDS_WORKERS | IDS_WORKERS_EXTRA <= set(ids), (IDS_WORKERS | IDS_WORKERS_EXTRA) - set(ids)
    assert len(ids) == len(set(ids)), "ids repetidos"
    # el campo de la clave es de contraseña y el navegador no la recuerda
    for id_ in ("worker-clave", "worker-clave-nueva"):
        campo = re.search(rf'<input\b[^>]*\bid="{id_}"[^>]*>', html)
        assert campo and 'type="password"' in campo.group(0) and 'autocomplete="off"' in campo.group(0), id_
    for id_ in ("worker-motor", "worker-reemplazo"):
        assert re.search(rf'<select\b[^>]*\bid="{id_}"', html), id_
    assert '<option value="NOWEB">' in html and '<option value="GOWS">' in html     # los dos motores de MOTORES
    assert re.search(r'<h2>Servidores WAHA</h2>', html) and re.search(r'<tbody id="workers">', html)
    # la Consola nunca muestra la clave, y lo dicen la nota de la sección, la del pie de la tabla de servidores y la
    # del pie de la tabla de líneas
    notas = re.findall(r'<p class="nota">([^<]*nunca muestra la clave[^<]*)</p>', html)
    assert len(notas) >= 3, notas


def test_el_js_de_servidores_waha():
    js = (ESTATICOS / "radar.js").read_text(encoding="utf-8")
    assert re.search(r'pedir\("GET",\s*"/radar/admin/workers"\)', js)
    assert re.search(r'pedir\("POST",\s*"/radar/admin/workers",', js)
    assert re.search(r'"/radar/admin/workers/"\s*\+[^;]*\+\s*"/clave"', js)
    assert re.search(r'"/radar/admin/workers/"\s*\+[^;]*\+\s*"/disco"', js)
    assert "window.prompt(" in js[js.index("function actualizarDisco"):]               # el botón "Disco" de cada fila
    for texto in ("WAHA rechazó la clave.", "No se pudo conectar con ese servidor WAHA.",
                  "El servidor WAHA usa otro motor.", "Ya hay un servidor con ese nombre."):
        assert texto in js, texto
    # la clave se vacía al terminar, con éxito o sin él: va en un finally
    for id_ in ("worker-clave", "worker-clave-nueva"):
        assert re.search(rf'finally\s*\{{[^}}]*\$\("{id_}"\)\.value\s*=\s*""', js), id_
    # probar la clave es un pedido a otro servidor, que puede tardar hasta el timeout: se avisa que está pasando
    assert "Probando la clave contra el servidor" in js
    # los errores de la sección salen en la sección (#worker-error), no arriba de la página: los dos formularios y el
    # botón "Disco" de cada fila
    for fragmento in ('registrarWorker, "worker-error")', 'reemplazarClave, "worker-error")',
                      'accion(() => actualizarDisco(w), "worker-error")'):
        assert fragmento in js, fragmento
