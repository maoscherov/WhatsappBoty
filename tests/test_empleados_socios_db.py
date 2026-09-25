"""
Persistencia en Postgres del padrón de socios y del listado de empleados
(20% de descuento, no acumulable con el de socio).
"""

import io

import pytest

from app.services.db import Database
from app.services.empleado_service import (
    EmpleadoService, cargar_desde_db as cargar_empleados_db,
    guardar_en_db as guardar_empleados_db, parsear_planilla,
)
from app.services.socio_service import (
    SocioService, cargar_desde_db as cargar_socios_db,
    guardar_en_db as guardar_socios_db,
)


@pytest.fixture
async def db(pg_dsn):
    d = Database(pg_dsn)
    assert await d.connect()
    await d.execute("TRUNCATE socios, empleados RESTART IDENTITY")
    yield d
    await d.close()


def _xlsx_empleados() -> bytes:
    """Arma un XLSX con la misma forma que la planilla real: filas vacías
    arriba, encabezado en cualquier fila, columna de orden, "Apellido y
    Nombre" en formato "APELLIDO, NOMBRE" y celulares en varios formatos
    (uno inválido)."""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([None, None, None])          # fila vacía
    ws.append([None, None, None])          # otra fila vacía
    ws.append(["N°", "Apellido y Nombre", "Celular"])   # encabezado
    ws.append([1, "PEREZ, JUAN", "3415551234"])          # directo, 10 dígitos
    ws.append([2, "GOMEZ, MARIA", "0341 15-555-2222"])   # con 0 y 15
    ws.append([3, "SIN CELULAR VALIDO", "abc"])          # inválido
    ws.append([4, "LOPEZ, ANA", "+54 9 341 555-3333"])   # con 54/9

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parsear_planilla_xlsx():
    data = _xlsx_empleados()
    empleados, reporte = parsear_planilla(data, "listado.xlsx", "mutual")

    assert reporte["total_filas"] == 4    # las filas vacías no cuentan
    assert reporte["cargados"] == 3       # una queda afuera por celular inválido
    assert len(reporte["sin_celular_valido"]) == 1
    assert reporte["sin_celular_valido"][0]["celular"] == "abc"
    assert "celular" in reporte["columnas_reconocidas"]
    assert "nombre_completo" in reporte["columnas_reconocidas"]

    por_celular = {e["celular"]: e for e in empleados}
    assert "3415551234" in por_celular
    assert por_celular["3415551234"]["apellido"] == "Perez"
    assert por_celular["3415551234"]["nombre"] == "Juan"
    assert por_celular["3415551234"]["nombre_pila"] == "Juan"
    assert por_celular["3415551234"]["grupo"] == "mutual"

    assert "3415552222" in por_celular   # sin 0, sin 15
    assert "3415553333" in por_celular   # sin 54, sin 9


def test_find_by_phone_formato_whatsapp():
    empleados, _ = parsear_planilla(_xlsx_empleados(), "listado.xlsx", "mutual")
    svc = EmpleadoService()
    svc.cargar_desde_lista(empleados)

    encontrado = svc.find_by_phone("5493415551234")   # formato WA típico
    assert encontrado is not None
    assert encontrado["apellido"] == "Perez"
    assert encontrado["grupo"] == "mutual"

    assert svc.find_by_phone("5493419999999") is None


async def test_empleados_guardar_y_cargar_por_grupo(db):
    empleados_a, _ = parsear_planilla(_xlsx_empleados(), "listado.xlsx", "mutual")
    await guardar_empleados_db(db, empleados_a, "mutual")

    empleados_b = [{
        "nombre": "Carlos", "apellido": "Diaz", "nombre_pila": "Carlos",
        "celular": "3415559999", "celular_original": "3415559999", "activo": True,
    }]
    await guardar_empleados_db(db, empleados_b, "cooperativa")

    svc = EmpleadoService()
    total = await cargar_empleados_db(db, svc)
    assert total == 4    # 3 de mutual + 1 de cooperativa

    por_grupo: dict[str, int] = {}
    for e in svc._empleados:
        por_grupo[e["grupo"]] = por_grupo.get(e["grupo"], 0) + 1
    assert por_grupo == {"mutual": 3, "cooperativa": 1}

    # Recargar solo "mutual" con una lista distinta no borra "cooperativa"
    empleados_a2 = empleados_a[:1]
    await guardar_empleados_db(db, empleados_a2, "mutual")
    svc2 = EmpleadoService()
    total2 = await cargar_empleados_db(db, svc2)
    assert total2 == 2    # 1 de mutual + 1 de cooperativa (intacta)
    assert svc2.find_by_phone("3415559999")["grupo"] == "cooperativa"


async def test_empleados_sin_grupo_reemplaza_la_lista_completa(db):
    # El backoffice sube sin grupo: una planilla nueva reemplaza TODO,
    # incluso lo que se hubiera cargado antes con un grupo.
    empleados, _ = parsear_planilla(_xlsx_empleados(), "listado.xlsx", "general")
    await guardar_empleados_db(db, [{
        "nombre": "Carlos", "apellido": "Diaz", "nombre_pila": "Carlos",
        "celular": "3415559999", "celular_original": "3415559999", "activo": True,
    }], "cooperativa")
    await guardar_empleados_db(db, empleados)
    svc = EmpleadoService()
    assert await cargar_empleados_db(db, svc) == 3
    assert svc.find_by_phone("3415559999") is None

    await guardar_empleados_db(db, empleados[:1])
    svc2 = EmpleadoService()
    assert await cargar_empleados_db(db, svc2) == 1
    assert {e["grupo"] for e in svc2._empleados} == {"general"}


async def test_socios_guardar_y_cargar_desde_db(db, tmp_path):
    p = tmp_path / "padron.csv"
    p.write_text(
        "APELLIDO,NOMBRE,DNI,SOCIO,CELULAR,DOMICILIO\n"
        "Muff,Claudia,20111222,4001,3415550001,Mitre 100\n"
        "Perez,Jose Maria,20333444,4002,3415550002,Salta 200\n",
        encoding="utf-8",
    )
    svc = SocioService(str(p))
    assert svc.total == 2

    guardados = await guardar_socios_db(db, svc)
    assert guardados == 2

    rows = await db.fetch("SELECT celular, nombre_pila FROM socios ORDER BY celular")
    assert [r["celular"] for r in rows] == ["3415550001", "3415550002"]

    # Cargar en una instancia nueva (sin archivo) desde la DB: find_by_phone y
    # nombre_pila tienen que seguir funcionando igual que con el archivo.
    svc2 = SocioService(str(tmp_path / "no_existe.csv"))
    assert svc2.total == 0

    total_cargado = await cargar_socios_db(db, svc2)
    assert total_cargado == 2

    socio = svc2.find_by_phone("5493415550001")
    assert socio is not None
    assert socio["nombre_pila"] == "Claudia"
    assert socio["nro_socio"] == "4001"

    socio2 = svc2.find_by_phone("93415550002")
    assert socio2 is not None
    assert socio2["apellido"] == "Perez"
