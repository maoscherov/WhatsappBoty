"""
Tests de PostgreSQL + pgvector contra la base embebida (pgserver) con el
esquema aplicado por Alembic. Los embeddings usan un fake determinístico.
"""

import pytest

from app.services.db import Database
from app.services.message_store import MessageStore
from app.services.rag_service import RagService


@pytest.fixture
async def db(pg_dsn):
    d = Database(pg_dsn)
    ok = await d.connect()
    assert ok, "no se pudo conectar al Postgres de test"
    # Limpiar tablas entre tests
    await d.execute("TRUNCATE messages, sku_embeddings, kb_documents RESTART IDENTITY")
    yield d
    await d.close()


# ── Esquema / migración ─────────────────────────────────────────────────────────
async def test_schema_existe(db):
    rows = await db.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
    )
    tablas = {r["tablename"] for r in rows}
    assert {"messages", "sku_embeddings", "kb_documents"} <= tablas


async def test_alembic_version(db):
    rows = await db.fetch("SELECT version_num FROM alembic_version")
    assert rows and rows[0]["version_num"] == "0001"


async def test_pgvector_habilitado(db):
    rows = await db.fetch("SELECT extname FROM pg_extension WHERE extname='vector'")
    assert rows, "la extensión vector no está instalada"


# ── Historial de mensajes ───────────────────────────────────────────────────────
async def test_message_store(db):
    store = MessageStore(db)
    await store.save("549341", "user", "hola")
    await store.save("549341", "assistant", "¡Hola! ¿En qué te ayudo?")
    await store.save("549999", "user", "otro cliente")
    hist = await store.history("549341")
    assert len(hist) == 2
    assert hist[0]["role"] == "user" and hist[0]["content"] == "hola"
    assert hist[1]["role"] == "assistant"


async def test_recent_phones(db):
    store = MessageStore(db)
    await store.save("549111", "user", "a")
    await store.save("549222", "user", "b")
    phones = await store.recent_phones()
    assert {"549111", "549222"} <= {p["phone"] for p in phones}


# ── RAG: catálogo semántico ─────────────────────────────────────────────────────
async def test_reindex_y_busqueda_semantica(db, fake_emb):
    rag = RagService(db, fake_emb)
    assert rag.enabled()
    productos = [
        {"sku_id": "1", "nombre": "Ibuprofeno 600", "requiere_receta": "si", "precio": 100.0},
        {"sku_id": "2", "nombre": "Buscapina gotas", "requiere_receta": "no", "precio": 200.0},
        {"sku_id": "3", "nombre": "Shampoo Sedal", "requiere_receta": "no", "precio": 300.0},
    ]
    total = await rag.reindex_catalogo(productos)
    assert total == 3
    assert await rag.count_indexed() == 3

    # El fake embed es determinístico: buscar el mismo texto devuelve ese producto
    res = await rag.buscar_semantico("Buscapina gotas", n=1)
    assert res and res[0]["sku_id"] == "2"
    assert res[0]["score"] > 0.99   # match casi exacto (coseno ~1)


async def test_reindex_upsert(db, fake_emb):
    rag = RagService(db, fake_emb)
    await rag.reindex_catalogo([{"sku_id": "1", "nombre": "X", "requiere_receta": "no", "precio": 1.0}])
    await rag.reindex_catalogo([{"sku_id": "1", "nombre": "X v2", "requiere_receta": "si", "precio": 2.0}])
    assert await rag.count_indexed() == 1   # upsert, no duplica
    res = await rag.buscar_semantico("X v2", n=1)
    assert res[0]["requiere_receta"] == "si"


# ── RAG: base de conocimiento ───────────────────────────────────────────────────
async def test_kb_add_search_delete(db, fake_emb):
    rag = RagService(db, fake_emb)
    assert await rag.kb_add("Horarios", "Atendemos de lunes a viernes de 9 a 18hs")
    assert await rag.kb_add("Envíos", "Hacemos envíos a domicilio dentro de Rosario")
    docs = await rag.kb_list()
    assert len(docs) == 2

    # min_score=0 para validar el RANKING (el fake embed da coseno bajo por
    # diferencia de largo, pero el doc correcto debe rankear primero).
    hits = await rag.kb_search("horarios atención lunes viernes", n=2, min_score=0.0)
    assert hits and hits[0]["titulo"] == "Horarios"

    await rag.kb_delete(docs[0]["id"])
    assert len(await rag.kb_list()) == 1
