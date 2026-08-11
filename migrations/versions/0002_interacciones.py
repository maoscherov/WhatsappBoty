"""interacciones: métricas históricas por respuesta del bot (dashboard)

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-10
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS interacciones (
            id          BIGSERIAL PRIMARY KEY,
            phone       TEXT NOT NULL,
            tipo        TEXT,                -- text | audio | image
            intencion   TEXT,
            total_ms    INTEGER,
            steps       JSONB,               -- tiempos por paso (claude1_ms, sku_ms, ...)
            apis        JSONB,               -- APIs externas usadas
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_inter_created ON interacciones (created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_inter_phone ON interacciones (phone, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_inter_intencion ON interacciones (intencion)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS interacciones")
