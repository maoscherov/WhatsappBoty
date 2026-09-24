"""
Configuración propia de Radar. Todas las variables llevan el prefijo RADAR_.

Radar corre como despliegue aparte (APP_MODE=radar) con Postgres y Redis
propios (§6.2, S7). Nunca comparte la base de un cliente del bot, por eso
ninguna de estas URLs es DATABASE_URL.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class RadarSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RADAR_", env_file=".env", extra="ignore")

    # Base de resultados, dos URLs y dos roles (§6.4, RLS efectiva).
    database_url: str = ""            # rol radar_app: NOSUPERUSER, NOBYPASSRLS, no dueño
    migrator_database_url: str = ""   # dueño de las tablas; solo Alembic

    # Almacén de fuente permanente (§6.4). En este tramo no tiene tablas de
    # conversación: solo su marcador de esquema.
    fuente_database_url: str = ""

    # Secretos fuera de la base: directorio del FileSecretStore (volumen,
    # fuera de los backups de Postgres). Ver docs/radar-despliegue.md.
    secrets_dir: str = "/data/radar-secrets"

    # Cookie de sesión firmada. Vacío = la app no arranca en modo radar.
    cookie_secret: str = ""
    cookie_secure: bool = True

    # Base pública para armar links mágicos, ej. https://radar.keepitsimple.com.ar
    public_base_url: str = "http://localhost:8000"

    # Backend de email: "log" (solo dominio y huella del token) o "memoria" (tests).
    mailer: str = "log"
    remitente: str = "radar@keepitsimple.com.ar"

    # WAHA (tramo 2). La clave admin de cada worker NO va acá: vive en el
    # SecretStore (ver app.radar.workers.nombre_clave_admin) y la registra
    # scripts/radar_workers.py.
    waha_webhook_url: str = ""        # URL de /webhook/waha que alcanza WAHA (red privada de Railway)
    waha_webhook_hmac_key: str = ""   # 32+ caracteres; vacía = el receptor rechaza todo (fail-closed)
    waha_timeout_s: float = 20.0

    # Worker de la cola dentro del servicio web (ver app/radar/worker.py).
    worker_embebido: bool = True


@lru_cache
def get_radar_settings() -> RadarSettings:
    return RadarSettings()
