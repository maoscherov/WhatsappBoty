"""marcas de error en conversaciones

Los operadores marcan errores del bot en una conversación (o en un mensaje
puntual) para medir qué tan lista está la operación para pasar a producción
sin supervisión. `message_id` es una referencia liviana (sin FK) a
`messages.id`: NULL significa que la marca es sobre toda la conversación.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-27
"""
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS marcas (
            id BIGSERIAL PRIMARY KEY,
            phone TEXT NOT NULL,
            message_id BIGINT NULL,
            categoria TEXT NOT NULL,
            observacion TEXT NOT NULL,
            autor TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_marcas_phone ON marcas (phone)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_marcas_created_at ON marcas (created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS marcas")
