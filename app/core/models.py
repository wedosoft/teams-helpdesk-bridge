"""공통 데이터 모델"""
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class Platform(str, Enum):
    """지원 플랫폼"""
    FRESHCHAT = "freshchat"
    ZENDESK = "zendesk"
    SALESFORCE = "salesforce"
    FRESHDESK = "freshdesk"


class MessageType(str, Enum):
    """메시지 타입"""
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"


class Attachment(BaseModel):
    """첨부파일"""
    type: MessageType
    url: str
    name: Optional[str] = None
    content_type: Optional[str] = None
    size: Optional[int] = None


class Message(BaseModel):
    """메시지"""
    id: Optional[str] = None
    text: Optional[str] = None
    attachments: list[Attachment] = Field(default_factory=list)
    sender_id: Optional[str] = None
    sender_name: Optional[str] = None
    timestamp: Optional[datetime] = None


class User(BaseModel):
    """사용자"""
    id: str
    name: Optional[str] = None
    email: Optional[str] = None
    job_title: Optional[str] = None
    department: Optional[str] = None


class Conversation(BaseModel):
    """대화"""
    id: str
    platform: Platform
    platform_conversation_id: str
    platform_user_id: Optional[str] = None
    teams_conversation_id: str
    teams_user_id: str
    conversation_reference: dict[str, Any] = Field(default_factory=dict)
    is_resolved: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ConversationMapping(BaseModel):
    """대화 매핑 (DB 저장용)"""
    teams_conversation_id: str
    teams_user_id: str
    conversation_reference: dict[str, Any]
    platform: Platform
    platform_conversation_id: str
    platform_user_id: Optional[str] = None
    is_resolved: bool = False
