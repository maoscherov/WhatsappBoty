"""sku_alias: nombre legible y términos de cliente por producto

Capa B del reconocimiento de productos (3/10): cada SKU con un nombre como lo
diría un vendedor ("ESTRELLA HIS POT x 125" → "Hisopos Estrella pote x 125")
y las palabras con que lo pide un cliente ("cotonetes"). Se generan con un
prompt por tandas y se importan desde el backoffice; entran al índice de
búsqueda, nunca a lo que ve el cliente.

nombre_base es el nombre del ERP cuando se tradujo: si el ERP lo cambia, el
alias deja de usarse hasta que se vuelva a traducir.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-03
"""
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS sku_alias (
            branch_id       TEXT NOT NULL,
            external_id     TEXT NOT NULL,
            nombre_base     TEXT NOT NULL,
            nombre_legible  TEXT NOT NULL,
            tipo            TEXT,
            terminos        TEXT,
            seguro          BOOLEAN NOT NULL DEFAULT TRUE,
            origen          TEXT NOT NULL DEFAULT 'prompt',
            autor           TEXT,
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (branch_id, external_id)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS sku_alias")
