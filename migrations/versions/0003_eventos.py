"""eventos: eventos de negocio para métricas (embudo, pagos, búsquedas, envíos)

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-11
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS eventos (
            id          BIGSERIAL PRIMARY KEY,
            tipo        TEXT NOT NULL,        -- producto_ofrecido | link_enviado | pago_aprobado
                                              -- pago_rechazado | busqueda_sin_resultado | wa_send_fallo
            phone       TEXT,
            dato        TEXT,                 -- payload corto (entidad buscada, marca de tarjeta…)
            monto       DOUBLE PRECISION,     -- cuando aplica (links, pagos)
            ref         TEXT,                 -- id externo (pid de pago, order_id)
            extra       JSONB,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_eventos_tipo ON eventos (tipo, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_eventos_phone ON eventos (phone, created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS eventos")
