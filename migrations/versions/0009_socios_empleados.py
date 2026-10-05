"""socios_empleados: persistir el padrón de socios y el listado de empleados

El padrón de socios y el listado de empleados (20% de descuento) vivían solo
en un archivo (CSV/XLSX) cargado en memoria por SocioService/EmpleadoService.
Con esto Postgres pasa a ser la fuente de verdad — sobrevive a un reinicio o
a un deploy sin volver a subir el archivo (fs efímero de Railway).

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-24
"""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS socios (
            id                BIGSERIAL PRIMARY KEY,
            celular           TEXT NOT NULL,
            celular_original  TEXT,
            nombre            TEXT,
            apellido          TEXT,
            nombre_pila       TEXT,
            nro_socio         TEXT,
            dni               TEXT,
            domicilio         TEXT,
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_socios_celular ON socios (celular)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_socios_dni ON socios (dni)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS empleados (
            id                BIGSERIAL PRIMARY KEY,
            celular           TEXT NOT NULL,
            celular_original  TEXT,
            nombre            TEXT,
            apellido          TEXT,
            nombre_pila       TEXT,
            grupo             TEXT NOT NULL DEFAULT '',
            activo            BOOLEAN NOT NULL DEFAULT true,
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_empleados_celular ON empleados (celular)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS empleados")
    op.execute("DROP TABLE IF EXISTS socios")
