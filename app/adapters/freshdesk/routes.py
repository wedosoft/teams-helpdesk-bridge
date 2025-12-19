"""Freshdesk Webhook 라우트 (멀티테넌트)

URL 형식: /api/webhook/freshdesk/{teams_tenant_id}
"""

from fastapi import APIRouter, Request, Response, HTTPException, Path

from app.core.tenant import get_tenant_service, Platform
from app.core.platform_factory import get_platform_factory
from app.core.router import get_message_router
from app.utils.logger import get_logger

router = APIRouter()
logger = get_logger(__name__)


@router.post("/{teams_tenant_id}")
async def freshdesk_webhook(
    request: Request,
    teams_tenant_id: str = Path(..., description="Teams 테넌트 ID"),
) -> Response:
    try:
        tenant_service = get_tenant_service()
        tenant = await tenant_service.get_tenant(teams_tenant_id)

        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")

        if tenant.platform != Platform.FRESHDESK:
            raise HTTPException(status_code=400, detail="Tenant is not using Freshdesk")

        payload = await request.json()

        factory = get_platform_factory()
        webhook_handler = factory.get_webhook_handler(tenant)
        if not webhook_handler:
            logger.error("No webhook handler for tenant")
            return Response(status_code=200)

        event = webhook_handler.parse_webhook(payload)
        if not event:
            return Response(status_code=200)

        message_router = get_message_router()
        await message_router.handle_webhook(tenant, event)

        return Response(status_code=200)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Freshdesk webhook error", error=str(e), teams_tenant_id=teams_tenant_id)
        return Response(status_code=500)


@router.get("/health")
async def webhook_health() -> dict:
    return {
        "status": "ok",
        "webhook": "freshdesk",
        "multi_tenant": True,
    }

