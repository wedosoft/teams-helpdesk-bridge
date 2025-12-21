"""Freshdesk Webhook 파서

Freshdesk Automation/Webhook에서 전달되는 payload는 설정에 따라 달라질 수 있으므로,
POC에서는 아래 두 형태를 모두 허용하도록 느슨하게 파싱합니다.

권장(POC) payload 예시:
{
  "event": "message_create",
  "ticket_id": 123,
  "actor_type": "agent",
  "actor_id": "456",
  "text": "추가자료 부탁드립니다.",
  "status": 3
}
"""

from __future__ import annotations

import re
from typing import Optional

from app.adapters.freshchat.webhook import ParsedMessage, WebhookEvent
from app.utils.logger import get_logger

logger = get_logger(__name__)


class FreshdeskWebhookHandler:
    """Freshdesk 웹훅 이벤트 파서"""

    def verify_signature(self, payload: bytes, signature: str) -> bool:
        # POC: 서명 검증은 선택 (운영 시 HMAC/허용 IP 제한 권장)
        return True

    def parse_webhook(self, payload: dict) -> Optional[WebhookEvent]:
        logger.info("Freshdesk webhook payload", payload=payload)
        # ticket_id 추출 (여러 케이스 대응)
        ticket_id = (
            payload.get("ticket_id")
            or payload.get("ticketId")
            or payload.get("id")
            or (payload.get("ticket") or {}).get("id")
            or (payload.get("data") or {}).get("ticket_id")
        )
        if ticket_id is None:
            logger.warning("Freshdesk webhook missing ticket_id", keys=list(payload.keys()))
            return None

        event_name = payload.get("event") or payload.get("action") or ""

        # 상태 기반 종료 판단 (Resolved/Closed)
        status = payload.get("status") or (payload.get("ticket") or {}).get("status")
        if isinstance(status, str) and status.lower() in {"resolved", "closed"}:
            return WebhookEvent(
                action="conversation_resolution",
                conversation_id=str(ticket_id),
                raw_data=payload,
            )
        if isinstance(status, int) and status in {4, 5}:  # Freshdesk 기본 상태 코드 관행
            return WebhookEvent(
                action="conversation_resolution",
                conversation_id=str(ticket_id),
                raw_data=payload,
            )

        # 메시지 텍스트 추출
        text = None
        
        # 1. 공식 문서: payload.conversations[*].body_text
        if isinstance(payload.get("conversations"), list):
            # 최신 대화(노트)를 찾기 위해 역순 순회
            # 주의: Freshdesk 웹훅에서 conversations 순서가 보장되지 않을 수 있으므로
            # id나 created_at으로 정렬하는 것이 안전하지만, POC에서는 리스트의 마지막이 최신이라고 가정
            # (또는 body_text가 있는 마지막 항목)
            
            # 정렬: id가 있다면 id 기준 오름차순 정렬 후 마지막 항목 선택
            conversations = sorted(
                payload["conversations"], 
                key=lambda x: x.get("id", 0) if isinstance(x, dict) else 0
            )
            
            for item in reversed(conversations):
                if isinstance(item, dict) and item.get("body_text"):
                    text = item.get("body_text")
                    break
        
        # 2. text (Fallback - 로그에서 확인됨, HTML 포함 가능성 있음)
        if not text and payload.get("text"):
            text = payload.get("text")
            # Freshdesk {{ticket.latest_public_comment_text}}가 "이름 : 내용" 형식으로 오는 경우 처리
            # 예: "우석 이 : 안녕하세요" -> "안녕하세요"
            if text and isinstance(text, str):
                # "이름 : " 패턴 제거 (이름은 20자 이내로 가정)
                text = re.sub(r"^[^:\n]{1,20}\s*:\s*", "", text, count=1)

        actor_type = payload.get("actor_type") or payload.get("actorType") or "agent"
        actor_id = payload.get("actor_id") or payload.get("actorId")

        message_id = (
            payload.get("message_id")
            or payload.get("note_id")
            or (payload.get("note") or {}).get("id")
            or f"{ticket_id}:{event_name or 'event'}"
        )

        return WebhookEvent(
            action="message_create",
            conversation_id=str(ticket_id),
            message=ParsedMessage(
                id=str(message_id),
                text=str(text) if text is not None else None,
                actor_type=str(actor_type),
                actor_id=str(actor_id) if actor_id is not None else None,
            ),
            raw_data=payload,
        )
