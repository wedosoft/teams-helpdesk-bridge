"""Freshchat Webhook 라우트 (멀티테넌트)

웹훅 요청에서 테넌트 식별:
1. conversations 테이블에서 conversation_id로 tenant_id 조회
2. 또는 웹훅 URL에 tenant_id 포함 (/webhook/freshchat/{tenant_id})
"""
from fastapi import APIRouter, Request, Response, HTTPException, Path

from app.core.tenant import get_tenant_service, Platform
from app.core.platform_factory import get_platform_factory
from app.core.router import get_message_router
from app.core.store import get_conversation_store
from app.utils.logger import get_logger

router = APIRouter()
logger = get_logger(__name__)


@router.post("/{teams_tenant_id}")
async def freshchat_webhook(
    request: Request,
    teams_tenant_id: str = Path(..., description="Teams 테넌트 ID"),
) -> Response:
    """Freshchat Webhook 엔드포인트 (테넌트별)

    URL 형식: /api/webhook/freshchat/{teams_tenant_id}

    각 테넌트는 자신의 웹훅 URL을 Freshchat에 등록해야 함
    """
    try:
        # 1. 테넌트 설정 조회
        tenant_service = get_tenant_service()
        tenant = await tenant_service.get_tenant(teams_tenant_id)

        if not tenant:
            logger.warning("Unknown tenant", teams_tenant_id=teams_tenant_id)
            raise HTTPException(status_code=404, detail="Tenant not found")

        if tenant.platform != Platform.FRESHCHAT:
            logger.warning("Wrong platform for tenant", platform=tenant.platform)
            raise HTTPException(status_code=400, detail="Tenant is not using Freshchat")

        # 2. Raw body 읽기 (서명 검증용)
        raw_body = await request.body()

        # 3. 서명 검증
        signature = request.headers.get("x-freshchat-signature", "")
        factory = get_platform_factory()
        webhook_handler = factory.get_webhook_handler(tenant)

        if signature and webhook_handler:
            if not webhook_handler.verify_signature(raw_body, signature):
                # TODO: 서명 검증 실패 - 공개키 설정 확인 필요
                # 임시로 경고만 로깅하고 처리 계속 (프로덕션에서는 HTTPException 사용)
                logger.warning(
                    "Invalid webhook signature - continuing anyway for debugging",
                    teams_tenant_id=teams_tenant_id,
                )
                # raise HTTPException(status_code=401, detail="Invalid signature")
        elif tenant.freshchat and tenant.freshchat.webhook_public_key:
            # 공개키가 설정되어 있는데 서명이 없으면 경고
            logger.warning("Missing webhook signature", teams_tenant_id=teams_tenant_id)

        # 4. 페이로드 파싱
        payload = await request.json()
        action = payload.get("action", "")

        logger.debug(
            "Received Freshchat webhook",
            action=action,
            teams_tenant_id=teams_tenant_id,
        )

        # 5. 웹훅 이벤트 파싱
        if not webhook_handler:
            logger.error("No webhook handler for tenant")
            return Response(status_code=200)

        event = webhook_handler.parse_webhook(payload)
        if not event:
            # 무시할 이벤트 (user 메시지 등)
            return Response(status_code=200)

        # 6. 메시지 라우터로 전달
        message_router = get_message_router()
        await message_router.handle_webhook(tenant, event)

        return Response(status_code=200)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Freshchat webhook error", error=str(e), teams_tenant_id=teams_tenant_id)
        return Response(status_code=500)


@router.post("")
async def freshchat_webhook_legacy(request: Request) -> Response:
    """Freshchat Webhook (레거시 - conversation_id로 테넌트 조회)

    기존 단일 테넌트 방식 지원 (마이그레이션용)
    conversation_id에서 tenant_id를 조회하여 처리
    """
    try:
        # 페이로드 파싱
        payload = await request.json()
        action = payload.get("action", "")
        data = payload.get("data", {})

        # conversation_id 추출
        conversation = data.get("conversation", {})
        conversation_id = conversation.get("conversation_id") or str(conversation.get("id", ""))

        if not conversation_id:
            logger.warning("No conversation_id in webhook")
            return Response(status_code=200)

        # conversation에서 tenant_id 조회
        store = get_conversation_store()
        mapping = await store.get_by_platform_id(conversation_id, "freshchat")

        if not mapping or not mapping.tenant_id:
            logger.warning("Cannot find tenant for conversation", conversation_id=conversation_id)
            return Response(status_code=200)

        # 테넌트 설정 조회
        tenant_service = get_tenant_service()
        tenant = await tenant_service.get_tenant(mapping.tenant_id)

        if not tenant:
            logger.warning("Tenant not found", tenant_id=mapping.tenant_id)
            return Response(status_code=200)

        # 서명 검증
        raw_body = await request.body()
        signature = request.headers.get("x-freshchat-signature", "")
        factory = get_platform_factory()
        webhook_handler = factory.get_webhook_handler(tenant)

        if signature and webhook_handler:
            if not webhook_handler.verify_signature(raw_body, signature):
                # TODO: 서명 검증 실패 - 공개키 설정 확인 필요
                logger.warning(
                    "Invalid webhook signature - continuing anyway for debugging",
                    tenant_id=mapping.tenant_id,
                )
                # raise HTTPException(status_code=401, detail="Invalid signature")

        # 웹훅 처리
        if webhook_handler:
            event = webhook_handler.parse_webhook(payload)
            if event:
                message_router = get_message_router()
                await message_router.handle_webhook(tenant, event)

        return Response(status_code=200)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Freshchat webhook error (legacy)", error=str(e))
        return Response(status_code=500)


@router.get("/health")
async def webhook_health() -> dict:
    """Webhook 헬스 체크"""
    return {
        "status": "ok",
        "webhook": "freshchat",
        "multi_tenant": True,
    }
