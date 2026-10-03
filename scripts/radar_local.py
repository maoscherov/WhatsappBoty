"""Radar local para la prueba manual contra WAHA real (runbook del tramo 2).

Levanta un Postgres embebido (pgserver) persistente en .radar_local/, crea los
roles reales de RLS (igual que tests/radar_tests/conftest.py), y corre Radar o
cualquier comando con las variables RADAR_* ya puestas. Los secretos locales
(cookie, HMAC del webhook) se generan una vez y quedan en .radar_local/, que
está ignorado por git. Nunca se imprimen.

    python scripts/radar_local.py serve --webhook-url https://<túnel>/webhook/waha
    python scripts/radar_local.py exec python scripts/radar_admin.py crear-admin --email ... --nombre ...
    python scripts/radar_local.py exec python scripts/radar_workers.py registrar ...

Solo para desarrollo local: nunca apuntar a una base de producción.
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DIR = RAIZ / ".radar_local"

ROLES_SQL = """
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'radar_migrator') THEN
        CREATE ROLE radar_migrator LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'radar_app') THEN
        CREATE ROLE radar_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'radar_admin') THEN
        CREATE ROLE radar_admin NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END $$;
GRANT radar_admin TO radar_migrator;
"""


def _config() -> dict:
    DIR.mkdir(exist_ok=True)
    f = DIR / "config.json"
    if f.exists():
        return json.loads(f.read_text())
    cfg = {"cookie_secret": secrets.token_urlsafe(48), "hmac_key": secrets.token_urlsafe(48)}
    f.write_text(json.dumps(cfg))
    return cfg


def _postgres():
    import pgserver
    import psycopg2

    DIR.mkdir(exist_ok=True)
    srv = pgserver.get_server(str(DIR / "pg"))
    info = srv.get_postmaster_info()
    su = psycopg2.connect(info.get_uri())
    su.autocommit = True
    cur = su.cursor()
    cur.execute(ROLES_SQL)
    for db in ("radar", "radar_fuente"):
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db,))
        if not cur.fetchone():
            cur.execute(f"CREATE DATABASE {db} OWNER radar_migrator")
    su.close()
    return srv, info


def _entorno(info, webhook_url: str | None) -> dict:
    cfg = _config()
    env = dict(os.environ)
    env.update({
        "APP_MODE": "radar",
        "RADAR_DATABASE_URL": info.get_uri(user="radar_app", database="radar"),
        "RADAR_MIGRATOR_DATABASE_URL": info.get_uri(user="radar_migrator", database="radar"),
        "RADAR_FUENTE_DATABASE_URL": info.get_uri(user="radar_migrator", database="radar_fuente"),
        "RADAR_SECRETS_DIR": str(DIR / "secretos"),
        "RADAR_COOKIE_SECRET": cfg["cookie_secret"],
        "RADAR_COOKIE_SECURE": "false",
        "RADAR_PUBLIC_BASE_URL": "http://localhost:8000",
        "RADAR_MAILER": "log",
        "RADAR_WAHA_WEBHOOK_HMAC_KEY": cfg["hmac_key"],
    })
    if webhook_url:
        env["RADAR_WAHA_WEBHOOK_URL"] = webhook_url
    return env


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="corre Radar en http://localhost:8000")
    s.add_argument("--webhook-url", required=True, help="https://<túnel>/webhook/waha")
    e = sub.add_parser("exec", help="corre un comando con las variables de Radar local")
    e.add_argument("argv", nargs=argparse.REMAINDER)
    a = p.parse_args()

    srv, info = _postgres()
    try:
        if a.cmd == "serve":
            env = _entorno(info, a.webhook_url)
            argv = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"]
        else:
            if not a.argv:
                p.error("exec necesita un comando")
            env = _entorno(info, None)
            argv = a.argv
        return subprocess.call(argv, cwd=RAIZ, env=env)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
