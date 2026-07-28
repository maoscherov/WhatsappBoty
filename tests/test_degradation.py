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


def test_parse_response_con_prefill():
    # Con prefill, raw = "{" + continuación → debe parsear a JSON válido
    cont = '"intencion": "pedido", "entidad_producto": "anti acné", "cantidad": 1, "respuesta": "ok"}'
    d = IntentService._parse_response("{" + cont)
    assert d["intencion"] == "pedido"
    assert d["entidad_producto"] == "anti acné"


def test_parse_response_fallback():
    # Prosa sin JSON → fallback controlado (no rompe)
    d = IntentService._parse_response("perdón, no sé")
    assert d["intencion"] == "desconocido"


def test_provider_setup():
    from app.services.intent_service import _MODELS
    # OpenAI como primario, sin key de Anthropic → solo cliente OpenAI
    s = IntentService("", "openai-key", "openai")
    assert s._provider == "openai"
    assert s._openai is not None and s._anthropic is None
    # Provider inválido → cae a anthropic
    s2 = IntentService("ak", "", "gemini")
    assert s2._provider == "anthropic"
    # Mapa de modelos por tier
    assert set(_MODELS) == {"anthropic", "openai"}
    assert _MODELS["openai"]["fast"] and _MODELS["openai"]["full"]


def test_build_messages_alterna_roles():
    """Historial con roles consecutivos / refs de imagen no debe romper la
    alternancia que exige la API (era la causa de 'tuve un problema')."""
    svc = IntentService("")
    hist = [
        {"role": "user", "content": "📷 /media/chat/abc"},   # ref de imagen → se saltea
        {"role": "user", "content": "hola"},                 # user consecutivo
        {"role": "assistant", "content": "buenas"},
        {"role": "user", "content": "tenés algo"},
    ]
    msgs = svc._armar(hist, "fresh anticaries")
    roles = [m["role"] for m in msgs]
    # No hay dos roles iguales consecutivos y arranca en user, termina en user
    assert all(roles[i] != roles[i + 1] for i in range(len(roles) - 1))
    assert roles[0] == "user" and roles[-1] == "user"
    assert "📷" not in " ".join(m["content"] for m in msgs)
