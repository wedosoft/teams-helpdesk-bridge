"""Supabase 데이터베이스 클라이언트"""
from functools import lru_cache
from typing import Optional

from supabase import create_client, Client

from app.config import get_settings


@lru_cache
def get_supabase_client() -> Client:
    """캐시된 Supabase 클라이언트 반환"""
    settings = get_settings()
    return create_client(settings.supabase_url, settings.supabase_service_role_key)


class Database:
    """데이터베이스 작업 클래스"""

    def __init__(self):
        self.client = get_supabase_client()

    # ===== Conversations =====

    async def get_conversation_by_teams_id(
        self, teams_conversation_id: str, platform: str
    ) -> Optional[dict]:
        """Teams 대화 ID로 매핑 조회"""
        result = (
            self.client.table("conversations")
            .select("*")
            .eq("teams_conversation_id", teams_conversation_id)
            .eq("platform", platform)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    async def get_conversation_by_platform_id(
        self, platform_conversation_id: str, platform: str
    ) -> Optional[dict]:
        """플랫폼 대화 ID로 매핑 조회"""
        result = (
            self.client.table("conversations")
            .select("*")
            .eq("platform_conversation_id", platform_conversation_id)
            .eq("platform", platform)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    async def upsert_conversation(self, data: dict) -> dict:
        """대화 매핑 생성/업데이트"""
        result = (
            self.client.table("conversations")
            .upsert(data, on_conflict="teams_conversation_id,platform")
            .execute()
        )
        return result.data[0] if result.data else {}

    async def update_conversation_resolved(
        self, platform_conversation_id: str, platform: str, is_resolved: bool
    ) -> None:
        """대화 해결 상태 업데이트"""
        self.client.table("conversations").update(
            {"is_resolved": is_resolved}
        ).eq("platform_conversation_id", platform_conversation_id).eq(
            "platform", platform
        ).execute()

    # ===== User Profiles =====

    async def get_user_profile(self, teams_user_id: str) -> Optional[dict]:
        """사용자 프로필 조회"""
        result = (
            self.client.table("user_profiles")
            .select("*")
            .eq("teams_user_id", teams_user_id)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    async def upsert_user_profile(self, data: dict) -> dict:
        """사용자 프로필 생성/업데이트"""
        result = (
            self.client.table("user_profiles")
            .upsert(data, on_conflict="teams_user_id")
            .execute()
        )
        return result.data[0] if result.data else {}

    # ===== Tenants =====

    async def get_tenant_by_teams_id(self, teams_tenant_id: str) -> Optional[dict]:
        """Teams 테넌트 ID로 설정 조회"""
        result = (
            self.client.table("tenants")
            .select("*")
            .eq("teams_tenant_id", teams_tenant_id)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    async def upsert_tenant(self, data: dict) -> dict:
        """테넌트 생성/업데이트"""
        result = (
            self.client.table("tenants")
            .upsert(data, on_conflict="teams_tenant_id")
            .execute()
        )
        return result.data[0] if result.data else {}

    async def update_tenant(self, teams_tenant_id: str, data: dict) -> None:
        """테넌트 업데이트"""
        self.client.table("tenants").update(data).eq(
            "teams_tenant_id", teams_tenant_id
        ).execute()

    async def delete_tenant(self, teams_tenant_id: str) -> None:
        """테넌트 삭제"""
        self.client.table("tenants").delete().eq(
            "teams_tenant_id", teams_tenant_id
        ).execute()
