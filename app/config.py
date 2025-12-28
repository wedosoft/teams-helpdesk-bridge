"""환경변수 설정 - Pydantic Settings"""
from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """애플리케이션 설정"""

    model_config = SettingsConfigDict(
        env_file=(".env.local",),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Server
    port: int = 8000
    public_url: str = "http://localhost:8000"

    # Bot Framework / Azure AD App
    bot_app_id: str = ""
    bot_app_password: str = ""
    bot_tenant_id: str = "common"

    # Admin OAuth (same Azure AD App)
    oauth_redirect_uri: str = ""  # e.g., https://your-domain/admin/callback

    # Supabase
    supabase_url: str = ""
    # Supabase API Key
    # - Some projects disable legacy keys (anon/service_role); use the new Secret key in that case.
    supabase_key: str = Field(
        default="",
        validation_alias="SUPABASE_SECRET_KEY",
    )

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

    # LLM (요약)
    llm_provider: str = "openai_compatible"
    llm_api_base: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.2
    llm_timeout: int = 60
    llm_azure_deployment: str = ""
    llm_azure_api_version: str = "2024-02-15-preview"

    # OCR
    ocr_provider: str = "none"
    ocr_endpoint: str = ""
    ocr_api_key: str = ""
    ocr_timeout: int = 60
    ocr_poll_interval: float = 1.5

    # Requester dashboard (PoC)
    # If requesters and agents share the same email, override requester identity with a fixed email.
    # MUST be set when requester dashboard endpoints are used.
    requester_email_override: str = Field(
        default="",
        validation_alias="REQUESTER_EMAIL_OVERRIDE",
    )


@lru_cache
def get_settings() -> Settings:
    """캐시된 설정 인스턴스 반환"""
    return Settings()
