"""환경변수 설정 - Pydantic Settings"""
from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """애플리케이션 설정"""

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Server
    port: int = 8000
    public_url: str = "http://localhost:8000"

    # Bot Framework
    bot_app_id: str = ""
    bot_app_password: str = ""
    bot_tenant_id: str = "common"

    # Supabase
    supabase_url: str = ""
    supabase_key: str = ""

    # Freshchat
    freshchat_api_key: str = ""
    freshchat_api_url: str = "https://api.freshchat.com/v2"
    freshchat_inbox_id: str = ""
    freshchat_webhook_public_key: str = ""

    # Zendesk
    zendesk_app_id: str = ""
    zendesk_key_id: str = ""
    zendesk_secret_key: str = ""

    # Encryption
    encryption_key: str = ""

    # Logging
    log_level: str = "info"


@lru_cache
def get_settings() -> Settings:
    """캐시된 설정 인스턴스 반환"""
    return Settings()
