"""conversacion_id en messages y marcas

Para nombrar una conversación puntual ("revisá la C-4066") sin mezclarla con
otras del mismo cliente (5/10). Una conversación es una racha de mensajes
del mismo teléfono sin cortes de más de 3 horas; su id es el del primer
mensaje. Las marcas guardan la conversación en la que se cargaron.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-05
"""
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

CORTE = "interval '3 hours'"


def upgrade() -> None:
    op.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS conversacion_id BIGINT")
    op.execute("ALTER TABLE marcas ADD COLUMN IF NOT EXISTS conversacion_id BIGINT")
    op.execute("CREATE INDEX IF NOT EXISTS ix_messages_conversacion ON messages (conversacion_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_messages_phone_id ON messages (phone, id)")
    # Histórico: rachas por teléfono con cortes > 3 h.
    op.execute(f"""
        WITH o AS (
            SELECT id, phone,
                   CASE WHEN lag(created_at) OVER w IS NULL
                          OR created_at - lag(created_at) OVER w > {CORTE}
                        THEN 1 ELSE 0 END AS nueva
            FROM messages WINDOW w AS (PARTITION BY phone ORDER BY id)
        ), g AS (
            SELECT id, phone, sum(nueva) OVER (PARTITION BY phone ORDER BY id) AS grupo FROM o
        ), c AS (
            SELECT id, min(id) OVER (PARTITION BY phone, grupo) AS cid FROM g
        )
        UPDATE messages m SET conversacion_id = c.cid FROM c
        WHERE c.id = m.id AND m.conversacion_id IS NULL
    """)
    op.execute("""
        UPDATE marcas k SET conversacion_id = COALESCE(
            (SELECT m.conversacion_id FROM messages m WHERE m.id = k.message_id),
            (SELECT m.conversacion_id FROM messages m
              WHERE m.phone = k.phone AND m.created_at <= k.created_at
              ORDER BY m.id DESC LIMIT 1))
        WHERE k.conversacion_id IS NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_messages_phone_id")
    op.execute("DROP INDEX IF EXISTS ix_messages_conversacion")
    op.execute("ALTER TABLE marcas DROP COLUMN IF EXISTS conversacion_id")
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS conversacion_id")
