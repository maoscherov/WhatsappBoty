"""Radar r0001: tenants, RLS efectiva y funciones de administración de KIS.

Los roles radar_app y radar_admin los crea scripts/radar_bootstrap_roles.sql
(necesita CREATEROLE). Acá solo se verifica que existan, para fallar con un
mensaje claro y no con un GRANT roto a mitad de camino.

Revision ID: r0001
Revises: None
Create Date: 2026-09-21
"""
from alembic import op

from app.radar.constantes import TENANT_KIS_STR
from app.radar.rls_sql import definir_funcion_admin, politica_por_tenant

revision = "r0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'radar_app') THEN
                RAISE EXCEPTION 'falta el rol radar_app: correr scripts/radar_bootstrap_roles.sql';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'radar_admin') THEN
                RAISE EXCEPTION 'falta el rol radar_admin: correr scripts/radar_bootstrap_roles.sql';
            END IF;
        END $$;
        GRANT USAGE ON SCHEMA public TO radar_app;
        GRANT USAGE ON SCHEMA public TO radar_admin;

        -- Tenant actual de la transacción (lo fija RadarDB.tenant_tx). NULL si no hay.
        CREATE FUNCTION radar_tenant_actual() RETURNS uuid
            LANGUAGE sql STABLE
            AS $$ SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid $$;
        GRANT EXECUTE ON FUNCTION radar_tenant_actual() TO radar_app;
        GRANT EXECUTE ON FUNCTION radar_tenant_actual() TO radar_admin;
    """)

    op.execute("""
        CREATE TABLE tenants (
            id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            nombre                 TEXT NOT NULL CHECK (length(nombre) BETWEEN 1 AND 120),
            rubro                  TEXT NOT NULL DEFAULT 'otro' CHECK (rubro ~ '^[a-z_]{1,40}$'),
            es_kis                 BOOLEAN NOT NULL DEFAULT FALSE,
            -- Parámetros de ámbito Tenant (§2.1)
            perfil_de_datos        TEXT NOT NULL DEFAULT 'estandar'
                                   CHECK (perfil_de_datos IN ('estandar', 'sensible')),
            retencion_fichas_meses INTEGER NOT NULL DEFAULT 12 CHECK (retencion_fichas_meses >= 0),
            retener_fragmentos     BOOLEAN NOT NULL DEFAULT FALSE,
            ia_habilitada          BOOLEAN NOT NULL DEFAULT TRUE,
            via_llm                TEXT NOT NULL DEFAULT 'lotes' CHECK (via_llm IN ('lotes', 'sincronica')),
            -- Propuesta pendiente de un admin de KIS para aflojar parámetros de
            -- tenant con líneas vivas (§2.1); la consume el consentimiento del dueño.
            parametros_propuestos  JSONB NULL
                                   CHECK (parametros_propuestos IS NULL OR jsonb_typeof(parametros_propuestos) = 'object'),
            -- Cuándo se fijó la propuesta vigente: solo cuentan los consentimientos
            -- posteriores (una fila nueva en consents por propuesta, §2.1).
            parametros_propuestos_at TIMESTAMPTZ NULL,
            created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)
    # La semilla va ANTES de activar RLS: con FORCE, el dueño tampoco puede
    # insertar sin una política que lo permita.
    op.execute(f"""
        INSERT INTO tenants (id, nombre, rubro, es_kis)
        VALUES ('{TENANT_KIS_STR}', 'Keep IT Simple', 'kis', TRUE);
    """)
    op.execute(politica_por_tenant("tenants", columna="id"))
    op.execute("""
        -- radar_app solo lee y actualiza SU tenant. Crear y listar es de radar_admin.
        -- UPDATE por columna: nunca id, es_kis ni created_at (un tenant no puede
        -- volverse KIS).
        GRANT SELECT ON tenants TO radar_app;
        GRANT UPDATE (nombre, rubro, perfil_de_datos, retencion_fichas_meses,
                      retener_fragmentos, ia_habilitada, via_llm,
                      parametros_propuestos, parametros_propuestos_at, updated_at) ON tenants TO radar_app;
        GRANT SELECT, INSERT ON tenants TO radar_admin;
        CREATE POLICY tenants_admin ON tenants FOR ALL TO radar_admin
            USING (true) WITH CHECK (true);
    """)
    op.execute(definir_funcion_admin(
        "radar_admin_crear_tenant(uuid, text, text, text, integer, boolean, boolean, text)",
        """
        CREATE FUNCTION radar_admin_crear_tenant(
            p_id uuid, p_nombre text, p_rubro text, p_perfil text,
            p_retencion_fichas_meses integer, p_retener_fragmentos boolean,
            p_ia_habilitada boolean, p_via_llm text
        ) RETURNS uuid
            LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp
            AS $f$
                INSERT INTO tenants (id, nombre, rubro, perfil_de_datos, retencion_fichas_meses,
                                     retener_fragmentos, ia_habilitada, via_llm)
                VALUES (p_id, p_nombre, p_rubro, p_perfil, p_retencion_fichas_meses,
                        p_retener_fragmentos, p_ia_habilitada, p_via_llm)
                RETURNING id
            $f$;
        """,
    ))
    op.execute(definir_funcion_admin(
        "radar_admin_listar_tenants()",
        """
        CREATE FUNCTION radar_admin_listar_tenants()
            RETURNS TABLE (id uuid, nombre text, rubro text, perfil_de_datos text, created_at timestamptz)
            LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
            AS $f$
                SELECT t.id, t.nombre, t.rubro, t.perfil_de_datos, t.created_at
                FROM tenants t WHERE NOT t.es_kis ORDER BY t.created_at
            $f$;
        """,
    ))


def downgrade() -> None:
    op.execute("""
        DROP FUNCTION IF EXISTS radar_admin_listar_tenants();
        DROP FUNCTION IF EXISTS radar_admin_crear_tenant(uuid, text, text, text, integer, boolean, boolean, text);
        DROP TABLE IF EXISTS tenants;
        DROP FUNCTION IF EXISTS radar_tenant_actual();
    """)
