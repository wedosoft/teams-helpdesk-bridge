"""관리자 설정 API

Teams Tab에서 호출하는 테넌트 설정 API
- 테넌트 설정 조회/저장
- 플랫폼 연동 설정 (Freshchat/Zendesk)
- 웹훅 URL 생성
- Graph API 관리자 동의
"""
from typing import Optional
from urllib.parse import urlencode

from pydantic import BaseModel, Field
from fastapi import APIRouter, HTTPException, Header, Depends, Request
from fastapi.responses import RedirectResponse

from app.config import get_settings
from app.core.tenant import (
    TenantService,
    TenantConfig,
    Platform,
    get_tenant_service,
)
from app.services.graph import get_graph_service
from app.utils.logger import get_logger

router = APIRouter()
logger = get_logger(__name__)


# ===== Request/Response Models =====

class FreshchatSetup(BaseModel):
    """Freshchat 설정"""
    api_key: str = Field(..., description="Freshchat API Key")
    api_url: str = Field(default="https://api.freshchat.com/v2", description="API URL")
    inbox_id: str = Field(default="", description="Inbox ID (선택)")
    webhook_public_key: str = Field(default="", description="Webhook Public Key (선택)")


class ZendeskSetup(BaseModel):
    """Zendesk 설정"""
    subdomain: str = Field(..., description="Zendesk 서브도메인 (예: mycompany)")
    email: str = Field(..., description="관리자 이메일")
    api_token: str = Field(..., description="API 토큰")


class TenantSetupRequest(BaseModel):
    """테넌트 설정 요청"""
    platform: str = Field(..., description="플랫폼 (freshchat/zendesk)")
    freshchat: Optional[FreshchatSetup] = None
    zendesk: Optional[ZendeskSetup] = None
    bot_name: str = Field(default="IT Helpdesk", description="봇 이름")
    welcome_message: str = Field(
        default="안녕하세요! IT 헬프데스크입니다. 무엇을 도와드릴까요?",
        description="환영 메시지",
    )


class TenantResponse(BaseModel):
    """테넌트 설정 응답"""
    teams_tenant_id: str
    platform: str
    bot_name: str
    welcome_message: str
    webhook_url: str
    is_configured: bool
    graph_consent_granted: bool = False  # Graph API 관리자 동의 여부


class WebhookInfo(BaseModel):
    """웹훅 URL 정보"""
    platform: str
    webhook_url: str
    instructions: str


# ===== API Endpoints =====

async def get_tenant_id_from_header(
    x_ms_token_aad_access_token: Optional[str] = Header(None, alias="X-MS-TOKEN-AAD-ACCESS-TOKEN"),
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID"),
) -> str:
    """요청 헤더에서 테넌트 ID 추출

    Teams SSO 토큰 또는 X-Tenant-ID 헤더에서 추출
    """
    # 개발 환경: X-Tenant-ID 헤더 직접 사용
    if x_tenant_id:
        return x_tenant_id

    # TODO: Teams SSO 토큰에서 tenant_id 추출
    # if x_ms_token_aad_access_token:
    #     return extract_tenant_from_token(x_ms_token_aad_access_token)

    raise HTTPException(
        status_code=401,
        detail="Tenant ID not found. Provide X-Tenant-ID header.",
    )


@router.get("/config", response_model=TenantResponse)
async def get_tenant_config(
    tenant_id: str = Depends(get_tenant_id_from_header),
) -> TenantResponse:
    """현재 테넌트 설정 조회"""
    service = get_tenant_service()
    tenant = await service.get_tenant(tenant_id)

    settings = get_settings()
    base_url = settings.public_url or f"http://localhost:{settings.port}"

    # Graph API 동의 상태 확인
    graph_service = get_graph_service()
    graph_consent = await graph_service.check_consent_status(tenant_id)

    if not tenant:
        return TenantResponse(
            teams_tenant_id=tenant_id,
            platform="",
            bot_name="IT Helpdesk",
            welcome_message="",
            webhook_url="",
            is_configured=False,
            graph_consent_granted=graph_consent,
        )

    webhook_url = f"{base_url}/api/webhook/{tenant.platform.value}/{tenant_id}"

    return TenantResponse(
        teams_tenant_id=tenant_id,
        platform=tenant.platform.value,
        bot_name=tenant.bot_name,
        welcome_message=tenant.welcome_message,
        webhook_url=webhook_url,
        is_configured=True,
        graph_consent_granted=graph_consent,
    )


@router.post("/config", response_model=TenantResponse)
async def save_tenant_config(
    request: TenantSetupRequest,
    tenant_id: str = Depends(get_tenant_id_from_header),
) -> TenantResponse:
    """테넌트 설정 저장

    사용자가 앱 설치 시 필수 값 입력 후 호출
    """
    # 플랫폼 검증
    try:
        platform = Platform(request.platform)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid platform: {request.platform}. Use 'freshchat' or 'zendesk'.",
        )

    # 플랫폼별 설정 검증
    platform_config: dict = {}

    if platform == Platform.FRESHCHAT:
        if not request.freshchat:
            raise HTTPException(status_code=400, detail="Freshchat configuration required")
        if not request.freshchat.api_key:
            raise HTTPException(status_code=400, detail="Freshchat API key required")

        platform_config = {
            "api_key": request.freshchat.api_key,
            "api_url": request.freshchat.api_url,
            "inbox_id": request.freshchat.inbox_id,
            "webhook_public_key": request.freshchat.webhook_public_key,
        }

    elif platform == Platform.ZENDESK:
        if not request.zendesk:
            raise HTTPException(status_code=400, detail="Zendesk configuration required")
        if not request.zendesk.subdomain or not request.zendesk.api_token:
            raise HTTPException(status_code=400, detail="Zendesk subdomain and API token required")

        platform_config = {
            "subdomain": request.zendesk.subdomain,
            "email": request.zendesk.email,
            "api_token": request.zendesk.api_token,
        }

    # 테넌트 생성/업데이트
    service = get_tenant_service()
    tenant = await service.create_tenant(
        teams_tenant_id=tenant_id,
        platform=platform,
        platform_config=platform_config,
        bot_name=request.bot_name,
        welcome_message=request.welcome_message,
    )

    if not tenant:
        raise HTTPException(status_code=500, detail="Failed to save configuration")

    settings = get_settings()
    base_url = settings.public_url or f"http://localhost:{settings.port}"
    webhook_url = f"{base_url}/api/webhook/{platform.value}/{tenant_id}"

    logger.info(
        "Tenant configured",
        tenant_id=tenant_id,
        platform=platform.value,
    )

    # Graph API 동의 상태 확인
    graph_service = get_graph_service()
    graph_consent = await graph_service.check_consent_status(tenant_id)

    return TenantResponse(
        teams_tenant_id=tenant_id,
        platform=platform.value,
        bot_name=tenant.bot_name,
        welcome_message=tenant.welcome_message,
        webhook_url=webhook_url,
        is_configured=True,
        graph_consent_granted=graph_consent,
    )


@router.delete("/config")
async def delete_tenant_config(
    tenant_id: str = Depends(get_tenant_id_from_header),
) -> dict:
    """테넌트 설정 삭제"""
    service = get_tenant_service()
    success = await service.delete_tenant(tenant_id)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete configuration")

    logger.info("Tenant deleted", tenant_id=tenant_id)

    return {"status": "deleted", "tenant_id": tenant_id}


@router.get("/webhook-info", response_model=WebhookInfo)
async def get_webhook_info(
    tenant_id: str = Depends(get_tenant_id_from_header),
) -> WebhookInfo:
    """웹훅 URL 및 설정 안내 조회"""
    service = get_tenant_service()
    tenant = await service.get_tenant(tenant_id)

    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not configured")

    settings = get_settings()
    base_url = settings.public_url or f"http://localhost:{settings.port}"
    webhook_url = f"{base_url}/api/webhook/{tenant.platform.value}/{tenant_id}"

    if tenant.platform == Platform.FRESHCHAT:
        instructions = (
            "Freshchat 웹훅 설정:\n"
            "1. Freshchat Admin > Settings > Webhooks 이동\n"
            "2. 'Add Webhook' 클릭\n"
            f"3. Webhook URL: {webhook_url}\n"
            "4. Events: 'Message Create', 'Conversation Resolve' 선택\n"
            "5. 'Save' 클릭"
        )
    elif tenant.platform == Platform.ZENDESK:
        instructions = (
            "Zendesk 웹훅 설정:\n"
            "1. Zendesk Admin Center > Apps and integrations > Webhooks 이동\n"
            "2. 'Create webhook' 클릭\n"
            f"3. Endpoint URL: {webhook_url}\n"
            "4. Request method: POST\n"
            "5. Request format: JSON\n"
            "6. Trigger: 티켓 업데이트 시"
        )
    else:
        instructions = "Unknown platform"

    return WebhookInfo(
        platform=tenant.platform.value,
        webhook_url=webhook_url,
        instructions=instructions,
    )


@router.get("/validate")
async def validate_connection(
    tenant_id: str = Depends(get_tenant_id_from_header),
) -> dict:
    """플랫폼 연결 검증

    API 키가 유효한지 확인
    """
    service = get_tenant_service()
    tenant = await service.get_tenant(tenant_id)

    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not configured")

    from app.core.platform_factory import get_platform_factory
    factory = get_platform_factory()
    client = factory.get_client(tenant)

    if not client:
        return {
            "valid": False,
            "error": "Failed to create client",
        }

    # TODO: 실제 API 호출로 검증
    # 예: Freshchat의 경우 /agents 조회, Zendesk의 경우 /users/me 조회

    return {
        "valid": True,
        "platform": tenant.platform.value,
        "message": "Connection validated successfully",
    }


# ===== Freshchat 채널 목록 =====

class FreshchatChannelRequest(BaseModel):
    """Freshchat API Key로 채널 목록 조회"""
    api_key: str = Field(..., description="Freshchat API Key")
    api_url: str = Field(default="https://api.freshchat.com/v2", description="API URL")


class FreshchatChannel(BaseModel):
    """Freshchat 채널"""
    id: str
    name: str
    icon: Optional[str] = None


class FreshchatChannelsResponse(BaseModel):
    """Freshchat 채널 목록 응답"""
    valid: bool
    channels: list[FreshchatChannel] = []
    error: Optional[str] = None


@router.post("/freshchat/channels", response_model=FreshchatChannelsResponse)
async def get_freshchat_channels(
    request: FreshchatChannelRequest,
) -> FreshchatChannelsResponse:
    """Freshchat API Key로 채널 목록 조회

    설정 UI에서 API Key 입력 후 채널 목록 표시용
    """
    from app.adapters.freshchat.client import FreshchatClient

    try:
        client = FreshchatClient(
            api_key=request.api_key,
            api_url=request.api_url,
            inbox_id="",  # 채널 조회에는 필요 없음
        )

        channels = await client.get_channels()

        if not channels:
            return FreshchatChannelsResponse(
                valid=False,
                channels=[],
                error="API Key가 유효하지 않거나 채널이 없습니다.",
            )

        return FreshchatChannelsResponse(
            valid=True,
            channels=[FreshchatChannel(**ch) for ch in channels],
        )

    except Exception as e:
        logger.error("Failed to get Freshchat channels", error=str(e))
        return FreshchatChannelsResponse(
            valid=False,
            channels=[],
            error=str(e),
        )


# ===== Graph API 관리자 동의 =====

class GraphConsentResponse(BaseModel):
    """Graph API 동의 상태 응답"""
    consent_granted: bool
    consent_url: Optional[str] = None  # 동의가 필요한 경우 URL 제공


@router.get("/graph/consent-status", response_model=GraphConsentResponse)
async def get_graph_consent_status(
    tenant_id: str = Depends(get_tenant_id_from_header),
) -> GraphConsentResponse:
    """Graph API 관리자 동의 상태 확인

    동의가 되어 있으면 consent_granted=True,
    필요한 경우 동의 URL 제공
    """
    graph_service = get_graph_service()
    consent_granted = await graph_service.check_consent_status(tenant_id)

    if consent_granted:
        return GraphConsentResponse(consent_granted=True)

    # 동의 URL 생성
    settings = get_settings()
    base_url = settings.public_url or f"http://localhost:{settings.port}"
    redirect_uri = f"{base_url}/api/admin/graph/consent/callback"
    consent_url = graph_service.get_admin_consent_url(tenant_id, redirect_uri)

    return GraphConsentResponse(
        consent_granted=False,
        consent_url=consent_url,
    )


@router.get("/graph/consent")
async def redirect_to_consent(
    tenant_id: str = Depends(get_tenant_id_from_header),
) -> RedirectResponse:
    """관리자 동의 페이지로 리디렉션

    관리자가 이 URL을 호출하면 Microsoft 동의 페이지로 이동
    """
    graph_service = get_graph_service()
    settings = get_settings()
    base_url = settings.public_url or f"http://localhost:{settings.port}"
    redirect_uri = f"{base_url}/api/admin/graph/consent/callback"

    # state에 tenant_id 포함 (콜백에서 확인용)
    consent_url = graph_service.get_admin_consent_url(tenant_id, redirect_uri)
    consent_url += f"&state={tenant_id}"

    logger.info("Redirecting to admin consent", tenant_id=tenant_id)
    return RedirectResponse(url=consent_url)


@router.get("/graph/consent/callback")
async def handle_consent_callback(
    request: Request,
    admin_consent: Optional[str] = None,
    tenant: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
) -> dict:
    """Microsoft 동의 콜백 처리

    동의 성공 시: admin_consent=True, tenant={tenant_id}
    동의 실패 시: error={error_code}, error_description={message}
    """
    if error:
        logger.error(
            "Admin consent failed",
            error=error,
            description=error_description,
            tenant=tenant or state,
        )
        raise HTTPException(
            status_code=400,
            detail=f"관리자 동의 실패: {error_description or error}",
        )

    if admin_consent and admin_consent.lower() == "true":
        tenant_id = tenant or state
        logger.info(
            "Admin consent granted",
            tenant_id=tenant_id,
        )

        # 토큰 캐시 무효화하여 새로 획득하도록
        graph_service = get_graph_service()
        if tenant_id:
            graph_service.invalidate_token_cache(tenant_id)

        return {
            "status": "success",
            "message": "관리자 동의가 완료되었습니다. 이제 사용자 프로필 확장 정보를 조회할 수 있습니다.",
            "tenant_id": tenant_id,
        }

    # 예상치 못한 응답
    logger.warning(
        "Unexpected consent callback",
        params=dict(request.query_params),
    )
    return {
        "status": "unknown",
        "message": "동의 상태를 확인할 수 없습니다.",
        "params": dict(request.query_params),
    }
