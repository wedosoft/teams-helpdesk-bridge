"""Freshchat Webhook 라우트"""
from fastapi import APIRouter, Request, Response, HTTPException

from app.config import get_settings
from app.adapters.freshchat import FreshchatAdapter
from app.core.router import MessageRouter
from app.utils.logger import get_logger

router = APIRouter()
logger = get_logger(__name__)


def get_freshchat_adapter() -> FreshchatAdapter:
    """Freshchat 어댑터 인스턴스 생성"""
    settings = get_settings()
    return FreshchatAdapter({
        "api_key": settings.freshchat_api_key,
        "api_url": settings.freshchat_api_url,
        "inbox_id": settings.freshchat_inbox_id,
        "webhook_public_key": settings.freshchat_webhook_public_key,
    })


@router.post("")
async def freshchat_webhook(request: Request) -> Response:
    """Freshchat Webhook 엔드포인트"""
    try:
        # Raw body 읽기 (서명 검증용)
        raw_body = await request.body()

        # 서명 검증
        signature = request.headers.get("x-freshchat-signature", "")
        adapter = get_freshchat_adapter()

        if signature and not adapter.verify_webhook(raw_body, signature):
            logger.warning("Invalid webhook signature")
            raise HTTPException(status_code=401, detail="Invalid signature")

        # 페이로드 파싱
        payload = await request.json()
        logger.debug("Received Freshchat webhook", action=payload.get("action"))

        # Webhook 처리
        result = await adapter.handle_webhook(payload)
        if not result:
            # 무시할 이벤트
            return Response(status_code=200)

        conversation_id, message = result

        # 메시지 라우터로 전달
        message_router = MessageRouter()
        await message_router.handle_platform_message(
            platform="freshchat",
            platform_conversation_id=conversation_id,
            message=message,
        )

        return Response(status_code=200)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Freshchat webhook error", error=str(e))
        return Response(status_code=500)
