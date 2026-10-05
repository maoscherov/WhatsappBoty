"""messages: origen del mensaje y archivo adjunto

Un audio se guardaba como si el cliente lo hubiera escrito: el operador no
distinguía una transcripción ni podía escuchar el original para verificarla
(caso real 23/9: "Atopix" transcripto como "Topics").
- origen: texto | audio | imagen | documento (NULL = texto, filas viejas)
- media:  ruta del archivo guardado (/media/chat/{id}), vence a los 7 días

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-24
"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS origen TEXT")
    op.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS media TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS media")
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS origen")
