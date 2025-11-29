"""Microsoft Graph API 서비스

멀티테넌트 앱에서 고객사 테넌트의 사용자 정보 조회
- 관리자 동의 후 client_credentials 흐름으로 토큰 획득
- 사용자 프로필 확장 정보 조회 (jobTitle, department, phone 등)
"""
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger(__name__)


# 토큰 캐시 TTL (50분 - 토큰은 1시간 유효)
TOKEN_CACHE_TTL = 50 * 60


@dataclass
class GraphUserProfile:
    """Graph API에서 조회한 사용자 프로필"""
    display_name: Optional[str] = None
    email: Optional[str] = None
    job_title: Optional[str] = None
    department: Optional[str] = None
    mobile_phone: Optional[str] = None
    office_phone: Optional[str] = None
    office_location: Optional[str] = None


@dataclass
class CachedToken:
    """캐시된 액세스 토큰"""
    token: str
    expires_at: float


class GraphService:
    """Microsoft Graph API 서비스

    멀티테넌트 앱에서 고객사 테넌트 사용자 정보 조회
    """

    def __init__(self):
        settings = get_settings()
        self._client_id = settings.bot_app_id
        self._client_secret = settings.bot_app_password

        # 테넌트별 토큰 캐시
        self._token_cache: dict[str, CachedToken] = {}

    async def get_access_token(self, tenant_id: str) -> Optional[str]:
        """
        특정 테넌트에 대한 Graph API 액세스 토큰 획득

        Args:
            tenant_id: 고객사 테넌트 ID (Azure AD tenant ID)

        Returns:
            액세스 토큰 또는 None (권한 없음)
        """
        # 캐시 확인
        cached = self._token_cache.get(tenant_id)
        if cached and time.time() < cached.expires_at:
            return cached.token

        try:
            token_endpoint = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    token_endpoint,
                    data={
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "scope": "https://graph.microsoft.com/.default",
                        "grant_type": "client_credentials",
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )

                if response.status_code != 200:
                    error_data = response.json() if response.content else {}
                    error_code = error_data.get("error", "unknown")
                    error_desc = error_data.get("error_description", "")

                    # AADSTS65001: 관리자 동의가 필요한 경우
                    if "AADSTS65001" in error_desc or "consent" in error_desc.lower():
                        logger.warning(
                            "Admin consent required for tenant",
                            tenant_id=tenant_id,
                        )
                    else:
                        logger.warning(
                            "Failed to get Graph token",
                            tenant_id=tenant_id,
                            error=error_code,
                            description=error_desc[:200],
                        )
                    return None

                data = response.json()
                access_token = data.get("access_token")
                expires_in = data.get("expires_in", 3600)

                # 캐시 저장
                self._token_cache[tenant_id] = CachedToken(
                    token=access_token,
                    expires_at=time.time() + min(expires_in - 60, TOKEN_CACHE_TTL),
                )

                logger.debug("Graph token acquired", tenant_id=tenant_id)
                return access_token

        except Exception as e:
            logger.error(
                "Error acquiring Graph token",
                tenant_id=tenant_id,
                error=str(e),
            )
            return None

    async def get_user_profile(
        self,
        tenant_id: str,
        aad_object_id: str,
    ) -> Optional[GraphUserProfile]:
        """
        Graph API로 사용자 프로필 조회

        Args:
            tenant_id: 고객사 테넌트 ID
            aad_object_id: 사용자의 Azure AD Object ID

        Returns:
            GraphUserProfile 또는 None
        """
        token = await self.get_access_token(tenant_id)
        if not token:
            return None

        try:
            select_fields = ",".join([
                "displayName",
                "mail",
                "userPrincipalName",
                "jobTitle",
                "department",
                "mobilePhone",
                "businessPhones",
                "officeLocation",
            ])

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"https://graph.microsoft.com/v1.0/users/{aad_object_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"$select": select_fields},
                )

                if response.status_code != 200:
                    logger.warning(
                        "Failed to get user profile from Graph",
                        aad_object_id=aad_object_id,
                        status=response.status_code,
                    )
                    return None

                data = response.json()

                # 비즈니스 전화번호 추출
                business_phones = data.get("businessPhones", [])
                office_phone = business_phones[0] if business_phones else None

                profile = GraphUserProfile(
                    display_name=data.get("displayName"),
                    email=data.get("mail") or data.get("userPrincipalName"),
                    job_title=data.get("jobTitle"),
                    department=data.get("department"),
                    mobile_phone=data.get("mobilePhone"),
                    office_phone=office_phone,
                    office_location=data.get("officeLocation"),
                )

                logger.debug(
                    "User profile retrieved from Graph",
                    aad_object_id=aad_object_id,
                    has_job_title=bool(profile.job_title),
                    has_department=bool(profile.department),
                )

                return profile

        except Exception as e:
            logger.error(
                "Error fetching user profile from Graph",
                aad_object_id=aad_object_id,
                error=str(e),
            )
            return None

    def get_admin_consent_url(self, tenant_id: str, redirect_uri: str) -> str:
        """
        관리자 동의 URL 생성

        Args:
            tenant_id: 대상 테넌트 ID
            redirect_uri: 동의 후 리디렉션 URL

        Returns:
            관리자 동의 URL
        """
        # User.Read.All 권한 요청
        scope = "https://graph.microsoft.com/.default"

        return (
            f"https://login.microsoftonline.com/{tenant_id}/adminconsent"
            f"?client_id={self._client_id}"
            f"&redirect_uri={redirect_uri}"
            f"&scope={scope}"
        )

    async def check_consent_status(self, tenant_id: str) -> bool:
        """
        테넌트에 대한 관리자 동의 상태 확인

        Args:
            tenant_id: 테넌트 ID

        Returns:
            동의 완료 여부
        """
        token = await self.get_access_token(tenant_id)
        return token is not None

    def invalidate_token_cache(self, tenant_id: str) -> None:
        """특정 테넌트의 토큰 캐시 무효화"""
        self._token_cache.pop(tenant_id, None)


# ===== 싱글톤 인스턴스 =====

_graph_service: Optional[GraphService] = None


def get_graph_service() -> GraphService:
    """GraphService 싱글톤 인스턴스 반환"""
    global _graph_service
    if _graph_service is None:
        _graph_service = GraphService()
    return _graph_service
