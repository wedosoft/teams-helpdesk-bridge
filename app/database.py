"""Supabase 데이터베이스 클라이언트"""
import uuid
from functools import lru_cache
from typing import Optional

from supabase import create_client, Client

from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger(__name__)


@lru_cache
def get_supabase_client() -> Client:
    """캐시된 Supabase 클라이언트 반환"""
    settings = get_settings()
    return create_client(settings.supabase_url, settings.supabase_key)


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

    # ===== Storage =====

    async def upload_to_storage(
        self,
        file_buffer: bytes,
        filename: str,
        content_type: str,
        bucket: str = "attachments",
    ) -> Optional[str]:
        """
        Supabase Storage에 파일 업로드 후 공개 URL 반환

        Args:
            file_buffer: 파일 바이너리 데이터
            filename: 원본 파일명
            content_type: MIME 타입
            bucket: 스토리지 버킷 이름

        Returns:
            공개 URL 또는 None (실패 시)
        """
        try:
            # 고유한 파일 경로 생성 (UUID + 확장자)
            unique_id = uuid.uuid4().hex[:12]

            # 파일 확장자 추출
            ext = ""
            if "." in filename:
                ext = "." + filename.rsplit(".", 1)[-1].lower()
                # 확장자도 ASCII만 허용
                if not ext[1:].isascii() or not ext[1:].isalnum():
                    ext = self._get_extension_from_content_type(content_type)

            # Supabase Storage는 ASCII 파일명만 허용
            # 한글 등 비-ASCII 문자가 포함된 파일명은 UUID + 확장자로 대체
            file_path = f"{unique_id}{ext}"

            # Storage에 업로드
            self.client.storage.from_(bucket).upload(
                path=file_path,
                file=file_buffer,
                file_options={"content-type": content_type},
            )

            # 공개 URL 생성
            settings = get_settings()
            public_url = f"{settings.supabase_url}/storage/v1/object/public/{bucket}/{file_path}"

            logger.info(
                "Uploaded file to storage",
                bucket=bucket,
                file_path=file_path,
                content_type=content_type,
            )

            return public_url

        except Exception as e:
            logger.error(
                "Failed to upload to storage",
                error=str(e),
                filename=filename,
                bucket=bucket,
            )
            return None

    def _get_extension_from_content_type(self, content_type: str) -> str:
        """content_type에서 파일 확장자 추론"""
        ext_map = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/bmp": ".bmp",
            "image/svg+xml": ".svg",
            "application/pdf": ".pdf",
            "application/zip": ".zip",
        }
        return ext_map.get(content_type, "")
