"""receta_referencia: condición de venta por código de barras

El ERP (Observer) no informa si un producto es de venta bajo receta y sus
categorías dicen solo "Medicamentos": con la regla por categoría, TODOS los
medicamentos quedaban como venta libre y el bot no derivaba ninguna receta
(caso real 24/9: ofreció Atenolol con precio y "¿te sirve?").
La referencia se carga desde el catálogo de la farmacia, que sí distingue
"Medicamentos Bajo Receta", y cada producto del ERP la toma por código de barras.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-25
"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS receta_referencia (
            barcode          TEXT PRIMARY KEY,
            nombre           TEXT NOT NULL DEFAULT '',
            requiere_receta  TEXT NOT NULL,
            fuente           TEXT NOT NULL DEFAULT '',
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS receta_referencia")
