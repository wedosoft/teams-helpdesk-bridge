"""FastAPI 앱 진입점"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

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

# Freshchat Webhook
from app.adapters.freshchat.routes import router as freshchat_router
app.include_router(freshchat_router, prefix=f"{API_PREFIX}/webhook/freshchat", tags=["Freshchat"])

# Zendesk Webhook (Phase 2)
# from app.adapters.zendesk.routes import router as zendesk_router
# app.include_router(zendesk_router, prefix=f"{API_PREFIX}/webhook/zendesk", tags=["Zendesk"])


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
