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

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from typing import Union

from app.core.tenant import Platform, get_tenant_service
from app.core.platform_factory import get_platform_factory
from app.config import get_settings
from app.utils.logger import get_logger

router = APIRouter()
logger = get_logger(__name__)


async def get_request_context(
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID"),
) -> tuple[str, str]:
    if not x_tenant_id:
        raise HTTPException(status_code=401, detail="Missing X-Tenant-ID")
    settings = get_settings()
    requester_email = (settings.requester_email_override or "").strip()
    if not requester_email:
        raise HTTPException(
            status_code=500,
            detail="REQUESTER_EMAIL_OVERRIDE is required for requester dashboard",
        )
    return (x_tenant_id, requester_email)


def _is_done(status_value) -> bool:
    if isinstance(status_value, int):
        return status_value in {4, 5}
    if isinstance(status_value, str):
        return status_value.lower() in {"resolved", "closed"}
    return False


class InquiryRequest(BaseModel):
    body: str = Field(..., description="문의 내용(공개 메모)")


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


@router.get("/requests", response_model=Union[dict, str])
async def list_my_requests(
    request: Request,
    page: int = 1,
    per_page: int = 5,  # POC: keep list compact
    recent_days: int = 30,
    ctx: tuple[str, str] = Depends(get_request_context),
) -> Union[dict, HTMLResponse]:
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

    try:
        responder_map = await client.get_agent_map()
    except Exception:
        responder_map = {}

    mappings = await client.get_ticket_field_mappings()
    status_map = mappings.get("status", {})
    priority_map = mappings.get("priority", {})

    tickets = await client.list_tickets_for_requester(
        requester_email=requester_email,
        page=page,
        per_page=per_page,
    )

    raw_page_size = len(tickets)

    cutoff = datetime.now(timezone.utc) - timedelta(days=recent_days)

    # Teams 탭에서 쓰기 좋은 형태로 최소 필드만 반환
    items = []
    for t in tickets:
        updated_at = _parse_iso_datetime(t.get("updated_at"))
        created_at = _parse_iso_datetime(t.get("created_at"))
        when = updated_at or created_at

        if when and when < cutoff:
            continue

        status_value = t.get("status")
        priority_value = t.get("priority")
        status_code = None
        priority_code = None
        try:
            status_code = int(status_value)
        except Exception:
            status_code = None
        try:
            priority_code = int(priority_value)
        except Exception:
            priority_code = None

        responder_id = t.get("responder_id")
        responder_name = None
        if responder_id is not None:
            responder_name = responder_map.get(str(responder_id))

        items.append(
            {
                "id": t.get("id"),
                "subject": t.get("subject"),
                "status": status_map.get(status_code, status_value),
                "priority": priority_map.get(priority_code, priority_value),
                "responder_id": responder_id,
                "responder_name": responder_name,
                "created_at": t.get("created_at"),
                "updated_at": t.get("updated_at"),
                "is_done": _is_done(t.get("status")),
            }
        )

    # HTMX Response
    if request.headers.get("HX-Request"):
        rows_html = ""
        if not items:
            rows_html = '<tr><td colspan="5" class="muted" style="text-align:center; padding: 20px;">요청 내역이 없습니다.</td></tr>'
        else:
            for item in items:
                status_class = "done" if item["is_done"] else "open"
                updated_str = _parse_iso_datetime(item["updated_at"]).strftime("%Y-%m-%d %H:%M") if item["updated_at"] else "-"
                assignee_str = item.get("responder_name") or "-"
                
                rows_html += f"""
                <tr style="cursor:pointer;" 
                    hx-get="/api/freshdesk/requests/{item['id']}" 
                    hx-target="#detail-container"
                    hx-trigger="click"
                    onclick="document.querySelectorAll('tbody tr').forEach(tr => tr.style.background=''); this.style.background='#f0f0f0';">
                    <td class="col-title">
                        <div class="title-main">{item['subject']}</div>
                        <div class="muted">#{item['id']}</div>
                    </td>
                    <td class="col-assignee muted" title="{assignee_str}">{assignee_str}</td>
                    <td class="col-updated muted">{updated_str}</td>
                    <td class="col-status"><span class="pill {status_class}">{item['status']}</span></td>
                    <td class="col-action"><button class="btn ghost" style="padding:4px 8px; font-size:12px;">상세보기</button></td>
                </tr>
                """
        
        # Pagination Controls
        prev_disabled = "disabled" if page <= 1 else ""
        next_disabled = "disabled" if raw_page_size < per_page else ""
        
        pagination_oob = f"""
        <div id=\"list-pagination\" hx-swap-oob=\"true\" style=\"display:flex; justify-content:center; gap:10px; align-items:center; padding-top:10px;\">
            <button class=\"btn ghost\" 
                hx-get=\"/api/freshdesk/requests?page={page-1}&per_page={per_page}\" 
                hx-target=\"#list-table\" 
                {prev_disabled}>
                &lt; 이전
            </button>
            <span class=\"muted\">Page {page}</span>
            <button class=\"btn ghost\" 
                hx-get=\"/api/freshdesk/requests?page={page+1}&per_page={per_page}\" 
                hx-target=\"#list-table\" 
                {next_disabled}>
                다음 &gt;
            </button>
        </div>
        """
        
        return HTMLResponse(content=f"""
            <table>
                <thead>
                    <tr>
                        <th class="th-title">제목</th>
                        <th class="th-assignee">담당자</th>
                        <th class="th-updated">업데이트</th>
                        <th class="th-status">상태</th>
                        <th class="th-action">상세보기</th>
                    </tr>
                </thead>
                <tbody>
                    {rows_html}
                </tbody>
            </table>
            {pagination_oob}
        """)

    return {
        "email": requester_email,
        "page": page,
        "per_page": per_page,
        "recent_days": recent_days,
        "items": items,
    }


@router.get("/requests/{ticket_id}", response_model=Union[dict, str])
async def get_request_detail(
    request: Request,
    ticket_id: str,
    ctx: tuple[str, str] = Depends(get_request_context),
) -> Union[dict, HTMLResponse]:
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

    mappings = await client.get_ticket_field_mappings()
    status_map = mappings.get("status", {})
    priority_map = mappings.get("priority", {})

    ticket = await client.view_ticket(ticket_id=ticket_id, include_requester=True)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    requester = ticket.get("requester") if isinstance(ticket.get("requester"), dict) else {}
    ticket_requester_email = (requester.get("email") or "").lower()

    # POC 보안: 최소한의 소유권 체크 (운영형에서는 Teams SSO 검증으로 교체)
    if ticket_requester_email and ticket_requester_email != requester_email.lower():
        raise HTTPException(status_code=403, detail="Forbidden (not your ticket)")

    status_value = ticket.get("status")
    priority_value = ticket.get("priority")
    status_code = None
    priority_code = None
    try:
        status_code = int(status_value)
    except Exception:
        status_code = None
    try:
        priority_code = int(priority_value)
    except Exception:
        priority_code = None

    responder_name = None
    if ticket.get("responder_id") is not None:
        try:
            responder_map = await client.get_agent_map()
        except Exception:
            responder_map = {}
        responder_name = responder_map.get(str(ticket.get("responder_id")))

    # HTMX Response
    if request.headers.get("HX-Request"):
        updated_str = _parse_iso_datetime(ticket.get("updated_at")).strftime("%Y-%m-%d %H:%M") if ticket.get("updated_at") else "-"
        status_display = status_map.get(status_code, status_value)
        priority_display = priority_map.get(priority_code, priority_value)
        is_done = _is_done(ticket.get("status"))
        
        inquiry_section = ""
        if not is_done:
            inquiry_section = f"""
            <div style="margin-top:14px;">
                <div class="row" style="margin-bottom:8px;">
                    <h2 style="font-size:14px; margin:0;">추가 문의(공개 메모)</h2>
                </div>
                <form hx-post="/api/freshdesk/requests/{ticket_id}/inquiry" hx-target="#inquiry-result">
                    <textarea name="body" placeholder="추가로 전달할 내용을 입력하세요." style="width:100%; min-height:80px; padding:8px; border:1px solid #ddd; border-radius:4px;"></textarea>
                    <div class="muted" style="margin-top:6px; font-size:12px;">추가 문의는 티켓에 공개 메모로 기록됩니다.</div>
                    <div style="margin-top:10px; text-align:right;">
                        <button type="submit" class="btn primary">보내기</button>
                    </div>
                </form>
                <div id="inquiry-result"></div>
            </div>
            """
        else:
            inquiry_section = '<div class="muted" style="margin-top:20px; padding:10px; background:#f5f5f5; border-radius:4px;">* 종료된 티켓에는 문의를 추가할 수 없습니다.</div>'

        return HTMLResponse(content=f"""
            <div class="row">
                <h1 style="margin-bottom:0;">상세 #{ticket.get('id')}</h1>
                <div class="spacer"></div>
            </div>

            <h2 style="margin-top:10px; font-size:16px;">{ticket.get('subject')}</h2>
            
            <div class="kv">
                <div class="k">상태</div><div>{status_display}</div>
                <div class="k">우선순위</div><div>{priority_display}</div>
                <div class="k">담당자</div><div>{responder_name or '-'}</div>
                <div class="k">업데이트</div><div>{updated_str}</div>
            </div>

            <div class="desc" style="margin-top:12px; padding:12px; background:#f7f7f7; border-radius:8px; font-size:13px; white-space:pre-wrap; max-height:300px; overflow:auto;">
                {ticket.get('description_text') or '(내용 없음)'}
            </div>

            {inquiry_section}
        """)

    return {
        "id": ticket.get("id"),
        "subject": ticket.get("subject"),
        "description_text": ticket.get("description_text"),
        "status": status_map.get(status_code, status_value),
        "priority": priority_map.get(priority_code, priority_value),
        "responder_id": ticket.get("responder_id"),
        "responder_name": responder_name,
        "cc_emails": ticket.get("cc_emails") or [],
        "custom_fields": ticket.get("custom_fields") or {},
        "created_at": ticket.get("created_at"),
        "updated_at": ticket.get("updated_at"),
        "is_done": _is_done(ticket.get("status")),
        "requester": {"email": requester.get("email"), "name": requester.get("name")},
    }


@router.post("/requests/{ticket_id}/inquiry", response_model=Union[dict, str])
async def add_inquiry(
    request: Request,
    ticket_id: str,
    req: InquiryRequest = None,  # Optional for Form Data
    ctx: tuple[str, str] = Depends(get_request_context),
) -> Union[dict, HTMLResponse]:
    teams_tenant_id, requester_email = ctx

    # Handle Form Data for HTMX
    body_text = ""
    if request.headers.get("HX-Request"):
        form = await request.form()
        body_text = form.get("body", "")
    elif req:
        body_text = req.body
    
    body_text = (body_text or "").strip()
    if not body_text:
        raise HTTPException(status_code=400, detail="Body is required")

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

    # 누가 남겼는지 명확히 남기기 (운영형에서는 UI/SSO 기반으로 더 정교화)
    note_body = f"{body_text}"
    requester_id = None
    try:
        requester_id = int(requester.get("id")) if requester.get("id") is not None else None
    except Exception:
        requester_id = None
    ok = await client.add_public_inquiry_note(
        ticket_id=ticket_id,
        body=note_body,
        user_id=requester_id,
    )

    if not ok:
        raise HTTPException(status_code=500, detail="Failed to add inquiry")

    if request.headers.get("HX-Request"):
        return HTMLResponse('<div class="success">문의가 등록되었습니다.</div>')

    return {"ok": True, "ticket_id": ticket_id}
