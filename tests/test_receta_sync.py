"""
Sincronización de la referencia de recetas desde un archivo (28/9): subir →
vista previa (nada se aplica) → confirmar o descartar. Reemplaza a la siembra
automática desde un CSV al arrancar, que en producción leyó 0 filas sin
avisar porque el catálogo restaurado desde Redis pisaba el CSV con otro
formato de columnas (ver docstring de app/services/receta_referencia.py).
"""
import hashlib
import io
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app.services.db as dbmod
from app.config import get_settings
from app.models.sync import CatalogItemIn
from app.services import receta_referencia as rr
from app.services.catalog_store import CatalogStore
from app.services.db import Database

BR = "suc"
HEADERS = {"x-bo-key": "CLAVE"}


@pytest.fixture(autouse=True)
def _mapa_limpio(monkeypatch):
    monkeypatch.setattr(rr, "_MAPA", {})


@pytest.fixture
async def db(pg_dsn):
    d = Database(pg_dsn)
    assert await d.connect()
    await d.execute("TRUNCATE catalog_extras, catalog_items, branches, receta_referencia, "
                    "receta_sincronizaciones RESTART IDENTITY")
    await d.execute("INSERT INTO branches (branch_id, nombre, token_hash) VALUES ($1, 'Suc', 'x') "
                    "ON CONFLICT DO NOTHING", BR)
    yield d
    await d.close()


def _it(eid, name, barcodes, category="Medicamentos", stock=2):
    return CatalogItemIn(
        external_id=eid, hash=hashlib.sha256(eid.encode()).hexdigest(), barcodes=barcodes,
        troquel=None, name=name, brand="", drug=None, form=None, category=category,
        rubro="Medicamentos" if category == "Medicamentos" else "", subrubro="",
        therapeutic_actions=[], price="1000", stock=stock, visible=True, active=True)


def _xlsx_bytes(headers: list[str], filas: list[list]) -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for f in filas:
        ws.append(f)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Parser: leer_archivo ─────────────────────────────────────────────────────

_CSV_BASE = (
    "SKU,Nombre,Precio,Marca,Laboratorio,Codigo_Barras_1,Codigo_Barras_2,Codigo_Barras_3,"
    "Codigo_Barras_4,Categoria,Es_Medicamento\n"
    "1,Atenolol Gador 50 Com X30,9000,Gador,Gador,7790001000011,,,,Medicamentos Bajo Receta,true\n"
    "2,Buscapina N Cto Gts X20,5000,Boehringer,Boehringer,7790002000028,,,,Venta Libre,true\n"
).encode("utf-8")


def test_leer_catalogo_base_csv():
    filas, info = rr.leer_archivo(_CSV_BASE, "base.csv")
    m = {b: f for b, _n, f in filas}
    assert m == {"7790001000011": "si", "7790002000028": "no"}
    assert info["formato"] == "catalogo_base"
    assert info["filas"] == 2
    assert info["codigos_validos"] == 2
    assert info["sin_codigo"] == 0
    assert info["conflictos_en_archivo"] == 0
    assert "Codigo_Barras_1" in info["columnas"]["barcodes"]


def test_leer_catalogo_bot_csv():
    data = (
        "barcode,sku_nombre,categoria,requiere_receta\n"
        "7790001000011,Atenolol Gador 50,Medicamentos,si\n"
        "7790002000028,Buscapina N Gotas,Medicamentos,no\n"
        "7790009999999,Producto Sin Flag,Medicamentos Bajo Receta,\n"
    ).encode("utf-8")
    filas, info = rr.leer_archivo(data, "bot.csv")
    m = {b: f for b, _n, f in filas}
    assert m["7790001000011"] == "si"      # columna explícita
    assert m["7790002000028"] == "no"      # columna explícita
    assert m["7790009999999"] == "si"      # sin flag: cae a la categoría
    assert info["formato"] == "catalogo_bot"


def test_leer_xlsx_con_alias():
    data = _xlsx_bytes(
        ["Código de barras", "Descripción", "Condición de venta"],
        [
            ["7790001000011", "Atenolol Gador 50", "Bajo receta"],
            ["7790002000028", "Buscapina N Gotas", "Venta libre"],
        ],
    )
    filas, info = rr.leer_archivo(data, "planilla.xlsx")
    m = {b: f for b, _n, f in filas}
    assert m == {"7790001000011": "si", "7790002000028": "no"}
    assert info["columnas"]["nombre"] == "Descripción"
    assert info["columnas"]["flag"] == "Condición de venta"


def test_leer_csv_latin1():
    # 'ó' (ó) como byte 0xF3 de latin-1 no es UTF-8 válido: fuerza el fallback.
    texto = ("barcode,sku_nombre,categoria\n"
            "7790001000011,Loción Capilar,Medicamentos Bajo Receta\n")
    data = texto.encode("latin-1")
    filas, info = rr.leer_archivo(data, "latin1.csv")
    assert filas == [("7790001000011", "Loción Capilar", "si")]


def test_leer_csv_delimitador_punto_y_coma():
    data = (
        "barcode;sku_nombre;categoria\n"
        "7790001000011;Atenolol Gador 50;Medicamentos Bajo Receta\n"
    ).encode("utf-8")
    filas, info = rr.leer_archivo(data, "pyc.csv")
    assert filas == [("7790001000011", "Atenolol Gador 50", "si")]


def test_venta_libre_columna_inversa():
    data = (
        "barcode,sku_nombre,venta_libre\n"
        "7790001000011,Atenolol Gador 50,no\n"    # NO es venta libre -> "si"
        "7790002000028,Buscapina N Gotas,si\n"     # SI es venta libre -> "no"
    ).encode("utf-8")
    filas, info = rr.leer_archivo(data, "vl.csv")
    m = {b: f for b, _n, f in filas}
    assert m == {"7790001000011": "si", "7790002000028": "no"}
    assert info["columnas"]["venta_libre"] == "venta_libre"


def test_conflicto_en_archivo_gana_si():
    data = (
        "barcode,sku_nombre,requiere_receta\n"
        "7790001000011,Producto A,no\n"
        "7790001000011,Producto A (dup),si\n"
    ).encode("utf-8")
    filas, info = rr.leer_archivo(data, "dup.csv")
    assert filas == [("7790001000011", "Producto A (dup)", "si")]
    assert info["conflictos_en_archivo"] == 1


def test_filas_sin_codigo_contadas():
    data = (
        "barcode,sku_nombre,requiere_receta\n"
        "7790001000011,Con codigo,si\n"
        ",Sin codigo,no\n"
        "123,Codigo corto,no\n"
    ).encode("utf-8")
    filas, info = rr.leer_archivo(data, "sc.csv")
    assert len(filas) == 1
    assert info["filas"] == 3
    assert info["sin_codigo"] == 2


def test_filas_desde_csv_wrapper_sigue_igual():
    """Compat: quien llamaba filas_desde_csv (formato viejo) sigue andando."""
    filas = rr.filas_desde_csv(_CSV_BASE)
    assert {b: f for b, _n, f in filas} == {"7790001000011": "si", "7790002000028": "no"}


# ── Preview: preparar_sincronizacion (nada se aplica) ───────────────────────

async def test_preview_referencia_agrega_cambia_sin_aplicar(db):
    await rr.reemplazar(db, [
        ("1111111", "Producto Viejo A", "no"),
        ("2222222", "Producto Viejo B", "si"),
    ], fuente="inicial")
    assert rr.cargada() == 2

    archivo = (
        "barcode,sku_nombre,requiere_receta\n"
        "1111111,Producto Viejo A,si\n"    # cambia: no -> si (pasa a receta)
        "2222222,Producto Viejo B,si\n"    # sin cambio
        "3333333,Producto Nuevo,no\n"      # agrega
    ).encode("utf-8")

    res = await rr.preparar_sincronizacion(db, archivo, "sync.csv", "actualizar", autor="belen")
    assert res["estado"] == "pendiente" and res["modo"] == "actualizar"
    ref = res["referencia"]
    assert (ref["agrega"], ref["cambia"], ref["sin_cambios"], ref["quita"]) == (1, 1, 1, 0)
    assert ref["pasan_a_receta"] == 1 and ref["pasan_a_venta_libre"] == 0
    assert ref["antes"] == {"si": 1, "no": 1, "ambiguo": 0, "total": 2}
    assert ref["despues"] == {"si": 2, "no": 1, "ambiguo": 0, "total": 3}
    assert [e["barcode"] for e in ref["ejemplos_pasan_a_receta"]] == ["1111111"]
    assert [e["barcode"] for e in ref["ejemplos_agrega"]] == ["3333333"]
    assert res["archivo_info"]["formato"] == "catalogo_bot"

    # Nada se aplicó: ni la memoria ni la base.
    assert rr.cargada() == 2
    filas_db = {r["barcode"]: r["requiere_receta"]
               for r in await db.fetch("SELECT barcode, requiere_receta FROM receta_referencia")}
    assert filas_db == {"1111111": "no", "2222222": "si"}


async def test_preview_reemplazar_calcula_quita_y_descarta_pendiente_previo(db):
    await rr.reemplazar(db, [("1111111", "A", "no"), ("2222222", "B", "si")], fuente="inicial")

    archivo1 = "barcode,sku_nombre,requiere_receta\n1111111,A,no\n".encode("utf-8")
    res1 = await rr.preparar_sincronizacion(db, archivo1, "f1.csv", "reemplazar")
    assert res1["referencia"]["quita"] == 1       # 2222222 no está en el archivo
    assert res1["referencia"]["sin_cambios"] == 1

    archivo2 = "barcode,sku_nombre,requiere_receta\n1111111,A,si\n".encode("utf-8")
    res2 = await rr.preparar_sincronizacion(db, archivo2, "f2.csv", "reemplazar")
    assert res2["id"] != res1["id"]

    filas = {r["id"]: r for r in await db.fetch(
        "SELECT id, estado, filas FROM receta_sincronizaciones ORDER BY id")}
    assert filas[res1["id"]]["estado"] == "descartada"
    assert filas[res1["id"]]["filas"] is None
    assert filas[res2["id"]]["estado"] == "pendiente"


async def test_preview_catalogo_impacto_y_marca_manual_en_conflicto(db):
    store = CatalogStore(db)
    await store.upsert_items(BR, [
        _it("a", "ATENOLOL GADOR 50 mg COM x 30", ["7790001000011"]),
        _it("c", "COLPURIL RETARD CAP x 50", ["9999999"], stock=5),
        _it("d", "PRODUCTO MARCADO A MANO", ["8888888"], stock=1),
    ])
    await rr.recalcular_catalogo(db)   # sin referencia: medicamentos quedan "ambiguo"
    await store.set_extras(BR, "d", requiere_receta_override="no")   # marca manual: venta libre

    archivo = (
        "barcode,sku_nombre,requiere_receta\n"
        "7790001000011,Atenolol,si\n"
        "8888888,Producto Marcado,si\n"
    ).encode("utf-8")
    res = await rr.preparar_sincronizacion(db, archivo, "sync.csv", "actualizar")
    cat = res["catalogo"]

    assert cat["antes"] == {"si": 0, "no": 1, "ambiguo": 2}          # d: "no" por la marca manual
    assert cat["despues"]["si"] == 1                                 # a: ambiguo -> si
    assert cat["despues"]["no"] == 1                                 # d sigue "no": la marca manual gana
    assert cat["a_validar_con_stock_antes"] == 2                     # a y c: ambiguo con stock
    assert any(e["external_id"] == "a" for e in cat["ejemplos_pasan_a_receta"])

    assert res["marcas_manuales_en_conflicto_total"] == 1
    conflicto = res["marcas_manuales_en_conflicto"][0]
    assert conflicto == {"external_id": "d", "nombre": "PRODUCTO MARCADO A MANO",
                         "marca_manual": "no", "segun_archivo": "si"}


async def test_preview_sin_codigos_valida_raise(db):
    archivo = "nombre,categoria\nProducto,Medicamentos\n".encode("utf-8")
    with pytest.raises(ValueError, match="código de barras"):
        await rr.preparar_sincronizacion(db, archivo, "sin_cb.csv", "actualizar")


async def test_preview_modo_invalido_raise(db):
    archivo = "barcode,sku_nombre,requiere_receta\n7790001000011,A,si\n".encode("utf-8")
    with pytest.raises(ValueError, match="modo"):
        await rr.preparar_sincronizacion(db, archivo, "f.csv", "borrar_todo")


# ── Confirmar / descartar ────────────────────────────────────────────────────

async def test_confirmar_actualizar_aplica_upsert_sin_borrar(db):
    await rr.reemplazar(db, [("1111111", "A", "no"), ("2222222", "B", "si")], fuente="inicial")
    archivo = ("barcode,sku_nombre,requiere_receta\n"
              "1111111,A,si\n"
              "3333333,C,no\n").encode("utf-8")
    prev = await rr.preparar_sincronizacion(db, archivo, "f.csv", "actualizar")

    res = await rr.confirmar_sincronizacion(db, prev["id"])
    assert res["estado"] == "aplicada" and res["referencia"] == 3

    filas = {r["barcode"]: r["requiere_receta"]
            for r in await db.fetch("SELECT barcode, requiere_receta FROM receta_referencia")}
    assert filas == {"1111111": "si", "2222222": "si", "3333333": "no"}
    assert rr.cargada() == 3

    fila_sync = await db.fetchrow(
        "SELECT estado, filas FROM receta_sincronizaciones WHERE id = $1", prev["id"])
    assert fila_sync["estado"] == "aplicada" and fila_sync["filas"] is None


async def test_confirmar_reemplazar_borra_lo_que_no_esta_en_el_archivo(db):
    await rr.reemplazar(db, [("1111111", "A", "no"), ("2222222", "B", "si")], fuente="inicial")
    archivo = "barcode,sku_nombre,requiere_receta\n1111111,A,si\n".encode("utf-8")
    prev = await rr.preparar_sincronizacion(db, archivo, "f.csv", "reemplazar")

    res = await rr.confirmar_sincronizacion(db, prev["id"])
    assert res["referencia"] == 1
    barcodes = {r["barcode"] for r in await db.fetch("SELECT barcode FROM receta_referencia")}
    assert barcodes == {"1111111"}


async def test_confirmar_recalcula_catalogo(db):
    store = CatalogStore(db)
    await store.upsert_items(BR, [_it("a", "ATENOLOL GADOR 50 mg COM x 30", ["7790001000011"])])
    await rr.recalcular_catalogo(db)
    antes = await db.fetch("SELECT requiere_receta FROM catalog_items WHERE external_id='a'")
    assert antes[0]["requiere_receta"] == "ambiguo"

    archivo = "barcode,sku_nombre,requiere_receta\n7790001000011,Atenolol,si\n".encode("utf-8")
    prev = await rr.preparar_sincronizacion(db, archivo, "f.csv", "actualizar")
    res = await rr.confirmar_sincronizacion(db, prev["id"])
    assert res["cambiados"] == 1 and res["totales"]["si"] == 1

    despues = await db.fetch("SELECT requiere_receta FROM catalog_items WHERE external_id='a'")
    assert despues[0]["requiere_receta"] == "si"


async def test_confirmar_404_id_desconocido(db):
    with pytest.raises(LookupError):
        await rr.confirmar_sincronizacion(db, 999999)


async def test_confirmar_409_ya_resuelta(db):
    archivo = "barcode,sku_nombre,requiere_receta\n7790001000011,A,si\n".encode("utf-8")
    prev = await rr.preparar_sincronizacion(db, archivo, "f.csv", "actualizar")
    await rr.confirmar_sincronizacion(db, prev["id"])
    with pytest.raises(rr.SincronizacionConflicto, match="aplicada"):
        await rr.confirmar_sincronizacion(db, prev["id"])


async def test_confirmar_409_vencida(db):
    archivo = "barcode,sku_nombre,requiere_receta\n7790001000011,A,si\n".encode("utf-8")
    prev = await rr.preparar_sincronizacion(db, archivo, "f.csv", "actualizar")
    hace_3h = datetime.now(timezone.utc) - timedelta(hours=3)
    await db.execute("UPDATE receta_sincronizaciones SET created_at = $1 WHERE id = $2",
                     hace_3h, prev["id"])
    with pytest.raises(rr.SincronizacionConflicto, match="venci"):
        await rr.confirmar_sincronizacion(db, prev["id"])


async def test_confirmar_409_referencia_cambio(db):
    archivo = "barcode,sku_nombre,requiere_receta\n7790001000011,A,si\n".encode("utf-8")
    prev = await rr.preparar_sincronizacion(db, archivo, "f.csv", "actualizar")
    # Otra sincronización se aplicó (o se editó a mano) mientras tanto.
    await rr.reemplazar(db, [("9999999", "Otro", "no")], fuente="otro")
    with pytest.raises(rr.SincronizacionConflicto, match="cambi"):
        await rr.confirmar_sincronizacion(db, prev["id"])


async def test_descartar(db):
    archivo = "barcode,sku_nombre,requiere_receta\n7790001000011,A,si\n".encode("utf-8")
    prev = await rr.preparar_sincronizacion(db, archivo, "f.csv", "actualizar")

    res = await rr.descartar_sincronizacion(db, prev["id"])
    assert res == {"id": prev["id"], "estado": "descartada"}

    with pytest.raises(rr.SincronizacionConflicto):
        await rr.confirmar_sincronizacion(db, prev["id"])
    with pytest.raises(rr.SincronizacionConflicto):
        await rr.descartar_sincronizacion(db, prev["id"])
    with pytest.raises(LookupError):
        await rr.descartar_sincronizacion(db, 999999)

    assert await db.fetch("SELECT 1 FROM receta_referencia") == []


async def test_listar_y_obtener_sin_filas(db):
    archivo = "barcode,sku_nombre,requiere_receta\n7790001000011,A,si\n".encode("utf-8")
    prev = await rr.preparar_sincronizacion(db, archivo, "f.csv", "actualizar")

    listado = await rr.listar_sincronizaciones(db)
    assert len(listado) == 1 and listado[0]["id"] == prev["id"]
    assert "filas" not in listado[0]
    assert listado[0]["resumen"]["referencia"]["agrega"] == 1

    uno = await rr.obtener_sincronizacion(db, prev["id"])
    assert uno["id"] == prev["id"] and "filas" not in uno
    assert await rr.obtener_sincronizacion(db, 999999) is None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@pytest.fixture
async def cliente(db, monkeypatch):
    from app.routers import backoffice_branches as bb
    monkeypatch.setattr(get_settings(), "bo_key", "CLAVE")

    recargas = []

    class _FakeRefresher:
        async def recargar(self):
            recargas.append(1)

    monkeypatch.setattr(bb, "get_catalog_refresher", lambda: _FakeRefresher())

    prev = dbmod._instance
    dbmod._instance = db
    app = FastAPI()
    app.include_router(bb.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac, recargas
    dbmod._instance = prev


async def test_endpoint_sincronizar_preview_confirmar(cliente):
    ac, recargas = cliente
    archivo = "barcode,sku_nombre,requiere_receta\n7790001000011,Atenolol,si\n".encode("utf-8")

    r = await ac.post("/bo/receta/sincronizar", headers=HEADERS,
                      params={"modo": "actualizar", "autor": "belen"},
                      files={"file": ("f.csv", archivo, "text/csv")})
    assert r.status_code == 200
    body = r.json()
    assert body["estado"] == "pendiente" and body["referencia"]["agrega"] == 1
    sid = body["id"]

    r = await ac.post(f"/bo/receta/sincronizar/{sid}/confirmar", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["estado"] == "aplicada"
    assert recargas == [1]     # recarga del catálogo best-effort tras confirmar

    r = await ac.post(f"/bo/receta/sincronizar/{sid}/confirmar", headers=HEADERS)
    assert r.status_code == 409


async def test_endpoint_sincronizar_archivo_vacio_400(cliente):
    ac, _recargas = cliente
    r = await ac.post("/bo/receta/sincronizar", headers=HEADERS,
                      files={"file": ("f.csv", b"", "text/csv")})
    assert r.status_code == 400


async def test_endpoint_sincronizar_sin_codigos_422(cliente):
    ac, _recargas = cliente
    archivo = "nombre,categoria\nProducto,Medicamentos\n".encode("utf-8")
    r = await ac.post("/bo/receta/sincronizar", headers=HEADERS,
                      files={"file": ("f.csv", archivo, "text/csv")})
    assert r.status_code == 422


async def test_endpoint_confirmar_404(cliente):
    ac, _recargas = cliente
    r = await ac.post("/bo/receta/sincronizar/999999/confirmar", headers=HEADERS)
    assert r.status_code == 404


async def test_endpoint_descartar(cliente):
    ac, _recargas = cliente
    archivo = "barcode,sku_nombre,requiere_receta\n7790001000011,Atenolol,si\n".encode("utf-8")
    r = await ac.post("/bo/receta/sincronizar", headers=HEADERS,
                      files={"file": ("f.csv", archivo, "text/csv")})
    sid = r.json()["id"]

    r = await ac.post(f"/bo/receta/sincronizar/{sid}/descartar", headers=HEADERS)
    assert r.status_code == 200 and r.json() == {"id": sid, "estado": "descartada"}

    r = await ac.post(f"/bo/receta/sincronizar/{sid}/descartar", headers=HEADERS)
    assert r.status_code == 409


async def test_endpoint_listar_y_uno(cliente):
    ac, _recargas = cliente
    archivo = "barcode,sku_nombre,requiere_receta\n7790001000011,Atenolol,si\n".encode("utf-8")
    r = await ac.post("/bo/receta/sincronizar", headers=HEADERS,
                      files={"file": ("f.csv", archivo, "text/csv")})
    sid = r.json()["id"]

    r = await ac.get("/bo/receta/sincronizaciones", headers=HEADERS)
    assert r.status_code == 200
    lista = r.json()["sincronizaciones"]
    assert len(lista) == 1 and lista[0]["id"] == sid
    assert "filas" not in lista[0]

    r = await ac.get(f"/bo/receta/sincronizaciones/{sid}", headers=HEADERS)
    assert r.status_code == 200 and r.json()["id"] == sid

    r = await ac.get("/bo/receta/sincronizaciones/999999", headers=HEADERS)
    assert r.status_code == 404


async def test_endpoint_sin_auth_403(cliente):
    ac, _recargas = cliente
    r = await ac.get("/bo/receta/sincronizaciones")
    assert r.status_code == 403
