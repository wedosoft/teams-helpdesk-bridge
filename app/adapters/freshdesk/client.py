"""Freshdesk API 클라이언트 (Freshdesk Omni 포함)

POC 목표:
- Teams 인테이크 → Freshdesk Ticket 생성
- Ticket 업데이트(노트/상태변경) → Teams 알림 (웹훅 연계)

인증:
- API Key 기반 Basic Auth (username=api_key, password='X')
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.utils.logger import get_logger

logger = get_logger(__name__)


API_TIMEOUT = 30.0
AGENT_CACHE_TTL_SECONDS = 1800


@dataclass
class CachedAgent:
    name: str
    cached_at: float


class FreshdeskClient:
    """Freshdesk API v2 클라이언트"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        weight_field_key: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.weight_field_key = weight_field_key

        self.api_url = f"{self.base_url}/api/v2"
        self._agent_cache: dict[str, CachedAgent] = {}

    def _get_auth_header(self) -> dict[str, str]:
        credentials = f"{self.api_key}:X"
        encoded = base64.b64encode(credentials.encode()).decode()
        return {"Authorization": f"Basic {encoded}"}

    async def _request(
        self,
        method: str,
        url: str,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> Optional[Any]:
        headers = self._get_auth_header()
        headers["Content-Type"] = "application/json"

        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=json,
                    params=params,
                )

                if response.status_code >= 400:
                    logger.error(
                        "Freshdesk API error",
                        status=response.status_code,
                        body=response.text[:500],
                    )
                    return None

                if response.status_code == 204:
                    return {}

                return response.json()
        except Exception as e:
            logger.error("Freshdesk API request failed", error=str(e))
            return None

    async def validate_api_key(self) -> bool:
        """API Key 유효성 검증 (간단 조회)"""
        url = f"{self.api_url}/tickets"
        result = await self._request("GET", url, params={"per_page": 1})
        return result is not None

    async def list_tickets(self, per_page: int = 100) -> list[dict]:
        """티켓 목록 조회 (POC용 단순 집계)"""
        url = f"{self.api_url}/tickets"
        result = await self._request("GET", url, params={"per_page": per_page})

        if isinstance(result, list):
            return [t for t in result if isinstance(t, dict)]
        if isinstance(result, dict) and isinstance(result.get("tickets"), list):
            return [t for t in result["tickets"] if isinstance(t, dict)]

        return []

    # ===== HelpdeskClient 인터페이스 =====

    async def get_or_create_user(
        self,
        reference_id: str,
        name: Optional[str] = None,
        email: Optional[str] = None,
        properties: Optional[dict] = None,
    ) -> Optional[str]:
        """Freshdesk는 ticket 생성 시 requester email을 사용하는 방식으로 처리 (POC 단순화)"""
        if not email:
            logger.error("Freshdesk requires requester email")
            return None
        return email

    def _extract_subject(self, text: Optional[str]) -> str:
        if not text:
            return "Teams 요청"
        first_line = (text or "").strip().splitlines()[0].strip()
        return first_line[:120] if first_line else "Teams 요청"

    async def create_conversation(
        self,
        user_id: str,
        user_name: str,
        message_text: Optional[str] = None,
        attachments: Optional[list[dict]] = None,
        metadata: Optional[dict] = None,
    ) -> Optional[dict]:
        """Freshdesk Ticket 생성 (케이스 생성)"""
        subject = (metadata or {}).get("subject") or self._extract_subject(message_text)
        description = (metadata or {}).get("description") or (message_text or "")

        # CC 이메일
        cc_emails = (metadata or {}).get("cc_emails")
        if cc_emails is not None and not isinstance(cc_emails, list):
            logger.error("Invalid cc_emails type", type=type(cc_emails).__name__)
            return None

        # Custom fields (가중치 등)
        custom_fields: dict[str, Any] = {}
        weight = (metadata or {}).get("weight")
        if weight is not None:
            if not self.weight_field_key:
                raise ValueError("Freshdesk weight_field_key not configured for this tenant")
            custom_fields[self.weight_field_key] = int(weight)

        payload: dict[str, Any] = {
            "subject": subject,
            "description": description,
            "email": user_id,
        }

        if cc_emails:
            payload["cc_emails"] = cc_emails

        if custom_fields:
            payload["custom_fields"] = custom_fields

        url = f"{self.api_url}/tickets"
        result = await self._request("POST", url, json=payload)
        if not result or not result.get("id"):
            return None

        ticket_id = str(result["id"])
        logger.info("Created Freshdesk ticket", ticket_id=ticket_id)
        return {"conversation_id": ticket_id, "id": result["id"]}

    async def send_message(
        self,
        conversation_id: str,
        user_id: str,
        message_text: Optional[str] = None,
        attachments: Optional[list[dict]] = None,
        metadata: Optional[dict] = None,
    ) -> bool:
        """Ticket에 노트 추가 (POC: Teams 메시지를 note로 적재)"""
        body = message_text or ""
        if not body.strip():
            # 빈 메시지는 전송하지 않음
            return True

        private_note = bool((metadata or {}).get("private", False))

        payload = {
            "body": body,
            "private": private_note,
        }

        url = f"{self.api_url}/tickets/{conversation_id}/notes"
        result = await self._request("POST", url, json=payload)
        return result is not None

    async def upload_file(
        self,
        file_buffer: bytes,
        filename: str,
        content_type: str,
    ) -> Optional[dict]:
        """POC 단계: Freshdesk 바이너리 첨부는 범위 밖 (Teams에서는 링크 첨부 권장)"""
        logger.info(
            "Freshdesk file upload not supported in this POC path; prefer attachment links",
            filename=filename,
            content_type=content_type,
            size=len(file_buffer),
        )
        return {"name": filename, "content_type": content_type, "size": len(file_buffer)}

    async def get_agent_name(self, agent_id: str) -> Optional[str]:
        """Agent 이름 조회 (캐시)"""
        cached = self._agent_cache.get(agent_id)
        if cached and (time.time() - cached.cached_at) < AGENT_CACHE_TTL_SECONDS:
            return cached.name

        url = f"{self.api_url}/agents/{agent_id}"
        result = await self._request("GET", url)
        if not result:
            return None

        name = result.get("contact", {}).get("name") or result.get("name")
        if not name:
            return None

        self._agent_cache[agent_id] = CachedAgent(name=name, cached_at=time.time())
        return name
