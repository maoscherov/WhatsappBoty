"""
Fixtures de test.

- pg_dsn: levanta un PostgreSQL embebido (pgserver, con pgvector) y aplica
  las migraciones de Alembic. Se saltea si pgserver no está disponible.
- fake_emb: servicio de embeddings determinístico (sin OpenAI) para testear
  el pipeline de pgvector sin llamar a una API externa.
"""

import hashlib
import math
import os
import re
import tempfile

import pytest


def _fake_vec(text: str, dim: int = 1536) -> list[float]:
    """
    Vector determinístico tipo bag-of-words: cada token suma en una posición
    hasheada. Textos que comparten palabras quedan cerca en coseno — simula
    (groseramente) la recuperación semántica sin llamar a OpenAI.
    """
    v = [0.0] * dim
    for tok in re.findall(r"\w+", text.lower()):
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        v[h % dim] += 1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


class FakeEmbedding:
    """Interfaz igual a EmbeddingService pero sin llamar a OpenAI."""
    enabled = True

    async def embed(self, texts):
        return [_fake_vec(t) for t in texts]

    async def embed_one(self, text):
        return _fake_vec(text)


@pytest.fixture
def fake_emb():
    return FakeEmbedding()


@pytest.fixture(scope="session")
def pg_dsn():
    try:
        import pgserver
    except ImportError:
        pytest.skip("pgserver no instalado")

    tmp = tempfile.mkdtemp(prefix="pgtest_remedia_")
    srv = pgserver.get_server(tmp)
    dsn = srv.get_uri()
    os.environ["DATABASE_URL"] = dsn

    # Aplicar migraciones Alembic
    from alembic.config import Config
    from alembic import command
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(root, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(root, "migrations"))
    command.upgrade(cfg, "head")

    yield dsn

    try:
        srv.cleanup()
    except Exception:
        pass
