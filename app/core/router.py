"""메시지 라우터 (Orchestrator)"""
from typing import Any, Optional

from botbuilder.core import TurnContext
from botbuilder.schema import Activity

from app.adapters import get_adapter
from app.config import get_settings
from app.core.models import Message, User, Platform, Attachment, MessageType
from app.database import Database
from app.teams.bot import get_teams_bot
from app.teams.graph import GraphClient
from app.utils.logger import get_logger

logger = get_logger(__name__)


class MessageRouter:
    """메시지 라우터 - Teams와 헬프데스크 플랫폼 간 메시지 중계"""

    def __init__(self):
        self.db = Database()
        self.graph_client = GraphClient()
        self.settings = get_settings()

    def _get_adapter_config(self, platform: str) -> dict:
        """플랫폼별 어댑터 설정 반환"""
        if platform == "freshchat":
            return {
                "api_key": self.settings.freshchat_api_key,
                "api_url": self.settings.freshchat_api_url,
                "inbox_id": self.settings.freshchat_inbox_id,
                "webhook_public_key": self.settings.freshchat_webhook_public_key,
            }
        # Phase 2: Zendesk
        # elif platform == "zendesk":
        #     return {...}
        return {}

    async def handle_teams_message(
        self,
        context: TurnContext,
        activity: Activity,
        conversation_reference: dict[str, Any],
    ) -> None:
        """
        Teams에서 받은 메시지 처리

        1. 사용자 프로필 조회
        2. 기존 대화 매핑 확인
        3. 없으면 새 대화 생성, 있으면 메시지 전송
        4. 대화 매핑 저장
        """
        teams_conversation_id = activity.conversation.id
        teams_user_id = activity.from_property.id
        teams_user_name = activity.from_property.name

        logger.info(
            "Processing Teams message",
            teams_conversation_id=teams_conversation_id,
            teams_user_id=teams_user_id,
        )

        # 현재는 Freshchat만 지원
        platform = "freshchat"

        try:
            # 1. 사용자 프로필 조회
            user_profile = await self.graph_client.get_user_profile(teams_user_id)
            user = User(
                id=teams_user_id,
                name=user_profile.name if user_profile else teams_user_name,
                email=user_profile.email if user_profile else None,
                job_title=user_profile.job_title if user_profile else None,
                department=user_profile.department if user_profile else None,
            )

            # 2. 메시지 구성
            message = self._build_message_from_activity(activity)

            # 3. 기존 대화 매핑 확인
            mapping = await self.db.get_conversation_by_teams_id(
                teams_conversation_id, platform
            )

            # 4. 어댑터 가져오기
            adapter_config = self._get_adapter_config(platform)
            adapter = get_adapter(platform, adapter_config)

            if mapping and not mapping.get("is_resolved"):
                # 기존 대화에 메시지 전송
                platform_conversation_id = mapping["platform_conversation_id"]
                platform_user_id = mapping.get("platform_user_id")

                # 대화 활성 상태 확인
                if hasattr(adapter, "is_conversation_active"):
                    is_active = await adapter.is_conversation_active(platform_conversation_id)
                    if not is_active:
                        # 대화가 종료됨 -> 새 대화 생성
                        logger.info("Conversation resolved, creating new one")
                        mapping = None

            if mapping and not mapping.get("is_resolved"):
                # 기존 대화에 메시지 전송
                adapter_config["current_user_id"] = mapping.get("platform_user_id")
                adapter = get_adapter(platform, adapter_config)

                success = await adapter.send_message(
                    conversation_id=mapping["platform_conversation_id"],
                    message=message,
                )

                if not success:
                    # 전송 실패 시 새 대화 생성 시도
                    logger.warning("Message send failed, trying new conversation")
                    mapping = None

            if not mapping or mapping.get("is_resolved"):
                # 새 대화 생성
                conversation = await adapter.create_conversation(
                    user=user,
                    initial_message=message,
                )

                if not conversation:
                    await context.send_activity(
                        "죄송합니다. 상담 연결에 실패했습니다. 잠시 후 다시 시도해 주세요."
                    )
                    return

                # 대화 매핑 저장
                await self.db.upsert_conversation({
                    "teams_conversation_id": teams_conversation_id,
                    "teams_user_id": teams_user_id,
                    "conversation_reference": conversation_reference,
                    "platform": platform,
                    "platform_conversation_id": conversation.platform_conversation_id,
                    "platform_user_id": conversation.platform_user_id,
                    "is_resolved": False,
                })

                logger.info(
                    "Created new conversation mapping",
                    teams_conversation_id=teams_conversation_id,
                    platform_conversation_id=conversation.platform_conversation_id,
                )

        except Exception as e:
            logger.error("Failed to process Teams message", error=str(e))
            await context.send_activity(
                "죄송합니다. 메시지 처리 중 오류가 발생했습니다."
            )

    async def handle_platform_message(
        self,
        platform: str,
        platform_conversation_id: str,
        message: Message,
    ) -> None:
        """
        헬프데스크 플랫폼에서 받은 메시지 처리

        1. 대화 매핑 조회
        2. Teams로 Proactive 메시지 전송
        """
        logger.info(
            "Processing platform message",
            platform=platform,
            platform_conversation_id=platform_conversation_id,
        )

        try:
            # 1. 대화 매핑 조회
            mapping = await self.db.get_conversation_by_platform_id(
                platform_conversation_id, platform
            )

            if not mapping:
                logger.warning(
                    "No conversation mapping found",
                    platform_conversation_id=platform_conversation_id,
                )
                return

            # 대화 종료 이벤트 처리
            if message.text == "[대화가 종료되었습니다]":
                await self.db.update_conversation_resolved(
                    platform_conversation_id, platform, True
                )
                # Teams에도 알림
                bot = get_teams_bot()
                await bot.send_proactive_message(
                    mapping["conversation_reference"],
                    "상담이 종료되었습니다. 새로운 문의가 있으시면 메시지를 보내주세요.",
                )
                return

            # 2. Teams로 메시지 전송
            bot = get_teams_bot()

            # 메시지 포맷팅
            text = self._format_agent_message(message)

            success = await bot.send_proactive_message(
                conversation_reference=mapping["conversation_reference"],
                message=text,
            )

            if not success:
                logger.error(
                    "Failed to send proactive message",
                    teams_conversation_id=mapping["teams_conversation_id"],
                )

        except Exception as e:
            logger.error("Failed to process platform message", error=str(e))

    def _build_message_from_activity(self, activity: Activity) -> Message:
        """Activity에서 Message 객체 생성"""
        attachments = []

        if activity.attachments:
            for att in activity.attachments:
                # Teams 첨부파일 처리
                content_url = att.content_url
                content_type = att.content_type or ""

                if content_type.startswith("image/"):
                    msg_type = MessageType.IMAGE
                else:
                    msg_type = MessageType.FILE

                attachments.append(Attachment(
                    type=msg_type,
                    url=content_url or "",
                    name=att.name,
                    content_type=content_type,
                ))

        return Message(
            text=activity.text,
            attachments=attachments,
            sender_id=activity.from_property.id,
            sender_name=activity.from_property.name,
        )

    def _format_agent_message(self, message: Message) -> str:
        """상담원 메시지 포맷팅"""
        parts = []

        # 상담원 이름
        if message.sender_name:
            parts.append(f"**{message.sender_name}**:")

        # 메시지 텍스트
        if message.text:
            parts.append(message.text)

        # 첨부파일
        if message.attachments:
            parts.append("")
            for att in message.attachments:
                if att.type == MessageType.IMAGE:
                    parts.append(f"![이미지]({att.url})")
                else:
                    name = att.name or "파일"
                    parts.append(f"[{name}]({att.url})")

        return "\n".join(parts)
