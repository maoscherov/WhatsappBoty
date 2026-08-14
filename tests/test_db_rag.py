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
    await d.execute("TRUNCATE messages, sku_embeddings, kb_documents, "
                    "interacciones, eventos RESTART IDENTITY")
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
    assert rows and rows[0]["version_num"] == "0003"


async def test_eventos_registro(db):
    """La migración 0003 crea la tabla de eventos de negocio."""
    from app.services.metrics_store import MetricsStore
    m = MetricsStore(db)
    await m.evento("link_enviado", phone="549111", monto=1500.0, ref="pid1")
    await m.evento("busqueda_sin_resultado", phone="549111", dato="framintrol")
    rows = await db.fetch("SELECT tipo, phone, dato, monto, ref FROM eventos ORDER BY id")
    assert [r["tipo"] for r in rows] == ["link_enviado", "busqueda_sin_resultado"]
    assert rows[0]["monto"] == 1500.0 and rows[0]["ref"] == "pid1"
    assert rows[1]["dato"] == "framintrol"


async def test_tabla_interacciones(db):
    """La migración 0002 crea la tabla de métricas del dashboard."""
    from app.services.metrics_store import MetricsStore
    m = MetricsStore(db)
    await m.record("549341000", "text", "pedido", 1200, {"claude1_ms": 800}, ["claude"])
    await m.record("549341000", "text", "derivado_humano", 900, {}, [])
    await m.record("549341999", "audio", "consulta_precio", 2500, {}, ["groq"])

    dash = await m.dashboard(7)
    assert dash is not None
    assert dash["totales"]["mensajes"] == 3
    assert dash["totales"]["conversaciones"] == 2
    assert dash["totales"]["derivaciones"] == 1
    assert any(i["intencion"] == "pedido" for i in dash["intenciones"])
    assert dash["por_dia"] and dash["por_dia"][0]["mensajes"] == 3


async def test_conversaciones_historicas(db):
    """Listado histórico agrupado por teléfono, con filtro por número."""
    from app.services.metrics_store import MetricsStore
    store = MessageStore(db)
    await store.save("549111", "user", "hola, tenés ibuprofeno?")
    await store.save("549111", "assistant", "Sí! Actron 600 a $4.770")
    await store.save("549222", "user", "cuánto sale el dove?")

    m = MetricsStore(db)
    todas = await m.conversaciones(days=7)
    assert {c["phone"] for c in todas} >= {"549111", "549222"}
    c1 = next(c for c in todas if c["phone"] == "549111")
    assert c1["mensajes"] == 2 and c1["mensajes_cliente"] == 1
    assert c1["ultimo_mensaje"].startswith("Sí! Actron")

    filtradas = await m.conversaciones(days=7, q="222")
    assert [c["phone"] for c in filtradas] == ["549222"]


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


async def test_embudo_venta(db):
    """Embudo: etapas, conversión y links abandonados con su monto."""
    from app.services.metrics_store import MetricsStore
    m = MetricsStore(db)
    # 2 conversaciones (interacciones), ambas con oferta y link, una sola paga
    await m.record("549A", "text", "pedido", 1000, {}, [])
    await m.record("549B", "text", "pedido", 1000, {}, [])
    await m.evento("producto_ofrecido", phone="549A", monto=1500.0, ref="S1")
    await m.evento("producto_ofrecido", phone="549B", monto=2000.0, ref="S2")
    await m.evento("link_enviado", phone="549A", monto=1500.0, ref="p1")
    await m.evento("link_enviado", phone="549B", monto=2000.0, ref="p2")
    await m.evento("pago_aprobado", phone="549A", monto=1500.0, ref="tx1", dato="Visa")

    emb = await m.embudo(7)
    assert emb is not None
    etapas = {e["etapa"]: e["cantidad"] for e in emb["etapas"]}
    assert etapas["Conversaciones"] == 2
    assert etapas["Producto ofrecido"] == 2
    assert etapas["Link enviado"] == 2
    assert etapas["Pago aprobado"] == 1
    assert emb["conversion_total_pct"] == 50.0
    assert emb["links"]["enviados"] == 2
    assert emb["links"]["abandonados"] == 1
    assert emb["links"]["monto_abandonado"] == 2000.0   # solo el link no pagado


async def test_envios_fallidos(db):
    """Los envíos rechazados por WhatsApp quedan registrados y contados."""
    from app.services.metrics_store import MetricsStore
    m = MetricsStore(db)
    await m.evento("wa_send_fallo", phone="549111", dato="text",
                   extra={"detalle": "HTTP 400: token expirado"})
    await m.evento("wa_send_fallo", phone="549222", dato="image",
                   extra={"detalle": "HTTP 470: fuera de ventana de 24hs"})
    f = await m.envios_fallidos(7)
    assert f["total"] == 2
    assert len(f["ultimos"]) == 2
    assert "token expirado" in " ".join(u["detalle"] for u in f["ultimos"])


async def test_pagos_por_marca(db):
    """Tasa de aprobación por marca; alerta cuando una marca casi no aprueba."""
    from app.services.metrics_store import MetricsStore
    m = MetricsStore(db)
    for _ in range(4):
        await m.evento("pago_aprobado", phone="549A", dato="Visa", monto=100.0)
    await m.evento("pago_rechazado", phone="549B", dato="Visa", monto=100.0,
                   extra={"motivo": "FONDOS INSUFICIENTES"})
    for _ in range(4):   # Mastercard nunca aprueba → alerta
        await m.evento("pago_rechazado", phone="549C", dato="MasterCard", monto=100.0,
                       extra={"motivo": "TARJETA INVALIDA"})

    marcas = {x["marca"]: x for x in await m.pagos_por_marca(7)}
    assert marcas["Visa"]["aprobados"] == 4 and marcas["Visa"]["tasa_aprobacion"] == 80.0
    assert marcas["Visa"]["alerta"] is False
    mc = marcas["MasterCard"]
    assert mc["intentos"] == 4 and mc["aprobados"] == 0
    assert mc["alerta"] is True
    assert mc["motivo_top"] == "TARJETA INVALIDA"


async def test_busquedas_sin_resultado(db):
    """Ranking de lo que pidieron y no tenemos, agrupando términos normalizados."""
    from app.services.metrics_store import MetricsStore
    m = MetricsStore(db)
    await m.evento("busqueda_sin_resultado", phone="549A", dato="framintrol nad")
    await m.evento("busqueda_sin_resultado", phone="549B", dato="framintrol nad")
    await m.evento("busqueda_sin_resultado", phone="549A", dato="collar antipulgas")

    r = await m.busquedas_sin_resultado(7)
    assert r[0]["termino"] == "framintrol nad"
    assert r[0]["veces"] == 2 and r[0]["clientes"] == 2
    assert r[1]["termino"] == "collar antipulgas" and r[1]["veces"] == 1


async def test_recurrencia_cliente(db):
    """Distingue a quien escribe por primera vez del que ya conversó (spec 4.3)."""
    store = MessageStore(db)
    assert (await store.recurrencia("549NUEVO"))["tipo"] == "primera_vez"

    await store.save("549VUELVE", "user", "hola")
    await store.save("549VUELVE", "assistant", "¡Hola!")
    rec = await store.recurrencia("549VUELVE")
    assert rec["tipo"] == "ocasional"
    assert rec["mensajes"] == 1        # solo cuenta los del cliente
    assert rec["conversaciones"] == 1


async def test_kpis_conversacionales(db):
    """FCR, duración e interacciones por conversación, y causales de derivación."""
    from app.services.metrics_store import MetricsStore
    m = MetricsStore(db)
    # Cliente A: dos interacciones, resuelto por el bot
    await m.record("549A", "text", "informacion", 900, {}, [])
    await m.record("549A", "text", "agradecimiento", 800, {}, [])
    # Cliente B: derivado por consulta de saldo
    await m.record("549B", "text", "informacion", 900, {}, [])
    await m.record("549B", "text", "derivado_saldo", 500, {}, [])
    await m.evento("sentimiento", phone="549B", dato="negativo")
    await m.evento("sentimiento", phone="549A", dato="positivo")

    k = await m.kpis_conversacionales(7)
    assert k["conversaciones"] == 2
    assert k["fcr_pct"] == 50.0                     # solo A se resolvió sin derivar
    assert k["interacciones_promedio"] == 2.0
    assert k["emocionalidad"]["negativo"] == 50.0
    assert k["derivaciones_por_causal"][0]["causal"] == "saldo"
