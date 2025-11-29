"""플랫폼 클라이언트 팩토리

테넌트별 플랫폼 클라이언트 생성 및 캐싱
- Freshchat
- Zendesk
"""
from typing import Optional, Protocol, Any
import time

from app.adapters.freshchat.client import FreshchatClient
from app.adapters.freshchat.webhook import FreshchatWebhookHandler
from app.adapters.zendesk.client import ZendeskClient
from app.adapters.zendesk.webhook import ZendeskWebhookHandler
from app.core.tenant import TenantConfig, Platform, FreshchatConfig, ZendeskConfig
from app.utils.logger import get_logger

logger = get_logger(__name__)


# 클라이언트 캐시 TTL (10분)
CLIENT_CACHE_TTL = 600


class HelpdeskClient(Protocol):
    """헬프데스크 클라이언트 프로토콜"""

    async def get_or_create_user(
        self,
        reference_id: str,
        name: Optional[str] = None,
        email: Optional[str] = None,
        properties: Optional[dict] = None,
    ) -> Optional[str]:
        """사용자 생성/조회"""
        ...

    async def create_conversation(
        self,
        user_id: str,
        user_name: str,
        message_text: Optional[str] = None,
        attachments: Optional[list[dict]] = None,
    ) -> Optional[dict]:
        """대화 생성"""
        ...

    async def send_message(
        self,
        conversation_id: str,
        user_id: str,
        message_text: Optional[str] = None,
        attachments: Optional[list[dict]] = None,
    ) -> bool:
        """메시지 전송"""
        ...

    async def upload_file(
        self,
        file_buffer: bytes,
        filename: str,
        content_type: str,
    ) -> Optional[dict]:
        """파일 업로드"""
        ...

    async def get_agent_name(self, agent_id: str) -> Optional[str]:
        """상담원 이름 조회"""
        ...


class CachedClient:
    """캐시된 클라이언트"""
    client: Any
    webhook_handler: Any
    cached_at: float


class PlatformFactory:
    """플랫폼 클라이언트 팩토리

    테넌트별 플랫폼 클라이언트 생성 및 캐싱
    """

    def __init__(self):
        # tenant_id -> CachedClient
        self._cache: dict[str, CachedClient] = {}

    def get_client(self, tenant: TenantConfig) -> Optional[HelpdeskClient]:
        """
        테넌트용 플랫폼 클라이언트 반환

        Args:
            tenant: 테넌트 설정

        Returns:
            플랫폼 클라이언트 또는 None
        """
        cache_key = tenant.id

        # 캐시 확인
        cached = self._cache.get(cache_key)
        if cached and not self._is_cache_expired(cached):
            return cached.client

        # 클라이언트 생성
        client = self._create_client(tenant)
        if not client:
            return None

        # 캐시 저장
        cached_client = CachedClient()
        cached_client.client = client
        cached_client.webhook_handler = self._create_webhook_handler(tenant)
        cached_client.cached_at = time.time()
        self._cache[cache_key] = cached_client

        return client

    def get_webhook_handler(self, tenant: TenantConfig) -> Optional[Any]:
        """
        테넌트용 웹훅 핸들러 반환

        Args:
            tenant: 테넌트 설정

        Returns:
            웹훅 핸들러 또는 None
        """
        cache_key = tenant.id

        # 캐시 확인 (클라이언트와 함께 캐시됨)
        cached = self._cache.get(cache_key)
        if cached and not self._is_cache_expired(cached):
            return cached.webhook_handler

        # 클라이언트 먼저 생성 (웹훅 핸들러도 함께 캐시됨)
        self.get_client(tenant)

        cached = self._cache.get(cache_key)
        return cached.webhook_handler if cached else None

    def _create_client(self, tenant: TenantConfig) -> Optional[HelpdeskClient]:
        """플랫폼 클라이언트 생성"""
        if tenant.platform == Platform.FRESHCHAT:
            return self._create_freshchat_client(tenant.freshchat)
        elif tenant.platform == Platform.ZENDESK:
            return self._create_zendesk_client(tenant.zendesk)

        logger.error("Unknown platform", platform=tenant.platform)
        return None

    def _create_freshchat_client(self, config: Optional[FreshchatConfig]) -> Optional[FreshchatClient]:
        """Freshchat 클라이언트 생성"""
        if not config or not config.api_key:
            logger.error("Freshchat config missing")
            return None

        return FreshchatClient(
            api_key=config.api_key,
            api_url=config.api_url,
            inbox_id=config.inbox_id,
        )

    def _create_zendesk_client(self, config: Optional[ZendeskConfig]) -> Optional[ZendeskClient]:
        """Zendesk 클라이언트 생성"""
        if not config or not config.subdomain:
            logger.error("Zendesk config missing")
            return None

        return ZendeskClient(
            subdomain=config.subdomain,
            email=config.email,
            api_token=config.api_token,
            oauth_token=config.oauth_token,
        )

    def _create_webhook_handler(self, tenant: TenantConfig) -> Optional[Any]:
        """웹훅 핸들러 생성"""
        if tenant.platform == Platform.FRESHCHAT:
            if tenant.freshchat and tenant.freshchat.webhook_public_key:
                public_key = tenant.freshchat.webhook_public_key
                logger.debug(
                    "Creating Freshchat webhook handler",
                    tenant_id=tenant.teams_tenant_id,
                    key_len=len(public_key) if public_key else 0,
                    key_preview=public_key[:50] + "..." if public_key and len(public_key) > 50 else public_key,
                )
                return FreshchatWebhookHandler(
                    public_key=public_key,
                )
        elif tenant.platform == Platform.ZENDESK:
            # Zendesk 웹훅은 HMAC-SHA256 시크릿 사용 (선택)
            return ZendeskWebhookHandler()

        return None

    def _is_cache_expired(self, cached: CachedClient) -> bool:
        """캐시 만료 확인"""
        return time.time() - cached.cached_at > CLIENT_CACHE_TTL

    def invalidate_cache(self, tenant_id: str) -> None:
        """특정 테넌트 캐시 무효화"""
        self._cache.pop(tenant_id, None)

    def clear_cache(self) -> None:
        """전체 캐시 클리어"""
        self._cache.clear()


# ===== 싱글톤 인스턴스 =====

_factory_instance: Optional[PlatformFactory] = None


def get_platform_factory() -> PlatformFactory:
    """PlatformFactory 싱글톤 인스턴스 반환"""
    global _factory_instance
    if _factory_instance is None:
        _factory_instance = PlatformFactory()
    return _factory_instance
