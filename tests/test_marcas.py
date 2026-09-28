"""
Marcas de error en conversaciones: el operador señala cuándo el bot se
equivocó (no derivó, cotizó mal, entendió mal...) para medir si la operación
está lista para migrar a producción sin supervisión (spec de indicadores).
"""

from datetime import date, datetime, timezone

import pytest
from fastapi import FastAPI

import app.services.db as dbmod
from app.config import get_settings
from app.services import marcas_service
from app.services.db import Database
from app.services.message_store import get_message_store


@pytest.fixture
async def db(pg_dsn):
    import app.services.message_store as msmod
    import app.services.metrics_store as mtmod

    d = Database(pg_dsn)
    assert await d.connect()
    await d.execute("TRUNCATE marcas, messages RESTART IDENTITY")
    # get_message_store()/get_metrics_store() cachean un singleton: sin
    # resetear queda pegado al `db` (posiblemente sin conectar) de otro test
    # que corrió antes.
    msmod._instance = None
    mtmod._instance = None
    yield d
    msmod._instance = None
    mtmod._instance = None
    await d.close()


async def _msg(db, phone: str, dia: date, role: str = "user", content: str = "hola") -> int:
    """Inserta un mensaje con created_at explícito (para armar escenarios de
    indicadores en días puntuales) y devuelve su id."""
    row = await db.fetchrow(
        "INSERT INTO messages (phone, role, content, created_at) "
        "VALUES ($1, $2, $3, $4) RETURNING id",
        phone, role, content, datetime(dia.year, dia.month, dia.day, 12, 0, tzinfo=timezone.utc),
    )
    return row["id"]


# ── CRUD básico + validaciones ──────────────────────────────────────────────────

async def test_categorias_lista():
    cats = marcas_service.categorias_lista()
    claves = {c["clave"] for c in cats}
    assert "receta_sin_derivar" in claves
    assert "otro" in claves
    assert all(c["etiqueta"] for c in cats)


async def test_crear_listar_eliminar(db):
    creada = await marcas_service.crear(
        db, "5493411112233", "no_derivo", "  Debía derivar y no lo hizo  ", autor="agente1")
    assert creada["categoria"] == "no_derivo"
    assert creada["etiqueta"] == "No derivó cuando debía"
    assert creada["observacion"] == "Debía derivar y no lo hizo"  # se recorta
    assert creada["message_id"] is None
    assert creada["autor"] == "agente1"

    marcas = await marcas_service.listar(db, phone="5493411112233")
    assert len(marcas) == 1 and marcas[0]["id"] == creada["id"]

    assert await marcas_service.eliminar(db, creada["id"]) is True
    assert await marcas_service.listar(db, phone="5493411112233") == []
    assert await marcas_service.eliminar(db, 999999) is False


async def test_crear_valida_categoria_y_observacion(db):
    with pytest.raises(ValueError):
        await marcas_service.crear(db, "54941", "categoria_inexistente", "algo")
    with pytest.raises(ValueError):
        await marcas_service.crear(db, "54941", "otro", "   ")


async def test_listar_filtra_por_fecha_y_categoria(db):
    await marcas_service.crear(db, "111", "otro", "vieja")
    await db.execute("UPDATE marcas SET created_at = $1 WHERE phone = '111'",
                     datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    await marcas_service.crear(db, "222", "entendio_mal", "nueva")
    await db.execute("UPDATE marcas SET created_at = $1 WHERE phone = '222'",
                     datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc))

    solo_nuevas = await marcas_service.listar(db, desde=date(2026, 1, 5))
    assert [m["phone"] for m in solo_nuevas] == ["222"]

    solo_viejas = await marcas_service.listar(db, hasta=date(2026, 1, 5))
    assert [m["phone"] for m in solo_viejas] == ["111"]

    por_categoria = await marcas_service.listar(db, categoria="entendio_mal")
    assert [m["phone"] for m in por_categoria] == ["222"]


# ── Asociación a mensajes / conversación (para /bo/history) ─────────────────────

async def test_marcas_de_phone_separa_por_mensaje_y_conversacion(db):
    mid = await _msg(db, "5493419999999", date(2026, 1, 5))
    await marcas_service.crear(db, "5493419999999", "dato_incorrecto", "precio mal", message_id=mid)
    await marcas_service.crear(db, "5493419999999", "otro", "algo de toda la charla")

    todas = await marcas_service.marcas_de_phone(db, "5493419999999")
    assert len(todas) == 2
    de_mensaje = [m for m in todas if m["message_id"] == mid]
    de_conversacion = [m for m in todas if m["message_id"] is None]
    assert len(de_mensaje) == 1 and de_mensaje[0]["categoria"] == "dato_incorrecto"
    assert len(de_conversacion) == 1 and de_conversacion[0]["categoria"] == "otro"


async def test_contar_por_phones(db):
    await marcas_service.crear(db, "aaa", "otro", "x")
    await marcas_service.crear(db, "aaa", "otro", "y")
    await marcas_service.crear(db, "bbb", "otro", "z")
    conteo = await marcas_service.contar_por_phones(db, ["aaa", "bbb", "ccc"])
    assert conteo == {"aaa": 2, "bbb": 1}
    assert await marcas_service.contar_por_phones(db, []) == {}


async def test_para_export_incluye_mensaje_truncado(db):
    mid = await _msg(db, "555", date(2026, 1, 5), content="x" * 400)
    await marcas_service.crear(db, "555", "otro", "obs", message_id=mid)
    filas = await marcas_service.para_export(db)
    assert len(filas) == 1
    assert len(filas[0]["mensaje"]) == 300


# ── Indicadores ──────────────────────────────────────────────────────────────────

async def _crear_conversaciones(db, dia: date, cantidad: int, prefijo: str):
    """`cantidad` teléfonos distintos con un mensaje de usuario en `dia`."""
    ids = []
    for i in range(cantidad):
        ids.append(await _msg(db, f"{prefijo}{i:03d}", dia))
    return ids


async def test_indicadores_racha_salta_fin_de_semana_y_habilita_migracion(db):
    # Diez días hábiles seguidos (05/01 lun a 16/01 vie, salteando el finde
    # 10-11/01), cada uno con 20 conversaciones (llega al piso) y una sola
    # marca de error puntual para que pct_exitosas no sea 100% pero siga
    # arriba de la meta.
    dias_habiles = [date(2026, 1, d) for d in list(range(5, 10)) + list(range(12, 17))]
    assert all(d.weekday() < 5 for d in dias_habiles)

    mid_con_error = None
    for i, dia in enumerate(dias_habiles):
        ids = await _crear_conversaciones(db, dia, 20, f"tel{i}_")
        if i == 0:
            mid_con_error = ids[0]

    await marcas_service.crear(db, "tel0_000", "entendio_mal", "se equivocó",
                               message_id=mid_con_error)

    resultado = await marcas_service.indicadores(db, desde=date(2026, 1, 5), hasta=date(2026, 1, 16))

    assert resultado["total_conversaciones"] == 200
    assert resultado["total_exitosas"] == 199
    assert resultado["pct_exitosas"] == round(100 * 199 / 200, 1)
    assert resultado["dias_validos_seguidos"] == 10
    assert resultado["meta_pct"] == 85
    assert resultado["piso_diario"] == 20
    assert resultado["dias_necesarios"] == 7
    assert resultado["recetas_sin_derivar"] == 0
    assert resultado["reinicia_conteo"] is False
    assert resultado["habilita_migracion"] is True

    por_fecha = {d["fecha"]: d for d in resultado["dias"]}
    assert por_fecha["2026-01-05"]["conversaciones"] == 20
    assert por_fecha["2026-01-05"]["con_error"] == 1
    assert por_fecha["2026-01-05"]["exitosas"] == 19
    assert por_fecha["2026-01-05"]["llega_al_piso"] is True
    # El finde no tuvo conversaciones y no debería figurar como piso alcanzado.
    assert por_fecha["2026-01-10"]["conversaciones"] == 0
    assert por_fecha["2026-01-10"]["llega_al_piso"] is False


async def test_indicadores_un_dia_flojo_corta_la_racha(db):
    # Mismos 10 días hábiles, pero el miércoles 07/01 (en el medio) sólo tiene
    # 5 conversaciones: no llega al piso y corta la racha contada desde el
    # día más reciente hacia atrás.
    dias_habiles = [date(2026, 1, d) for d in list(range(5, 10)) + list(range(12, 17))]
    for i, dia in enumerate(dias_habiles):
        cantidad = 5 if dia == date(2026, 1, 7) else 20
        await _crear_conversaciones(db, dia, cantidad, f"tel{i}_")

    resultado = await marcas_service.indicadores(db, desde=date(2026, 1, 5), hasta=date(2026, 1, 16))
    por_fecha = {d["fecha"]: d for d in resultado["dias"]}
    assert por_fecha["2026-01-07"]["llega_al_piso"] is False
    # Racha desde el 16/01 hacia atrás: 16,15,14 son válidos, el 13 y 12
    # también, el finde se saltea, y el 09/08 también válidos, pero el 07
    # corta antes de seguir contando el 06/05.
    assert resultado["dias_validos_seguidos"] == 7  # 16,15,14,13,12,(9,8) antes de cortar en el 7
    # Justo llega a los 7 días necesarios, sin errores ni recetas: habilita.
    assert resultado["habilita_migracion"] is True


async def test_indicadores_receta_sin_derivar_reinicia_conteo(db):
    dias_habiles = [date(2026, 1, d) for d in list(range(5, 10)) + list(range(12, 17))]
    mid = None
    for i, dia in enumerate(dias_habiles):
        ids = await _crear_conversaciones(db, dia, 20, f"tel{i}_")
        if dia == date(2026, 1, 16):
            mid = ids[0]

    await marcas_service.crear(db, "tel9_000", "receta_sin_derivar", "no derivó la receta",
                               message_id=mid)

    resultado = await marcas_service.indicadores(db, desde=date(2026, 1, 5), hasta=date(2026, 1, 16))
    assert resultado["recetas_sin_derivar"] == 1
    assert resultado["reinicia_conteo"] is True
    assert resultado["habilita_migracion"] is False
    # La receta del último día reinicia la racha (minuta 24/9)
    assert resultado["dias_validos_seguidos"] == 0

    # Receta a mitad del período: la racha cuenta desde el día siguiente
    await db.execute("DELETE FROM marcas")
    mid7 = (await db.fetch(
        "SELECT id FROM messages WHERE phone = 'tel2_000' ORDER BY id LIMIT 1"))[0]["id"]
    await marcas_service.crear(db, "tel2_000", "receta_sin_derivar", "receta", message_id=mid7)
    r2 = await marcas_service.indicadores(db, desde=date(2026, 1, 5), hasta=date(2026, 1, 16))
    assert r2["dias_validos_seguidos"] == 7          # 8, 9, 12, 13, 14, 15 y 16 de enero
    assert r2["pct_exitosas_racha"] == 100.0 and r2["habilita_migracion"] is True
    assert r2["reinicia_conteo"] is True


async def test_indicadores_default_ultimos_14_dias(db):
    # Sin desde/hasta no debe explotar y arma el rango solo.
    resultado = await marcas_service.indicadores(db)
    assert len(resultado["dias"]) == 14


# ── Endpoints (AsyncClient sobre el mismo event loop que el pool de asyncpg;
# TestClient corre en otro hilo/loop y las queries fallarían silenciosamente) ────

@pytest.fixture
async def cliente(monkeypatch, db):
    from httpx import ASGITransport, AsyncClient
    from app.routers import backoffice as bo
    monkeypatch.setattr(get_settings(), "bo_key", "CLAVE")
    prev = dbmod._instance
    dbmod._instance = db
    app = FastAPI()
    app.include_router(bo.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    dbmod._instance = prev


async def test_endpoint_categorias(cliente):
    r = await cliente.get("/bo/marcas/categorias", headers={"x-bo-key": "CLAVE"})
    assert r.status_code == 200
    claves = {c["clave"] for c in r.json()}
    assert "no_derivo" in claves


async def test_endpoint_crear_listar_eliminar(cliente):
    r = await cliente.post("/bo/marcas", headers={"x-bo-key": "CLAVE"}, json={
        "phone": "5493410001111", "categoria": "link_incompleto", "observacion": "faltó el link",
    })
    assert r.status_code == 200
    creada = r.json()
    assert creada["etiqueta"] == "Link incompleto"

    r = await cliente.get("/bo/marcas", headers={"x-bo-key": "CLAVE"},
                          params={"phone": "5493410001111"})
    assert r.status_code == 200
    assert len(r.json()["marcas"]) == 1

    r = await cliente.post("/bo/marcas", headers={"x-bo-key": "CLAVE"}, json={
        "phone": "111", "categoria": "no_existe", "observacion": "x",
    })
    assert r.status_code == 422

    r = await cliente.delete(f"/bo/marcas/{creada['id']}", headers={"x-bo-key": "CLAVE"})
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    r = await cliente.delete(f"/bo/marcas/{creada['id']}", headers={"x-bo-key": "CLAVE"})
    assert r.status_code == 404


async def test_endpoint_history_incluye_marcas(cliente, db):
    mid = await _msg(db, "5493412223344", date(2026, 1, 5), content="hola bot")
    await marcas_service.crear(db, "5493412223344", "dato_incorrecto", "precio mal", message_id=mid)
    await marcas_service.crear(db, "5493412223344", "otro", "de toda la charla")

    r = await cliente.get("/bo/history/5493412223344", headers={"x-bo-key": "CLAVE"})
    assert r.status_code == 200
    data = r.json()
    assert len(data["marcas_conversacion"]) == 1
    assert data["marcas_conversacion"][0]["categoria"] == "otro"
    mensaje = next(m for m in data["messages"] if m["id"] == mid)
    assert len(mensaje["marcas"]) == 1
    assert mensaje["marcas"][0]["categoria"] == "dato_incorrecto"


async def test_endpoint_conversaciones_cuenta_y_filtra_marcas(cliente, db):
    await _msg(db, "5493415556677", date.today())
    await marcas_service.crear(db, "5493415556677", "otro", "algo")
    await _msg(db, "5493418889900", date.today())

    r = await cliente.get("/bo/conversaciones", headers={"x-bo-key": "CLAVE"})
    assert r.status_code == 200
    por_tel = {c["phone"]: c for c in r.json()["conversaciones"]}
    assert por_tel["5493415556677"]["marcas"] == 1
    assert por_tel["5493418889900"]["marcas"] == 0

    r = await cliente.get("/bo/conversaciones", headers={"x-bo-key": "CLAVE"}, params={"con_marcas": True})
    telefonos = {c["phone"] for c in r.json()["conversaciones"]}
    assert telefonos == {"5493415556677"}


async def test_crear_con_la_base_rechazando_da_error_claro():
    """28/9: sin la tabla (migración sin aplicar) el INSERT fallaba en
    silencio y el endpoint devolvía 500 con un TypeError."""
    class _DbQueFalla:
        async def fetchrow(self, *a):
            return None
    with pytest.raises(RuntimeError, match="No se pudo guardar la marca"):
        await marcas_service.crear(_DbQueFalla(), "549", "otro", "prueba")
