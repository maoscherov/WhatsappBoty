"""operadores: lista de usuarios del backoffice

Hasta el 6/10 cada operador escribía su nombre como quería: en la base hay
"claudia", "Lore" y "lorena", "01"…"07", "carcarana". No se podía saber quién
atendió qué ni elegir el usuario al tomar una conversación. Ahora hay una
lista: cada operador tiene un nombre y los `aliases` con que figuraba antes
(se unifican al mostrar y al registrar acciones nuevas).

Se siembra con los nombres que ya aparecen en mensajes, marcas y eventos;
la farmacia los renombra ("02" → "María") y fusiona los repetidos desde el
backoffice.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-06
"""
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS operadores (
            id          BIGSERIAL PRIMARY KEY,
            nombre      TEXT NOT NULL UNIQUE,
            aliases     TEXT[] NOT NULL DEFAULT '{}',
            activo      BOOLEAN NOT NULL DEFAULT TRUE,
            nota        TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        INSERT INTO operadores (nombre, nota)
        SELECT DISTINCT btrim(n), 'Cargado desde el historial: completar el nombre real'
        FROM (
            SELECT autor AS n FROM messages WHERE role = 'operator'
            UNION SELECT autor FROM marcas
            UNION SELECT ref FROM eventos
             WHERE tipo IN ('conversacion_tomada', 'derivacion_atendida', 'pedido_operador',
                            'conversacion_devuelta')
        ) t
        WHERE n IS NOT NULL AND btrim(n) <> ''
        ON CONFLICT (nombre) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS operadores")
