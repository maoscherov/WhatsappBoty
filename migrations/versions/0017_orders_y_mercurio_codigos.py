"""Pedidos durables y códigos de pedido de Mercurio

`orders`: copia durable de los pedidos que hoy viven solo en Redis con TTL de
7 días (order:{id}). Un pedido COBRADO que se pierde por un reinicio o una
evicción de Redis es plata: acá queda para siempre, con el estado del alta en
el ERP (F5). Redis sigue siendo lo que lee el backoffice; esta tabla es la
fuente durable y la cola de reintentos del ERP.

`mercurio_codigos`: el POST /pedidos de Mercurio exige `variant_id` = codigo
de la variante y `product_id` = codigo_padre (spec 14/9), pero catalog_items
solo guarda external_id (id_articulo_mercurio). El sync llena esta tabla
satélite sin tocar el contrato compartido con el agente de la farmacia.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-05
"""
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id            TEXT PRIMARY KEY,
            phone               TEXT NOT NULL,
            estado              TEXT NOT NULL DEFAULT 'pendiente',
            total               NUMERIC(12,2),
            pago                TEXT,
            payment_id          TEXT,
            data                JSONB NOT NULL,
            erp_estado          TEXT,            -- NULL = no aplica | pendiente | enviado | rechazado
            erp_id_comprobante  TEXT,
            erp_numero          TEXT,
            erp_intentos        INT NOT NULL DEFAULT 0,
            erp_ultimo_error    TEXT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_orders_erp_pendiente ON orders (created_at) "
               "WHERE erp_estado = 'pendiente'")
    op.execute("CREATE INDEX IF NOT EXISTS ix_orders_payment ON orders (payment_id)")
    op.execute("""
        CREATE TABLE IF NOT EXISTS mercurio_codigos (
            branch_id     TEXT NOT NULL,
            external_id   TEXT NOT NULL,
            codigo        TEXT NOT NULL,
            codigo_padre  TEXT NOT NULL,
            PRIMARY KEY (branch_id, external_id)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS mercurio_codigos")
    op.execute("DROP INDEX IF EXISTS ix_orders_payment")
    op.execute("DROP INDEX IF EXISTS ix_orders_erp_pendiente")
    op.execute("DROP TABLE IF EXISTS orders")
