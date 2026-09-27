"""messages: nombre original del archivo adjunto

Los adjuntos (del cliente y del operador) pasan al volumen de Railway y se
guardan 6 meses. El backoffice muestra cada archivo con su nombre original
(receta.pdf, orden.docx) en vez del id interno.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-27
"""
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS media_nombre TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS media_nombre")
