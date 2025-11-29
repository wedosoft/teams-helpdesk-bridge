"""Freshchat API 클라이언트"""
from typing import Any, Optional

import httpx

from app.core.models import Message, User, Attachment, MessageType
from app.utils.logger import get_logger

logger = get_logger(__name__)


class FreshchatClient:
    """Freshchat API 클라이언트"""

    def __init__(self, api_key: str, api_url: str, inbox_id: str):
        self.api_key = api_key
        self.api_url = api_url.rstrip("/")
        self.inbox_id = inbox_id
        self._agent_cache: dict[str, str] = {}  # agent_id -> name

    def _get_headers(self) -> dict[str, str]:
        """API 요청 헤더"""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def get_or_create_user(
        self,
        reference_id: str,
        name: Optional[str] = None,
        email: Optional[str] = None,
        properties: Optional[dict] = None,
    ) -> Optional[str]:
        """
        사용자 조회 또는 생성

        Args:
            reference_id: 외부 참조 ID (Teams user ID)
            name: 사용자 이름
            email: 이메일
            properties: 추가 속성

        Returns:
            Freshchat 사용자 ID
        """
        async with httpx.AsyncClient() as client:
            # 기존 사용자 검색
            try:
                response = await client.get(
                    f"{self.api_url}/users",
                    headers=self._get_headers(),
                    params={"reference_id": reference_id},
                )

                if response.status_code == 200:
                    data = response.json()
                    users = data.get("users", [])
                    if users:
                        user_id = users[0].get("id")
                        logger.debug("Found existing Freshchat user", user_id=user_id)
                        return user_id
            except Exception as e:
                logger.warning("Failed to search user", error=str(e))

            # 새 사용자 생성
            try:
                user_data = {
                    "reference_id": reference_id,
                }
                if name:
                    # first_name, last_name 분리
                    parts = name.split(" ", 1)
                    user_data["first_name"] = parts[0]
                    if len(parts) > 1:
                        user_data["last_name"] = parts[1]

                if email:
                    user_data["email"] = email

                if properties:
                    user_data["properties"] = [
                        {"name": k, "value": v} for k, v in properties.items()
                    ]

                response = await client.post(
                    f"{self.api_url}/users",
                    headers=self._get_headers(),
                    json=user_data,
                )
                response.raise_for_status()
                data = response.json()
                user_id = data.get("id")
                logger.info("Created Freshchat user", user_id=user_id)
                return user_id

            except Exception as e:
                logger.error("Failed to create user", error=str(e))
                return None

    async def create_conversation(
        self,
        user_id: str,
        message: Message,
    ) -> Optional[dict[str, Any]]:
        """
        새 대화 생성

        Args:
            user_id: Freshchat 사용자 ID
            message: 첫 메시지

        Returns:
            대화 정보 (conversation_id, numeric_id 등)
        """
        async with httpx.AsyncClient() as client:
            try:
                # 메시지 파츠 구성
                message_parts = []

                if message.text:
                    message_parts.append({
                        "text": {"content": message.text}
                    })

                for attachment in message.attachments:
                    if attachment.type == MessageType.IMAGE:
                        message_parts.append({
                            "image": {"url": attachment.url}
                        })
                    else:
                        message_parts.append({
                            "file": {
                                "url": attachment.url,
                                "name": attachment.name or "file",
                                "content_type": attachment.content_type or "application/octet-stream",
                            }
                        })

                payload = {
                    "channel_id": self.inbox_id,
                    "users": [{"id": user_id}],
                    "messages": [{
                        "message_parts": message_parts,
                        "actor_type": "user",
                        "actor_id": user_id,
                    }],
                }

                response = await client.post(
                    f"{self.api_url}/conversations",
                    headers=self._get_headers(),
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()

                conversation_id = data.get("conversation_id")
                logger.info("Created Freshchat conversation", conversation_id=conversation_id)

                return {
                    "conversation_id": conversation_id,
                    "numeric_id": data.get("id"),  # Numeric ID for webhook
                }

            except httpx.HTTPStatusError as e:
                # 400 에러는 대화가 이미 종료되었을 수 있음
                if e.response.status_code == 400:
                    logger.warning("Conversation creation failed (may be resolved)", error=str(e))
                else:
                    logger.error("Failed to create conversation", error=str(e))
                return None
            except Exception as e:
                logger.error("Failed to create conversation", error=str(e))
                return None

    async def send_message(
        self,
        conversation_id: str,
        message: Message,
        user_id: str,
    ) -> bool:
        """
        메시지 전송

        Args:
            conversation_id: Freshchat 대화 ID
            message: 메시지
            user_id: Freshchat 사용자 ID

        Returns:
            성공 여부
        """
        async with httpx.AsyncClient() as client:
            try:
                message_parts = []

                if message.text:
                    message_parts.append({
                        "text": {"content": message.text}
                    })

                for attachment in message.attachments:
                    if attachment.type == MessageType.IMAGE:
                        message_parts.append({
                            "image": {"url": attachment.url}
                        })
                    else:
                        message_parts.append({
                            "file": {
                                "url": attachment.url,
                                "name": attachment.name or "file",
                                "content_type": attachment.content_type or "application/octet-stream",
                            }
                        })

                payload = {
                    "message_parts": message_parts,
                    "actor_type": "user",
                    "actor_id": user_id,
                }

                response = await client.post(
                    f"{self.api_url}/conversations/{conversation_id}/messages",
                    headers=self._get_headers(),
                    json=payload,
                )

                # 400 에러는 대화가 종료되었을 수 있음 -> 새 대화 필요
                if response.status_code == 400:
                    logger.warning("Message send failed (conversation may be resolved)")
                    return False

                response.raise_for_status()
                logger.info("Message sent to Freshchat", conversation_id=conversation_id)
                return True

            except Exception as e:
                logger.error("Failed to send message", error=str(e))
                return False

    async def get_agent_name(self, agent_id: str) -> str:
        """
        상담원 이름 조회 (캐시 포함)

        Args:
            agent_id: Freshchat 상담원 ID

        Returns:
            상담원 이름
        """
        # 캐시 확인
        if agent_id in self._agent_cache:
            return self._agent_cache[agent_id]

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.api_url}/agents/{agent_id}",
                    headers=self._get_headers(),
                )
                response.raise_for_status()
                data = response.json()

                name = f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()
                if not name:
                    name = data.get("email", "상담원")

                # 캐시 저장
                self._agent_cache[agent_id] = name
                return name

            except Exception as e:
                logger.warning("Failed to get agent name", agent_id=agent_id, error=str(e))
                return "상담원"

    async def is_conversation_active(self, conversation_id: str) -> bool:
        """
        대화 활성 상태 확인

        Args:
            conversation_id: Freshchat 대화 ID

        Returns:
            활성 여부
        """
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.api_url}/conversations/{conversation_id}",
                    headers=self._get_headers(),
                )
                response.raise_for_status()
                data = response.json()
                status = data.get("status")
                return status != "resolved"
            except Exception as e:
                logger.warning("Failed to check conversation status", error=str(e))
                return False
