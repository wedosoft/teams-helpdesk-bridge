from __future__ import annotations

"""테넌트 서비스

멀티테넌트 지원을 위한 테넌트 설정 관리
- 테넌트별 플랫폼 설정 조회
- 설정 캐싱 (성능)
- API 키 암호화/복호화
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional
import time

from app.database import Database
from app.utils.logger import get_logger
from app.utils.crypto import decrypt_config, encrypt_config
from app.config import get_settings

logger = get_logger(__name__)


# 캐시 TTL (5분)
TENANT_CACHE_TTL = 300


class Platform(str, Enum):
    """지원 플랫폼"""
    FRESHCHAT = "freshchat"
    ZENDESK = "zendesk"
    FRESHDESK = "freshdesk"


@dataclass
class FreshchatConfig:
    """Freshchat 설정"""
    api_key: str
    api_url: str = "https://api.freshchat.com/v2"
    inbox_id: str = ""
    webhook_public_key: str = ""


@dataclass
class ZendeskConfig:
    """Zendesk 설정"""
    subdomain: str  # {subdomain}.zendesk.com
    email: str
    api_token: str
    # 또는 OAuth
    oauth_token: Optional[str] = None


@dataclass
class FreshdeskConfig:
    """Freshdesk 설정 (Freshdesk Omni 포함)

    Notes:
      - base_url 예: https://{domain}.freshdesk.com
      - weight_field_key 예: cf_weight
    """
    base_url: str
    api_key: str
    weight_field_key: str = ""


@dataclass
class TenantConfig:
    """테넌트 설정"""
    id: str
    teams_tenant_id: str
    platform: Platform

    # 플랫폼별 설정
    freshchat: Optional[FreshchatConfig] = None
    zendesk: Optional[ZendeskConfig] = None
    freshdesk: Optional[FreshdeskConfig] = None

    # UI 설정
    bot_name: str = "IT Helpdesk"
    welcome_message: str = "안녕하세요! IT 헬프데스크입니다. 무엇을 도와드릴까요?"

    # 메타데이터
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def get_platform_config(self) -> FreshchatConfig | ZendeskConfig | FreshdeskConfig | None:
        """현재 플랫폼 설정 반환"""
        if self.platform == Platform.FRESHCHAT:
            return self.freshchat
        elif self.platform == Platform.ZENDESK:
            return self.zendesk
        elif self.platform == Platform.FRESHDESK:
            return self.freshdesk
        return None


@dataclass
class CachedTenant:
    """캐시된 테넌트"""
    config: TenantConfig
    cached_at: float


class TenantService:
    """테넌트 서비스

    멀티테넌트 환경에서 테넌트별 설정 관리
    """

    def __init__(self):
        self._db: Optional[Database] = None
        self._cache: dict[str, CachedTenant] = {}

    @property
    def db(self) -> Database:
        """Database (Supabase) 클라이언트

        로컬/POC에서 Supabase를 설정하지 않은 경우에도 서버가 기동될 수 있도록 lazy 로드합니다.
        """
        if self._db is None:
            self._db = Database()
        return self._db

    async def get_tenant(self, teams_tenant_id: str) -> Optional[TenantConfig]:
        """
        테넌트 설정 조회

        Args:
            teams_tenant_id: Teams 테넌트 ID

        Returns:
            TenantConfig 또는 None (미등록 테넌트)
        """
        # 1. 캐시 확인
        cached = self._cache.get(teams_tenant_id)
        if cached and not self._is_cache_expired(cached):
            logger.debug("Tenant cache hit", teams_tenant_id=teams_tenant_id)
            return cached.config

        # 2. DB 조회
        try:
            settings = get_settings()
            if not settings.supabase_url or not settings.supabase_key:
                raise RuntimeError("Supabase is not configured (SUPABASE_URL/SUPABASE_SECRET_KEY)")

            data = await self.db.get_tenant_by_teams_id(teams_tenant_id)
            if not data:
                logger.debug("Tenant not found", teams_tenant_id=teams_tenant_id)
                return None

            # 설정 복호화 및 파싱
            config = self._parse_tenant_config(data)

            # 캐시 저장
            self._cache[teams_tenant_id] = CachedTenant(
                config=config,
                cached_at=time.time(),
            )

            logger.info("Loaded tenant config", teams_tenant_id=teams_tenant_id, platform=config.platform)
            return config

        except RuntimeError:
            # 설정 누락 등은 호출자에서 5xx로 처리하도록 전파
            raise
        except Exception as e:
            msg = str(e)
            if "Legacy API keys are disabled" in msg or "401 Unauthorized" in msg:
                raise RuntimeError(
                    "Supabase credentials rejected (401). If your project disabled legacy keys, "
                    "set SUPABASE_SECRET_KEY to the new secret API key in the Supabase dashboard "
                    "(or re-enable legacy keys)."
                )

            logger.error("Failed to get tenant", teams_tenant_id=teams_tenant_id, error=msg)
            return None

    async def create_tenant(
        self,
        teams_tenant_id: str,
        platform: Platform,
        platform_config: dict,
        bot_name: str = "IT Helpdesk",
        welcome_message: str = "",
    ) -> Optional[TenantConfig]:
        """
        테넌트 생성

        Args:
            teams_tenant_id: Teams 테넌트 ID
            platform: 플랫폼 (freshchat/zendesk)
            platform_config: 플랫폼 설정 (암호화 전)
            bot_name: 봇 이름
            welcome_message: 환영 메시지

        Returns:
            생성된 TenantConfig 또는 None
        """
        try:
            settings = get_settings()
            if not settings.supabase_url or not settings.supabase_key:
                raise RuntimeError("Supabase is not configured (SUPABASE_URL/SUPABASE_SECRET_KEY)")

            # 설정 암호화
            encrypted_config = encrypt_config(platform_config)

            data = {
                "teams_tenant_id": teams_tenant_id,
                "platform": platform.value,
                "platform_config": encrypted_config,
                "bot_name": bot_name,
                "welcome_message": welcome_message,
            }

            result = await self.db.upsert_tenant(data)
            if not result:
                return None

            # 캐시 무효화
            self._invalidate_cache(teams_tenant_id)

            # 새로 조회하여 반환
            return await self.get_tenant(teams_tenant_id)

        except Exception as e:
            logger.error("Failed to create tenant", teams_tenant_id=teams_tenant_id, error=str(e))
            return None

    async def update_tenant(
        self,
        teams_tenant_id: str,
        platform: Optional[Platform] = None,
        platform_config: Optional[dict] = None,
        bot_name: Optional[str] = None,
        welcome_message: Optional[str] = None,
    ) -> Optional[TenantConfig]:
        """
        테넌트 업데이트

        Args:
            teams_tenant_id: Teams 테넌트 ID
            platform: 새 플랫폼 (선택)
            platform_config: 새 플랫폼 설정 (선택)
            bot_name: 새 봇 이름 (선택)
            welcome_message: 새 환영 메시지 (선택)

        Returns:
            업데이트된 TenantConfig 또는 None
        """
        try:
            settings = get_settings()
            if not settings.supabase_url or not settings.supabase_key:
                raise RuntimeError("Supabase is not configured (SUPABASE_URL/SUPABASE_SECRET_KEY)")

            # 기존 설정 조회
            existing = await self.db.get_tenant_by_teams_id(teams_tenant_id)
            if not existing:
                logger.warning("Tenant not found for update", teams_tenant_id=teams_tenant_id)
                return None

            # 업데이트할 필드 구성
            update_data: dict[str, Any] = {}

            if platform is not None:
                update_data["platform"] = platform.value

            if platform_config is not None:
                update_data["platform_config"] = encrypt_config(platform_config)

            if bot_name is not None:
                update_data["bot_name"] = bot_name

            if welcome_message is not None:
                update_data["welcome_message"] = welcome_message

            if not update_data:
                return await self.get_tenant(teams_tenant_id)

            # DB 업데이트
            await self.db.update_tenant(teams_tenant_id, update_data)

            # 캐시 무효화
            self._invalidate_cache(teams_tenant_id)

            return await self.get_tenant(teams_tenant_id)

        except Exception as e:
            logger.error("Failed to update tenant", teams_tenant_id=teams_tenant_id, error=str(e))
            return None

    async def delete_tenant(self, teams_tenant_id: str) -> bool:
        """
        테넌트 삭제

        Args:
            teams_tenant_id: Teams 테넌트 ID

        Returns:
            성공 여부
        """
        try:
            settings = get_settings()
            if not settings.supabase_url or not settings.supabase_key:
                raise RuntimeError("Supabase is not configured (SUPABASE_URL/SUPABASE_SECRET_KEY)")

            await self.db.delete_tenant(teams_tenant_id)
            self._invalidate_cache(teams_tenant_id)
            logger.info("Deleted tenant", teams_tenant_id=teams_tenant_id)
            return True
        except Exception as e:
            logger.error("Failed to delete tenant", teams_tenant_id=teams_tenant_id, error=str(e))
            return False

    def _parse_tenant_config(self, data: dict) -> TenantConfig:
        """DB 데이터에서 TenantConfig 파싱"""
        platform = Platform(data["platform"])

        # 플랫폼 설정 복호화
        encrypted_config = data.get("platform_config", {})
        try:
            platform_config = decrypt_config(encrypted_config)
        except RuntimeError as e:
            raise RuntimeError(
                "Failed to decrypt tenant platform_config. Check ENCRYPTION_KEY is set and matches the key "
                "used when the tenant was saved. If the key changed, re-save the tenant config to re-encrypt."
            ) from e

        config = TenantConfig(
            id=data["id"],
            teams_tenant_id=data["teams_tenant_id"],
            platform=platform,
            bot_name=data.get("bot_name", "IT Helpdesk"),
            welcome_message=data.get("welcome_message", ""),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )

        # 플랫폼별 설정 파싱
        if platform == Platform.FRESHCHAT:
            config.freshchat = FreshchatConfig(
                api_key=platform_config.get("api_key", ""),
                api_url=platform_config.get("api_url", "https://api.freshchat.com/v2"),
                inbox_id=platform_config.get("inbox_id", ""),
                webhook_public_key=platform_config.get("webhook_public_key", ""),
            )
        elif platform == Platform.ZENDESK:
            config.zendesk = ZendeskConfig(
                subdomain=platform_config.get("subdomain", ""),
                email=platform_config.get("email", ""),
                api_token=platform_config.get("api_token", ""),
                oauth_token=platform_config.get("oauth_token"),
            )
        elif platform == Platform.FRESHDESK:
            config.freshdesk = FreshdeskConfig(
                base_url=platform_config.get("base_url", ""),
                api_key=platform_config.get("api_key", ""),
                weight_field_key=platform_config.get("weight_field_key", ""),
            )

        return config

    def _is_cache_expired(self, cached: CachedTenant) -> bool:
        """캐시 만료 확인"""
        return time.time() - cached.cached_at > TENANT_CACHE_TTL

    def _invalidate_cache(self, teams_tenant_id: str) -> None:
        """캐시 무효화"""
        self._cache.pop(teams_tenant_id, None)

    def clear_cache(self) -> None:
        """전체 캐시 클리어"""
        self._cache.clear()


# ===== 싱글톤 인스턴스 =====

_tenant_service: Optional[TenantService] = None


def get_tenant_service() -> TenantService:
    """TenantService 싱글톤 인스턴스 반환"""
    global _tenant_service
    if _tenant_service is None:
        _tenant_service = TenantService()
    return _tenant_service
