"""messages: autor del mensaje (historial de conversaciones del backoffice)

El historial permanente guardaba solo cliente y bot. Los mensajes que escribe
una persona de la farmacia desde el backoffice no quedaban en Postgres, así que
después de una derivación el histórico mostraba al cliente hablando solo.
Se agrega `autor` (nombre del operador; NULL para cliente y bot).

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-18
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS autor TEXT")
    # Paginado hacia atrás por id dentro de un teléfono.
    op.execute("CREATE INDEX IF NOT EXISTS idx_messages_phone_id ON messages (phone, id DESC)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_messages_phone_id")
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS autor")
