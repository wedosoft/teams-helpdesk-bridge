"""MS Graph API 클라이언트"""
from datetime import datetime, timedelta
from typing import Optional

import httpx

from app.config import get_settings
from app.core.models import User
from app.database import Database
from app.utils.logger import get_logger

logger = get_logger(__name__)

# 프로필 캐시 TTL (24시간)
PROFILE_CACHE_TTL = timedelta(hours=24)


class GraphClient:
    """MS Graph API 클라이언트"""

    def __init__(self):
        self.settings = get_settings()
        self._access_token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None
        self.db = Database()

    async def _get_access_token(self) -> str:
        """앱 전용 액세스 토큰 획득"""
        # 토큰이 유효하면 재사용
        if (
            self._access_token
            and self._token_expires_at
            and datetime.now() < self._token_expires_at - timedelta(minutes=5)
        ):
            return self._access_token

        # 새 토큰 요청
        token_url = (
            f"https://login.microsoftonline.com/"
            f"{self.settings.bot_tenant_id}/oauth2/v2.0/token"
        )

        async with httpx.AsyncClient() as client:
            response = await client.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.settings.bot_app_id,
                    "client_secret": self.settings.bot_app_password,
                    "scope": "https://graph.microsoft.com/.default",
                },
            )
            response.raise_for_status()
            data = response.json()

        self._access_token = data["access_token"]
        self._token_expires_at = datetime.now() + timedelta(
            seconds=data.get("expires_in", 3600)
        )

        return self._access_token

    async def get_user_profile(self, user_id: str) -> Optional[User]:
        """사용자 프로필 조회 (캐시 포함)"""
        # 캐시 확인
        cached = await self.db.get_user_profile(user_id)
        if cached:
            cached_at = datetime.fromisoformat(cached["cached_at"].replace("Z", "+00:00"))
            if datetime.now(cached_at.tzinfo) - cached_at < PROFILE_CACHE_TTL:
                logger.debug("Using cached user profile", user_id=user_id)
                return User(
                    id=user_id,
                    name=cached.get("display_name"),
                    email=cached.get("email"),
                    job_title=cached.get("job_title"),
                    department=cached.get("department"),
                )

        # Graph API 호출
        try:
            token = await self._get_access_token()

            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"https://graph.microsoft.com/v1.0/users/{user_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"$select": "displayName,mail,jobTitle,department"},
                )

                if response.status_code == 404:
                    logger.warning("User not found in Graph API", user_id=user_id)
                    return None

                response.raise_for_status()
                data = response.json()

            # 캐시 저장
            await self.db.upsert_user_profile({
                "teams_user_id": user_id,
                "display_name": data.get("displayName"),
                "email": data.get("mail"),
                "job_title": data.get("jobTitle"),
                "department": data.get("department"),
                "cached_at": datetime.now().isoformat(),
            })

            logger.info("Fetched user profile from Graph API", user_id=user_id)

            return User(
                id=user_id,
                name=data.get("displayName"),
                email=data.get("mail"),
                job_title=data.get("jobTitle"),
                department=data.get("department"),
            )

        except Exception as e:
            logger.error("Failed to fetch user profile", user_id=user_id, error=str(e))
            return None
