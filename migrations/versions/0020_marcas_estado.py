"""marcas: estado, resolución e historial

Hasta el 15/10 una marca era solo "hay un error acá": no se sabía cuáles se
arreglaron ni con qué cambio. Ahora cada marca tiene un estado (abierta, en
revisión, resuelta, validada, reabierta, descartada), la observación del
cambio con la versión que lo resolvió, y un historial con cada paso.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-16
"""
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE marcas ADD COLUMN IF NOT EXISTS estado TEXT NOT NULL DEFAULT 'abierta'")
    op.execute("ALTER TABLE marcas ADD COLUMN IF NOT EXISTS resolucion TEXT")
    op.execute("ALTER TABLE marcas ADD COLUMN IF NOT EXISTS resuelta_por TEXT")
    op.execute("ALTER TABLE marcas ADD COLUMN IF NOT EXISTS resuelta_at TIMESTAMPTZ")
    op.execute("ALTER TABLE marcas ADD COLUMN IF NOT EXISTS version TEXT")
    op.execute("ALTER TABLE marcas ADD COLUMN IF NOT EXISTS estado_at TIMESTAMPTZ")
    op.execute("CREATE INDEX IF NOT EXISTS ix_marcas_estado ON marcas (estado)")
    op.execute("""
        CREATE TABLE IF NOT EXISTS marcas_historial (
            id               BIGSERIAL PRIMARY KEY,
            marca_id         BIGINT NOT NULL REFERENCES marcas(id) ON DELETE CASCADE,
            estado_anterior  TEXT,
            estado           TEXT NOT NULL,
            observacion      TEXT,
            autor            TEXT,
            version          TEXT,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_marcas_historial_marca ON marcas_historial (marca_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS marcas_historial")
    for col in ("estado", "resolucion", "resuelta_por", "resuelta_at", "version", "estado_at"):
        op.execute(f"ALTER TABLE marcas DROP COLUMN IF EXISTS {col}")
