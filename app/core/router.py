"""메시지 라우터 (Orchestrator)

멀티테넌트 지원 메시지 라우터
- Teams → Helpdesk (Freshchat/Zendesk)
- Helpdesk → Teams

주요 기능:
- 테넌트별 플랫폼 라우팅
- 대화 생성 및 매핑 관리
- 첨부파일 양방향 전송
"""
import asyncio
from typing import Any, Optional

from botbuilder.core import TurnContext
from botbuilder.schema import Activity, ActivityTypes, Attachment as BotAttachment

from app.adapters.freshchat.webhook import ParsedMessage, ParsedAttachment, WebhookEvent
from app.core.tenant import TenantConfig, Platform, get_tenant_service
from app.core.platform_factory import get_platform_factory, HelpdeskClient
from app.core.store import (
    ConversationStore,
    ConversationMapping,
    get_conversation_store,
)
from app.database import Database
from app.teams.bot import (
    TeamsBot,
    TeamsMessage,
    TeamsAttachment,
    get_teams_bot,
    build_file_card,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)


class MessageRouter:
    """메시지 라우터 - 멀티테넌트 메시지 중계

    Teams 메시지 수신 → 테넌트 설정 조회 → 해당 플랫폼으로 전달
    """

    def __init__(self):
        self._store: Optional[ConversationStore] = None
        self._bot: Optional[TeamsBot] = None
        self._db: Optional[Database] = None

    @property
    def store(self) -> ConversationStore:
        """대화 매핑 스토어"""
        if self._store is None:
            self._store = get_conversation_store()
        return self._store

    @property
    def bot(self) -> TeamsBot:
        """Teams Bot"""
        if self._bot is None:
            self._bot = get_teams_bot()
        return self._bot

    @property
    def db(self) -> Database:
        """Database 클라이언트"""
        if self._db is None:
            self._db = Database()
        return self._db

    # ===== Teams → Helpdesk =====

    async def handle_teams_message(
        self,
        context: TurnContext,
        message: TeamsMessage,
    ) -> None:
        """
        Teams에서 받은 메시지 처리

        Flow:
        1. 테넌트 설정 조회
        2. 미등록 테넌트 → 설정 안내 메시지
        3. 기존 대화 매핑 확인
        4. 없으면 → 새 대화 생성
        5. 있으면 → 기존 대화에 메시지 전송
        """
        teams_conversation_id = message.conversation_id
        teams_tenant_id = message.user.tenant_id if message.user else None
        conversation_reference = message.conversation_reference or {}

        logger.info(
            "Processing Teams message",
            teams_conversation_id=teams_conversation_id,
            teams_tenant_id=teams_tenant_id,
            has_text=bool(message.text),
            attachment_count=len(message.attachments),
        )

        # 1. 테넌트 설정 조회
        if not teams_tenant_id:
            logger.error("Missing tenant_id in message")
            await context.send_activity(
                "테넌트 정보를 확인할 수 없습니다. 관리자에게 문의해 주세요."
            )
            return

        tenant_service = get_tenant_service()
        tenant = await tenant_service.get_tenant(teams_tenant_id)

        # 2. 미등록 테넌트 처리
        if not tenant:
            logger.info("Unregistered tenant", teams_tenant_id=teams_tenant_id)
            await self._send_setup_required_message(context)
            return

        # Freshdesk(법무 POC): 인테이크 카드 요청 커맨드 처리
        if tenant.platform == Platform.FRESHDESK:
            text = (message.text or "").strip()
            if text in {"검토요청", "검토 요청", "legal", "/legal", "new", "/new"}:
                from app.teams.bot import build_legal_intake_card

                card = build_legal_intake_card()
                await context.send_activity(
                    Activity(
                        type=ActivityTypes.message,
                        attachments=[
                            BotAttachment(
                                content_type="application/vnd.microsoft.card.adaptive",
                                content=card,
                            )
                        ],
                    )
                )
                return

        # 3. 플랫폼 클라이언트 가져오기
        factory = get_platform_factory()
        client = factory.get_client(tenant)

        if not client:
            logger.error("Failed to get platform client", platform=tenant.platform)
            await context.send_activity(
                "헬프데스크 연결에 실패했습니다. 설정을 확인해 주세요."
            )
            return

        try:
            # 4. 기존 대화 매핑 확인
            force_new = bool(getattr(message, "metadata", None) and message.metadata.get("force_new_conversation"))
            mapping = None
            if not force_new:
                mapping = await self.store.get_by_teams_id(
                    teams_conversation_id, tenant.platform.value
                )
            else:
                # 기존 매핑이 있으면 "활성 케이스"를 새 케이스로 전환 (DB 내에서만 종료 처리)
                existing = await self.store.get_by_teams_id(
                    teams_conversation_id, tenant.platform.value
                )
                if existing and not existing.is_resolved and existing.platform_conversation_id:
                    await self.store.mark_resolved(
                        existing.platform_conversation_id,
                        tenant.platform.value,
                        True,
                    )

            # 5. 매핑이 없거나 종료된 경우 → 새 대화 생성
            if not mapping or mapping.is_resolved:
                try:
                    mapping = await self._create_new_conversation(
                        context=context,
                        message=message,
                        tenant=tenant,
                        client=client,
                        conversation_reference=conversation_reference,
                    )
                except ValueError as e:
                    # 플랫폼 설정 누락 등 사용자 조치가 필요한 케이스
                    logger.warning("Conversation creation rejected", error=str(e))
                    await context.send_activity(f"설정 오류로 접수할 수 없습니다: {e}")
                    return
                if not mapping:
                    await context.send_activity(
                        "죄송합니다. 상담 연결에 실패했습니다. 잠시 후 다시 시도해 주세요."
                    )
                    return

                # Greeting 메시지 (새 대화 시에만)
                if not mapping.greeting_sent:
                    if tenant.platform == Platform.FRESHDESK:
                        case_id = mapping.platform_conversation_id or mapping.platform_conversation_numeric_id or ""
                        welcome_msg = f"접수되었습니다. (케이스 번호: {case_id})"
                    else:
                        welcome_msg = tenant.welcome_message or "안녕하세요! 상담원이 곧 연결됩니다."
                    await context.send_activity(welcome_msg)
                    mapping.greeting_sent = True
                    await self.store.upsert(mapping)

            else:
                # 6. 기존 대화에 메시지 전송
                success = await self._send_to_helpdesk(
                    context=context,
                    message=message,
                    tenant=tenant,
                    client=client,
                    mapping=mapping,
                )

                if not success:
                    # 전송 실패 → 새 대화 생성
                    logger.info("Message send failed, creating new conversation")
                    await self.store.mark_resolved(
                        mapping.platform_conversation_id or "",
                        tenant.platform.value,
                        True,
                    )

                    mapping = await self._create_new_conversation(
                        context=context,
                        message=message,
                        tenant=tenant,
                        client=client,
                        conversation_reference=conversation_reference,
                    )

                    if not mapping:
                        await context.send_activity(
                            "죄송합니다. 상담 연결에 실패했습니다."
                        )
                        return

                    await context.send_activity(
                        "이전 상담이 종료되어 새로운 상담이 시작되었습니다. 🙂"
                    )

            # ConversationReference 업데이트
            if conversation_reference:
                await self.store.update_conversation_reference(
                    teams_conversation_id,
                    tenant.platform.value,
                    conversation_reference,
                )

        except Exception as e:
            logger.error(
                "Failed to process Teams message",
                error=str(e),
                teams_conversation_id=teams_conversation_id,
            )
            await context.send_activity(
                "죄송합니다. 메시지 처리 중 오류가 발생했습니다."
            )

    async def _send_setup_required_message(self, context: TurnContext) -> None:
        """설정 필요 안내 메시지"""
        message = (
            "🔧 **헬프데스크 설정이 필요합니다**\n\n"
            "IT 관리자가 아직 헬프데스크를 설정하지 않았습니다.\n\n"
            "관리자에게 Teams 관리 센터에서 앱 설정을 완료해 달라고 요청해 주세요."
        )
        await context.send_activity(message)

    async def _create_new_conversation(
        self,
        context: TurnContext,
        message: TeamsMessage,
        tenant: TenantConfig,
        client: HelpdeskClient,
        conversation_reference: dict,
    ) -> Optional[ConversationMapping]:
        """새 대화 생성"""
        user = message.user
        if not user:
            logger.error("No user info in message")
            return None

        # 사용자 프로필 (기본 + 확장 정보)
        properties = {}
        if user.tenant_id:
            properties["tenant_id"] = user.tenant_id

        # Graph API에서 수집된 확장 정보 추가
        if user.job_title:
            properties["job_title"] = user.job_title
        if user.department:
            properties["department"] = user.department
        if user.mobile_phone:
            properties["mobile_phone"] = user.mobile_phone
        if user.office_phone:
            properties["office_phone"] = user.office_phone
        if user.office_location:
            properties["office_location"] = user.office_location

        # 1. 플랫폼 사용자 생성/조회
        platform_user_id = await client.get_or_create_user(
            reference_id=user.id,
            name=user.name,
            email=user.email,
            properties=properties if properties else None,
        )

        if not platform_user_id:
            logger.error("Failed to create platform user")
            return None

        # 2. 첨부파일 병렬 처리
        message_text = message.text
        attachments = await self._process_attachments_parallel(
            context, message.attachments or [], client
        )

        # 3. 대화 생성
        result = await client.create_conversation(
            user_id=platform_user_id,
            user_name=user.name or "Unknown",
            message_text=message_text,
            attachments=attachments if attachments else None,
            metadata=getattr(message, "metadata", None),
        )

        if not result:
            logger.error("Failed to create conversation")
            return None

        conversation_id = result.get("conversation_id", "")
        numeric_id = str(result.get("id", "")) if result.get("id") else None

        logger.info(
            "Created new conversation",
            platform=tenant.platform.value,
            conversation_id=conversation_id,
        )

        # 4. 매핑 저장
        mapping = ConversationMapping(
            teams_conversation_id=message.conversation_id,
            teams_user_id=user.id,
            conversation_reference=conversation_reference,
            platform=tenant.platform.value,
            platform_conversation_id=conversation_id,
            platform_conversation_numeric_id=numeric_id,
            platform_user_id=platform_user_id,
            is_resolved=False,
            greeting_sent=False,
            tenant_id=tenant.id,  # DB의 tenant UUID 사용
        )

        return await self.store.upsert(mapping)

    async def _send_to_helpdesk(
        self,
        context: TurnContext,
        message: TeamsMessage,
        tenant: TenantConfig,
        client: HelpdeskClient,
        mapping: ConversationMapping,
    ) -> bool:
        """기존 대화에 메시지 전송"""
        conversation_id = mapping.platform_conversation_id
        user_id = mapping.platform_user_id

        if not conversation_id or not user_id:
            return False

        # 첨부파일 병렬 처리
        attachments = await self._process_attachments_parallel(
            context, message.attachments or [], client
        )

        # 메시지 전송
        return await client.send_message(
            conversation_id=conversation_id,
            user_id=user_id,
            message_text=message.text,
            attachments=attachments if attachments else None,
            metadata=getattr(message, "metadata", None),
        )

    # ===== Helpdesk → Teams =====

    async def handle_webhook(
        self,
        tenant: TenantConfig,
        event: WebhookEvent,
    ) -> None:
        """
        헬프데스크 웹훅 이벤트 처리

        Args:
            tenant: 테넌트 설정
            event: 파싱된 웹훅 이벤트
        """
        conversation_id = event.conversation_id or event.conversation_numeric_id
        if not conversation_id:
            logger.warning("No conversation ID in webhook event")
            return

        logger.info(
            "Processing webhook",
            platform=tenant.platform.value,
            action=event.action,
            conversation_id=conversation_id,
        )

        try:
            # 대화 매핑 조회
            mapping = await self._find_mapping(event, tenant.platform.value)
            if not mapping:
                logger.warning(
                    "No conversation mapping found",
                    conversation_id=conversation_id,
                )
                return

            # 대화 종료 이벤트
            if event.action == "conversation_resolution":
                await self._handle_resolution(mapping, tenant)
                return

            # 메시지 이벤트
            if event.action == "message_create" and event.message:
                await self._send_to_teams(event, mapping, tenant)

        except Exception as e:
            logger.error(
                "Failed to process webhook",
                error=str(e),
                conversation_id=conversation_id,
            )

    async def _find_mapping(
        self, event: WebhookEvent, platform: str
    ) -> Optional[ConversationMapping]:
        """대화 매핑 조회"""
        if event.conversation_id:
            mapping = await self.store.get_by_platform_id(
                event.conversation_id, platform
            )
            if mapping:
                return mapping

        if event.conversation_numeric_id:
            mapping = await self.store.get_by_platform_id(
                event.conversation_numeric_id, platform
            )
            if mapping:
                return mapping

        return None

    async def _handle_resolution(
        self, mapping: ConversationMapping, tenant: TenantConfig
    ) -> None:
        """대화 종료 처리"""
        await self.store.mark_resolved(
            mapping.platform_conversation_id or "",
            tenant.platform.value,
            True,
        )

        if mapping.conversation_reference:
            await self.bot.send_proactive_message(
                conversation_reference=mapping.conversation_reference,
                text="✅ 상담이 종료되었습니다. 새로운 문의가 있으시면 메시지를 보내주세요.",
            )

        logger.info(
            "Conversation resolved",
            teams_conversation_id=mapping.teams_conversation_id,
        )

    async def _send_to_teams(
        self,
        event: WebhookEvent,
        mapping: ConversationMapping,
        tenant: TenantConfig,
    ) -> None:
        """헬프데스크 메시지를 Teams로 전송"""
        if not mapping.conversation_reference:
            logger.error("No conversation reference")
            return

        message = event.message
        if not message:
            return

        # 상담원 이름 조회
        agent_name = None
        if message.actor_type == "agent" and message.actor_id:
            factory = get_platform_factory()
            client = factory.get_client(tenant)
            if client:
                agent_name = await client.get_agent_name(message.actor_id)

        # 텍스트와 첨부파일을 하나의 메시지로 통합 전송
        await self._send_combined_message_to_teams(
            text=message.text,
            attachments=message.attachments,
            mapping=mapping,
            agent_name=agent_name,
        )

        logger.info(
            "Sent message to Teams",
            teams_conversation_id=mapping.teams_conversation_id,
            actor_type=message.actor_type,
        )

    async def _send_combined_message_to_teams(
        self,
        text: Optional[str],
        attachments: Optional[list[ParsedAttachment]],
        mapping: ConversationMapping,
        agent_name: Optional[str] = None,
    ) -> None:
        """
        텍스트와 모든 첨부파일을 하나의 메시지로 통합 전송

        - 이미지: Adaptive Card로 원본 비율 유지하여 표시
        - 비디오/파일: 텍스트에 링크로 추가
        - 모든 내용을 하나의 메시지로 전송
        """
        from botbuilder.schema import Attachment

        # 첨부파일 분류
        image_attachments = []
        video_attachments = []
        file_attachments = []

        if attachments:
            for att in attachments:
                if not att.url:
                    continue

                is_image = att.type == "image" or self._is_image_content_type(att.content_type, att.name)
                is_video = att.type == "video" or self._is_video_content_type(att.content_type, att.name)

                if is_image:
                    image_attachments.append(att)
                elif is_video:
                    video_attachments.append(att)
                else:
                    file_attachments.append(att)

        # 텍스트 구성 (원본 텍스트 + 비디오/파일 링크)
        message_parts = []

        if text:
            message_parts.append(text)

        # 비디오 링크 추가
        for att in video_attachments:
            display_name = att.name or "video"
            message_parts.append(f"🎬 [{display_name}]({att.url})")

        # 파일 링크 추가
        for att in file_attachments:
            display_name = att.name or "file"
            message_parts.append(f"📎 [{display_name}]({att.url})")

        combined_text = "\n\n".join(message_parts) if message_parts else None

        # Bot attachments (이미지는 Adaptive Card로 적절한 크기 + 비율 유지)
        bot_attachments = []
        if image_attachments:
            # Adaptive Card body에 이미지들 추가
            card_body = []
            for att in image_attachments:
                card_body.append({
                    "type": "Image",
                    "url": att.url,
                    "size": "Medium",  # 적절한 크기로 제한 (비율 유지)
                    "altText": att.name or "image",
                    "selectAction": {  # 클릭 시 원본 이미지 열기
                        "type": "Action.OpenUrl",
                        "url": att.url,
                    },
                })

            adaptive_card = {
                "type": "AdaptiveCard",
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "version": "1.4",
                "body": card_body,
            }
            bot_attachments.append(Attachment(
                content_type="application/vnd.microsoft.card.adaptive",
                content=adaptive_card,
            ))

        # 텍스트나 첨부파일이 있으면 하나의 메시지로 전송
        if combined_text or bot_attachments:
            await self.bot.send_proactive_message(
                conversation_reference=mapping.conversation_reference,
                text=combined_text,
                attachments=bot_attachments if bot_attachments else None,
                sender_name=agent_name,
            )

    async def _send_attachments_to_teams(
        self,
        attachments: list[ParsedAttachment],
        mapping: ConversationMapping,
        agent_name: Optional[str] = None,
    ) -> None:
        """
        첨부파일을 Teams로 전송

        - 이미지: Adaptive Card로 원본 비율 유지하여 표시
        - 비디오: 링크로 표시
        - 기타 파일: Adaptive Card로 다운로드 링크 제공
        """
        from botbuilder.schema import Attachment

        for att in attachments:
            if not att.url:
                continue

            # 이미지 타입 확인 (type 필드 또는 content_type 기반)
            is_image = att.type == "image" or self._is_image_content_type(att.content_type, att.name)

            if is_image:
                # 이미지는 Adaptive Card로 적절한 크기 + 비율 유지
                adaptive_card = {
                    "type": "AdaptiveCard",
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "Image",
                            "url": att.url,
                            "size": "Medium",  # 적절한 크기로 제한 (비율 유지)
                            "altText": att.name or "image",
                            "selectAction": {  # 클릭 시 원본 이미지 열기
                                "type": "Action.OpenUrl",
                                "url": att.url,
                            },
                        }
                    ],
                }
                card_attachment = Attachment(
                    content_type="application/vnd.microsoft.card.adaptive",
                    content=adaptive_card,
                )

                # 발신자 이름 포함
                text = f"👤 **{agent_name}**" if agent_name else None

                await self.bot.send_proactive_message(
                    conversation_reference=mapping.conversation_reference,
                    text=text,
                    attachments=[card_attachment],
                )

            elif att.type == "video" or self._is_video_content_type(att.content_type, att.name):
                # 비디오는 마크다운 링크로 전송
                display_name = att.name or "video"
                text = f"👤 **{agent_name}**\n\n" if agent_name else ""
                text += f"🎬 [{display_name}]({att.url})"

                await self.bot.send_proactive_message(
                    conversation_reference=mapping.conversation_reference,
                    text=text,
                )

            else:
                # 일반 파일은 Adaptive Card로 다운로드 링크 제공
                card = build_file_card(
                    filename=att.name or "file",
                    file_url=att.url,
                    content_type=att.content_type,
                )
                await self.bot.send_proactive_card(
                    conversation_reference=mapping.conversation_reference,
                    card=card,
                    sender_name=agent_name,
                )

    def _is_image_content_type(self, content_type: Optional[str], filename: Optional[str]) -> bool:
        """이미지 content_type 또는 파일 확장자 확인"""
        if content_type and content_type.lower().startswith("image/"):
            return True

        if filename:
            image_exts = [".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".ico", ".tiff", ".heic", ".heif"]
            lower_name = filename.lower()
            return any(lower_name.endswith(ext) for ext in image_exts)

        return False

    def _is_video_content_type(self, content_type: Optional[str], filename: Optional[str]) -> bool:
        """비디오 content_type 또는 파일 확장자 확인"""
        if content_type and content_type.lower().startswith("video/"):
            return True

        if filename:
            video_exts = [".mp4", ".webm", ".mov", ".avi", ".mkv", ".m4v", ".wmv"]
            lower_name = filename.lower()
            return any(lower_name.endswith(ext) for ext in video_exts)

        return False

    async def _process_attachment_parallel(
        self,
        context: TurnContext,
        att: TeamsAttachment,
        client: HelpdeskClient,
    ) -> Optional[dict]:
        """
        단일 첨부파일을 병렬로 처리 (다운로드 → Supabase + Freshchat 동시 업로드)

        Returns:
            첨부파일 정보 dict (url, file_hash 등) 또는 None
        """
        downloaded = await self.bot.download_attachment(context, att)
        if not downloaded:
            return None

        file_buffer, content_type, filename = downloaded

        # 이미지인 경우 Supabase + Freshchat 동시 업로드
        if self._is_image_content_type(content_type, filename):
            # 병렬 업로드
            supabase_task = self.db.upload_to_storage(
                file_buffer=file_buffer,
                filename=filename,
                content_type=content_type,
            )
            freshchat_task = client.upload_file(
                file_buffer=file_buffer,
                filename=filename,
                content_type=content_type,
            )

            public_url, uploaded = await asyncio.gather(supabase_task, freshchat_task)

            if uploaded:
                if public_url:
                    uploaded["url"] = public_url
                    logger.info(
                        "Uploaded image in parallel",
                        filename=filename,
                        public_url=public_url,
                    )
                return uploaded
            return None
        else:
            # 비-이미지는 Freshchat만 업로드
            uploaded = await client.upload_file(
                file_buffer=file_buffer,
                filename=filename,
                content_type=content_type,
            )
            return uploaded

    async def _process_attachments_parallel(
        self,
        context: TurnContext,
        attachments: list[TeamsAttachment],
        client: HelpdeskClient,
    ) -> list[dict]:
        """
        여러 첨부파일을 병렬로 처리

        Returns:
            처리된 첨부파일 정보 리스트
        """
        if not attachments:
            return []

        tasks = [
            self._process_attachment_parallel(context, att, client)
            for att in attachments
        ]

        results = await asyncio.gather(*tasks)

        # None 제외하고 반환
        return [r for r in results if r is not None]


# ===== 싱글톤 =====

_router_instance: Optional[MessageRouter] = None


def get_message_router() -> MessageRouter:
    """MessageRouter 싱글톤"""
    global _router_instance
    if _router_instance is None:
        _router_instance = MessageRouter()
    return _router_instance
