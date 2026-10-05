"""receta: sincronizaciones confirmadas y registro de marcas manuales

- receta_sincronizaciones: cada carga de la referencia de recetas desde un
  archivo. Primero queda "pendiente" con el resumen para revisar (qué se
  agrega, qué pasa a venta libre, impacto en el catálogo) y recién al
  confirmar se aplica. Reemplaza a la carga automática desde un CSV al
  arrancar, que en producción leyó 0 filas sin avisar (28/9).
- receta_cambios: quién marcó un producto como venta libre / con receta,
  cuándo y desde dónde (conversación o ABM). Es un riesgo regulatorio: tiene
  que quedar trazado.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-28
"""
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS receta_sincronizaciones (
            id           BIGSERIAL PRIMARY KEY,
            archivo      TEXT NOT NULL DEFAULT '',
            modo         TEXT NOT NULL,
            estado       TEXT NOT NULL DEFAULT 'pendiente',
            autor        TEXT,
            resumen      JSONB,
            filas        JSONB,
            version_ref  TEXT,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            resuelta_at  TIMESTAMPTZ
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS receta_cambios (
            id           BIGSERIAL PRIMARY KEY,
            branch_id    TEXT NOT NULL,
            external_id  TEXT NOT NULL,
            nombre       TEXT NOT NULL DEFAULT '',
            barcode      TEXT,
            anterior     TEXT,
            nuevo        TEXT,
            marca        TEXT,
            autor        TEXT,
            origen       TEXT NOT NULL,
            phone        TEXT,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_receta_cambios_producto "
               "ON receta_cambios (branch_id, external_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_receta_cambios_fecha ON receta_cambios (created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS receta_cambios")
    op.execute("DROP TABLE IF EXISTS receta_sincronizaciones")
