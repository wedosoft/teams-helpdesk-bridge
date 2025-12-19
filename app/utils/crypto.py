"""암호화 유틸리티

API 키 등 민감한 설정 암호화/복호화
Fernet 대칭키 암호화 사용 (AES-128-CBC)
"""
import base64
import json
import os
from typing import Any

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _get_fernet() -> Fernet:
    """Fernet 인스턴스 반환

    ENCRYPTION_KEY 환경변수에서 키 로드
    """
    settings = get_settings()
    key = settings.encryption_key

    if not key:
        raise RuntimeError("ENCRYPTION_KEY is required")

    # PBKDF2로 키 유도 (32바이트 키 → Fernet용 URL-safe base64)
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"teams-helpdesk-bridge-salt",  # 고정 salt (키 유도 일관성)
        iterations=100000,
    )
    derived_key = base64.urlsafe_b64encode(kdf.derive(key.encode()))

    return Fernet(derived_key)


def encrypt_config(config: dict[str, Any]) -> dict[str, Any]:
    """
    설정 dict 암호화

    민감한 필드(api_key, api_token, oauth_token 등)만 암호화
    나머지는 평문 유지

    Args:
        config: 원본 설정 dict

    Returns:
        암호화된 설정 dict
    """
    if not config:
        return {}

    # 암호화 대상 필드
    sensitive_fields = {
        "api_key",
        "api_token",
        "oauth_token",
        "password",
        "secret",
        "secret_key",
        "webhook_public_key",
    }

    fernet = _get_fernet()
    encrypted = {}

    for key, value in config.items():
        if key in sensitive_fields and value:
            # 문자열만 암호화
            if isinstance(value, str):
                encrypted_value = fernet.encrypt(value.encode()).decode()
                encrypted[key] = {"encrypted": True, "value": encrypted_value}
            else:
                encrypted[key] = value
        else:
            encrypted[key] = value

    return encrypted


def decrypt_config(config: dict[str, Any]) -> dict[str, Any]:
    """
    설정 dict 복호화

    Args:
        config: 암호화된 설정 dict

    Returns:
        복호화된 설정 dict
    """
    if not config:
        return {}

    fernet = _get_fernet()
    decrypted = {}

    for key, value in config.items():
        if isinstance(value, dict) and value.get("encrypted"):
            # 암호화된 필드 복호화
            try:
                encrypted_value = value.get("value", "")
                decrypted_value = fernet.decrypt(encrypted_value.encode()).decode()
                decrypted[key] = decrypted_value
            except Exception as e:
                raise RuntimeError(f"Failed to decrypt field: {key}") from e
        else:
            decrypted[key] = value

    return decrypted


def generate_encryption_key() -> str:
    """
    새 암호화 키 생성

    Returns:
        URL-safe base64 인코딩된 32바이트 키
    """
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def is_encrypted(config: dict[str, Any]) -> bool:
    """
    설정이 암호화되었는지 확인

    Args:
        config: 설정 dict

    Returns:
        암호화 여부
    """
    if not config:
        return False

    for value in config.values():
        if isinstance(value, dict) and value.get("encrypted"):
            return True

    return False
