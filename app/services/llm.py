"""LLM 요약 서비스 (OpenAI-compatible / Azure OpenAI)"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger(__name__)


SUMMARY_SYSTEM_PROMPT = (
    "역할: 중립적 기록자.\n"
    "목표: 원문 대화에서 '누가/언제/무엇을 말했다·요청했다'를 가능한 한 많이 포함하되 "
    "잡담·반복·시스템 메시지는 제거한다.\n"
    "금지: 의견, 추정, 해석, 평가, 감정 표현.\n"
    "형식: 불릿 리스트만 출력한다. (머릿말/제목 없이 줄마다 '- '로 시작)\n"
    "추가 규칙: 이름과 시간 정보는 원문에 있으면 반드시 포함한다."
)


def _normalize_input(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines()]
    cleaned: list[str] = []
    for line in lines:
        if not line:
            continue
        lower = line.lower()
        if "님이" in line and ("입장" in line or "퇴장" in line):
            continue
        if "reacted" in lower or "liked" in lower:
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def _heuristic_summary(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    deduped: list[str] = []
    seen = set()
    for line in lines:
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(line)
    bullets = []
    for line in deduped:
        bullets.append(f"- {line}" if not line.startswith("- ") else line)
    return "\n".join(bullets)


@dataclass
class LLMConfig:
    provider: str
    api_base: str
    api_key: str
    model: str
    temperature: float
    timeout: int
    azure_deployment: str
    azure_api_version: str


class LLMService:
    """LLM 요약 서비스 (OpenAI-compatible / Azure OpenAI)"""

    def __init__(self):
        settings = get_settings()
        self._config = LLMConfig(
            provider=(settings.llm_provider or "openai_compatible").lower(),
            api_base=settings.llm_api_base.rstrip("/"),
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            temperature=settings.llm_temperature,
            timeout=settings.llm_timeout,
            azure_deployment=settings.llm_azure_deployment,
            azure_api_version=settings.llm_azure_api_version,
        )

    def _is_configured(self) -> bool:
        if not self._config.api_key:
            return False
        if self._config.provider == "azure_openai":
            return bool(self._config.api_base and self._config.azure_deployment)
        return bool(self._config.api_base and self._config.model)

    def _build_request(self, text: str) -> tuple[str, dict, dict]:
        messages = [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ]
        payload = {
            "messages": messages,
            "temperature": self._config.temperature,
        }

        if self._config.provider == "azure_openai":
            url = (
                f"{self._config.api_base}/openai/deployments/"
                f"{self._config.azure_deployment}/chat/completions"
                f"?api-version={self._config.azure_api_version}"
            )
            headers = {"api-key": self._config.api_key}
        else:
            url = f"{self._config.api_base}/chat/completions"
            headers = {"Authorization": f"Bearer {self._config.api_key}"}
            payload["model"] = self._config.model

        return url, headers, payload

    async def summarize(self, text: str) -> Optional[str]:
        normalized = _normalize_input(text)
        if not normalized:
            return None

        if not self._is_configured():
            return _heuristic_summary(normalized)

        try:
            url, headers, payload = self._build_request(normalized)
            async with httpx.AsyncClient(timeout=self._config.timeout) as client:
                response = await client.post(url, headers=headers, json=payload)
                if response.status_code >= 400:
                    logger.warning(
                        "LLM summarize failed",
                        status=response.status_code,
                        response=response.text[:200],
                    )
                    return _heuristic_summary(normalized)

                data = response.json()
                content = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                )
                return content or _heuristic_summary(normalized)

        except Exception as e:
            logger.warning("LLM summarize error", error=str(e))
            return _heuristic_summary(normalized)


_llm_instance: Optional[LLMService] = None


def get_llm_service() -> LLMService:
    global _llm_instance
    if _llm_instance is None:
        _llm_instance = LLMService()
    return _llm_instance
