"""Freshchat Webhook 처리"""
import base64
import hashlib
from typing import Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

from app.core.models import Message, Attachment, MessageType
from app.utils.logger import get_logger

logger = get_logger(__name__)


class FreshchatWebhookHandler:
    """Freshchat Webhook 핸들러"""

    def __init__(self, public_key: str):
        """
        Args:
            public_key: RSA 공개키 (PEM 형식)
        """
        self.public_key = self._normalize_public_key(public_key)
        self._processed_messages: dict[str, float] = {}  # message_id -> timestamp
        self._dedup_ttl_seconds = 600  # 10분

    def _normalize_public_key(self, key: str) -> str:
        """공개키 형식 정규화"""
        # 이스케이프된 줄바꿈 처리
        key = key.replace("\\n", "\n")

        # 헤더/푸터가 없으면 추가
        if not key.startswith("-----BEGIN"):
            # Base64 문자열만 있는 경우
            key = key.replace(" ", "").replace("\n", "")
            key = f"-----BEGIN PUBLIC KEY-----\n{key}\n-----END PUBLIC KEY-----"

        return key

    def verify_signature(self, payload: bytes, signature: str) -> bool:
        """
        Webhook 서명 검증

        Args:
            payload: 요청 본문 (raw bytes)
            signature: x-freshchat-signature 헤더 값

        Returns:
            검증 성공 여부
        """
        try:
            # 공개키 로드
            public_key = serialization.load_pem_public_key(
                self.public_key.encode(),
                backend=default_backend(),
            )

            # 서명 디코드
            signature_bytes = base64.b64decode(signature)

            # RSA-SHA256 검증
            public_key.verify(
                signature_bytes,
                payload,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )

            logger.debug("Webhook signature verified")
            return True

        except Exception as e:
            logger.error("Webhook signature verification failed", error=str(e))
            return False

    def _is_duplicate_message(self, message_id: str) -> bool:
        """메시지 중복 체크"""
        import time

        current_time = time.time()

        # 만료된 항목 정리
        expired = [
            mid for mid, ts in self._processed_messages.items()
            if current_time - ts > self._dedup_ttl_seconds
        ]
        for mid in expired:
            del self._processed_messages[mid]

        # 중복 체크
        if message_id in self._processed_messages:
            return True

        # 처리 완료 표시
        self._processed_messages[message_id] = current_time
        return False

    def parse_webhook(self, payload: dict) -> Optional[tuple[str, str, Message]]:
        """
        Webhook 페이로드 파싱

        Args:
            payload: Webhook 페이로드

        Returns:
            (conversation_id, actor_type, message) 튜플 또는 None
        """
        try:
            event = payload.get("action")
            data = payload.get("data", {})

            # message_create 이벤트만 처리
            if event != "message_create":
                if event == "conversation_resolution":
                    # 대화 종료 이벤트
                    conv = data.get("conversation", {})
                    conv_id = conv.get("conversation_id")
                    if conv_id:
                        logger.info("Conversation resolved", conversation_id=conv_id)
                        return (conv_id, "system", Message(text="[대화가 종료되었습니다]"))
                return None

            message_data = data.get("message", {})
            message_id = message_data.get("id")

            # 중복 체크
            if message_id and self._is_duplicate_message(message_id):
                logger.debug("Duplicate message ignored", message_id=message_id)
                return None

            # 사용자 메시지는 무시 (상담원/시스템 메시지만 Teams로 전송)
            actor_type = message_data.get("actor_type")
            if actor_type == "user":
                return None

            # 대화 정보
            conversation = data.get("conversation", {})
            conversation_id = conversation.get("conversation_id")

            if not conversation_id:
                logger.warning("Missing conversation_id in webhook")
                return None

            # 메시지 파츠 파싱
            message_parts = message_data.get("message_parts", [])
            text_parts = []
            attachments = []

            for part in message_parts:
                if "text" in part:
                    text_parts.append(part["text"].get("content", ""))
                elif "image" in part:
                    image = part["image"]
                    attachments.append(Attachment(
                        type=MessageType.IMAGE,
                        url=image.get("url", ""),
                        name=image.get("name"),
                    ))
                elif "file" in part:
                    file = part["file"]
                    attachments.append(Attachment(
                        type=MessageType.FILE,
                        url=file.get("url", ""),
                        name=file.get("name"),
                        content_type=file.get("content_type"),
                    ))

            # 상담원 정보
            actor_id = message_data.get("actor_id")
            sender_name = None
            if actor_type == "agent" and actor_id:
                # 상담원 이름은 나중에 조회
                sender_name = actor_id  # 임시로 ID 사용

            message = Message(
                id=message_id,
                text="\n".join(text_parts) if text_parts else None,
                attachments=attachments,
                sender_id=actor_id,
                sender_name=sender_name,
            )

            logger.info(
                "Parsed Freshchat webhook",
                conversation_id=conversation_id,
                actor_type=actor_type,
                has_text=bool(message.text),
                attachment_count=len(attachments),
            )

            return (conversation_id, actor_type, message)

        except Exception as e:
            logger.error("Failed to parse webhook", error=str(e))
            return None
