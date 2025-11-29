"""Freshchat 어댑터 구현"""
from typing import Optional

from app.adapters.base import BaseAdapter
from app.adapters.freshchat.client import FreshchatClient
from app.adapters.freshchat.webhook import FreshchatWebhookHandler
from app.core.models import Message, Conversation, User, Platform
from app.utils.logger import get_logger

logger = get_logger(__name__)


class FreshchatAdapter(BaseAdapter):
    """Freshchat 플랫폼 어댑터"""

    def __init__(self, config: dict):
        super().__init__(config)

        self.client = FreshchatClient(
            api_key=config.get("api_key", ""),
            api_url=config.get("api_url", "https://api.freshchat.com/v2"),
            inbox_id=config.get("inbox_id", ""),
        )

        self.webhook_handler = FreshchatWebhookHandler(
            public_key=config.get("webhook_public_key", ""),
        )

    async def send_message(
        self,
        conversation_id: str,
        message: Message,
    ) -> bool:
        """Freshchat으로 메시지 전송"""
        # 사용자 ID가 필요 - config에서 가져오거나 조회
        user_id = self.config.get("current_user_id")
        if not user_id:
            logger.error("Missing user_id for sending message")
            return False

        return await self.client.send_message(
            conversation_id=conversation_id,
            message=message,
            user_id=user_id,
        )

    async def create_conversation(
        self,
        user: User,
        initial_message: Message,
    ) -> Optional[Conversation]:
        """새 Freshchat 대화 생성"""
        # 사용자 생성/조회
        freshchat_user_id = await self.get_or_create_user(user)
        if not freshchat_user_id:
            return None

        # 대화 생성
        result = await self.client.create_conversation(
            user_id=freshchat_user_id,
            message=initial_message,
        )

        if not result:
            return None

        return Conversation(
            id=result["conversation_id"],
            platform=Platform.FRESHCHAT,
            platform_conversation_id=result["conversation_id"],
            platform_user_id=freshchat_user_id,
            teams_conversation_id="",  # 호출자가 설정
            teams_user_id=user.id,
        )

    async def get_or_create_user(
        self,
        teams_user: User,
    ) -> Optional[str]:
        """Freshchat 사용자 생성/조회"""
        properties = {}
        if teams_user.job_title:
            properties["job_title"] = teams_user.job_title
        if teams_user.department:
            properties["department"] = teams_user.department

        return await self.client.get_or_create_user(
            reference_id=teams_user.id,
            name=teams_user.name,
            email=teams_user.email,
            properties=properties if properties else None,
        )

    def verify_webhook(
        self,
        payload: bytes,
        signature: str,
    ) -> bool:
        """Webhook 서명 검증"""
        return self.webhook_handler.verify_signature(payload, signature)

    async def handle_webhook(
        self,
        payload: dict,
    ) -> Optional[tuple[str, Message]]:
        """Webhook 이벤트 처리"""
        result = self.webhook_handler.parse_webhook(payload)
        if not result:
            return None

        conversation_id, actor_type, message = result

        # 상담원 이름 조회
        if actor_type == "agent" and message.sender_id:
            agent_name = await self.client.get_agent_name(message.sender_id)
            message.sender_name = agent_name

        return (conversation_id, message)

    async def is_conversation_active(self, conversation_id: str) -> bool:
        """대화 활성 상태 확인"""
        return await self.client.is_conversation_active(conversation_id)
