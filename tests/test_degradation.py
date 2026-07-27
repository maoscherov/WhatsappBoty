"""
Sin Postgres ni embeddings, todo debe no-opear sin romper (el bot sigue con Redis).
"""

from app.services.db import Database
from app.services.embeddings import EmbeddingService
from app.services.rag_service import RagService
from app.services.message_store import MessageStore
from app.services.intent_service import IntentService


async def test_db_sin_dsn():
    db = Database("")
    assert await db.connect() is False
    assert db.available() is False
    assert await db.fetch("SELECT 1") == []
    assert await db.execute("SELECT 1") is None


async def test_rag_deshabilitado():
    db = Database("")
    await db.connect()
    emb = EmbeddingService("")   # sin API key
    assert emb.enabled is False
    rag = RagService(db, emb)
    assert rag.enabled() is False
    assert await rag.buscar_semantico("tos") == []
    assert await rag.kb_search("horario") == []
    assert await rag.reindex_catalogo([{"sku_id": "1", "nombre": "x"}]) == 0


async def test_message_store_sin_db():
    db = Database("")
    await db.connect()
    store = MessageStore(db)
    await store.save("549", "user", "hola")   # no rompe
    assert await store.history("549") == []


def test_contexto_prompt_socio_y_kb():
    # El armado de contexto no depende de red
    base = IntentService._con_contexto("mensaje", "Nombre: María", "Horario: 9 a 18")
    assert "[DATOS DEL SOCIO]" in base
    assert "[INFORMACIÓN DE LA FARMACIA]" in base
    assert IntentService._con_contexto("m", None, None) == "m"
