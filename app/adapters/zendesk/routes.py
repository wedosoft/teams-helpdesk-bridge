from __future__ import annotations

"""Zendesk Webhook 라우트 (멀티테넌트)"""
from fastapi import APIRouter, Request, Response, HTTPException, Path

from app.core.tenant import get_tenant_service, Platform
from app.core.platform_factory import get_platform_factory
from app.core.router import get_message_router
from app.adapters.zendesk.webhook import ZendeskWebhookHandler, ZendeskWebhookEvent
from app.adapters.freshchat.webhook import WebhookEvent, ParsedMessage, ParsedAttachment
from app.utils.logger import get_logger

router = APIRouter()
logger = get_logger(__name__)


# 웹훅 핸들러 캐시 (테넌트별)
_webhook_handlers: dict[str, ZendeskWebhookHandler] = {}


def get_webhook_handler(tenant_id: str, secret: str = "") -> ZendeskWebhookHandler:
    """테넌트별 웹훅 핸들러"""
    if tenant_id not in _webhook_handlers:
        _webhook_handlers[tenant_id] = ZendeskWebhookHandler(webhook_secret=secret)
    return _webhook_handlers[tenant_id]


@router.post("/{teams_tenant_id}")
async def zendesk_webhook(
    request: Request,
    teams_tenant_id: str = Path(..., description="Teams 테넌트 ID"),
) -> Response:
    """Zendesk Webhook 엔드포인트 (테넌트별)

    URL 형식: /api/webhook/zendesk/{teams_tenant_id}
    """
    try:
        # 1. 테넌트 설정 조회
        tenant_service = get_tenant_service()
        tenant = await tenant_service.get_tenant(teams_tenant_id)

        if not tenant:
            logger.warning("Unknown tenant", teams_tenant_id=teams_tenant_id)
            raise HTTPException(status_code=404, detail="Tenant not found")

        if tenant.platform != Platform.ZENDESK:
            logger.warning("Wrong platform for tenant", platform=tenant.platform)
            raise HTTPException(status_code=400, detail="Tenant is not using Zendesk")

        # 2. Raw body 읽기
        raw_body = await request.body()

        # 3. 서명 검증 (Zendesk는 X-Zendesk-Webhook-Signature 사용)
        signature = request.headers.get("X-Zendesk-Webhook-Signature", "")
        webhook_secret = ""  # TODO: 테넌트 설정에서 가져오기

        handler = get_webhook_handler(teams_tenant_id, webhook_secret)

        if signature and webhook_secret:
            if not handler.verify_signature(raw_body, signature):
                logger.warning("Invalid webhook signature", teams_tenant_id=teams_tenant_id)
                raise HTTPException(status_code=401, detail="Invalid signature")

        # 4. 페이로드 파싱
        payload = await request.json()

        logger.debug(
            "Received Zendesk webhook",
            teams_tenant_id=teams_tenant_id,
        )

        # 5. 웹훅 이벤트 파싱
        zendesk_event = handler.parse_webhook(payload)
        if not zendesk_event:
            return Response(status_code=200)

        # 6. 공통 WebhookEvent 형식으로 변환
        event = _convert_to_common_event(zendesk_event)
        if not event:
            return Response(status_code=200)

        # 7. 메시지 라우터로 전달
        message_router = get_message_router()
        await message_router.handle_webhook(tenant, event)

        return Response(status_code=200)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Zendesk webhook error", error=str(e), teams_tenant_id=teams_tenant_id)
        return Response(status_code=500)


def _convert_to_common_event(zendesk_event: ZendeskWebhookEvent) -> WebhookEvent | None:
    """Zendesk 이벤트를 공통 형식으로 변환"""
    if not zendesk_event.message:
        return None

    # 액션 매핑
    action = zendesk_event.action
    if action == "ticket_solved":
        action = "conversation_resolution"
    elif action == "ticket_comment_created":
        action = "message_create"

    # 첨부파일 변환
    attachments = []
    if zendesk_event.message.attachments:
        for att in zendesk_event.message.attachments:
            attachments.append(ParsedAttachment(
                type=att.type,
                url=att.url,
                name=att.name,
                content_type=att.content_type,
            ))

    # 메시지 변환
    message = ParsedMessage(
        id=zendesk_event.message.id,
        text=zendesk_event.message.text,
        attachments=attachments,
        actor_type=zendesk_event.message.actor_type,
        actor_id=zendesk_event.message.actor_id,
        created_time=zendesk_event.message.created_at,
    )

    return WebhookEvent(
        action=action,
        conversation_id=zendesk_event.ticket_id,
        message=message,
        raw_data=zendesk_event.raw_data,
    )


@router.get("/health")
async def webhook_health() -> dict:
    """Webhook 헬스 체크"""
    return {
        "status": "ok",
        "webhook": "zendesk",
        "multi_tenant": True,
    }
