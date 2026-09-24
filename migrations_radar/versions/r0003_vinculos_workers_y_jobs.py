"""Radar r0003: workers de WAHA, vínculos, eventos de estado, cola de jobs y
consentimiento asistido (§3.1, §6.2, §6.3 punto 6, §6.4).

Ninguna tabla guarda teléfonos, JID, QR, códigos ni claves de WAHA: del número
solo queda el sufijo de 4 dígitos (numero_sufijo) y de la clave de lectura su
id (key_id). La clave admin de cada worker vive en el SecretStore.

Revision ID: r0003
Revises: r0002
Create Date: 2026-09-23
"""
from alembic import op

from app.radar.constantes import CAUSAS_FIN, ESTADOS_LINK, TENANT_KIS_STR
from app.radar.rls_sql import definir_funcion_admin, grants_app, politica_por_tenant

revision = "r0003"
down_revision = "r0002"
branch_labels = None
depends_on = None

# Sin DELETE en nada: vínculos, eventos y jobs son evidencia operativa.
GRANTS = {
    "waha_workers": "SELECT, INSERT",
    "links": "SELECT, INSERT",
    "link_status_events": "SELECT, INSERT",
    "jobs": "SELECT, INSERT",
}
# Columnas actualizables por radar_app, con el criterio de r0002: nunca id,
# tenant_id, identidad, FK de pertenencia ni created_at. El motor de un worker
# no cambia (cambiarlo es un worker nuevo, §6.2); un vínculo no cambia de línea,
# worker, consentimiento ni sesión; un job no cambia de tipo, link ni causa.
UPDATES_POR_COLUMNA = {
    "waha_workers": "base_url, max_sesiones, disco_max_gb, disco_usado_gb, activo, updated_at",
    "links": ("estado, waha_status, qr_reinicios, key_id, numero_sufijo, conectado_at, caido_desde, "
              "ultimo_status_at, ultimo_chequeo_at, observado_hasta, restriccion_hasta, restriccion_sin_fecha, "
              "fin_causa, fin_resultado, desvinculo_confirmado, updated_at, cerrado_at"),
    "jobs": "estado, intentos, ejecutar_desde, bloqueado_hasta, ultimo_error, updated_at",
}
# Listas de los CHECK derivadas de las constantes (como TENANT_KIS_STR en
# r0002), para que el esquema y el código no se separen.
_ESTADOS = "(" + ", ".join(f"'{e}'" for e in ESTADOS_LINK) + ")"
_CAUSAS = "(" + ", ".join(f"'{c}'" for c in CAUSAS_FIN) + ")"
# Estados en los que la sesión existe en WAHA y ocupa lugar en el worker.
_CON_SESION = "('creando', 'esperando_qr', 'vinculado', 'caido', 'cerrando')"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE waha_workers (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id      UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id)
                           CHECK (tenant_id = '""" + TENANT_KIS_STR + """'),
            nombre         TEXT NOT NULL UNIQUE CHECK (nombre ~ '^[a-z0-9_-]{1,40}$'),
            base_url       TEXT NOT NULL CHECK (base_url ~ '^https?://[^\\s]+$'),
            engine         TEXT NOT NULL CHECK (engine IN ('NOWEB', 'GOWS')),
            max_sesiones   INTEGER NOT NULL CHECK (max_sesiones > 0),
            disco_max_gb   NUMERIC(8, 2) NOT NULL CHECK (disco_max_gb > 0),
            disco_usado_gb NUMERIC(8, 2) NOT NULL DEFAULT 0 CHECK (disco_usado_gb >= 0),
            activo         BOOLEAN NOT NULL DEFAULT TRUE,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        ALTER TABLE lines ADD COLUMN borrado_solicitado_at TIMESTAMPTZ NULL;

        -- §3.1 C2: consentimiento asistido por un admin de KIS.
        ALTER TABLE consents
            ADD COLUMN modo TEXT NOT NULL DEFAULT 'propio' CHECK (modo IN ('propio', 'asistido')),
            ADD COLUMN cargado_por UUID NULL REFERENCES users(id),
            ADD COLUMN modo_asistencia TEXT NULL CHECK (modo_asistencia IN ('presencial', 'videollamada')),
            ADD COLUMN aceptado_por_nombre TEXT NULL
                CHECK (aceptado_por_nombre IS NULL OR length(aceptado_por_nombre) BETWEEN 1 AND 120),
            ADD CONSTRAINT consents_asistido_completo CHECK (
                (modo = 'asistido') = (cargado_por IS NOT NULL AND modo_asistencia IS NOT NULL
                                       AND aceptado_por_nombre IS NOT NULL));

        CREATE TABLE links (
            id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id             UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            line_id               UUID NOT NULL REFERENCES lines(id),
            worker_id             UUID NOT NULL REFERENCES waha_workers(id),
            consent_id            UUID NOT NULL REFERENCES consents(id),
            proveedor             TEXT NOT NULL DEFAULT 'waha' CHECK (proveedor IN ('waha', 'meta', 'kapso')),
            session_name          TEXT NOT NULL UNIQUE CHECK (session_name ~ '^v_[0-9a-f]{12}$'),
            engine                TEXT NOT NULL CHECK (engine IN ('NOWEB', 'GOWS')),
            full_sync             BOOLEAN NOT NULL DEFAULT FALSE,
            estado                TEXT NOT NULL DEFAULT 'creando' CHECK (estado IN """ + _ESTADOS + """),
            waha_status           TEXT NULL CHECK (waha_status ~ '^[A-Z_]{1,40}$'),
            qr_reinicios          INTEGER NOT NULL DEFAULT 0 CHECK (qr_reinicios BETWEEN 0 AND 3),
            key_id                TEXT NULL CHECK (key_id ~ '^[A-Za-z0-9_-]{1,80}$'),
            numero_sufijo         TEXT NULL CHECK (numero_sufijo ~ '^[0-9]{4}$'),
            creado_por            UUID NULL,
            conectado_at          TIMESTAMPTZ NULL,
            caido_desde           TIMESTAMPTZ NULL,
            ultimo_status_at      TIMESTAMPTZ NULL,
            ultimo_chequeo_at     TIMESTAMPTZ NULL,
            observado_hasta       TIMESTAMPTZ NULL,
            restriccion_hasta     TIMESTAMPTZ NULL,
            restriccion_sin_fecha BOOLEAN NOT NULL DEFAULT FALSE,
            fin_causa             TEXT NULL CHECK (fin_causa IN """ + _CAUSAS + """),
            fin_resultado         JSONB NULL CHECK (fin_resultado IS NULL OR (
                                      jsonb_typeof(fin_resultado) = 'object'
                                      AND NOT jsonb_path_exists(fin_resultado,
                                          '$.** ? (@.type() == "string" && (@ like_regex "[0-9]{6}" || @ like_regex "@"))'))),
            desvinculo_confirmado BOOLEAN NULL,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            cerrado_at            TIMESTAMPTZ NULL
        );
        CREATE UNIQUE INDEX links_un_activo_por_linea ON links (line_id)
            WHERE estado IN ('creando', 'esperando_qr', 'vinculado');
        CREATE INDEX links_linea ON links (tenant_id, line_id, created_at DESC);
        CREATE INDEX links_worker ON links (worker_id) WHERE estado IN """ + _CON_SESION + """;

        CREATE TABLE link_status_events (
            id          BIGSERIAL PRIMARY KEY,
            tenant_id   UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            link_id     UUID NOT NULL REFERENCES links(id),
            waha_status TEXT NOT NULL CHECK (waha_status ~ '^[A-Z_]{1,40}$'),
            origen      TEXT NOT NULL CHECK (origen IN ('webhook', 'polling', 'salud', 'accion')),
            estado_link TEXT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX link_status_events_link ON link_status_events (tenant_id, link_id, created_at);

        -- §6.2: cola en Postgres, sin contenido.
        CREATE TABLE jobs (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       UUID NOT NULL DEFAULT radar_tenant_actual() REFERENCES tenants(id),
            tipo            TEXT NOT NULL CHECK (tipo IN ('fin_vinculo', 'chequeo_salud', 'aviso_caida')),
            link_id         UUID NOT NULL REFERENCES links(id),
            causa           TEXT NULL CHECK (causa ~ '^[a-z_]{1,40}$'),
            estado          TEXT NOT NULL DEFAULT 'pendiente'
                            CHECK (estado IN ('pendiente', 'corriendo', 'hecho', 'fallido')),
            intentos        INTEGER NOT NULL DEFAULT 0,
            max_intentos    INTEGER NOT NULL DEFAULT 8,
            ejecutar_desde  TIMESTAMPTZ NOT NULL DEFAULT now(),
            bloqueado_hasta TIMESTAMPTZ NULL,
            ultimo_error    TEXT NULL CHECK (ultimo_error ~ '^[A-Za-z_]{1,60}$'),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE UNIQUE INDEX jobs_uno_vivo_por_link_y_tipo ON jobs (link_id, tipo)
            WHERE estado IN ('pendiente', 'corriendo');
        CREATE INDEX jobs_listos ON jobs (ejecutar_desde) WHERE estado IN ('pendiente', 'corriendo');
    """)

    for tabla, privilegios in GRANTS.items():
        op.execute(politica_por_tenant(tabla))
        op.execute(grants_app(tabla, privilegios))
        columnas = UPDATES_POR_COLUMNA.get(tabla)
        if columnas:
            op.execute(f"GRANT UPDATE ({columnas}) ON {tabla} TO radar_app;")
    # Columna nueva de lines: el GRANT UPDATE por columna de r0002 no la incluye
    # y sin esto "Desconectar y borrar todo" falla con InsufficientPrivilege.
    op.execute("GRANT UPDATE (borrado_solicitado_at) ON lines TO radar_app;")
    op.execute("GRANT USAGE ON SEQUENCE link_status_events_id_seq TO radar_app;")

    # radar_admin lee entre tenants solo lo que necesitan las funciones de abajo.
    op.execute("""
        GRANT SELECT ON waha_workers, links, lines TO radar_admin;
        GRANT SELECT, INSERT, UPDATE ON jobs TO radar_admin;
        CREATE POLICY waha_workers_admin ON waha_workers FOR SELECT TO radar_admin USING (true);
        CREATE POLICY links_admin ON links FOR SELECT TO radar_admin USING (true);
        CREATE POLICY lines_admin ON lines FOR SELECT TO radar_admin USING (true);
        CREATE POLICY jobs_admin ON jobs FOR ALL TO radar_admin USING (true) WITH CHECK (true);
    """)

    op.execute(definir_funcion_admin(
        "radar_admin_ocupacion_workers()",
        """
        CREATE FUNCTION radar_admin_ocupacion_workers()
            RETURNS TABLE (worker_id uuid, nombre text, base_url text, engine text, max_sesiones int,
                           disco_max_gb numeric, disco_usado_gb numeric, activo bool, sesiones int)
            LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
            AS $f$
                SELECT w.id, w.nombre, w.base_url, w.engine, w.max_sesiones, w.disco_max_gb,
                       w.disco_usado_gb, w.activo,
                       (SELECT count(*)::int FROM links l
                        WHERE l.worker_id = w.id AND l.estado IN """ + _CON_SESION + """)
                FROM waha_workers w ORDER BY w.nombre
            $f$;
        """,
    ))
    op.execute(definir_funcion_admin(
        "radar_admin_consola_lineas()",
        """
        CREATE FUNCTION radar_admin_consola_lineas()
            RETURNS TABLE (tenant_id uuid, tenant_nombre text, line_id uuid, line_nombre text, line_estado text,
                           link_id uuid, link_estado text, waha_status text, numero_sufijo text,
                           observado_hasta timestamptz, restriccion_hasta timestamptz, restriccion_sin_fecha bool,
                           caido_desde timestamptz, ultimo_status_at timestamptz, engine text,
                           worker_nombre text, worker_max_sesiones int, worker_sesiones int)
            LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
            AS $f$
                SELECT t.id, t.nombre, li.id, li.nombre, li.estado,
                       lk.id, lk.estado, lk.waha_status, lk.numero_sufijo, lk.observado_hasta,
                       lk.restriccion_hasta, lk.restriccion_sin_fecha, lk.caido_desde, lk.ultimo_status_at,
                       lk.engine, w.nombre, w.max_sesiones,
                       (SELECT count(*)::int FROM links x
                        WHERE x.worker_id = w.id AND x.estado IN """ + _CON_SESION + """)
                FROM lines li
                JOIN tenants t ON t.id = li.tenant_id AND NOT t.es_kis
                LEFT JOIN LATERAL (SELECT * FROM links l WHERE l.line_id = li.id
                                   ORDER BY l.created_at DESC LIMIT 1) lk ON true
                LEFT JOIN waha_workers w ON w.id = lk.worker_id
                ORDER BY t.nombre, li.nombre
            $f$;
        """,
    ))
    op.execute(definir_funcion_admin(
        "radar_jobs_reclamar(integer, integer)",
        """
        CREATE FUNCTION radar_jobs_reclamar(p_lote integer, p_lease_s integer)
            RETURNS TABLE (id uuid, tenant_id uuid, tipo text, link_id uuid, causa text, intentos int)
            LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
            AS $f$
                UPDATE jobs j
                   SET estado = 'corriendo', intentos = j.intentos + 1,
                       bloqueado_hasta = now() + make_interval(secs => p_lease_s), updated_at = now()
                 WHERE j.id IN (
                       SELECT x.id FROM jobs x
                        WHERE (x.estado = 'pendiente' AND x.ejecutar_desde <= now())
                           OR (x.estado = 'corriendo' AND x.bloqueado_hasta < now())
                        ORDER BY x.ejecutar_desde
                        FOR UPDATE SKIP LOCKED
                        LIMIT p_lote)
                RETURNING j.id, j.tenant_id, j.tipo, j.link_id, j.causa, j.intentos
            $f$;
        """,
    ))
    op.execute(definir_funcion_admin(
        "radar_jobs_programar_salud()",
        """
        CREATE FUNCTION radar_jobs_programar_salud() RETURNS integer
            LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
            AS $f$
                WITH nuevos AS (
                    INSERT INTO jobs (tenant_id, tipo, link_id)
                    SELECT l.tenant_id, 'chequeo_salud', l.id FROM links l
                     WHERE l.estado IN ('creando', 'esperando_qr', 'vinculado', 'caido')
                    ON CONFLICT DO NOTHING
                    RETURNING 1)
                SELECT count(*)::int FROM nuevos
            $f$;
        """,
    ))


def downgrade() -> None:
    op.execute("""
        DROP FUNCTION IF EXISTS radar_jobs_programar_salud();
        DROP FUNCTION IF EXISTS radar_jobs_reclamar(integer, integer);
        DROP FUNCTION IF EXISTS radar_admin_consola_lineas();
        DROP FUNCTION IF EXISTS radar_admin_ocupacion_workers();
        DROP POLICY IF EXISTS lines_admin ON lines;
        REVOKE SELECT ON lines FROM radar_admin;
        DROP TABLE IF EXISTS jobs, link_status_events, links, waha_workers;
        ALTER TABLE consents DROP CONSTRAINT IF EXISTS consents_asistido_completo,
            DROP COLUMN IF EXISTS aceptado_por_nombre, DROP COLUMN IF EXISTS modo_asistencia,
            DROP COLUMN IF EXISTS cargado_por, DROP COLUMN IF EXISTS modo;
        REVOKE UPDATE (borrado_solicitado_at) ON lines FROM radar_app;
        ALTER TABLE lines DROP COLUMN IF EXISTS borrado_solicitado_at;
    """)
