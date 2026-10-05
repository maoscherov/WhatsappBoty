"""branches: productos que el ERP no puede servir y sondeo por código de barras

Caso real 28/9: el lote 11 de ObServer daba 500 por 4 productos rotos y el
agente abortaba el ciclo entero (catálogo congelado horas); además el listado
por lotes omite productos que existen (SESAREN XR). El agente 0.3.5 recupera
el lote producto por producto y sondea por código de barras lo que falta, y
lo informa en el heartbeat para mostrarlo en el backoffice.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-28
"""
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE branches ADD COLUMN IF NOT EXISTS erp_productos_rotos JSONB")
    op.execute("ALTER TABLE branches ADD COLUMN IF NOT EXISTS erp_lotes_fallidos JSONB")
    op.execute("ALTER TABLE branches ADD COLUMN IF NOT EXISTS sondeo JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE branches DROP COLUMN IF EXISTS sondeo")
    op.execute("ALTER TABLE branches DROP COLUMN IF EXISTS erp_lotes_fallidos")
    op.execute("ALTER TABLE branches DROP COLUMN IF EXISTS erp_productos_rotos")
