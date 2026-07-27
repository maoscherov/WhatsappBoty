"""initial schema: messages, sku_embeddings, kb_documents (pgvector)

Revision ID: 0001
Revises:
Create Date: 2026-07-27
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

EMBED_DIM = 1536  # text-embedding-3-small


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id          BIGSERIAL PRIMARY KEY,
            phone       TEXT NOT NULL,
            role        TEXT NOT NULL,
            content     TEXT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_messages_phone ON messages (phone, created_at)")

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS sku_embeddings (
            sku_id          TEXT PRIMARY KEY,
            nombre          TEXT,
            requiere_receta TEXT,
            precio          DOUBLE PRECISION,
            embedding       vector({EMBED_DIM})
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_sku_emb ON sku_embeddings
            USING hnsw (embedding vector_cosine_ops)
    """)

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS kb_documents (
            id          BIGSERIAL PRIMARY KEY,
            titulo      TEXT,
            contenido   TEXT NOT NULL,
            embedding   vector({EMBED_DIM}),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_kb_emb ON kb_documents
            USING hnsw (embedding vector_cosine_ops)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS kb_documents")
    op.execute("DROP TABLE IF EXISTS sku_embeddings")
    op.execute("DROP TABLE IF EXISTS messages")
