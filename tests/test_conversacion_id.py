"""
ID de conversación (5/10): "C-4066" nombra una charla puntual — racha de
mensajes del mismo teléfono sin cortes de más de 3 horas — para pasarla a
revisar sin mezclarla con otras del mismo cliente.
"""
import pytest

from app.services import marcas_service as mk
from app.services.db import Database
from app.services.message_store import MessageStore

PHONE = "5493410000321"


@pytest.fixture
async def db(pg_dsn):
    d = Database(pg_dsn)
    assert await d.connect()
    await d.execute("DELETE FROM messages WHERE phone = $1", PHONE)
    await d.execute("DELETE FROM marcas WHERE phone = $1", PHONE)
    await d.execute("DELETE FROM eventos WHERE phone = $1", PHONE)
    yield d
    await d.close()


async def _ids(db):
    return [(r["id"], r["conversacion_id"]) for r in await db.fetch(
        "SELECT id, conversacion_id FROM messages WHERE phone = $1 ORDER BY id", PHONE)]


async def test_misma_racha_misma_conversacion_y_corte_a_las_3_horas(db):
    st = MessageStore(db)
    await st.save(PHONE, "user", "hola")
    await st.save(PHONE, "assistant", "¡Hola!")
    filas = await _ids(db)
    assert filas[0][1] == filas[0][0] and filas[1][1] == filas[0][0]

    await db.execute("UPDATE messages SET created_at = now() - interval '4 hours' WHERE phone = $1", PHONE)
    await st.save(PHONE, "user", "volví a la tarde")
    filas = await _ids(db)
    assert filas[2][1] == filas[2][0] != filas[0][0]          # conversación nueva


async def test_la_marca_guarda_su_conversacion_y_el_historial_la_trae(db):
    st = MessageStore(db)
    await st.save(PHONE, "user", "quiero talco")
    await st.save(PHONE, "assistant", "no me figura")
    msg_id, cid = (await _ids(db))[-1]
    marca = await mk.crear(db, PHONE, "dato_incorrecto", "había stock", message_id=msg_id, autor="Belen")
    assert marca["conversacion"] == f"C-{cid}" and marca["codigo"] == f"#{marca['id']}"
    hist = await st.history(PHONE)
    assert hist[-1]["conversacion"] == f"C-{cid}"


async def test_problemas_por_telefono(db):
    await db.execute("INSERT INTO eventos (tipo, phone, dato) VALUES ('derivacion', $1, 'no_entendido')", PHONE)
    await db.execute("INSERT INTO eventos (tipo, phone, dato) VALUES ('derivacion', $1, 'receta')", PHONE)
    await db.execute("INSERT INTO eventos (tipo, phone) VALUES ('respuesta_bloqueada', $1)", PHONE)
    p = await mk.problemas_por_phones(db, [PHONE])
    assert p[PHONE] == {"no_entendido": 1, "respuesta_bloqueada": 1}   # receta no es problema
