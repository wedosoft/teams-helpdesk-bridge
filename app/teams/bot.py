"""Teams Bot Framework 어댑터

Express poc-bridge.js의 BotFrameworkAdapter 및 handleTeamsMessage 포팅
주요 기능:
- Bot Framework SDK 래핑
- Activity 처리 (message, conversationUpdate, installationUpdate)
- Proactive 메시지 전송
- ConversationReference 관리
- 첨부파일 다운로드
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
import json

from aiohttp import ClientSession
from botbuilder.core import (
    BotFrameworkAdapter,
    BotFrameworkAdapterSettings,
    TurnContext,
)
from botbuilder.core.teams import TeamsInfo
from botbuilder.schema import (
    Activity,
    ActivityTypes,
    Attachment,
    ConversationReference,
    HeroCard,
    CardImage,
    CardAction,
    ActionTypes,
)
import httpx

from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class TeamsUser:
    """Teams 사용자 정보"""
    id: str
    name: Optional[str] = None
    email: Optional[str] = None
    aad_object_id: Optional[str] = None
    tenant_id: Optional[str] = None
    # Graph API 확장 정보
    job_title: Optional[str] = None
    department: Optional[str] = None
    mobile_phone: Optional[str] = None
    office_phone: Optional[str] = None
    office_location: Optional[str] = None


@dataclass
class TeamsAttachment:
    """Teams 첨부파일 정보"""
    name: str
    content_type: str
    content_url: Optional[str] = None
    content: Optional[dict] = None


@dataclass
class TeamsMessage:
    """Teams 메시지"""
    id: str
    text: Optional[str] = None
    attachments: list[TeamsAttachment] = field(default_factory=list)
    user: Optional[TeamsUser] = None
    conversation_id: str = ""
    conversation_reference: Optional[dict] = None


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

        # 설정 저장
        self._app_id = settings.bot_app_id
        self._app_password = settings.bot_app_password

        # 메시지 핸들러 (나중에 주입)
        self._message_handler: Optional[Callable] = None

        # 환영 메시지 설정 (TODO: 테넌트별 설정에서 로드)
        self._welcome_message = "안녕하세요! IT 헬프데스크입니다. 무엇을 도와드릴까요?"

    def set_message_handler(self, handler: Callable) -> None:
        """메시지 핸들러 설정"""
        self._message_handler = handler

    async def _on_turn_error(self, context: TurnContext, error: Exception) -> None:
        """에러 핸들러"""
        logger.error(
            "Bot turn error",
            error=str(error),
            error_type=type(error).__name__,
            conversation_id=context.activity.conversation.id if context.activity.conversation else None,
        )
        # 사용자에게 에러 메시지 전송
        try:
            await context.send_activity("죄송합니다. 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.")
        except Exception:
            pass  # 에러 메시지 전송 실패는 무시

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

        if activity.type == ActivityTypes.message:
            await self._handle_message(context)
        elif activity.type == ActivityTypes.conversation_update:
            await self._handle_conversation_update(context)
        elif activity.type == ActivityTypes.installation_update:
            await self._handle_installation_update(context)
        else:
            logger.debug("Unhandled activity type", activity_type=activity.type)

    async def _handle_message(self, context: TurnContext) -> None:
        """메시지 핸들러"""
        activity = context.activity

        # 봇 자신의 메시지는 무시
        if activity.from_property and activity.recipient:
            if activity.from_property.id == activity.recipient.id:
                return

        # 사용자 정보 수집
        user = await self._collect_user_info(context)

        # 첨부파일 파싱
        attachments = self._parse_attachments(activity)

        logger.info(
            "Received message from Teams",
            user_id=user.id,
            user_name=user.name,
            user_email=user.email,
            conversation_id=activity.conversation.id,
            text_preview=activity.text[:50] if activity.text else None,
            attachment_count=len(attachments),
        )

        # ConversationReference 추출 (proactive 메시지용)
        conversation_reference = TurnContext.get_conversation_reference(activity)
        conversation_reference_dict = self._serialize_conversation_reference(conversation_reference)

        # TeamsMessage 구성
        message = TeamsMessage(
            id=activity.id or "",
            text=activity.text,
            attachments=attachments,
            user=user,
            conversation_id=activity.conversation.id,
            conversation_reference=conversation_reference_dict,
        )

        # 외부 핸들러 호출 (메시지 라우터)
        if self._message_handler:
            await self._message_handler(
                context=context,
                message=message,
            )

    async def _collect_user_info(self, context: TurnContext) -> TeamsUser:
        """사용자 정보 수집 (Activity + TeamsInfo + Graph API)"""
        activity = context.activity
        from_property = activity.from_property

        user = TeamsUser(
            id=from_property.id if from_property else "",
            name=from_property.name if from_property else None,
            aad_object_id=from_property.aad_object_id if from_property else None,
        )

        # Teams 채널의 경우 TeamsInfo에서 추가 정보 조회
        if activity.channel_id == "msteams":
            try:
                member = await TeamsInfo.get_member(context, from_property.id)
                if member:
                    user.name = member.name or user.name
                    user.email = member.email
                    user.aad_object_id = member.aad_object_id or user.aad_object_id

                    # user_principal_name이 이메일 형식이면 사용
                    if not user.email and member.user_principal_name:
                        if "@" in member.user_principal_name:
                            user.email = member.user_principal_name

            except Exception as e:
                logger.warning("Failed to get Teams member info", error=str(e))

        # 테넌트 ID
        if activity.conversation and activity.conversation.tenant_id:
            user.tenant_id = activity.conversation.tenant_id

        # Graph API로 확장 정보 조회 (관리자 동의 완료된 경우)
        if user.tenant_id and user.aad_object_id:
            await self._enrich_user_from_graph(user)

        return user

    async def _enrich_user_from_graph(self, user: TeamsUser) -> None:
        """Graph API에서 확장 사용자 정보 조회

        관리자 동의가 완료된 테넌트에서만 동작
        """
        try:
            from app.services.graph import get_graph_service

            graph_service = get_graph_service()
            profile = await graph_service.get_user_profile(
                tenant_id=user.tenant_id,
                aad_object_id=user.aad_object_id,
            )

            if profile:
                # 기존 정보 보완 (Graph 정보가 더 정확할 수 있음)
                user.name = profile.display_name or user.name
                user.email = profile.email or user.email
                # 확장 정보 추가
                user.job_title = profile.job_title
                user.department = profile.department
                user.mobile_phone = profile.mobile_phone
                user.office_phone = profile.office_phone
                user.office_location = profile.office_location

                logger.debug(
                    "User profile enriched from Graph API",
                    user_id=user.id,
                    has_job_title=bool(user.job_title),
                    has_department=bool(user.department),
                )

        except Exception as e:
            # Graph API 실패는 무시 (기본 정보로 진행)
            logger.debug(
                "Failed to enrich user from Graph API",
                error=str(e),
                user_id=user.id,
            )

    def _parse_attachments(self, activity: Activity) -> list[TeamsAttachment]:
        """Activity에서 첨부파일 파싱 (모든 포맷 지원)"""
        attachments: list[TeamsAttachment] = []

        if not activity.attachments:
            return attachments

        for att in activity.attachments:
            # Adaptive Card 등 인라인 콘텐츠는 스킵
            if att.content_type and att.content_type.startswith("application/vnd.microsoft"):
                continue

            # 파일 첨부 URL 결정 (여러 위치에서 찾기)
            content_url = None
            content_data = att.content if isinstance(att.content, dict) else {}

            # 1. content_url 직접 사용
            if att.content_url:
                content_url = att.content_url

            # 2. content.downloadUrl (파일 첨부)
            if not content_url and content_data.get("downloadUrl"):
                content_url = content_data.get("downloadUrl")

            # 3. content.fileUrl (일부 케이스)
            if not content_url and content_data.get("fileUrl"):
                content_url = content_data.get("fileUrl")

            # 4. content.url (이미지 첨부)
            if not content_url and content_data.get("url"):
                content_url = content_data.get("url")

            # 파일명 결정
            filename = att.name or content_data.get("name") or content_data.get("fileName")
            if not filename:
                # content_type에서 확장자 추론
                ext = self._get_extension_from_content_type(att.content_type)
                filename = f"file{ext}"

            # content_type 결정
            content_type = att.content_type or content_data.get("mimeType") or "application/octet-stream"

            if content_url:
                attachments.append(TeamsAttachment(
                    name=filename,
                    content_type=content_type,
                    content_url=content_url,
                    content=content_data if content_data else None,
                ))

                logger.debug(
                    "Parsed attachment",
                    name=filename,
                    content_type=content_type,
                    has_url=bool(content_url),
                )
            else:
                logger.warning(
                    "Attachment without downloadable URL",
                    name=att.name,
                    content_type=att.content_type,
                    content_keys=list(content_data.keys()) if content_data else [],
                )

        return attachments

    def _get_extension_from_content_type(self, content_type: Optional[str]) -> str:
        """content_type에서 파일 확장자 추론"""
        if not content_type:
            return ""

        ext_map = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/bmp": ".bmp",
            "image/svg+xml": ".svg",
            "application/pdf": ".pdf",
            "application/zip": ".zip",
            "text/plain": ".txt",
            "text/html": ".html",
            "text/csv": ".csv",
            "application/json": ".json",
            "application/xml": ".xml",
            "video/mp4": ".mp4",
            "video/webm": ".webm",
            "audio/mpeg": ".mp3",
            "audio/wav": ".wav",
        }

        return ext_map.get(content_type, "")

    def _serialize_conversation_reference(self, ref: ConversationReference) -> dict:
        """ConversationReference를 JSON 직렬화 가능한 dict로 변환"""
        return {
            "activityId": ref.activity_id,
            "user": {
                "id": ref.user.id if ref.user else None,
                "name": ref.user.name if ref.user else None,
                "aadObjectId": ref.user.aad_object_id if ref.user else None,
            } if ref.user else None,
            "bot": {
                "id": ref.bot.id if ref.bot else None,
                "name": ref.bot.name if ref.bot else None,
            } if ref.bot else None,
            "conversation": {
                "id": ref.conversation.id if ref.conversation else None,
                "isGroup": ref.conversation.is_group if ref.conversation else None,
                "conversationType": ref.conversation.conversation_type if ref.conversation else None,
                "tenantId": ref.conversation.tenant_id if ref.conversation else None,
            } if ref.conversation else None,
            "channelId": ref.channel_id,
            "serviceUrl": ref.service_url,
            "locale": ref.locale,
        }

    def _deserialize_conversation_reference(self, data: dict) -> ConversationReference:
        """dict에서 ConversationReference로 변환"""
        ref = ConversationReference()

        ref.activity_id = data.get("activityId")
        ref.channel_id = data.get("channelId")
        ref.service_url = data.get("serviceUrl")
        ref.locale = data.get("locale")

        if data.get("user"):
            from botbuilder.schema import ChannelAccount
            ref.user = ChannelAccount(
                id=data["user"].get("id"),
                name=data["user"].get("name"),
                aad_object_id=data["user"].get("aadObjectId"),
            )

        if data.get("bot"):
            from botbuilder.schema import ChannelAccount
            ref.bot = ChannelAccount(
                id=data["bot"].get("id"),
                name=data["bot"].get("name"),
            )

        if data.get("conversation"):
            from botbuilder.schema import ConversationAccount
            ref.conversation = ConversationAccount(
                id=data["conversation"].get("id"),
                is_group=data["conversation"].get("isGroup"),
                conversation_type=data["conversation"].get("conversationType"),
                tenant_id=data["conversation"].get("tenantId"),
            )

        return ref

    async def _handle_conversation_update(self, context: TurnContext) -> None:
        """대화 업데이트 핸들러 (봇 추가/제거)"""
        activity = context.activity

        if activity.members_added:
            for member in activity.members_added:
                # 봇 자신이 추가된 경우는 무시
                if member.id == activity.recipient.id:
                    continue

                logger.info(
                    "New member added to conversation",
                    member_id=member.id,
                    member_name=member.name,
                    conversation_id=activity.conversation.id,
                )

                # 환영 메시지 전송
                if self._welcome_message:
                    await context.send_activity(self._welcome_message)

    async def _handle_installation_update(self, context: TurnContext) -> None:
        """설치 업데이트 핸들러"""
        activity = context.activity
        action = activity.action

        if action == "add":
            logger.info(
                "Bot installed",
                conversation_id=activity.conversation.id if activity.conversation else None,
                tenant_id=activity.conversation.tenant_id if activity.conversation else None,
            )
        elif action == "remove":
            logger.info(
                "Bot uninstalled",
                conversation_id=activity.conversation.id if activity.conversation else None,
            )

    # ===== Proactive 메시지 =====

    async def send_proactive_message(
        self,
        conversation_reference: dict,
        text: Optional[str] = None,
        attachments: Optional[list[Attachment]] = None,
        sender_name: Optional[str] = None,
    ) -> bool:
        """
        Proactive 메시지 전송 (Freshchat → Teams)

        Args:
            conversation_reference: 저장된 ConversationReference dict
            text: 메시지 텍스트
            attachments: Bot Framework Attachment 목록
            sender_name: 발신자 이름 (상담원)

        Returns:
            성공 여부
        """
        try:
            ref = self._deserialize_conversation_reference(conversation_reference)

            async def send_callback(context: TurnContext):
                # 발신자 이름 포맷팅
                formatted_text = text
                if sender_name and text:
                    formatted_text = f"👤 **{sender_name}**\n\n{text}"

                activity = Activity(
                    type=ActivityTypes.message,
                    text=formatted_text,
                    attachments=attachments,
                )

                await context.send_activity(activity)

            await self.adapter.continue_conversation(
                ref,
                send_callback,
                self._app_id,
            )

            logger.info(
                "Proactive message sent",
                conversation_id=conversation_reference.get("conversation", {}).get("id"),
                sender_name=sender_name,
            )
            return True

        except Exception as e:
            logger.error(
                "Failed to send proactive message",
                error=str(e),
                conversation_id=conversation_reference.get("conversation", {}).get("id"),
            )
            return False

    async def send_proactive_card(
        self,
        conversation_reference: dict,
        card: dict,
        sender_name: Optional[str] = None,
    ) -> bool:
        """
        Proactive Adaptive Card 전송

        Args:
            conversation_reference: 저장된 ConversationReference dict
            card: Adaptive Card JSON
            sender_name: 발신자 이름

        Returns:
            성공 여부
        """
        try:
            attachment = Attachment(
                content_type="application/vnd.microsoft.card.adaptive",
                content=card,
            )

            return await self.send_proactive_message(
                conversation_reference=conversation_reference,
                attachments=[attachment],
                sender_name=sender_name,
            )

        except Exception as e:
            logger.error("Failed to send proactive card", error=str(e))
            return False

    # ===== 첨부파일 다운로드 =====

    async def download_attachment(
        self,
        context: TurnContext,
        attachment: TeamsAttachment,
    ) -> Optional[tuple[bytes, str, str]]:
        """
        Teams 첨부파일 다운로드 (다중 URL 소스 시도)

        Args:
            context: TurnContext (인증 토큰용)
            attachment: TeamsAttachment

        Returns:
            (file_buffer, content_type, filename) 또는 None
        """
        # URL 후보 수집 (우선순위 순)
        candidates: list[dict] = []
        content_data = attachment.content or {}

        # 1. contentUrl (인증 필요)
        if attachment.content_url:
            candidates.append({
                "url": attachment.content_url,
                "label": "contentUrl",
                "requires_auth": True,
            })

        # 2. content 내 대체 URL들 (인증 불필요)
        alt_urls = [
            ("downloadUrl", content_data.get("downloadUrl")),
            ("download-url", content_data.get("download-url")),
            ("fileUrl", content_data.get("fileUrl")),
            ("file-url", content_data.get("file-url")),
            ("contentUrl", content_data.get("contentUrl")),
            ("content-url", content_data.get("content-url")),
            ("url", content_data.get("url")),
        ]

        for key, value in alt_urls:
            if isinstance(value, str) and value.startswith("http"):
                candidates.append({
                    "url": value,
                    "label": key,
                    "requires_auth": False,
                })

        if not candidates:
            logger.warning(
                "No downloadable URL found for attachment",
                name=attachment.name,
                content_keys=list(content_data.keys()),
            )
            return None

        # 토큰 획득 (한 번만)
        token = await self._get_attachment_token(context)
        last_error = None

        for candidate in candidates:
            try:
                headers = {
                    "User-Agent": "Microsoft-BotFramework/3.0 (Python)",
                    "Accept": "*/*",
                }

                if candidate["requires_auth"] and token:
                    headers["Authorization"] = f"Bearer {token}"

                async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
                    response = await client.get(candidate["url"], headers=headers)
                    response.raise_for_status()

                    # content_type 결정 (다운로드 응답 우선)
                    downloaded_ct = response.headers.get("content-type")
                    initial_ct = (attachment.content_type or "").lower()

                    # 이미지 타입 보존 로직 (Node.js 참조)
                    resolved_ct = self._resolve_content_type(
                        downloaded_ct=downloaded_ct,
                        initial_ct=initial_ct,
                        filename=attachment.name,
                    )

                    logger.debug(
                        "Downloaded Teams attachment",
                        filename=attachment.name,
                        size=len(response.content),
                        content_type=resolved_ct,
                        source=candidate["label"],
                    )

                    return (response.content, resolved_ct, attachment.name)

            except Exception as e:
                last_error = e
                status = getattr(getattr(e, "response", None), "status_code", None)
                logger.warning(
                    f"Download attempt failed ({candidate['label']})",
                    url=candidate["url"][:80],
                    status=status,
                    error=str(e),
                )

        logger.error(
            "Failed to download Teams attachment after all attempts",
            filename=attachment.name,
            error=str(last_error) if last_error else "Unknown error",
        )
        return None

    def _resolve_content_type(
        self,
        downloaded_ct: Optional[str],
        initial_ct: str,
        filename: str,
    ) -> str:
        """
        최종 content_type 결정 (이미지 타입 보존)

        우선순위:
        1. 초기 타입이 image/*이고 다운로드가 generic이면 → 파일명에서 추론 또는 image/png 기본값
        2. 다운로드된 content_type 사용
        3. 초기 content_type 사용
        4. 파일명에서 추론
        5. application/octet-stream
        """
        is_image_initial = initial_ct.startswith("image/") or initial_ct == "image/*"
        downloaded_is_generic = not downloaded_ct or downloaded_ct == "application/octet-stream"

        if is_image_initial and downloaded_is_generic:
            # 이미지 타입 보존 - 파일명에서 추론 시도
            detected = self._detect_content_type_from_filename(filename)
            if detected and detected.startswith("image/"):
                return detected
            return "image/png"  # 기본값

        if downloaded_ct and downloaded_ct != "application/octet-stream":
            return downloaded_ct

        if initial_ct and initial_ct != "application/octet-stream":
            return initial_ct

        # 파일명에서 추론
        detected = self._detect_content_type_from_filename(filename)
        if detected:
            return detected

        return "application/octet-stream"

    def _detect_content_type_from_filename(self, filename: str) -> Optional[str]:
        """파일명에서 MIME 타입 추론"""
        if not filename or "." not in filename:
            return None

        ext = filename.rsplit(".", 1)[-1].lower()
        mime_map = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "webp": "image/webp",
            "bmp": "image/bmp",
            "svg": "image/svg+xml",
            "ico": "image/x-icon",
            "tiff": "image/tiff",
            "tif": "image/tiff",
            "heic": "image/heic",
            "heif": "image/heif",
            "pdf": "application/pdf",
            "doc": "application/msword",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "xls": "application/vnd.ms-excel",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "ppt": "application/vnd.ms-powerpoint",
            "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "zip": "application/zip",
            "rar": "application/vnd.rar",
            "7z": "application/x-7z-compressed",
            "tar": "application/x-tar",
            "gz": "application/gzip",
            "txt": "text/plain",
            "html": "text/html",
            "css": "text/css",
            "js": "application/javascript",
            "json": "application/json",
            "xml": "application/xml",
            "csv": "text/csv",
            "mp4": "video/mp4",
            "webm": "video/webm",
            "mov": "video/quicktime",
            "avi": "video/x-msvideo",
            "mkv": "video/x-matroska",
            "mp3": "audio/mpeg",
            "wav": "audio/wav",
            "ogg": "audio/ogg",
            "m4a": "audio/mp4",
            "flac": "audio/flac",
        }
        return mime_map.get(ext)

    def _is_image_type(self, content_type: str, filename: str) -> bool:
        """이미지 여부 확인 (content_type + 파일 확장자)"""
        if content_type and content_type.lower().startswith("image/"):
            return True

        if filename:
            image_extensions = [".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".ico", ".tiff", ".heic", ".heif"]
            lower_name = filename.lower()
            return any(lower_name.endswith(ext) for ext in image_extensions)

        return False

    async def _get_attachment_token(self, context: TurnContext) -> Optional[str]:
        """첨부파일 다운로드용 토큰 획득 (다중 소스 시도)"""
        token = None

        # 1. MicrosoftAppCredentials로 직접 토큰 획득 (가장 신뢰성 있음)
        try:
            from botbuilder.connector import MicrosoftAppCredentials

            credentials = MicrosoftAppCredentials(
                app_id=self._app_id,
                password=self._app_password,
            )
            token = await credentials.get_token()
            if token:
                logger.debug("Got attachment token from MicrosoftAppCredentials")
                return token
        except Exception as e:
            logger.debug("Failed to get token from MicrosoftAppCredentials", error=str(e))

        # 2. adapter.credentials에서 시도
        try:
            if hasattr(self.adapter, "credentials") and self.adapter.credentials:
                result = await self.adapter.credentials.get_token()
                if result:
                    # 다양한 응답 형태 처리
                    if isinstance(result, str):
                        token = result
                    elif hasattr(result, "token"):
                        token = result.token
                    elif hasattr(result, "access_token"):
                        token = result.access_token
                    elif isinstance(result, dict):
                        token = result.get("token") or result.get("access_token") or result.get("value")

                if token:
                    logger.debug("Got attachment token from adapter.credentials")
                    return token
        except Exception as e:
            logger.debug("Failed to get token from adapter.credentials", error=str(e))

        # 3. ConnectorClient 생성하여 시도 (Fallback)
        try:
            service_url = context.activity.service_url
            if service_url:
                connector_client = await self.adapter.create_connector_client(service_url)
                if connector_client and hasattr(connector_client, "config"):
                    creds = getattr(connector_client.config, "credentials", None)
                    if creds and hasattr(creds, "get_token"):
                        result = await creds.get_token()
                        if isinstance(result, str):
                            token = result
                        elif hasattr(result, "token"):
                            token = result.token
                        elif hasattr(result, "access_token"):
                            token = result.access_token
                        if token:
                            logger.debug("Got attachment token from connector client")
        except Exception as e:
            logger.debug("Failed to get token from connector client", error=str(e))

        if not token:
            logger.warning("Failed to get attachment token from all sources")

        return token


# ===== Adaptive Card 빌더 =====

def build_file_card(
    filename: str,
    file_url: str,
    file_size: Optional[int] = None,
    content_type: Optional[str] = None,
) -> dict:
    """
    파일 다운로드용 Adaptive Card 생성

    Args:
        filename: 파일명
        file_url: 다운로드 URL
        file_size: 파일 크기 (bytes)
        content_type: MIME 타입

    Returns:
        Adaptive Card JSON
    """
    # 파일 아이콘 결정
    icon_url = _get_file_icon_url(content_type, filename)

    # 파일 크기 포맷팅
    size_text = ""
    if file_size:
        if file_size >= 1024 * 1024:
            size_text = f"{file_size / (1024 * 1024):.1f} MB"
        elif file_size >= 1024:
            size_text = f"{file_size / 1024:.1f} KB"
        else:
            size_text = f"{file_size} bytes"

    return {
        "type": "AdaptiveCard",
        "version": "1.4",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "body": [
            {
                "type": "ColumnSet",
                "columns": [
                    {
                        "type": "Column",
                        "width": "auto",
                        "items": [
                            {
                                "type": "Image",
                                "url": icon_url,
                                "size": "Medium",
                                "altText": "File icon",
                            }
                        ],
                    },
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": filename,
                                "weight": "Bolder",
                                "wrap": True,
                            },
                            {
                                "type": "TextBlock",
                                "text": size_text,
                                "size": "Small",
                                "isSubtle": True,
                                "spacing": "None",
                            } if size_text else None,
                            {
                                "type": "TextBlock",
                                "text": f"[Download]({file_url})",
                                "spacing": "Small",
                            },
                        ],
                    },
                ],
            }
        ],
    }


def _get_file_icon_url(content_type: Optional[str], filename: str) -> str:
    """파일 타입에 따른 아이콘 URL 반환"""
    # Microsoft 365 파일 아이콘 (공개 URL)
    base_url = "https://res-1.cdn.office.net/files/fabric-cdn-prod_20230815.001/assets/item-types/48"

    if not content_type:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    else:
        ext = ""
        if "pdf" in content_type:
            ext = "pdf"
        elif "word" in content_type or "document" in content_type:
            ext = "docx"
        elif "excel" in content_type or "spreadsheet" in content_type:
            ext = "xlsx"
        elif "powerpoint" in content_type or "presentation" in content_type:
            ext = "pptx"
        elif "zip" in content_type or "compressed" in content_type:
            ext = "zip"
        elif "image" in content_type:
            ext = "photo"
        elif "video" in content_type:
            ext = "video"
        elif "audio" in content_type:
            ext = "audio"

    icon_map = {
        "pdf": "pdf",
        "doc": "docx",
        "docx": "docx",
        "xls": "xlsx",
        "xlsx": "xlsx",
        "ppt": "pptx",
        "pptx": "pptx",
        "zip": "zip",
        "rar": "zip",
        "7z": "zip",
        "png": "photo",
        "jpg": "photo",
        "jpeg": "photo",
        "gif": "photo",
        "mp4": "video",
        "mov": "video",
        "avi": "video",
        "mp3": "audio",
        "wav": "audio",
        "txt": "txt",
        "csv": "csv",
    }

    icon_name = icon_map.get(ext, "genericfile")
    return f"{base_url}/{icon_name}.svg"


# ===== 싱글톤 인스턴스 =====

_bot_instance: Optional[TeamsBot] = None


def get_teams_bot() -> TeamsBot:
    """Teams Bot 싱글톤 인스턴스 반환"""
    global _bot_instance
    if _bot_instance is None:
        _bot_instance = TeamsBot()
    return _bot_instance
