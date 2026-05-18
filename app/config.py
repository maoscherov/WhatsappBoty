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

    env: str = "development"
    sku_csv_path: str = "data/catalogo_base.csv"
    log_level: str = "INFO"
    bo_key: str = ""
    pickup_minutes: int = 30
    mp_webhook_secret: str = ""

    audio_provider: str = "groq"   # "groq" | "openai"
    groq_api_key: str = ""
    openai_api_key: str = ""

    mp_sandbox: bool = False   # True → usa sandbox_init_point de MP

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()
