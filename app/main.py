"""FastAPI 앱 진입점"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.utils.logger import setup_logging, get_logger

# 로깅 설정
setup_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 라이프사이클 관리"""
    settings = get_settings()
    logger.info("Starting Teams-Helpdesk Bridge", port=settings.port)
    yield
    logger.info("Shutting down Teams-Helpdesk Bridge")


app = FastAPI(
    title="Teams-Helpdesk Bridge",
    description="MS Teams와 헬프데스크 솔루션 간 양방향 채팅 브릿지",
    version="0.1.0",
    lifespan=lifespan,
)


# API prefix
API_PREFIX = "/api"

# Root: 사람이 브라우저에서 열었을 때 404 혼선 방지
@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/admin/setup", status_code=302)


# ===== Health Check =====

@app.get(f"{API_PREFIX}/")
async def health_check():
    """헬스 체크"""
    return {
        "status": "ok",
        "service": "teams-helpdesk-bridge",
        "version": "0.1.0",
    }


@app.get(f"{API_PREFIX}/health")
async def health():
    """상세 헬스 체크"""
    return {
        "status": "healthy",
        "components": {
            "api": "ok",
            "database": "ok",  # TODO: 실제 DB 연결 체크
        },
    }


# ===== 라우터 등록 =====

# Teams Bot
from app.teams.routes import router as teams_router
app.include_router(teams_router, prefix=f"{API_PREFIX}/bot", tags=["Teams Bot"])

# Azure Bot Service 기본 엔드포인트 별칭
# (Azure Portal에서 messaging endpoint를 /api/messages로 두는 경우가 많아 PoC 편의상 제공)
from app.teams.routes import bot_messages as _bot_messages_handler


@app.post(f"{API_PREFIX}/messages")
async def bot_messages_alias(request: Request):
    return await _bot_messages_handler(request)

# Freshchat Webhook
from app.adapters.freshchat.routes import router as freshchat_router
app.include_router(freshchat_router, prefix=f"{API_PREFIX}/webhook/freshchat", tags=["Freshchat"])

# Zendesk Webhook
from app.adapters.zendesk.routes import router as zendesk_router
app.include_router(zendesk_router, prefix=f"{API_PREFIX}/webhook/zendesk", tags=["Zendesk"])

# Freshdesk Webhook
from app.adapters.freshdesk.routes import router as freshdesk_router
app.include_router(freshdesk_router, prefix=f"{API_PREFIX}/webhook/freshdesk", tags=["Freshdesk"])

# Freshdesk Requester API (Teams dashboard)
from app.adapters.freshdesk.requester_routes import router as freshdesk_requester_router
app.include_router(freshdesk_requester_router, prefix=f"{API_PREFIX}/freshdesk", tags=["Freshdesk Requester"])

# Admin API (Teams Tab 설정용)
from app.admin.routes import router as admin_router
app.include_router(admin_router, prefix=f"{API_PREFIX}/admin", tags=["Admin"])

# Admin OAuth (관리자 포털 인증)
from app.admin.oauth import router as oauth_router
app.include_router(oauth_router, prefix="/admin", tags=["Admin OAuth"])


# ===== 정적 파일 및 Tab 페이지 =====

# 정적 파일 디렉토리
STATIC_DIR = Path(__file__).parent / "static"

# Tab 설정 페이지
@app.get("/tab/config")
async def tab_config():
    """Teams Tab 설정 페이지"""
    return FileResponse(STATIC_DIR / "config.html")


@app.get("/tab/content")
async def tab_content():
    """Teams Tab 콘텐츠 페이지 (설정 완료 후 표시)"""
    return FileResponse(STATIC_DIR / "content.html")

@app.get("/tab/requests")
async def tab_requests():
    """요청자 대시보드 (내 요청함)"""
    return FileResponse(STATIC_DIR / "requests.html")


@app.get("/admin/setup")
async def admin_setup():
    """관리자 설정 페이지 (OAuth 로그인 후)"""
    return FileResponse(STATIC_DIR / "admin-setup.html")


# 정적 파일 서빙 (CSS, JS 등)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ===== 에러 핸들러 =====

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """전역 예외 핸들러"""
    logger.error(
        "Unhandled exception",
        path=request.url.path,
        method=request.method,
        error=str(exc),
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run(app, host="0.0.0.0", port=settings.port)
