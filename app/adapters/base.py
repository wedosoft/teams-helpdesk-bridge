"""플랫폼 어댑터 추상 인터페이스"""
from abc import ABC, abstractmethod
from typing import Optional

from app.core.models import Message, Conversation, User


class BaseAdapter(ABC):
    """플랫폼 어댑터 추상 인터페이스"""

    def __init__(self, config: dict):
        """
        Args:
            config: 플랫폼별 설정 (API 키, URL 등)
        """
        self.config = config

    @abstractmethod
    async def send_message(
        self,
        conversation_id: str,
        message: Message,
    ) -> bool:
        """
        플랫폼으로 메시지 전송

        Args:
            conversation_id: 플랫폼 대화 ID
            message: 전송할 메시지

        Returns:
            성공 여부
        """
        pass

    @abstractmethod
    async def create_conversation(
        self,
        user: User,
        initial_message: Message,
    ) -> Optional[Conversation]:
        """
        새 대화 생성

        Args:
            user: Teams 사용자 정보
            initial_message: 첫 메시지

        Returns:
            생성된 대화 정보 또는 None
        """
        pass

    @abstractmethod
    async def get_or_create_user(
        self,
        teams_user: User,
    ) -> Optional[str]:
        """
        플랫폼 사용자 생성 또는 조회

        Args:
            teams_user: Teams 사용자 정보

        Returns:
            플랫폼 사용자 ID 또는 None
        """
        pass

    @abstractmethod
    def verify_webhook(
        self,
        payload: bytes,
        signature: str,
    ) -> bool:
        """
        Webhook 서명 검증

        Args:
            payload: 요청 본문 (raw bytes)
            signature: 서명 헤더 값

        Returns:
            검증 성공 여부
        """
        pass

    @abstractmethod
    async def handle_webhook(
        self,
        payload: dict,
    ) -> Optional[tuple[str, Message]]:
        """
        Webhook 이벤트 처리

        Args:
            payload: Webhook 페이로드

        Returns:
            (platform_conversation_id, message) 튜플 또는 None
        """
        pass
