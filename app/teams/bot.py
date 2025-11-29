"""Teams Bot Framework 어댑터"""
from typing import Any, Callable, Optional

from aiohttp import ClientSession
from botbuilder.core import (
    BotFrameworkAdapter,
    BotFrameworkAdapterSettings,
    TurnContext,
)
from botbuilder.schema import Activity, ConversationReference

from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger(__name__)


class TeamsBot:
    """Teams Bot 어댑터"""

    def __init__(self):
        settings = get_settings()

        # Bot Framework 어댑터 설정
        adapter_settings = BotFrameworkAdapterSettings(
            app_id=settings.bot_app_id,
            app_password=settings.bot_app_password,
            channel_auth_tenant=(
                "organizations" if settings.bot_tenant_id == "common"
                else settings.bot_tenant_id
            ),
        )
        self.adapter = BotFrameworkAdapter(adapter_settings)
        self.adapter.on_turn_error = self._on_turn_error

        # 메시지 핸들러 (나중에 주입)
        self._message_handler: Optional[Callable] = None

    def set_message_handler(self, handler: Callable) -> None:
        """메시지 핸들러 설정"""
        self._message_handler = handler

    async def _on_turn_error(self, context: TurnContext, error: Exception) -> None:
        """에러 핸들러"""
        logger.error(
            "Bot turn error",
            error=str(error),
            conversation_id=context.activity.conversation.id if context.activity.conversation else None,
        )
        # 사용자에게 에러 메시지 전송
        await context.send_activity("죄송합니다. 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.")

    async def process_activity(self, activity: Activity, auth_header: str) -> None:
        """Teams에서 받은 Activity 처리"""
        await self.adapter.process_activity(
            activity,
            auth_header,
            self._handle_turn,
        )

    async def _handle_turn(self, context: TurnContext) -> None:
        """Turn 핸들러"""
        activity = context.activity

        if activity.type == "message":
            await self._handle_message(context)
        elif activity.type == "conversationUpdate":
            await self._handle_conversation_update(context)
        elif activity.type == "installationUpdate":
            await self._handle_installation_update(context)
        else:
            logger.debug("Unhandled activity type", activity_type=activity.type)

    async def _handle_message(self, context: TurnContext) -> None:
        """메시지 핸들러"""
        activity = context.activity

        # 봇 자신의 메시지는 무시
        if activity.from_property.id == activity.recipient.id:
            return

        logger.info(
            "Received message from Teams",
            user_id=activity.from_property.id,
            user_name=activity.from_property.name,
            conversation_id=activity.conversation.id,
            text=activity.text[:50] if activity.text else None,
        )

        # ConversationReference 추출
        conversation_reference = TurnContext.get_conversation_reference(activity)

        # 외부 핸들러 호출 (메시지 라우터)
        if self._message_handler:
            await self._message_handler(
                context=context,
                activity=activity,
                conversation_reference=conversation_reference,
            )

    async def _handle_conversation_update(self, context: TurnContext) -> None:
        """대화 업데이트 핸들러 (봇 추가/제거)"""
        activity = context.activity

        if activity.members_added:
            for member in activity.members_added:
                if member.id != activity.recipient.id:
                    # 새 사용자 추가 시 환영 메시지
                    logger.info(
                        "New member added",
                        member_id=member.id,
                        member_name=member.name,
                    )
                    await context.send_activity(
                        "안녕하세요! IT 헬프데스크입니다. 무엇을 도와드릴까요?"
                    )

    async def _handle_installation_update(self, context: TurnContext) -> None:
        """설치 업데이트 핸들러"""
        activity = context.activity
        action = activity.action

        if action == "add":
            logger.info("Bot installed", conversation_id=activity.conversation.id)
        elif action == "remove":
            logger.info("Bot removed", conversation_id=activity.conversation.id)

    async def send_proactive_message(
        self,
        conversation_reference: dict[str, Any],
        message: str,
        attachments: Optional[list] = None,
    ) -> bool:
        """Proactive 메시지 전송"""
        try:
            # dict를 ConversationReference로 변환
            ref = ConversationReference().from_dict(conversation_reference)

            async def send_callback(context: TurnContext):
                if attachments:
                    await context.send_activity(
                        Activity(
                            type="message",
                            text=message,
                            attachments=attachments,
                        )
                    )
                else:
                    await context.send_activity(message)

            await self.adapter.continue_conversation(
                ref,
                send_callback,
                get_settings().bot_app_id,
            )

            logger.info(
                "Proactive message sent",
                conversation_id=conversation_reference.get("conversation", {}).get("id"),
            )
            return True

        except Exception as e:
            logger.error(
                "Failed to send proactive message",
                error=str(e),
                conversation_id=conversation_reference.get("conversation", {}).get("id"),
            )
            return False


# 싱글톤 인스턴스
_bot_instance: Optional[TeamsBot] = None


def get_teams_bot() -> TeamsBot:
    """Teams Bot 싱글톤 인스턴스 반환"""
    global _bot_instance
    if _bot_instance is None:
        _bot_instance = TeamsBot()
    return _bot_instance
