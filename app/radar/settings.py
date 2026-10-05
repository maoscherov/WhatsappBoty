"""
Configuración propia de Radar. Todas las variables llevan el prefijo RADAR_.

Radar corre como despliegue aparte (APP_MODE=radar) con Postgres y Redis
propios (§6.2, S7). Nunca comparte la base de un cliente del bot, por eso
ninguna de estas URLs es DATABASE_URL.
"""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class RadarSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RADAR_", env_file=".env", extra="ignore")

    # Base de resultados, dos URLs y dos roles (§6.4, RLS efectiva). Las tres URLs
    # llevan contraseñas: repr=False para que no salgan en el repr de los settings
    # ni en el del RadarContexto que los lleva.
    database_url: str = Field("", repr=False)            # rol radar_app: NOSUPERUSER, NOBYPASSRLS, no dueño
    migrator_database_url: str = Field("", repr=False)   # dueño de las tablas; solo Alembic

    # Roles de Postgres al arrancar (app/radar/bootstrap.py): crea radar_admin y
    # radar_app si faltan y le pone a radar_app la contraseña de database_url.
    # Apagado: se crean a mano con scripts/radar_bootstrap_roles.sql.
    bootstrap_roles: bool = False

    # Almacén de fuente permanente (§6.4). En este tramo no tiene tablas de
    # conversación: solo su marcador de esquema.
    fuente_database_url: str = Field("", repr=False)

    # Secretos fuera de la base: directorio del FileSecretStore (volumen,
    # fuera de los backups de Postgres). Ver docs/radar-despliegue.md.
    secrets_dir: str = "/data/radar-secrets"

    # Cookie de sesión firmada. Vacío = la app no arranca en modo radar.
    cookie_secret: str = ""
    cookie_secure: bool = True

    # Base pública para armar links mágicos, ej. https://radar.keepitsimple.com.ar
    public_base_url: str = "http://localhost:8000"

    # Backend de email: "log" (solo dominio y huella del token), "memoria" (tests) o "smtp".
    mailer: str = "log"
    remitente: str = "radar@keepitsimple.com.ar"

    # SMTP genérico (RADAR_MAILER=smtp): Google Workspace, Resend, SES, Brevo. La
    # contraseña es un SecretStr para que ningún repr de los settings la muestre;
    # se lee con .get_secret_value() (validar_settings y el mailer, nadie más).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_usuario: str = ""            # vacío = sin autenticar (relay de una red privada)
    smtp_password: SecretStr = SecretStr("")
    smtp_seguridad: str = "starttls"  # starttls | ssl | ninguna
    smtp_timeout_s: float = 20.0

    # Admins de KIS que crea el arranque si faltan (app/radar/bootstrap.py), emails separados por coma. No les
    # manda nada: cada uno entra después por /radar/login, que necesita mailer=smtp. Vacío: no crea ninguno.
    admins_iniciales: str = ""

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
