"""Freshchat API 클라이언트

Express poc-bridge.js의 FreshchatClient 클래스를 Python으로 포팅
주요 기능:
- 사용자 조회/생성/업데이트
- 대화 생성 및 메시지 전송
- 파일 업로드/다운로드
- 상담원 정보 조회 (캐시)
- 대화 상태 확인 및 resolved 자동 복구
"""
from datetime import datetime, timedelta
from typing import Any, Optional
import asyncio
import re

import httpx

from app.utils.logger import get_logger

logger = get_logger(__name__)


# 상담원 캐시 TTL (30분)
AGENT_CACHE_TTL = timedelta(minutes=30)


class FreshchatClient:
    """Freshchat API 클라이언트"""

    def __init__(self, api_key: str, api_url: str, inbox_id: str):
        self.api_key = api_key
        self.api_url = api_url.rstrip("/")
        self.inbox_id = inbox_id
        self._agent_cache: dict[str, tuple[str, datetime]] = {}  # agent_id -> (name, timestamp)

    def _get_headers(self) -> dict[str, str]:
        """API 요청 헤더"""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    # ===== 채널(Inbox) 목록 =====

    async def get_channels(self) -> list[dict]:
        """
        채널(Inbox) 목록 조회

        Returns:
            채널 목록 [{id, name, enabled, ...}, ...]
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(
                    f"{self.api_url}/channels",
                    headers=self._get_headers(),
                )
                response.raise_for_status()
                data = response.json()
                channels = data.get("channels", [])

                # 활성화된 채널만 필터링 및 정리
                result = []
                for ch in channels:
                    if ch.get("enabled", True):
                        result.append({
                            "id": ch.get("id"),
                            "name": ch.get("name", "Unnamed Channel"),
                            "icon": ch.get("icon", {}).get("url"),
                        })

                logger.debug("Fetched Freshchat channels", count=len(result))
                return result

            except Exception as e:
                logger.error("Failed to get channels", error=str(e))
                return []

    async def validate_api_key(self) -> bool:
        """
        API Key 유효성 검증

        Returns:
            유효 여부
        """
        channels = await self.get_channels()
        return len(channels) > 0

    # ===== 사용자 관리 =====

    async def get_or_create_user(
        self,
        reference_id: str,
        name: Optional[str] = None,
        email: Optional[str] = None,
        properties: Optional[dict] = None,
    ) -> Optional[str]:
        """
        사용자 조회 또는 생성

        Args:
            reference_id: 외부 참조 ID (Teams user ID)
            name: 사용자 이름
            email: 이메일
            properties: 추가 속성 (teams_conversation_id 등)

        Returns:
            Freshchat 사용자 ID
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            # 1. reference_id로 기존 사용자 검색
            try:
                response = await client.get(
                    f"{self.api_url}/users",
                    headers=self._get_headers(),
                    params={"reference_id": reference_id},
                )

                if response.status_code == 200:
                    data = response.json()
                    users = data.get("users", [])
                    if users:
                        user_id = users[0].get("id")
                        logger.debug("Found existing Freshchat user", user_id=user_id)
                        return user_id
            except Exception as e:
                logger.warning("Failed to search user by reference_id", error=str(e))

            # 2. 이메일로 검색 (fallback)
            if email:
                try:
                    response = await client.get(
                        f"{self.api_url}/users",
                        headers=self._get_headers(),
                        params={"email": email},
                    )
                    if response.status_code == 200:
                        data = response.json()
                        users = data.get("users", [])
                        if users:
                            user_id = users[0].get("id")
                            logger.debug("Found existing Freshchat user by email", user_id=user_id)
                            return user_id
                except Exception as e:
                    logger.warning("Failed to search user by email", error=str(e))

            # 3. 새 사용자 생성
            try:
                user_data: dict[str, Any] = {
                    "reference_id": reference_id,
                }

                if name:
                    # first_name, last_name 분리
                    parts = name.split(" ", 1)
                    user_data["first_name"] = parts[0]
                    if len(parts) > 1:
                        user_data["last_name"] = parts[1]

                if email:
                    user_data["email"] = email

                if properties:
                    user_data["properties"] = [
                        {"name": k, "value": str(v)} for k, v in properties.items()
                    ]

                response = await client.post(
                    f"{self.api_url}/users",
                    headers=self._get_headers(),
                    json=user_data,
                )
                response.raise_for_status()
                data = response.json()
                user_id = data.get("id")
                logger.info("Created Freshchat user", user_id=user_id, reference_id=reference_id)
                return user_id

            except httpx.HTTPStatusError as e:
                # 409 Conflict = 이미 존재 (race condition)
                if e.response.status_code == 409:
                    logger.info("User already exists, retrying search")
                    return await self.get_or_create_user(reference_id, name, email, properties)
                logger.error("Failed to create user", status=e.response.status_code, error=str(e))
                return None
            except Exception as e:
                logger.error("Failed to create user", error=str(e))
                return None

    async def update_user_profile(
        self,
        user_id: str,
        email: Optional[str] = None,
        properties: Optional[dict] = None,
    ) -> bool:
        """
        사용자 프로필 업데이트

        Args:
            user_id: Freshchat 사용자 ID
            email: 이메일
            properties: 추가 속성

        Returns:
            성공 여부
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                update_data: dict[str, Any] = {}

                if email:
                    update_data["email"] = email

                if properties:
                    update_data["properties"] = [
                        {"name": k, "value": str(v)} for k, v in properties.items()
                    ]

                if not update_data:
                    return True

                response = await client.put(
                    f"{self.api_url}/users/{user_id}",
                    headers=self._get_headers(),
                    json=update_data,
                )
                response.raise_for_status()
                logger.debug("Updated user profile", user_id=user_id)
                return True

            except Exception as e:
                logger.warning("Failed to update user profile", user_id=user_id, error=str(e))
                return False

    async def update_user_teams_conversation(
        self,
        user_id: str,
        teams_conversation_id: str,
    ) -> bool:
        """
        사용자에게 Teams 대화 ID 저장 (백업 매핑)

        Args:
            user_id: Freshchat 사용자 ID
            teams_conversation_id: Teams 대화 ID

        Returns:
            성공 여부
        """
        return await self.update_user_profile(
            user_id,
            properties={"teams_conversation_id": teams_conversation_id},
        )

    async def get_user_teams_conversation(self, user_id: str) -> Optional[str]:
        """
        사용자로부터 Teams 대화 ID 조회

        Args:
            user_id: Freshchat 사용자 ID

        Returns:
            Teams 대화 ID 또는 None
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(
                    f"{self.api_url}/users/{user_id}",
                    headers=self._get_headers(),
                )
                response.raise_for_status()
                data = response.json()

                properties = data.get("properties", [])
                for prop in properties:
                    if prop.get("name") == "teams_conversation_id":
                        return prop.get("value")

                return None

            except Exception as e:
                logger.warning("Failed to get user properties", user_id=user_id, error=str(e))
                return None

    # ===== 대화 관리 =====

    async def create_conversation(
        self,
        user_id: str,
        user_name: str,
        message_text: Optional[str] = None,
        attachments: Optional[list[dict]] = None,
        user_profile: Optional[dict] = None,
    ) -> Optional[dict[str, Any]]:
        """
        새 대화 생성

        Args:
            user_id: Freshchat 사용자 ID
            user_name: 사용자 표시 이름
            message_text: 첫 메시지 텍스트
            attachments: 첨부파일 목록
            user_profile: 사용자 프로필 정보

        Returns:
            대화 정보 (conversation_id, numeric_id 등)
        """
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                # 메시지 파츠 구성
                message_parts = self._build_message_parts(message_text, attachments)

                if not message_parts:
                    # 빈 메시지는 기본 텍스트 추가
                    message_parts.append({"text": {"content": "(대화 시작)"}})

                payload: dict[str, Any] = {
                    "channel_id": self.inbox_id,
                    "users": [{"id": user_id}],
                    "messages": [{
                        "message_parts": message_parts,
                        "actor_type": "user",
                        "actor_id": user_id,
                    }],
                }

                response = await client.post(
                    f"{self.api_url}/conversations",
                    headers=self._get_headers(),
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()

                conversation_id = data.get("conversation_id")
                numeric_id = data.get("id")

                logger.info(
                    "Created Freshchat conversation",
                    conversation_id=conversation_id,
                    numeric_id=numeric_id,
                    user_id=user_id,
                )

                return {
                    "conversation_id": conversation_id,
                    "numeric_id": numeric_id,
                    "user_id": user_id,
                }

            except httpx.HTTPStatusError as e:
                logger.error(
                    "Failed to create conversation",
                    status=e.response.status_code,
                    response=e.response.text[:500],
                )
                return None
            except Exception as e:
                logger.error("Failed to create conversation", error=str(e))
                return None

    async def send_message(
        self,
        conversation_id: str,
        user_id: str,
        message_text: Optional[str] = None,
        attachments: Optional[list[dict]] = None,
        auto_recover: bool = True,
        user_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        메시지 전송

        Args:
            conversation_id: Freshchat 대화 ID (GUID 또는 numeric)
            user_id: Freshchat 사용자 ID
            message_text: 메시지 텍스트
            attachments: 첨부파일 목록
            auto_recover: 대화 종료 시 자동으로 새 대화 생성
            user_name: 새 대화 생성 시 사용할 사용자 이름

        Returns:
            결과 dict (success, new_conversation_id 등)
        """
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                message_parts = self._build_message_parts(message_text, attachments)

                if not message_parts:
                    return {"success": False, "error": "Empty message"}

                payload = {
                    "message_parts": message_parts,
                    "actor_type": "user",
                    "actor_id": user_id,
                }

                response = await client.post(
                    f"{self.api_url}/conversations/{conversation_id}/messages",
                    headers=self._get_headers(),
                    json=payload,
                )

                # 400 에러 처리 (대화 종료됨)
                if response.status_code == 400:
                    error_text = response.text.lower()

                    # "not the latest conversation" = 대화 종료됨
                    if "not the latest conversation" in error_text or "resolved" in error_text:
                        if auto_recover:
                            logger.info("Conversation resolved, creating new one")
                            new_conv = await self.create_conversation(
                                user_id=user_id,
                                user_name=user_name or "User",
                                message_text=message_text,
                                attachments=attachments,
                            )
                            if new_conv:
                                return {
                                    "success": True,
                                    "new_conversation_id": new_conv["conversation_id"],
                                    "new_numeric_id": new_conv["numeric_id"],
                                }
                        return {"success": False, "error": "conversation_resolved"}

                    logger.warning("Message send failed with 400", response=response.text[:500])
                    return {"success": False, "error": response.text}

                response.raise_for_status()
                logger.debug("Message sent to Freshchat", conversation_id=conversation_id)
                return {"success": True}

            except httpx.HTTPStatusError as e:
                # 404 = 대화 없음 (GUID/numeric ID 불일치일 수 있음)
                if e.response.status_code == 404:
                    return {"success": False, "error": "conversation_not_found"}

                logger.error("Failed to send message", status=e.response.status_code, error=str(e))
                return {"success": False, "error": str(e)}
            except Exception as e:
                logger.error("Failed to send message", error=str(e))
                return {"success": False, "error": str(e)}

    async def send_message_with_fallback(
        self,
        conversation_ids: list[str],
        user_id: str,
        message_text: Optional[str] = None,
        attachments: Optional[list[dict]] = None,
        user_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        여러 대화 ID로 메시지 전송 시도 (fallback)

        Args:
            conversation_ids: 시도할 대화 ID 목록 (GUID, numeric 등)
            user_id: Freshchat 사용자 ID
            message_text: 메시지 텍스트
            attachments: 첨부파일 목록
            user_name: 새 대화 생성 시 사용할 사용자 이름

        Returns:
            결과 dict
        """
        valid_ids = [cid for cid in conversation_ids if cid]

        if not valid_ids:
            return {"success": False, "error": "No valid conversation IDs"}

        last_error = None

        for idx, conv_id in enumerate(valid_ids):
            result = await self.send_message(
                conversation_id=conv_id,
                user_id=user_id,
                message_text=message_text,
                attachments=attachments,
                auto_recover=(idx == len(valid_ids) - 1),  # 마지막에만 auto_recover
                user_name=user_name,
            )

            if result.get("success"):
                return result

            last_error = result.get("error")

            # 404면 다음 ID 시도
            if last_error == "conversation_not_found" and idx < len(valid_ids) - 1:
                logger.debug(f"Conversation {conv_id} not found, trying next")
                continue

            # 다른 에러면 중단
            break

        return {"success": False, "error": last_error}

    async def is_conversation_active(self, conversation_id: str) -> bool:
        """
        대화 활성 상태 확인

        Args:
            conversation_id: Freshchat 대화 ID

        Returns:
            활성 여부 (resolved가 아니면 True)
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(
                    f"{self.api_url}/conversations/{conversation_id}",
                    headers=self._get_headers(),
                )
                response.raise_for_status()
                data = response.json()
                status = data.get("status")
                return status != "resolved"
            except Exception as e:
                logger.warning("Failed to check conversation status", conversation_id=conversation_id, error=str(e))
                return False

    # ===== 파일 업로드/다운로드 =====

    async def upload_file(
        self,
        file_buffer: bytes,
        filename: str,
        content_type: str,
    ) -> Optional[dict[str, Any]]:
        """
        파일 업로드

        Args:
            file_buffer: 파일 바이너리
            filename: 파일명
            content_type: MIME 타입

        Returns:
            업로드 결과 (file_hash, file_id, url 등)
        """
        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                # 파일명에 확장자가 없으면 content_type 기반으로 추가
                safe_filename = self._ensure_filename_extension(filename, content_type)

                files = {
                    "file": (safe_filename, file_buffer, content_type),
                }

                logger.debug(
                    "Uploading file to Freshchat",
                    original_filename=filename,
                    safe_filename=safe_filename,
                    content_type=content_type,
                    size=len(file_buffer),
                )

                # Authorization 헤더만 (Content-Type은 multipart로 자동 설정)
                headers = {"Authorization": f"Bearer {self.api_key}"}

                # Freshchat /files/upload 사용 (이미지/파일 모두 동일)
                # 레거시 코드에서 검증된 방식 - /images/upload는 특정 조건에서 실패할 수 있음
                upload_files = {
                    "file": (safe_filename, file_buffer, content_type),
                }

                response = await client.post(
                    f"{self.api_url}/files/upload",
                    headers=headers,
                    files=upload_files,
                )
                response.raise_for_status()
                data = response.json()

                # 응답 정규화 (다양한 응답 형태 처리)
                return self._normalize_upload_response(data, filename, content_type)

            except httpx.HTTPStatusError as e:
                # Freshchat에서 400/401 원인 파악을 위해 응답 본문을 함께 로깅
                logger.error(
                    "Failed to upload file",
                    filename=filename,
                    status=e.response.status_code,
                    response=e.response.text[:500],
                )
                return None
            except Exception as e:
                logger.error("Failed to upload file", filename=filename, error=str(e))
                return None

    async def download_file(self, file_url: str) -> Optional[tuple[bytes, str, str]]:
        """
        파일 다운로드

        Args:
            file_url: 파일 URL

        Returns:
            (file_buffer, content_type, filename) 또는 None
        """
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            try:
                # Freshchat API 도메인이면 인증 헤더 추가
                headers = {}
                if self.api_url in file_url or "freshchat.com" in file_url:
                    headers["Authorization"] = f"Bearer {self.api_key}"

                response = await client.get(file_url, headers=headers)
                response.raise_for_status()

                content_type = response.headers.get("content-type", "application/octet-stream")

                # Content-Disposition에서 파일명 추출
                filename = self._extract_filename_from_header(
                    response.headers.get("content-disposition", ""),
                    file_url,
                )

                return (response.content, content_type, filename)

            except Exception as e:
                logger.error("Failed to download file", url=file_url, error=str(e))
                return None

    # ===== 메시지 조회 =====

    async def get_message(
        self,
        conversation_id: str,
        message_id: str,
    ) -> Optional[dict[str, Any]]:
        """
        메시지 상세 조회

        Args:
            conversation_id: 대화 ID
            message_id: 메시지 ID

        Returns:
            메시지 정보
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(
                    f"{self.api_url}/conversations/{conversation_id}/messages/{message_id}",
                    headers=self._get_headers(),
                )
                response.raise_for_status()
                return response.json()
            except Exception as e:
                logger.warning("Failed to get message", message_id=message_id, error=str(e))
                return None

    async def get_message_with_retry(
        self,
        conversation_ids: list[str],
        message_id: str,
        max_attempts: int = 3,
        delay_seconds: float = 1.5,
    ) -> Optional[dict[str, Any]]:
        """
        메시지 조회 (재시도 + URL 대기)

        Freshchat 웹훅 초기에는 download URL이 없을 수 있음

        Args:
            conversation_ids: 시도할 대화 ID 목록
            message_id: 메시지 ID
            max_attempts: 최대 시도 횟수
            delay_seconds: 재시도 간격

        Returns:
            메시지 정보 (URL 포함)
        """
        valid_ids = [cid for cid in conversation_ids if cid]

        for attempt in range(max_attempts):
            for conv_id in valid_ids:
                message = await self.get_message(conv_id, message_id)
                if message:
                    # URL이 있는지 확인
                    if self._has_attachment_urls(message):
                        return message

            # 마지막 시도가 아니면 대기
            if attempt < max_attempts - 1:
                await asyncio.sleep(delay_seconds * (attempt + 1))

        # URL 없어도 마지막 결과 반환
        for conv_id in valid_ids:
            message = await self.get_message(conv_id, message_id)
            if message:
                return message

        return None

    # ===== 상담원 =====

    async def get_agent_name(self, agent_id: str) -> str:
        """
        상담원 이름 조회 (캐시 포함)

        Args:
            agent_id: Freshchat 상담원 ID

        Returns:
            상담원 이름
        """
        # 캐시 확인
        if agent_id in self._agent_cache:
            name, cached_at = self._agent_cache[agent_id]
            if datetime.now() - cached_at < AGENT_CACHE_TTL:
                return name

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(
                    f"{self.api_url}/agents/{agent_id}",
                    headers=self._get_headers(),
                )
                response.raise_for_status()
                data = response.json()

                first_name = data.get("first_name", "")
                last_name = data.get("last_name", "")
                name = f"{first_name} {last_name}".strip()

                if not name:
                    name = data.get("email", "상담원")

                # 캐시 저장
                self._agent_cache[agent_id] = (name, datetime.now())
                return name

            except Exception as e:
                logger.warning("Failed to get agent name", agent_id=agent_id, error=str(e))
                return "상담원"

    # ===== 헬퍼 메서드 =====

    def _build_message_parts(
        self,
        message_text: Optional[str],
        attachments: Optional[list[dict]],
    ) -> list[dict]:
        """메시지 파츠 구성"""
        parts: list[dict] = []

        if message_text:
            parts.append({"text": {"content": message_text}})

        if attachments:
            for att in attachments:
                # URL이 있는 경우
                if att.get("url"):
                    content_type = att.get("content_type", "application/octet-stream")

                    if content_type.startswith("image/"):
                        parts.append({
                            "image": {
                                "url": att["url"],
                            }
                        })
                    else:
                        parts.append({
                            "file": {
                                "url": att["url"],
                                "name": att.get("name", "file"),
                                "content_type": content_type,
                            }
                        })

                # file_hash/file_id가 있는 경우 (업로드된 파일)
                elif att.get("file_hash") or att.get("file_id"):
                    content_type = att.get("content_type", "application/octet-stream")

                    if content_type.startswith("image/"):
                        parts.append({
                            "image": {
                                "file_hash": att.get("file_hash"),
                                "file_id": att.get("file_id"),
                            }
                        })
                    else:
                        parts.append({
                            "file": {
                                "file_hash": att.get("file_hash"),
                                "file_id": att.get("file_id"),
                                "name": att.get("name", "file"),
                                "content_type": content_type,
                            }
                        })

        return parts

    def _ensure_filename_extension(self, filename: str, content_type: str) -> str:
        """파일명에 확장자가 없으면 content_type 기반으로 추가"""
        if not filename:
            filename = "attachment"

        # 이미 확장자가 있으면 그대로 반환
        if "." in filename.split("/")[-1]:
            return filename

        # content_type에서 확장자 매핑
        ext_map = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/bmp": ".bmp",
            "image/svg+xml": ".svg",
            "application/pdf": ".pdf",
            "application/zip": ".zip",
            "application/x-zip-compressed": ".zip",
            "application/msword": ".doc",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            "application/vnd.ms-excel": ".xls",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            "application/vnd.ms-powerpoint": ".ppt",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
            "text/plain": ".txt",
            "text/html": ".html",
            "text/csv": ".csv",
            "application/json": ".json",
            "application/xml": ".xml",
            "video/mp4": ".mp4",
            "video/webm": ".webm",
            "video/quicktime": ".mov",
            "audio/mpeg": ".mp3",
            "audio/wav": ".wav",
            "application/octet-stream": "",  # 기본값은 확장자 없음
        }

        ext = ext_map.get(content_type, "")
        if not ext and content_type:
            # 매핑에 없으면 content_type 서브타입 사용 (예: image/png -> .png)
            parts = content_type.split("/")
            if len(parts) == 2:
                subtype = parts[1].split(";")[0].strip()
                if subtype and len(subtype) <= 5 and subtype.isalnum():
                    ext = f".{subtype}"

        return f"{filename}{ext}" if ext else filename

    def _normalize_upload_response(
        self,
        data: dict,
        filename: str,
        content_type: str,
    ) -> dict[str, Any]:
        """업로드 응답 정규화"""
        # 다양한 응답 형태 처리
        file_data = data.get("file") or data.get("data") or data

        return {
            "file_hash": file_data.get("file_hash") or file_data.get("fileHash"),
            "file_id": file_data.get("file_id") or file_data.get("fileId") or file_data.get("id"),
            "name": file_data.get("name") or filename,
            "content_type": file_data.get("content_type") or file_data.get("contentType") or content_type,
            "url": file_data.get("url") or file_data.get("download_url"),
        }

    def _has_attachment_urls(self, message: dict) -> bool:
        """메시지에 첨부파일 URL이 있는지 확인"""
        message_parts = message.get("message_parts", [])

        for part in message_parts:
            if "file" in part:
                file_info = part["file"]
                if not file_info.get("url") and not file_info.get("download_url"):
                    return False
            if "image" in part:
                image_info = part["image"]
                if not image_info.get("url"):
                    return False

        return True

    def _extract_filename_from_header(
        self,
        content_disposition: str,
        fallback_url: str,
    ) -> str:
        """Content-Disposition 헤더에서 파일명 추출"""
        if content_disposition:
            # filename*=UTF-8''... 형식
            match = re.search(r"filename\*=UTF-8''(.+)", content_disposition)
            if match:
                from urllib.parse import unquote
                return unquote(match.group(1))

            # filename="..." 형식
            match = re.search(r'filename="(.+?)"', content_disposition)
            if match:
                return match.group(1)

            # filename=... 형식 (따옴표 없음)
            match = re.search(r"filename=([^\s;]+)", content_disposition)
            if match:
                return match.group(1)

        # URL에서 추출
        from urllib.parse import urlparse, unquote
        path = urlparse(fallback_url).path
        return unquote(path.split("/")[-1]) or "file"
