"""Fuente f0001: marcador de esquema del almacén de fuente permanente.

Sin tablas de conversación: llegan en el tramo 3 (wa_message_bodies,
wa_message_provider_ids, wa_contact_identities, webhook_inbox). Acá solo
queda `fuente_meta`, que el health check lee para confirmar que la URL apunta
a un almacén de fuente y de qué tipo es.

Revision ID: f0001
Revises: None
Create Date: 2026-09-21
"""
from alembic import op

revision = "f0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE fuente_meta (
            clave      TEXT PRIMARY KEY,
            valor      TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        INSERT INTO fuente_meta (clave, valor) VALUES ('almacen', 'permanente'), ('esquema', '1');
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS fuente_meta")
