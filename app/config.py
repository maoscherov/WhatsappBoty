from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = "farma_verify_token"

    anthropic_api_key: str = ""

    mp_access_token: str = ""
    mp_notification_url: str = ""

    redis_url: str = "redis://localhost:6379"
    database_url: str = ""   # Postgres (Railway) — vacío = features de Postgres/RAG inactivas

    env: str = "development"
    sku_csv_path: str = "data/catalogo_base.csv"
    socios_path: str = "data/socios.csv"      # padrón de socios (CSV o XLSX)
    log_level: str = "INFO"
    bo_key: str = ""
    pickup_minutes: int = 30
    mp_webhook_secret: str = ""

    audio_provider: str = "groq"   # "groq" | "openai"
    llm_provider: str = "anthropic"  # "anthropic" | "openai" (con fallback automático al otro)
    groq_api_key: str = ""
    openai_api_key: str = ""

    mp_sandbox: bool = False   # True → usa sandbox_init_point de MP

    # Proveedor de pago: "mercadopago" | "payway"
    payment_provider: str = "mercadopago"
    payway_public_key: str = ""
    payway_private_key: str = ""
    payway_site_id: str = ""
    payway_template_id: str = ""   # ID del template del panel (solo para checkout hosteado/GenerateLink)
    payway_cybersource: bool = False   # True → el comercio exige datos antifraude en el cobro
    payway_cs_org_id: str = ""         # org_id del device fingerprint de Cybersource (lo da Payway)
    payway_sandbox: bool = True
    public_base_url: str = ""   # ej: https://cerca.remedia.ar (para success/notif urls)

    images_dir: str = "/data/images"          # volume mount en Railway
    image_server_api_key: str = ""            # clave para subir imágenes
    images_base_url: str = ""                 # ej: https://tuapp.railway.app/media

    # Mercurio ERP (SOAP) — vacíos hasta tener WSDL y credenciales
    mercurio_wsdl_url: str = ""
    mercurio_user: str = ""
    mercurio_password: str = ""

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()
