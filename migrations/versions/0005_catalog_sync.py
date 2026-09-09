"""catalog_sync: sucursales + catálogo ERP sincronizado por el agente

El catálogo deja de ser solo un CSV subido a mano: el agente local
(remedia-agent, servicio de Windows en la farmacia) lo empuja desde el ERP
sucursal por sucursal. `branches` registra cada sucursal con su token;
`catalog_items` es la foto del catálogo del ERP; `catalog_extras` guarda lo
que NO viene del ERP (receta manual, imagen, pausa) y sobrevive a los syncs.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-08
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS branches (
            branch_id        TEXT PRIMARY KEY,
            nombre           TEXT NOT NULL,
            token_hash       TEXT NOT NULL,
            activa           BOOLEAN NOT NULL DEFAULT TRUE,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_heartbeat_at TIMESTAMPTZ,
            agent_version    TEXT,
            erp_version      TEXT,
            erp_status       TEXT,
            last_sync_ok_at  TIMESTAMPTZ,
            catalog_count    INTEGER,
            pending_batches  INTEGER,
            last_catalog_push_at TIMESTAMPTZ,
            last_manifest_at     TIMESTAMPTZ
        )
    """)
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS branches_token_hash ON branches(token_hash)")
    op.execute("""
        CREATE TABLE IF NOT EXISTS catalog_items (
            branch_id            TEXT NOT NULL REFERENCES branches(branch_id) ON DELETE CASCADE,
            external_id          TEXT NOT NULL,
            hash                 TEXT NOT NULL,
            barcodes             TEXT[] NOT NULL DEFAULT '{}',
            troquel              BIGINT,
            name                 TEXT NOT NULL,
            brand                TEXT,
            drug                 TEXT,
            form                 TEXT,
            category             TEXT NOT NULL DEFAULT '',
            rubro                TEXT NOT NULL DEFAULT '',
            subrubro             TEXT NOT NULL DEFAULT '',
            therapeutic_actions  TEXT[] NOT NULL DEFAULT '{}',
            price                NUMERIC(12,2),
            stock                INTEGER NOT NULL DEFAULT 0,
            visible              BOOLEAN NOT NULL DEFAULT TRUE,
            active               BOOLEAN NOT NULL DEFAULT TRUE,
            requiere_receta      TEXT NOT NULL DEFAULT 'no',
            source               TEXT NOT NULL DEFAULT 'observer-gestion',
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (branch_id, external_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS catalog_items_barcodes ON catalog_items USING GIN (barcodes)")
    op.execute("CREATE INDEX IF NOT EXISTS catalog_items_branch_active "
               "ON catalog_items(branch_id) WHERE active AND visible")
    op.execute("""
        CREATE TABLE IF NOT EXISTS catalog_extras (
            branch_id                TEXT NOT NULL,
            external_id              TEXT NOT NULL,
            requiere_receta_override TEXT,
            pausado_manual           BOOLEAN NOT NULL DEFAULT FALSE,
            imagen_url               TEXT,
            ventas_mes               DOUBLE PRECISION,
            prom_semanal             DOUBLE PRECISION,
            clasificacion            TEXT,
            tipo_producto            TEXT,
            updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (branch_id, external_id)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS catalog_extras")
    op.execute("DROP TABLE IF EXISTS catalog_items")
    op.execute("DROP TABLE IF EXISTS branches")
