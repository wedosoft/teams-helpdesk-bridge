"""Microsoft OAuth 인증 처리

관리자 포털 로그인을 위한 Microsoft OAuth 2.0 플로우:
1. /admin/login - Azure AD 로그인 페이지로 리다이렉트
2. /admin/callback - OAuth 콜백 처리, 토큰에서 tenant_id 추출
3. 세션 생성 후 설정 페이지로 리다이렉트
"""
import secrets
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse, HTMLResponse
from jose import jwt, JWTError

from app.config import get_settings
from app.utils.logger import get_logger

router = APIRouter()
logger = get_logger(__name__)

# 간단한 인메모리 세션 저장소 (프로덕션에서는 Redis 등 사용)
# state -> redirect_url 매핑 (CSRF 방지)
oauth_states: dict[str, dict] = {}

# session_id -> session_data 매핑
admin_sessions: dict[str, dict] = {}


# ===== OAuth 설정 =====

AZURE_AD_AUTHORITY = "https://login.microsoftonline.com/common"
AZURE_AD_TOKEN_URL = f"{AZURE_AD_AUTHORITY}/oauth2/v2.0/token"
AZURE_AD_AUTHORIZE_URL = f"{AZURE_AD_AUTHORITY}/oauth2/v2.0/authorize"

# Microsoft Graph User.Read 스코프로 사용자 정보 조회
OAUTH_SCOPES = "openid profile email User.Read"


def get_oauth_config():
    """OAuth 설정 반환"""
    settings = get_settings()

    # redirect_uri가 설정되지 않은 경우 public_url 기반으로 생성
    redirect_uri = settings.oauth_redirect_uri
    if not redirect_uri:
        redirect_uri = f"{settings.public_url}/admin/callback"

    return {
        "client_id": settings.bot_app_id,
        "client_secret": settings.bot_app_password,
        "redirect_uri": redirect_uri,
    }


# ===== 세션 관리 =====

def create_session(tenant_id: str, user_info: dict) -> str:
    """관리자 세션 생성"""
    session_id = secrets.token_urlsafe(32)
    admin_sessions[session_id] = {
        "tenant_id": tenant_id,
        "user_info": user_info,
        "created_at": datetime.utcnow(),
        "expires_at": datetime.utcnow() + timedelta(hours=24),
    }
    return session_id


def get_session(session_id: str) -> Optional[dict]:
    """세션 조회"""
    if not session_id:
        return None

    session = admin_sessions.get(session_id)
    if not session:
        return None

    # 만료 체크
    if datetime.utcnow() > session["expires_at"]:
        del admin_sessions[session_id]
        return None

    return session


def get_session_from_cookie(request: Request) -> Optional[dict]:
    """쿠키에서 세션 조회"""
    session_id = request.cookies.get("admin_session")
    return get_session(session_id)


# ===== OAuth 라우트 =====

@router.get("/login")
async def admin_login(request: Request):
    """Microsoft OAuth 로그인 시작

    Azure AD 로그인 페이지로 리다이렉트
    """
    oauth_config = get_oauth_config()

    if not oauth_config["client_id"]:
        raise HTTPException(
            status_code=500,
            detail="OAuth not configured. Set BOT_APP_ID and BOT_APP_PASSWORD.",
        )

    # CSRF 방지용 state 생성
    state = secrets.token_urlsafe(32)
    oauth_states[state] = {
        "created_at": datetime.utcnow(),
        "redirect_url": "/admin/setup",  # 로그인 후 리다이렉트할 URL
    }

    # Azure AD 로그인 URL 생성
    params = {
        "client_id": oauth_config["client_id"],
        "response_type": "code",
        "redirect_uri": oauth_config["redirect_uri"],
        "response_mode": "query",
        "scope": OAUTH_SCOPES,
        "state": state,
        "prompt": "select_account",  # 계정 선택 화면 표시
    }

    auth_url = f"{AZURE_AD_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    logger.info("Redirecting to Azure AD login", redirect_uri=oauth_config["redirect_uri"])

    return RedirectResponse(url=auth_url)


@router.get("/callback")
async def oauth_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
):
    """OAuth 콜백 처리

    Azure AD에서 인증 후 리다이렉트됨
    """
    # 에러 처리
    if error:
        logger.error("OAuth error", error=error, description=error_description)
        return HTMLResponse(
            content=f"""
            <html>
            <head><title>Login Failed</title></head>
            <body style="font-family: 'Segoe UI', sans-serif; padding: 40px; text-align: center;">
                <h1>로그인 실패</h1>
                <p style="color: #c62828;">{error_description or error}</p>
                <a href="/admin/login">다시 시도</a>
            </body>
            </html>
            """,
            status_code=400,
        )

    # state 검증
    if not state or state not in oauth_states:
        raise HTTPException(status_code=400, detail="Invalid state parameter")

    state_data = oauth_states.pop(state)

    # state 만료 체크 (10분)
    if datetime.utcnow() - state_data["created_at"] > timedelta(minutes=10):
        raise HTTPException(status_code=400, detail="State expired")

    if not code:
        raise HTTPException(status_code=400, detail="Authorization code not provided")

    oauth_config = get_oauth_config()

    # 토큰 교환
    async with httpx.AsyncClient() as client:
        token_response = await client.post(
            AZURE_AD_TOKEN_URL,
            data={
                "client_id": oauth_config["client_id"],
                "client_secret": oauth_config["client_secret"],
                "code": code,
                "redirect_uri": oauth_config["redirect_uri"],
                "grant_type": "authorization_code",
            },
        )

    if token_response.status_code != 200:
        logger.error(
            "Token exchange failed",
            status=token_response.status_code,
            body=token_response.text,
        )
        raise HTTPException(status_code=400, detail="Failed to exchange token")

    token_data = token_response.json()
    id_token = token_data.get("id_token")
    access_token = token_data.get("access_token")

    if not id_token:
        raise HTTPException(status_code=400, detail="No ID token received")

    # ID 토큰 디코딩 (검증 없이 - Azure AD가 이미 검증)
    try:
        # 서명 검증 없이 디코딩 (Azure AD에서 직접 받은 토큰)
        claims = jwt.get_unverified_claims(id_token)
    except JWTError as e:
        logger.error("Failed to decode ID token", error=str(e))
        raise HTTPException(status_code=400, detail="Invalid ID token")

    # 테넌트 ID 추출
    tenant_id = claims.get("tid")
    if not tenant_id:
        raise HTTPException(status_code=400, detail="Tenant ID not found in token")

    # 사용자 정보
    user_info = {
        "email": claims.get("preferred_username") or claims.get("email"),
        "name": claims.get("name"),
        "oid": claims.get("oid"),  # Object ID
    }

    logger.info(
        "OAuth login successful",
        tenant_id=tenant_id,
        email=user_info["email"],
    )

    # 세션 생성
    session_id = create_session(tenant_id, user_info)

    # 설정 페이지로 리다이렉트
    redirect_url = state_data.get("redirect_url", "/admin/setup")
    response = RedirectResponse(url=redirect_url, status_code=302)

    # 세션 쿠키 설정
    response.set_cookie(
        key="admin_session",
        value=session_id,
        httponly=True,
        secure=True,  # HTTPS에서만
        samesite="lax",
        max_age=86400,  # 24시간
    )

    return response


@router.get("/logout")
async def admin_logout(request: Request):
    """로그아웃"""
    session_id = request.cookies.get("admin_session")

    if session_id and session_id in admin_sessions:
        del admin_sessions[session_id]

    response = RedirectResponse(url="/admin/login", status_code=302)
    response.delete_cookie("admin_session")

    return response


@router.get("/me")
async def get_current_admin(request: Request):
    """현재 로그인한 관리자 정보"""
    session = get_session_from_cookie(request)

    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")

    return {
        "tenant_id": session["tenant_id"],
        "user": session["user_info"],
    }
