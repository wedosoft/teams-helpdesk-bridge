"""Freshchat Webhook 처리

Express poc-bridge.js의 verifyFreshchatSignature 및 웹훅 핸들러 포팅
주요 기능:
- RSA-SHA256 서명 검증 (PKCS#1/SPKI 자동 감지)
- 메시지 중복 제거
- 메시지 파츠 파싱 (text, image, file, video)
- 대화 종료 이벤트 처리
"""
import base64
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.backends import default_backend
from cryptography.exceptions import InvalidSignature

from app.utils.logger import get_logger

logger = get_logger(__name__)


# 중복 메시지 TTL (10분)
DEDUP_TTL_SECONDS = 600
# 최대 저장 메시지 수
MAX_PROCESSED_MESSAGES = 2000


@dataclass
class ParsedAttachment:
    """파싱된 첨부파일"""
    type: str  # "image", "file", "video"
    url: Optional[str] = None
    name: Optional[str] = None
    content_type: Optional[str] = None
    file_hash: Optional[str] = None
    file_id: Optional[str] = None


@dataclass
class ParsedMessage:
    """파싱된 메시지"""
    id: str
    text: Optional[str] = None
    attachments: list[ParsedAttachment] = field(default_factory=list)
    actor_type: str = "user"  # "user", "agent", "system"
    actor_id: Optional[str] = None
    created_time: Optional[str] = None


@dataclass
class WebhookEvent:
    """웹훅 이벤트"""
    action: str  # "message_create", "conversation_resolution", etc.
    conversation_id: Optional[str] = None
    conversation_numeric_id: Optional[str] = None
    message: Optional[ParsedMessage] = None
    user_id: Optional[str] = None
    raw_data: dict = field(default_factory=dict)


class FreshchatWebhookHandler:
    """Freshchat Webhook 핸들러"""

    def __init__(self, public_key: str):
        """
        Args:
            public_key: RSA 공개키 (PEM 형식 또는 base64)
        """
        self.public_key_pem = self._normalize_public_key(public_key)
        self._public_key = None  # 지연 로드
        self._processed_messages: dict[str, float] = {}  # message_id -> timestamp

    def _normalize_public_key(self, key: str) -> str:
        """
        공개키 형식 정규화

        Fly.io 등에서 발생하는 다양한 PEM 형식 문제 처리:
        - 이스케이프된 줄바꿈 (\\n)
        - 단일 라인 base64
        - 공백/탭 문제
        """
        if not key:
            return ""

        # 1. 이스케이프된 줄바꿈 처리
        key = key.replace("\\n", "\n")
        key = key.replace("\\r", "")

        # 2. 헤더/푸터 확인
        if "-----BEGIN" not in key:
            # Base64 문자열만 있는 경우 -> 공백 제거 후 래핑
            key = key.replace(" ", "").replace("\n", "").replace("\t", "")
            key = f"-----BEGIN PUBLIC KEY-----\n{key}\n-----END PUBLIC KEY-----"

        # 3. 헤더/푸터 형식 정리
        lines = key.strip().split("\n")
        cleaned_lines = []

        for line in lines:
            line = line.strip()
            if line:
                cleaned_lines.append(line)

        # 4. base64 본문이 여러 줄로 분리되어야 함 (64자마다)
        result_lines = []
        in_body = False

        for line in cleaned_lines:
            if line.startswith("-----BEGIN"):
                result_lines.append(line)
                in_body = True
            elif line.startswith("-----END"):
                in_body = False
                result_lines.append(line)
            elif in_body:
                # 64자마다 줄바꿈
                for i in range(0, len(line), 64):
                    result_lines.append(line[i:i + 64])

        return "\n".join(result_lines)

    def _load_public_key(self):
        """공개키 로드 (지연 로드 + PKCS#1/SPKI 자동 감지)"""
        if self._public_key is not None:
            return self._public_key

        if not self.public_key_pem:
            raise ValueError("Public key not configured")

        key_bytes = self.public_key_pem.encode()

        # SPKI 형식 (BEGIN PUBLIC KEY) 시도
        try:
            self._public_key = serialization.load_pem_public_key(
                key_bytes,
                backend=default_backend(),
            )
            logger.debug("Loaded public key in SPKI format")
            return self._public_key
        except Exception:
            pass

        # PKCS#1 형식 (BEGIN RSA PUBLIC KEY) 시도
        try:
            # PKCS#1 헤더로 변환
            pkcs1_pem = self.public_key_pem.replace(
                "BEGIN PUBLIC KEY",
                "BEGIN RSA PUBLIC KEY"
            ).replace(
                "END PUBLIC KEY",
                "END RSA PUBLIC KEY"
            )

            self._public_key = serialization.load_pem_public_key(
                pkcs1_pem.encode(),
                backend=default_backend(),
            )
            logger.debug("Loaded public key in PKCS#1 format")
            return self._public_key
        except Exception:
            pass

        # DER 디코드 후 SPKI로 래핑 시도
        try:
            # base64 본문 추출
            lines = self.public_key_pem.split("\n")
            b64_content = "".join(
                line for line in lines
                if not line.startswith("-----")
            )
            der_bytes = base64.b64decode(b64_content)

            # PKCS#1 DER을 SPKI로 변환
            # (cryptography 라이브러리가 자동으로 처리)
            self._public_key = serialization.load_der_public_key(
                der_bytes,
                backend=default_backend(),
            )
            logger.debug("Loaded public key from DER")
            return self._public_key
        except Exception as e:
            logger.error("Failed to load public key", error=str(e))
            raise ValueError(f"Invalid public key format: {e}")

    def verify_signature(self, payload: bytes, signature: str) -> bool:
        """
        Webhook 서명 검증 (RSA-SHA256)

        Args:
            payload: 요청 본문 (raw bytes)
            signature: x-freshchat-signature 헤더 값 (base64)

        Returns:
            검증 성공 여부
        """
        if not signature:
            logger.warning("Missing signature header")
            return False

        try:
            public_key = self._load_public_key()

            # 서명 디코드 (URL-safe base64 지원)
            try:
                signature_bytes = base64.b64decode(signature)
            except Exception:
                # URL-safe base64 시도
                signature_bytes = base64.urlsafe_b64decode(signature + "==")

            logger.debug(
                "Verifying signature",
                payload_len=len(payload),
                signature_len=len(signature_bytes),
                payload_preview=payload[:100].decode('utf-8', errors='replace') if payload else None,
            )

            # RSA-SHA256 with PKCS#1 v1.5 패딩 검증
            public_key.verify(
                signature_bytes,
                payload,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )

            logger.debug("Webhook signature verified successfully")
            return True

        except InvalidSignature:
            logger.warning(
                "Invalid webhook signature",
                payload_len=len(payload) if payload else 0,
                sig_len=len(signature_bytes) if 'signature_bytes' in dir() else 0,
            )
            return False
        except Exception as e:
            logger.error("Signature verification error", error=str(e))
            return False

    def is_duplicate_message(self, message_id: str) -> bool:
        """
        메시지 중복 체크

        Args:
            message_id: Freshchat 메시지 ID

        Returns:
            중복 여부
        """
        if not message_id:
            return False

        current_time = time.time()

        # 만료된 항목 정리 (2000개 초과 시에만)
        if len(self._processed_messages) > MAX_PROCESSED_MESSAGES:
            expired = [
                mid for mid, ts in self._processed_messages.items()
                if current_time - ts > DEDUP_TTL_SECONDS
            ]
            for mid in expired:
                del self._processed_messages[mid]

        # 중복 체크
        if message_id in self._processed_messages:
            logger.debug("Duplicate message ignored", message_id=message_id)
            return True

        # 처리 완료 표시
        self._processed_messages[message_id] = current_time
        return False

    def mark_message_processed(self, message_id: str) -> None:
        """메시지를 처리 완료로 표시"""
        if message_id:
            self._processed_messages[message_id] = time.time()

    def parse_webhook(self, payload: dict) -> Optional[WebhookEvent]:
        """
        Webhook 페이로드 파싱

        Args:
            payload: Webhook JSON 페이로드

        Returns:
            WebhookEvent 또는 None (무시할 이벤트)
        """
        try:
            action = payload.get("action", "")
            data = payload.get("data", {})

            # 대화 종료 이벤트
            if action == "conversation_resolution" or action == "conversation:resolve":
                return self._parse_resolution_event(data)

            # 메시지 생성 이벤트
            if action == "message_create":
                return self._parse_message_event(data)

            # 기타 이벤트는 무시
            logger.debug("Ignoring webhook action", action=action)
            return None

        except Exception as e:
            logger.error("Failed to parse webhook", error=str(e))
            return None

    def _parse_resolution_event(self, data: dict) -> Optional[WebhookEvent]:
        """대화 종료 이벤트 파싱"""
        conversation = data.get("conversation", {})
        conversation_id = conversation.get("conversation_id")
        numeric_id = conversation.get("id") or conversation.get("conversation_numeric_id")

        if not conversation_id and not numeric_id:
            return None

        logger.info(
            "Conversation resolved",
            conversation_id=conversation_id,
            numeric_id=numeric_id,
        )

        return WebhookEvent(
            action="conversation_resolution",
            conversation_id=conversation_id,
            conversation_numeric_id=str(numeric_id) if numeric_id else None,
            message=ParsedMessage(
                id="resolution",
                text="[대화가 종료되었습니다]",
                actor_type="system",
            ),
            raw_data=data,
        )

    def _parse_message_event(self, data: dict) -> Optional[WebhookEvent]:
        """메시지 생성 이벤트 파싱"""
        # 디버그: 전체 페이로드 구조 로깅
        logger.debug("Webhook payload keys", keys=list(data.keys()))

        message_data = data.get("message", {})
        message_id = message_data.get("id")

        if not message_id:
            logger.warning("Missing message ID in webhook")
            return None

        # 중복 체크
        if self.is_duplicate_message(message_id):
            return None

        # actor_type 확인 (user 메시지는 무시 - 에코 방지)
        actor_type = message_data.get("actor_type", "user")
        if actor_type == "user":
            logger.debug("Ignoring user message (echo prevention)")
            return None

        # 대화 정보
        conversation = data.get("conversation", {})
        logger.debug("Conversation data", conversation=conversation)

        conversation_id = conversation.get("conversation_id")
        numeric_id = conversation.get("id")

        # Freshchat API v2에서는 data.data.message.conversation_id 구조일 수 있음
        if not conversation_id and not numeric_id:
            # message_data에서 직접 가져오기 시도
            conversation_id = message_data.get("conversation_id")
            logger.debug("Trying message_data.conversation_id", conversation_id=conversation_id)

        if not conversation_id and not numeric_id:
            logger.warning("Missing conversation ID in webhook", data_keys=list(data.keys()))
            return None

        # 메시지 파싱
        message = self._parse_message(message_data)

        # 사용자 정보
        user = data.get("user", {})
        user_id = user.get("id")

        logger.info(
            "Parsed message webhook",
            message_id=message_id,
            conversation_id=conversation_id,
            actor_type=actor_type,
            has_text=bool(message.text),
            attachment_count=len(message.attachments),
        )

        return WebhookEvent(
            action="message_create",
            conversation_id=conversation_id,
            conversation_numeric_id=str(numeric_id) if numeric_id else None,
            message=message,
            user_id=user_id,
            raw_data=data,
        )

    def _parse_message(self, message_data: dict) -> ParsedMessage:
        """메시지 데이터 파싱"""
        message_parts = message_data.get("message_parts", [])
        text_parts: list[str] = []
        attachments: list[ParsedAttachment] = []

        for part in message_parts:
            # 텍스트
            if "text" in part:
                content = part["text"].get("content", "")
                if content:
                    text_parts.append(content)

            # 이미지
            elif "image" in part:
                image = part["image"]
                # 다양한 URL 필드 시도 (Node.js poc-bridge.js 참조)
                image_url = (
                    image.get("url")
                    or image.get("download_url")
                    or image.get("downloadUrl")
                )
                attachments.append(ParsedAttachment(
                    type="image",
                    url=image_url,
                    name=image.get("name"),
                    content_type=image.get("content_type") or image.get("contentType") or "image/png",
                    file_hash=image.get("file_hash") or image.get("fileHash"),
                    file_id=image.get("file_id") or image.get("fileId"),
                ))

            # 파일
            elif "file" in part:
                file = part["file"]
                # 다양한 URL 필드 시도
                file_url = (
                    file.get("url")
                    or file.get("download_url")
                    or file.get("downloadUrl")
                )
                attachments.append(ParsedAttachment(
                    type="file",
                    url=file_url,
                    name=file.get("name"),
                    content_type=file.get("content_type") or file.get("contentType") or "application/octet-stream",
                    file_hash=file.get("file_hash") or file.get("fileHash"),
                    file_id=file.get("file_id") or file.get("fileId"),
                ))

            # 비디오
            elif "video" in part:
                video = part["video"]
                video_url = (
                    video.get("url")
                    or video.get("download_url")
                    or video.get("downloadUrl")
                )
                attachments.append(ParsedAttachment(
                    type="video",
                    url=video_url,
                    name=video.get("name"),
                    content_type=video.get("content_type") or video.get("contentType") or "video/mp4",
                ))

        return ParsedMessage(
            id=message_data.get("id", ""),
            text="\n".join(text_parts) if text_parts else None,
            attachments=attachments,
            actor_type=message_data.get("actor_type", "user"),
            actor_id=message_data.get("actor_id"),
            created_time=message_data.get("created_time"),
        )

    def extract_attachment_info(
        self,
        detailed_message: dict,
        attachment_index: int,
    ) -> Optional[ParsedAttachment]:
        """
        상세 메시지에서 특정 첨부파일 정보 추출

        웹훅 초기에는 URL이 없을 수 있어서 get_message_with_retry 후 호출

        Args:
            detailed_message: Freshchat API에서 조회한 메시지
            attachment_index: 첨부파일 인덱스

        Returns:
            업데이트된 첨부파일 정보
        """
        message_parts = detailed_message.get("message_parts", [])

        # 첨부파일만 필터링
        attachment_parts = [
            p for p in message_parts
            if "image" in p or "file" in p or "video" in p
        ]

        if attachment_index >= len(attachment_parts):
            return None

        part = attachment_parts[attachment_index]

        if "image" in part:
            image = part["image"]
            return ParsedAttachment(
                type="image",
                url=image.get("url"),
                name=image.get("name"),
                content_type=image.get("content_type", "image/png"),
            )
        elif "file" in part:
            file = part["file"]
            return ParsedAttachment(
                type="file",
                url=file.get("url") or file.get("download_url"),
                name=file.get("name"),
                content_type=file.get("content_type", "application/octet-stream"),
            )
        elif "video" in part:
            video = part["video"]
            return ParsedAttachment(
                type="video",
                url=video.get("url"),
                name=video.get("name"),
                content_type=video.get("content_type", "video/mp4"),
            )

        return None
