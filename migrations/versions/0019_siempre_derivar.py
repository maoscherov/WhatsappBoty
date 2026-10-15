"""catalog_extras.siempre_derivar: productos que siempre atiende una persona

Tinturas (15/10, Claudia): el color y el tono los tiene que ver alguien de la
farmacia; además están cargadas de forma particular y el bot no las encuentra.
Con esta marca (por producto, editable desde el panel del diccionario) el bot
pasa la charla al equipo en vez de ofrecerlo.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-15
"""
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE catalog_extras ADD COLUMN IF NOT EXISTS "
               "siempre_derivar BOOLEAN NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE catalog_extras DROP COLUMN IF EXISTS siempre_derivar")
