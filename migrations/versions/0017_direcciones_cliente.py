"""direcciones_cliente: última dirección de envío de cada teléfono

Minuta 24/9 punto 8 (confirmación del domicilio precargado) y C-3854 (5/10):
a una empleada que pidió envío el bot le volvió a pedir la dirección — el
domicilio solo salía del padrón de socios. Ahora vale también la última
dirección a la que se le mandó un pedido. Se siembra desde el historial
("Te lo enviamos a *San Javier 837*").

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
        CREATE TABLE IF NOT EXISTS direcciones_cliente (
            phone       TEXT PRIMARY KEY,
            direccion   TEXT NOT NULL,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute(r"""
        INSERT INTO direcciones_cliente (phone, direccion, updated_at)
        SELECT DISTINCT ON (phone) phone,
               btrim(substring(content from '(?\:enviamos a (?\:domicilio a )?|Sale para )\*([^*]{4,120})\*')),
               created_at
        FROM messages
        WHERE role IN ('assistant', 'operator')
          AND content ~ '(enviamos a (domicilio a )?|Sale para )\*[^*]{4,60}\*'
          -- Solo direcciones de verdad: con número de calle y sin los textos
          -- que el bug viejo tomaba como domicilio ("te pedi 1 blister").
          AND substring(content from '(?\:enviamos a (?\:domicilio a )?|Sale para )\*([^*]{4,60})\*') ~ '[0-9]{2,5}'
          AND substring(content from '(?\:enviamos a (?\:domicilio a )?|Sale para )\*([^*]{4,60})\*')
              !~* '(ped[ií]|blister|comprim|[0-9] ?mg|quiero|precio|link|\?)'
        ORDER BY phone, id DESC
        ON CONFLICT (phone) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS direcciones_cliente")
