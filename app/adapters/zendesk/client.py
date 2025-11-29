"""Zendesk Chat API 클라이언트

Zendesk Messaging/Chat API 통합
- REST API 기반 메시지 전송
- OAuth 또는 API 토큰 인증
- 첨부파일 업로드/다운로드

API 문서: https://developer.zendesk.com/api-reference/
"""
import base64
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.utils.logger import get_logger

logger = get_logger(__name__)


# API 타임아웃
API_TIMEOUT = 30.0

# 에이전트 캐시 TTL (30분)
AGENT_CACHE_TTL = 1800


@dataclass
class CachedAgent:
    """캐시된 에이전트 정보"""
    name: str
    cached_at: float


class ZendeskClient:
    """Zendesk Chat/Messaging API 클라이언트"""

    def __init__(
        self,
        subdomain: str,
        email: str,
        api_token: str,
        oauth_token: Optional[str] = None,
    ):
        """
        Args:
            subdomain: Zendesk 서브도메인 ({subdomain}.zendesk.com)
            email: 관리자 이메일 (API 토큰 인증용)
            api_token: API 토큰
            oauth_token: OAuth 토큰 (선택, 우선 사용)
        """
        self.subdomain = subdomain
        self.email = email
        self.api_token = api_token
        self.oauth_token = oauth_token

        self.base_url = f"https://{subdomain}.zendesk.com/api/v2"
        self.sunshine_url = f"https://{subdomain}.zendesk.com/api/v2/conversations"

        # 에이전트 캐시
        self._agent_cache: dict[str, CachedAgent] = {}

    def _get_auth_header(self) -> dict[str, str]:
        """인증 헤더 생성"""
        if self.oauth_token:
            return {"Authorization": f"Bearer {self.oauth_token}"}

        # Basic Auth: email/token:{api_token}
        credentials = f"{self.email}/token:{self.api_token}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return {"Authorization": f"Basic {encoded}"}

    async def _request(
        self,
        method: str,
        url: str,
        json: Optional[dict] = None,
        data: Optional[dict] = None,
        files: Optional[dict] = None,
    ) -> Optional[dict]:
        """HTTP 요청"""
        headers = self._get_auth_header()
        headers["Content-Type"] = "application/json"

        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=json,
                    data=data,
                    files=files,
                )

                if response.status_code >= 400:
                    logger.error(
                        "Zendesk API error",
                        status=response.status_code,
                        body=response.text[:500],
                    )
                    return None

                if response.status_code == 204:
                    return {}

                return response.json()

        except Exception as e:
            logger.error("Zendesk API request failed", error=str(e))
            return None

    # ===== 사용자 관리 =====

    async def get_or_create_user(
        self,
        reference_id: str,
        name: Optional[str] = None,
        email: Optional[str] = None,
        properties: Optional[dict] = None,
    ) -> Optional[str]:
        """
        Zendesk 사용자 생성/조회

        Args:
            reference_id: 외부 ID (Teams user ID)
            name: 사용자 이름
            email: 이메일
            properties: 추가 속성

        Returns:
            Zendesk 사용자 ID 또는 None
        """
        # 1. 기존 사용자 검색 (외부 ID로)
        search_url = f"{self.base_url}/users/search.json"
        result = await self._request(
            "GET",
            search_url,
            json={"query": f"external_id:{reference_id}"},
        )

        if result and result.get("users"):
            user = result["users"][0]
            logger.debug("Found existing Zendesk user", user_id=user["id"])
            return str(user["id"])

        # 2. 새 사용자 생성
        create_url = f"{self.base_url}/users.json"
        user_data = {
            "user": {
                "name": name or f"User_{reference_id[:8]}",
                "external_id": reference_id,
                "verified": True,
            }
        }

        if email:
            user_data["user"]["email"] = email

        if properties:
            user_data["user"]["user_fields"] = properties

        result = await self._request("POST", create_url, json=user_data)

        if result and result.get("user"):
            user_id = str(result["user"]["id"])
            logger.info("Created Zendesk user", user_id=user_id)
            return user_id

        return None

    # ===== 대화 관리 =====

    async def create_conversation(
        self,
        user_id: str,
        user_name: str,
        message_text: Optional[str] = None,
        attachments: Optional[list[dict]] = None,
    ) -> Optional[dict]:
        """
        새 대화(티켓) 생성

        Args:
            user_id: Zendesk 사용자 ID
            user_name: 사용자 표시 이름 (Zendesk에서는 미사용)
            message_text: 초기 메시지
            attachments: 첨부파일 목록

        Returns:
            {"conversation_id": ..., "id": ...} 또는 None
        """
        # Zendesk는 티켓 기반이므로 티켓 생성
        create_url = f"{self.base_url}/tickets.json"

        ticket_data = {
            "ticket": {
                "requester_id": int(user_id),
                "subject": self._extract_subject(message_text),
                "comment": {
                    "body": message_text or "(첨부파일 참조)",
                },
                "priority": "normal",
            }
        }

        # 첨부파일이 있으면 upload tokens 추가
        if attachments:
            upload_tokens = [att.get("token") for att in attachments if att.get("token")]
            if upload_tokens:
                ticket_data["ticket"]["comment"]["uploads"] = upload_tokens

        result = await self._request("POST", create_url, json=ticket_data)

        if result and result.get("ticket"):
            ticket = result["ticket"]
            ticket_id = str(ticket["id"])

            logger.info("Created Zendesk ticket", ticket_id=ticket_id)

            return {
                "conversation_id": ticket_id,
                "id": ticket["id"],
            }

        return None

    async def send_message(
        self,
        conversation_id: str,
        user_id: str,
        message_text: Optional[str] = None,
        attachments: Optional[list[dict]] = None,
    ) -> bool:
        """
        기존 대화(티켓)에 메시지 추가

        Args:
            conversation_id: 티켓 ID
            user_id: 사용자 ID
            message_text: 메시지 텍스트
            attachments: 첨부파일 목록

        Returns:
            성공 여부
        """
        update_url = f"{self.base_url}/tickets/{conversation_id}.json"

        comment_data: dict[str, Any] = {
            "body": message_text or "(첨부파일 참조)",
            "author_id": int(user_id),
        }

        # 첨부파일
        if attachments:
            upload_tokens = [att.get("token") for att in attachments if att.get("token")]
            if upload_tokens:
                comment_data["uploads"] = upload_tokens

        ticket_data = {
            "ticket": {
                "comment": comment_data,
            }
        }

        result = await self._request("PUT", update_url, json=ticket_data)

        if result and result.get("ticket"):
            logger.debug("Added comment to Zendesk ticket", ticket_id=conversation_id)
            return True

        return False

    async def is_conversation_active(self, conversation_id: str) -> bool:
        """대화(티켓) 활성 상태 확인"""
        url = f"{self.base_url}/tickets/{conversation_id}.json"
        result = await self._request("GET", url)

        if result and result.get("ticket"):
            status = result["ticket"].get("status", "")
            # open, pending, hold는 활성, solved, closed는 비활성
            return status in ["new", "open", "pending", "hold"]

        return False

    # ===== 파일 업로드 =====

    async def upload_file(
        self,
        file_buffer: bytes,
        filename: str,
        content_type: str,
    ) -> Optional[dict]:
        """
        파일 업로드

        Args:
            file_buffer: 파일 데이터
            filename: 파일명
            content_type: MIME 타입

        Returns:
            {"token": upload_token, "url": attachment_url} 또는 None
        """
        upload_url = f"{self.base_url}/uploads.json"

        try:
            headers = self._get_auth_header()

            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    upload_url,
                    headers=headers,
                    params={"filename": filename},
                    content=file_buffer,
                )

                if response.status_code >= 400:
                    logger.error(
                        "Zendesk upload failed",
                        status=response.status_code,
                        error=response.text[:200],
                    )
                    return None

                result = response.json()

            if result and result.get("upload"):
                upload = result["upload"]
                token = upload.get("token")

                # 첨부파일 URL (있으면)
                attachments = upload.get("attachments", [])
                url = attachments[0].get("content_url") if attachments else None

                logger.debug("Uploaded file to Zendesk", filename=filename, token=token)

                return {
                    "token": token,
                    "url": url,
                    "name": filename,
                    "content_type": content_type,
                }

        except Exception as e:
            logger.error("Zendesk file upload failed", error=str(e))

        return None

    # ===== 에이전트 정보 =====

    async def get_agent_name(self, agent_id: str) -> Optional[str]:
        """
        에이전트 이름 조회 (캐시)

        Args:
            agent_id: Zendesk 에이전트 ID

        Returns:
            에이전트 이름 또는 None
        """
        # 캐시 확인
        cached = self._agent_cache.get(agent_id)
        if cached and time.time() - cached.cached_at < AGENT_CACHE_TTL:
            return cached.name

        # API 조회
        url = f"{self.base_url}/users/{agent_id}.json"
        result = await self._request("GET", url)

        if result and result.get("user"):
            name = result["user"].get("name")
            if name:
                self._agent_cache[agent_id] = CachedAgent(
                    name=name,
                    cached_at=time.time(),
                )
                return name

        return None

    def _extract_subject(self, text: Optional[str], max_length: int = 80) -> str:
        """메시지에서 제목 추출"""
        if not text:
            return "Teams Support Request"

        # 첫 줄 또는 첫 문장
        first_line = text.split("\n")[0].strip()
        if len(first_line) <= max_length:
            return first_line

        return first_line[:max_length - 3] + "..."
