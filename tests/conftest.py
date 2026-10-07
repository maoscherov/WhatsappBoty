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


@pytest.fixture(autouse=True)
def _adjuntos_en_tmp(tmp_path, monkeypatch):
    """Los adjuntos de las conversaciones van a una carpeta temporal (en
    producción, el volumen /data/chat)."""
    from app.services import chat_media
    monkeypatch.setattr(chat_media, "_dir", lambda: tmp_path / "chat")


@pytest.fixture
def usar_perfil():
    """Cambia el perfil de rubro durante el test y devuelve el Perfil activo:
    usar_perfil("petshop") o usar_perfil("petshop", comercio="MO Prueba").

    Pisa `vertical` y `comercio_nombre` en el Settings cacheado (no setea
    variables de entorno ni recrea Settings: pg_dsn deja DATABASE_URL en
    os.environ para toda la sesión y un Settings nuevo lo levantaría) y limpia
    el cache de get_perfil antes y en el teardown, DESPUÉS de restaurar los
    atributos. Sin ese cache_clear el perfil se filtra al resto de la suite.
    Como no recrea Settings, convive con monkeypatch.setattr(get_settings(), ...)
    en cualquier orden.

    Requiere app/services/perfil.py (Task 2 del plan): los imports son
    diferidos para que este conftest cargue antes de que exista.
    """
    from app.config import get_settings
    from app.services.perfil import get_perfil

    mp = pytest.MonkeyPatch()

    def _usar(clave: str, comercio: str | None = None):
        s = get_settings()
        mp.setattr(s, "vertical", clave)
        mp.setattr(s, "comercio_nombre", comercio or "")
        get_perfil.cache_clear()
        return get_perfil()

    yield _usar
    mp.undo()
    get_perfil.cache_clear()
