"""Freshdesk 요청자(현업)용 API

Teams 탭(요청자 대시보드)에서 사용하는 최소 API:
- 내 티켓 목록 조회 (email 기준)
- 티켓 상세 조회
- 문의 추가(공개 메모)

POC 단계에서는 Teams SSO 대신 헤더로 식별:
- X-Tenant-ID: Teams tenant id
- X-Requester-Email: 요청자 이메일
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.core.tenant import Platform, get_tenant_service
from app.core.platform_factory import get_platform_factory
from app.utils.logger import get_logger

router = APIRouter()
logger = get_logger(__name__)


async def get_request_context(
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID"),
    x_requester_email: Optional[str] = Header(None, alias="X-Requester-Email"),
) -> tuple[str, str]:
    if not x_tenant_id:
        raise HTTPException(status_code=401, detail="Missing X-Tenant-ID")
    if not x_requester_email:
        raise HTTPException(status_code=401, detail="Missing X-Requester-Email")
    return (x_tenant_id, x_requester_email)


def _is_done(status_value) -> bool:
    if isinstance(status_value, int):
        return status_value in {4, 5}
    if isinstance(status_value, str):
        return status_value.lower() in {"resolved", "closed"}
    return False


class InquiryRequest(BaseModel):
    body: str = Field(..., description="문의 내용(공개 메모)")


@router.get("/requests")
async def list_my_requests(
    page: int = 1,
    per_page: int = 30,
    ctx: tuple[str, str] = Depends(get_request_context),
) -> dict:
    teams_tenant_id, requester_email = ctx

    tenant_service = get_tenant_service()
    try:
        tenant = await tenant_service.get_tenant(teams_tenant_id)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not configured")
    if tenant.platform != Platform.FRESHDESK:
        raise HTTPException(status_code=400, detail="Tenant is not using Freshdesk")

    factory = get_platform_factory()
    client = factory.get_client(tenant)
    if not client:
        raise HTTPException(status_code=500, detail="Failed to create Freshdesk client")

    tickets = await client.list_tickets_for_requester(
        requester_email=requester_email,
        page=page,
        per_page=per_page,
    )

    # Teams 탭에서 쓰기 좋은 형태로 최소 필드만 반환
    items = []
    for t in tickets:
        items.append(
            {
                "id": t.get("id"),
                "subject": t.get("subject"),
                "status": t.get("status"),
                "priority": t.get("priority"),
                "responder_id": t.get("responder_id"),
                "created_at": t.get("created_at"),
                "updated_at": t.get("updated_at"),
                "is_done": _is_done(t.get("status")),
            }
        )

    return {"email": requester_email, "page": page, "per_page": per_page, "items": items}


@router.get("/requests/{ticket_id}")
async def get_request_detail(
    ticket_id: str,
    ctx: tuple[str, str] = Depends(get_request_context),
) -> dict:
    teams_tenant_id, requester_email = ctx

    tenant_service = get_tenant_service()
    try:
        tenant = await tenant_service.get_tenant(teams_tenant_id)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not configured")
    if tenant.platform != Platform.FRESHDESK:
        raise HTTPException(status_code=400, detail="Tenant is not using Freshdesk")

    factory = get_platform_factory()
    client = factory.get_client(tenant)
    if not client:
        raise HTTPException(status_code=500, detail="Failed to create Freshdesk client")

    ticket = await client.view_ticket(ticket_id=ticket_id, include_requester=True)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    requester = ticket.get("requester") if isinstance(ticket.get("requester"), dict) else {}
    ticket_requester_email = (requester.get("email") or "").lower()

    # POC 보안: 최소한의 소유권 체크 (운영형에서는 Teams SSO 검증으로 교체)
    if ticket_requester_email and ticket_requester_email != requester_email.lower():
        raise HTTPException(status_code=403, detail="Forbidden (not your ticket)")

    return {
        "id": ticket.get("id"),
        "subject": ticket.get("subject"),
        "description_text": ticket.get("description_text"),
        "status": ticket.get("status"),
        "priority": ticket.get("priority"),
        "responder_id": ticket.get("responder_id"),
        "cc_emails": ticket.get("cc_emails") or [],
        "custom_fields": ticket.get("custom_fields") or {},
        "created_at": ticket.get("created_at"),
        "updated_at": ticket.get("updated_at"),
        "is_done": _is_done(ticket.get("status")),
        "requester": {"email": requester.get("email"), "name": requester.get("name")},
    }


@router.post("/requests/{ticket_id}/inquiry")
async def add_inquiry(
    ticket_id: str,
    req: InquiryRequest,
    ctx: tuple[str, str] = Depends(get_request_context),
) -> dict:
    teams_tenant_id, requester_email = ctx

    tenant_service = get_tenant_service()
    try:
        tenant = await tenant_service.get_tenant(teams_tenant_id)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not configured")
    if tenant.platform != Platform.FRESHDESK:
        raise HTTPException(status_code=400, detail="Tenant is not using Freshdesk")

    factory = get_platform_factory()
    client = factory.get_client(tenant)
    if not client:
        raise HTTPException(status_code=500, detail="Failed to create Freshdesk client")

    ticket = await client.view_ticket(ticket_id=ticket_id, include_requester=True)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    requester = ticket.get("requester") if isinstance(ticket.get("requester"), dict) else {}
    ticket_requester_email = (requester.get("email") or "").lower()
    if ticket_requester_email and ticket_requester_email != requester_email.lower():
        raise HTTPException(status_code=403, detail="Forbidden (not your ticket)")

    if _is_done(ticket.get("status")):
        raise HTTPException(status_code=409, detail="Ticket is already resolved/closed")

    body = (req.body or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="Body is required")

    # 누가 남겼는지 명확히 남기기 (운영형에서는 UI/SSO 기반으로 더 정교화)
    note_body = f"[요청자 문의: {requester_email}]\n\n{body}"
    ok = await client.add_public_inquiry_note(ticket_id=ticket_id, body=note_body)

    if not ok:
        raise HTTPException(status_code=500, detail="Failed to add inquiry")

    return {"ok": True, "ticket_id": ticket_id}
