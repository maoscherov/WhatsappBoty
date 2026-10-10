-- Roles de Radar. Correr UNA vez, como superusuario del servidor Postgres de
-- Radar (nunca en el Postgres de un cliente del bot), antes de la primera
-- migración:
--   psql "$URL_SUPERUSUARIO" -v migrator=postgres -f scripts/radar_bootstrap_roles.sql
-- :migrator es el rol que corre Alembic (dueño de las tablas). Las
-- contraseñas se fijan aparte y nunca en este archivo:
--   ALTER ROLE radar_app PASSWORD '...';
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'radar_app') THEN
        CREATE ROLE radar_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'radar_admin') THEN
        CREATE ROLE radar_admin NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END $$;
-- El migrator tiene que poder ceder funciones a radar_admin (ALTER ... OWNER TO).
GRANT radar_admin TO :migrator;
