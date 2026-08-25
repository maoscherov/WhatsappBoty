"""config: configuración del bot con persistencia durable

Redis venía siendo la única copia de la config editable desde el backoffice.
Si Redis se reinicia (o se vacía), esos valores se pierden en silencio y todo
vuelve a los defaults del código: el caso concreto fue el descuento de socios,
que quedaba en 0 sin que nadie se enterara. Postgres pasa a ser la fuente de
verdad y Redis queda como cache.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-21
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS config (
            clave       TEXT PRIMARY KEY,
            valor       TEXT NOT NULL,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS config")
