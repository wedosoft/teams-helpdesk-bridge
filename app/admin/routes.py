"""관리자 설정 API

Teams Tab에서 호출하는 테넌트 설정 API
- 테넌트 설정 조회/저장
- 플랫폼 연동 설정 (Freshchat/Zendesk/Freshdesk)
- 웹훅 URL 생성
- Graph API 관리자 동의
"""
from typing import Optional, Union
from urllib.parse import urlencode

from pydantic import BaseModel, Field
from fastapi import APIRouter, HTTPException, Header, Depends, Request
from fastapi.responses import RedirectResponse, HTMLResponse

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


class FreshdeskSetup(BaseModel):
    """Freshdesk 설정 (Freshdesk Omni 포함)"""
    base_url: str = Field(..., description="Freshdesk Base URL (예: https://yourdomain.freshdesk.com)")
    api_key: str = Field(..., description="Freshdesk API Key")


class TenantSetupRequest(BaseModel):
    """테넌트 설정 요청"""
    platform: str = Field(..., description="플랫폼 (freshchat/zendesk/freshdesk)")
    freshchat: Optional[FreshchatSetup] = None
    zendesk: Optional[ZendeskSetup] = None
    freshdesk: Optional[FreshdeskSetup] = None
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
    request: Request,
    x_ms_token_aad_access_token: Optional[str] = Header(None, alias="X-MS-TOKEN-AAD-ACCESS-TOKEN"),
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID"),
) -> str:
    """요청 헤더 또는 쿠키에서 테넌트 ID 추출

    우선순위:
    1. admin_session 쿠키 (관리자 포털)
    2. X-Tenant-ID 헤더 (API 호출)
    3. Teams SSO 토큰 (Teams 탭)
    """
    # 1. 쿠키 세션 확인
    session_id = request.cookies.get("admin_session")
    if session_id:
        from app.admin.oauth import admin_sessions
        from datetime import datetime
        
        session = admin_sessions.get(session_id)
        if session and session["expires_at"] > datetime.utcnow():
            return session["tenant_id"]

    # 2. 개발 환경/API: X-Tenant-ID 헤더 직접 사용
    if x_tenant_id:
        return x_tenant_id

    # TODO: Teams SSO 토큰에서 tenant_id 추출
    # if x_ms_token_aad_access_token:
    #     return extract_tenant_from_token(x_ms_token_aad_access_token)

    raise HTTPException(
        status_code=401,
        detail="Tenant ID not found. Please login or provide X-Tenant-ID header.",
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


@router.get("/tenant-info", response_class=HTMLResponse)
async def get_tenant_info(
    tenant_id: str = Depends(get_tenant_id_from_header),
):
    """HTMX: 테넌트 정보 HTML 반환"""
    service = get_tenant_service()
    tenant = await service.get_tenant(tenant_id)
    
    status_badge = '<span style="background:#dff6dd;color:#1e4620;padding:2px 8px;border-radius:12px;font-size:12px;">Configured</span>' if tenant else '<span style="background:#fde8e8;color:#9b1c1c;padding:2px 8px;border-radius:12px;font-size:12px;">Not Configured</span>'
    
    platform_info = f"<p><strong>Platform:</strong> {tenant.platform.value}</p>" if tenant else ""
    
    return f"""
        <div class="card">
            <h2>Tenant Information {status_badge}</h2>
            <p><strong>Tenant ID:</strong> {tenant_id}</p>
            {platform_info}
        </div>
    """


@router.get("/platform-fields", response_class=HTMLResponse)
async def get_platform_fields(platform: str):
    """HTMX: 플랫폼별 입력 필드 반환"""
    if platform == "freshchat":
        return """
            <div class="form-group">
                <label>API URL</label>
                <input type="text" name="freshchat_api_url" value="https://api.freshchat.com/v2" required>
            </div>
            <div class="form-group">
                <label>API Key</label>
                <input type="password" name="freshchat_api_key" required>
            </div>
            <div class="form-group">
                <label>Inbox ID (Optional)</label>
                <input type="text" name="freshchat_inbox_id">
            </div>
            <div class="form-group">
                <label>Webhook Public Key (Optional)</label>
                <input type="text" name="freshchat_webhook_public_key">
            </div>
        """
    elif platform == "zendesk":
        return """
            <div class="form-group">
                <label>Subdomain</label>
                <input type="text" name="zendesk_subdomain" placeholder="mycompany" required>
            </div>
            <div class="form-group">
                <label>Admin Email</label>
                <input type="email" name="zendesk_email" required>
            </div>
            <div class="form-group">
                <label>API Token</label>
                <input type="password" name="zendesk_api_token" required>
            </div>
        """
    elif platform == "freshdesk":
        return """
            <div class="form-group">
                <label>Base URL</label>
                <input type="text" name="freshdesk_base_url" placeholder="https://domain.freshdesk.com" required>
            </div>
            <div class="form-group">
                <label>API Key</label>
                <input type="password" name="freshdesk_api_key" required>
            </div>
        """
    return ""


@router.post("/config", response_model=Union[TenantResponse, str])
async def save_tenant_config(
    request: Request,
    tenant_id: str = Depends(get_tenant_id_from_header),
) -> Union[TenantResponse, HTMLResponse]:
    """테넌트 설정 저장

    사용자가 앱 설치 시 필수 값 입력 후 호출
    Supports both JSON (API) and Form Data (HTMX)
    """
    # 1. Parse Request Data
    content_type = request.headers.get("content-type", "")
    setup_request: TenantSetupRequest = None

    if "application/json" in content_type:
        try:
            data = await request.json()
            setup_request = TenantSetupRequest(**data)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")
    elif "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        try:
            form_data = await request.form()
            data = dict(form_data)
            
            # Construct nested objects
            platform_val = data.get("platform")
            
            # Clean data for Pydantic
            clean_data = {
                "platform": platform_val,
                "bot_name": data.get("bot_name"),
                "welcome_message": data.get("welcome_message"),
            }

            if platform_val == "freshchat":
                clean_data["freshchat"] = {
                    "api_key": data.get("freshchat_api_key"),
                    "api_url": data.get("freshchat_api_url"),
                    "inbox_id": data.get("freshchat_inbox_id"),
                    "webhook_public_key": data.get("freshchat_webhook_public_key"),
                }
            elif platform_val == "zendesk":
                clean_data["zendesk"] = {
                    "subdomain": data.get("zendesk_subdomain"),
                    "email": data.get("zendesk_email"),
                    "api_token": data.get("zendesk_api_token"),
                }
            elif platform_val == "freshdesk":
                # Only add if fields are present to avoid validation error on None
                base_url = data.get("freshdesk_base_url")
                api_key = data.get("freshdesk_api_key")
                if base_url and api_key:
                    clean_data["freshdesk"] = {
                        "base_url": base_url,
                        "api_key": api_key,
                    }
            
            setup_request = TenantSetupRequest(**clean_data)
        except Exception as e:
            logger.error(f"Form Data Parsing Error: {str(e)}", exc_info=True)
            raise HTTPException(status_code=400, detail=f"Invalid Form Data: {str(e)}")
    else:
        raise HTTPException(status_code=400, detail="Unsupported Content-Type")

    # 2. Validate Platform
    try:
        platform = Platform(setup_request.platform)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid platform: {setup_request.platform}. Use 'freshchat', 'zendesk', or 'freshdesk'.",
        )

    # 3. Validate Platform Config
    platform_config: dict = {}

    if platform == Platform.FRESHCHAT:
        if not setup_request.freshchat:
            raise HTTPException(status_code=400, detail="Freshchat configuration required")
        if not setup_request.freshchat.api_key:
            raise HTTPException(status_code=400, detail="Freshchat API key required")

        platform_config = {
            "api_key": setup_request.freshchat.api_key,
            "api_url": setup_request.freshchat.api_url,
            "inbox_id": setup_request.freshchat.inbox_id,
            "webhook_public_key": setup_request.freshchat.webhook_public_key,
        }

    elif platform == Platform.ZENDESK:
        if not setup_request.zendesk:
            raise HTTPException(status_code=400, detail="Zendesk configuration required")
        if not setup_request.zendesk.subdomain or not setup_request.zendesk.api_token:
            raise HTTPException(status_code=400, detail="Zendesk subdomain and API token required")

        platform_config = {
            "subdomain": setup_request.zendesk.subdomain,
            "email": setup_request.zendesk.email,
            "api_token": setup_request.zendesk.api_token,
        }
    elif platform == Platform.FRESHDESK:
        if not setup_request.freshdesk:
            raise HTTPException(status_code=400, detail="Freshdesk configuration required")
        if not setup_request.freshdesk.base_url or not setup_request.freshdesk.api_key:
            raise HTTPException(status_code=400, detail="Freshdesk base_url and API key required")

        platform_config = {
            "base_url": setup_request.freshdesk.base_url,
            "api_key": setup_request.freshdesk.api_key,
        }

    # 4. Save Tenant
    service = get_tenant_service()
    tenant = await service.create_tenant(
        teams_tenant_id=tenant_id,
        platform=platform,
        platform_config=platform_config,
        bot_name=setup_request.bot_name,
        welcome_message=setup_request.welcome_message,
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

    # 5. Check Graph Consent
    graph_service = get_graph_service()
    graph_consent = await graph_service.check_consent_status(tenant_id)

    response_data = TenantResponse(
        teams_tenant_id=tenant_id,
        platform=platform.value,
        bot_name=tenant.bot_name,
        welcome_message=tenant.welcome_message,
        webhook_url=webhook_url,
        is_configured=True,
        graph_consent_granted=graph_consent,
    )

    # 6. Return Response (HTML for HTMX, JSON for API)
    if request.headers.get("HX-Request"):
        return HTMLResponse(content=f"""
            <div class="alert alert-success">
                <h3>Configuration Saved!</h3>
                <p><strong>Webhook URL:</strong> {webhook_url}</p>
                <p>Please register this URL in your {platform.value} settings.</p>
            </div>
        """)
    
    return response_data


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
        raise HTTPException(
            status_code=404,
            detail="Tenant not configured. Open /admin/setup (Teams tab) or POST /api/admin/config with X-Tenant-ID to create tenant settings.",
        )

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
    elif tenant.platform == Platform.FRESHDESK:
        instructions = (
            "Freshdesk 웹훅 설정(POC 권장):\n"
            "1. Freshdesk Admin > Workflows/Automation에서 티켓 업데이트 트리거 선택\n"
            "2. Action: Trigger Webhook (POST)\n"
            f"3. Webhook URL: {webhook_url}\n"
            "4. Payload에 최소한 ticket_id, text(또는 body), status를 포함하도록 설정\n"
            "5. Save"
        )
    else:
        instructions = "Unknown platform"

    return WebhookInfo(
        platform=tenant.platform.value,
        webhook_url=webhook_url,
        instructions=instructions,
    )


@router.get("/app-info")
async def get_app_info() -> dict:
    """프론트(정적 HTML)에서 사용할 기본 앱 정보

    - Bot App ID는 민감정보가 아니므로 노출 가능
    - Admin UI에서 Graph admin consent URL 생성 등에 사용
    """
    settings = get_settings()
    return {
        "bot_app_id": settings.bot_app_id,
        "public_url": settings.public_url,
    }


@router.get("/validate")
async def validate_connection(
    tenant_id: str = Depends(get_tenant_id_from_header),
) -> dict:
    """플랫폼 연결 검증

    API 키가 유효한지 확인
    """
    service = get_tenant_service()
    try:
        tenant = await service.get_tenant(tenant_id)
    except RuntimeError as e:
        # Admin UI/PoC에서는 5xx 대신 결과 JSON으로 돌려주는 편이 디버깅이 쉽다.
        return {"valid": False, "error": str(e)}

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

    # 실제 API 호출로 검증 (플랫폼별 validate_api_key 제공)
    validate_detail_fn = getattr(client, "validate_api_key_detail", None)
    if callable(validate_detail_fn):
        detail = await validate_detail_fn()
        if detail.get("valid"):
            return {"valid": True, "platform": tenant.platform.value, "message": "Connection validated successfully"}
        return {
            "valid": False,
            "platform": tenant.platform.value,
            "status": detail.get("status"),
            "error": detail.get("error") or "Invalid credentials or cannot reach API",
        }

    validate_fn = getattr(client, "validate_api_key", None)
    if not callable(validate_fn):
        return {
            "valid": False,
            "platform": tenant.platform.value,
            "error": "Validation is not implemented for this platform client",
        }

    try:
        valid = await validate_fn()
    except Exception as e:
        return {
            "valid": False,
            "platform": tenant.platform.value,
            "error": f"Validation request failed: {e}",
        }

    if not valid:
        return {
            "valid": False,
            "platform": tenant.platform.value,
            "error": "Invalid credentials or cannot reach API",
        }

    return {
        "valid": True,
        "platform": tenant.platform.value,
        "message": "Connection validated successfully",
    }


@router.get("/freshdesk/dashboard")
async def freshdesk_dashboard(
    tenant_id: str = Depends(get_tenant_id_from_header),
    per_page: int = 100,
) -> dict:
    """Freshdesk 티켓 간단 집계(POC용)

    - 실원별 진행/완료 건수
    - 가중치 합(옵션: weight_field_key 설정 시)
    """
    service = get_tenant_service()
    tenant = await service.get_tenant(tenant_id)

    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not configured")

    if tenant.platform != Platform.FRESHDESK:
        raise HTTPException(status_code=400, detail="Tenant is not using Freshdesk")

    from app.core.platform_factory import get_platform_factory

    factory = get_platform_factory()
    client = factory.get_client(tenant)

    if not client:
        raise HTTPException(status_code=500, detail="Failed to create Freshdesk client")

    list_tickets_fn = getattr(client, "list_tickets", None)
    if not callable(list_tickets_fn):
        raise HTTPException(status_code=500, detail="Freshdesk client does not support list_tickets")

    tickets = await list_tickets_fn(per_page=per_page)

    summary = {
        "total": {"all": 0, "open": 0, "done": 0},
        "by_responder": {},
    }

    for t in tickets:
        summary["total"]["all"] += 1

        status_value = t.get("status")
        done = is_done(status_value)
        if done:
            summary["total"]["done"] += 1
        else:
            summary["total"]["open"] += 1

        responder_id = t.get("responder_id") or "unassigned"
        bucket = summary["by_responder"].setdefault(
            str(responder_id),
            {"responder_id": responder_id, "open": 0, "done": 0},
        )

        if done:
            bucket["done"] += 1
        else:
            bucket["open"] += 1

    # responder 이름 보강
    get_agent_name_fn = getattr(client, "get_agent_name", None)
    if callable(get_agent_name_fn):
        for key, bucket in summary["by_responder"].items():
            if key == "unassigned":
                bucket["responder_name"] = "Unassigned"
                continue
            try:
                bucket["responder_name"] = await get_agent_name_fn(str(bucket["responder_id"]))
            except Exception:
                bucket["responder_name"] = None

    summary["by_responder"] = list(summary["by_responder"].values())
    return summary


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


@router.get("/graph/consent/callback", response_class=HTMLResponse)
async def handle_consent_callback(
    request: Request,
    admin_consent: Optional[str] = None,
    tenant: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
) -> HTMLResponse:
    """Microsoft 동의 콜백 처리

    동의 성공 시: admin_consent=True, tenant={tenant_id}
    동의 실패 시: error={error_code}, error_description={message}

    팝업 창에서 실행되므로 HTML로 결과를 표시하고 자동으로 창을 닫음
    """
    if error:
        logger.error(
            "Admin consent failed",
            error=error,
            description=error_description,
            tenant=tenant or state,
        )
        return HTMLResponse(content=f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>권한 승인 실패</title>
            <style>
                body {{ font-family: 'Segoe UI', sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background: #f5f5f5; }}
                .container {{ text-align: center; background: white; padding: 40px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); max-width: 400px; }}
                .icon {{ font-size: 48px; margin-bottom: 16px; }}
                h2 {{ color: #c00; margin-bottom: 12px; }}
                p {{ color: #666; margin-bottom: 20px; }}
                button {{ background: #5558AF; color: white; border: none; padding: 10px 24px; border-radius: 4px; cursor: pointer; font-size: 14px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="icon">❌</div>
                <h2>권한 승인 실패</h2>
                <p>{error_description or error}</p>
                <button onclick="window.close()">닫기</button>
            </div>
        </body>
        </html>
        """)

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

        return HTMLResponse(content="""
        <!DOCTYPE html>
        <html>
        <head>
            <title>권한 승인 완료</title>
            <style>
                body { font-family: 'Segoe UI', sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background: #f5f5f5; }
                .container { text-align: center; background: white; padding: 40px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); max-width: 400px; }
                .icon { font-size: 48px; margin-bottom: 16px; }
                h2 { color: #2e7d32; margin-bottom: 12px; }
                p { color: #666; margin-bottom: 20px; }
                .closing { color: #888; font-size: 13px; }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="icon">✅</div>
                <h2>권한 승인 완료!</h2>
                <p>Microsoft Graph API 권한이 승인되었습니다.<br>이제 사용자 프로필 정보를 조회할 수 있습니다.</p>
                <p class="closing">잠시 후 창이 자동으로 닫힙니다...</p>
            </div>
            <script>
                setTimeout(function() { window.close(); }, 2000);
            </script>
        </body>
        </html>
        """)

    # 예상치 못한 응답
    logger.warning(
        "Unexpected consent callback",
        params=dict(request.query_params),
    )
    return HTMLResponse(content="""
    <!DOCTYPE html>
    <html>
    <head>
        <title>알 수 없는 응답</title>
        <style>
            body { font-family: 'Segoe UI', sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background: #f5f5f5; }
            .container { text-align: center; background: white; padding: 40px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); max-width: 400px; }
            .icon { font-size: 48px; margin-bottom: 16px; }
            h2 { color: #f57c00; margin-bottom: 12px; }
            p { color: #666; margin-bottom: 20px; }
            button { background: #5558AF; color: white; border: none; padding: 10px 24px; border-radius: 4px; cursor: pointer; font-size: 14px; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="icon">⚠️</div>
            <h2>응답을 확인할 수 없음</h2>
            <p>동의 상태를 확인할 수 없습니다. 창을 닫고 다시 시도해주세요.</p>
            <button onclick="window.close()">닫기</button>
        </div>
    </body>
    </html>
    """)
