"""Radar r0002: usuarios, membresías, líneas, tokens de login, sesiones,
consentimientos, soporte, auditoría y eventos de producto (§2.1, §4.4, §6.4).

Toda tabla lleva tenant_id NOT NULL con DEFAULT radar_tenant_actual(): un
INSERT desde tenant_tx() puede omitirlo y RLS (WITH CHECK) rechaza cualquier
valor distinto del tenant de la transacción.

Revision ID: r0002
Revises: r0001
Create Date: 2026-09-21
"""
from alembic import op

from app.radar.constantes import TENANT_KIS_STR
from app.radar.rls_sql import definir_funcion_admin, grants_app, politica_por_tenant

revision = "r0002"
down_revision = "r0001"
branch_labels = None
depends_on = None

# Privilegios de radar_app por tabla. Sin DELETE en nada que sea evidencia
# (tokens, consentimientos, auditoría, eventos): las purgas son del tramo 6 y
# corren con el rol dueño.
#
# UPDATE por columna (mismo criterio que r0001 con `tenants`): radar_app
# nunca puede cambiar id, tenant_id ni columnas de identidad (email,
# user_id, token_hash, otorgado_por) o de creación (created_at). En
# `memberships` sí puede cambiar `rol` (promover/degradar es una operación
# legítima de la app; el CHECK de la tabla impide que alguien se vuelva
# admin fuera del tenant KIS) y `lineas_permitidas`.
GRANTS = {
    "users": "SELECT, INSERT",
    "memberships": "SELECT, INSERT, DELETE",
    "lines": "SELECT, INSERT",
    "login_tokens": "SELECT, INSERT",
    "sessions": "SELECT, INSERT",
    "consents": "SELECT, INSERT",
    "support_grants": "SELECT, INSERT",
    "access_audit_log": "SELECT, INSERT",
    "product_events": "SELECT, INSERT",
}

# Columnas actualizables por radar_app, por tabla (nunca id, tenant_id ni
# columnas de identidad/creación).
UPDATES_POR_COLUMNA = {
    "users": "nombre",
    "memberships": "rol, lineas_permitidas",
    "lines": ("nombre, estado, almacen_fuente, duracion_vinculo_dias, retencion_fuente_dias, "
              "retencion_tras_desvinculo_dias, tope_ia_mensual_usd, parametros_propuestos, "
              "fuente_purgada_hasta, de_baja_at, updated_at"),
    "login_tokens": "used_at",
    "sessions": "revoked_at, last_seen_at",
    "support_grants": "revocado_at",
}


def upgrade() -> None:
    op.execute("""
        CREATE TABLE users (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id  UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            email      TEXT NOT NULL CHECK (email = lower(btrim(email)) AND email ~ '^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$'),
            nombre     TEXT NOT NULL DEFAULT '' CHECK (length(nombre) <= 120),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, email)
        );

        CREATE TABLE memberships (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id         UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            rol               TEXT NOT NULL CHECK (rol IN ('admin', 'dueno', 'gestor', 'lector')),
            lineas_permitidas UUID[] NULL,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, user_id),
            CHECK (rol <> 'admin' OR tenant_id = '""" + TENANT_KIS_STR + """')
        );

        CREATE TABLE lines (
            id                             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id                      UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            nombre                         TEXT NOT NULL CHECK (length(nombre) BETWEEN 1 AND 80),
            estado                         TEXT NOT NULL DEFAULT 'sin_vinculo'
                                           CHECK (estado IN ('vinculada', 'sin_vinculo', 'de_baja')),
            almacen_fuente                 TEXT NOT NULL DEFAULT 'permanente'
                                           CHECK (almacen_fuente IN ('permanente', 'purgable')),
            -- Parámetros de ámbito Línea (§2.1)
            duracion_vinculo_dias          INTEGER NOT NULL DEFAULT 0 CHECK (duracion_vinculo_dias >= 0),
            retencion_fuente_dias          INTEGER NOT NULL DEFAULT 0 CHECK (retencion_fuente_dias >= 0),
            retencion_tras_desvinculo_dias INTEGER NOT NULL DEFAULT 0 CHECK (retencion_tras_desvinculo_dias >= 0),
            tope_ia_mensual_usd            NUMERIC(10, 2) NULL CHECK (tope_ia_mensual_usd IS NULL OR tope_ia_mensual_usd >= 0),
            -- Propuesta pendiente de un admin de KIS para aflojar parámetros de una
            -- línea viva (§2.1); la consume el consentimiento del dueño.
            parametros_propuestos          JSONB NULL
                                           CHECK (parametros_propuestos IS NULL OR jsonb_typeof(parametros_propuestos) = 'object'),
            fuente_purgada_hasta           TIMESTAMPTZ NULL,
            de_baja_at                     TIMESTAMPTZ NULL,
            created_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),
            CHECK ((almacen_fuente = 'purgable') = (retencion_fuente_dias > 0)),
            CHECK ((estado = 'de_baja') = (de_baja_at IS NOT NULL))
        );

        CREATE TABLE login_tokens (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id    UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash   TEXT NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
            proposito    TEXT NOT NULL CHECK (proposito IN ('login', 'invitacion')),
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at   TIMESTAMPTZ NOT NULL,
            used_at      TIMESTAMPTZ NULL,
            ip_solicitud TEXT NULL
        );
        CREATE INDEX login_tokens_user_reciente ON login_tokens (tenant_id, user_id, created_at DESC);

        CREATE TABLE sessions (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id    UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            -- Una sesión de soporte referencia a un usuario del tenant KIS: la FK
            -- se valida igual (las comprobaciones de integridad no pasan por RLS).
            user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            rol          TEXT NOT NULL CHECK (rol IN ('admin', 'dueno', 'gestor', 'lector', 'soporte')),
            token_hash   TEXT NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at   TIMESTAMPTZ NOT NULL,
            revoked_at   TIMESTAMPTZ NULL,
            last_seen_at TIMESTAMPTZ NULL,
            ip           TEXT NULL
        );
        CREATE INDEX sessions_user ON sessions (tenant_id, user_id);

        CREATE TABLE consents (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id     UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            line_id       UUID NOT NULL REFERENCES lines(id),
            user_id       UUID NOT NULL REFERENCES users(id),
            version_texto TEXT NOT NULL CHECK (version_texto ~ '^v[0-9]+$'),
            hash_texto    TEXT NOT NULL CHECK (hash_texto ~ '^[0-9a-f]{64}$'),
            opciones      JSONB NOT NULL CHECK (jsonb_typeof(opciones) = 'object'),
            ip            TEXT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX consents_line ON consents (tenant_id, line_id, created_at DESC);

        CREATE TABLE support_grants (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id    UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            otorgado_por UUID NOT NULL REFERENCES users(id),
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at   TIMESTAMPTZ NOT NULL,
            revocado_at  TIMESTAMPTZ NULL,
            CHECK (expires_at > created_at AND expires_at <= created_at + interval '72 hours')
        );

        -- §4.4: actor, rol, acción, tipo de objeto, UUID interno, fecha e IP.
        -- Nunca teléfonos, JID, nombres ni texto. El CHECK rechaza cualquier
        -- string con 6 dígitos seguidos o '@' salvo en contact_hmac (hex).
        CREATE TABLE access_audit_log (
            id            BIGSERIAL PRIMARY KEY,
            tenant_id     UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            actor_user_id UUID NULL,
            actor_rol     TEXT NOT NULL CHECK (actor_rol IN ('admin', 'dueno', 'gestor', 'lector', 'soporte', 'sistema')),
            accion        TEXT NOT NULL CHECK (accion ~ '^[a-z_]{1,40}$'),
            tipo_objeto   TEXT NOT NULL CHECK (tipo_objeto ~ '^[a-z_]{1,40}$'),
            objeto_id     UUID NULL,
            ip            TEXT NULL,
            detalle       JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (
                              jsonb_typeof(detalle) = 'object'
                              AND NOT jsonb_path_exists(detalle,
                                  '$.keyvalue() ? (@.key != "contact_hmac" && @.value.type() == "string" && (@.value like_regex "[0-9]{6}" || @.value like_regex "@"))')
                          ),
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX access_audit_log_tenant_fecha ON access_audit_log (tenant_id, created_at);

        -- §6.4: solo tenant, línea, usuario, nombre de evento, UUID internos y
        -- valores numéricos. Ningún string en `valores`, a ninguna profundidad.
        CREATE TABLE product_events (
            id         BIGSERIAL PRIMARY KEY,
            tenant_id  UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            line_id    UUID NULL REFERENCES lines(id),
            user_id    UUID NULL,
            evento     TEXT NOT NULL CHECK (evento ~ '^[a-z_]{1,40}$'),
            objeto_id  UUID NULL,
            valores    JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (
                           jsonb_typeof(valores) = 'object'
                           AND NOT jsonb_path_exists(valores, '$.** ? (@.type() == "string")')
                       ),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX product_events_tenant_fecha ON product_events (tenant_id, created_at);
    """)

    for tabla, privilegios in GRANTS.items():
        op.execute(politica_por_tenant(tabla))
        op.execute(grants_app(tabla, privilegios))
        columnas = UPDATES_POR_COLUMNA.get(tabla)
        if columnas:
            op.execute(f"GRANT UPDATE ({columnas}) ON {tabla} TO radar_app;")
    op.execute("""
        GRANT USAGE ON SEQUENCE access_audit_log_id_seq TO radar_app;
        GRANT USAGE ON SEQUENCE product_events_id_seq TO radar_app;
    """)

    # Buscar usuarios por email cruza tenants (el que pide el link no sabe su
    # tenant): función SECURITY DEFINER de radar_admin, solo lectura.
    op.execute("""
        GRANT SELECT ON users TO radar_admin;
        CREATE POLICY users_admin ON users FOR SELECT TO radar_admin USING (true);
    """)
    op.execute(definir_funcion_admin(
        "radar_auth_usuarios_por_email(text)",
        """
        CREATE FUNCTION radar_auth_usuarios_por_email(p_email text)
            RETURNS TABLE (user_id uuid, tenant_id uuid)
            LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
            AS $f$
                SELECT u.id, u.tenant_id FROM users u
                WHERE u.email = lower(btrim(p_email)) ORDER BY u.created_at
            $f$;
        """,
    ))


def downgrade() -> None:
    op.execute("""
        DROP FUNCTION IF EXISTS radar_auth_usuarios_por_email(text);
        DROP TABLE IF EXISTS product_events, access_audit_log, support_grants, consents,
                             sessions, login_tokens, lines, memberships, users;
    """)
