"""OCR 서비스 (Azure Vision Read)"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import time
import asyncio

import httpx

from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class OCRConfig:
    provider: str
    endpoint: str
    api_key: str
    timeout: int
    poll_interval: float


class OCRService:
    """OCR 서비스 (현재 Azure Vision Read 지원)"""

    def __init__(self):
        settings = get_settings()
        self._config = OCRConfig(
            provider=(settings.ocr_provider or "none").lower(),
            endpoint=settings.ocr_endpoint.rstrip("/"),
            api_key=settings.ocr_api_key,
            timeout=settings.ocr_timeout,
            poll_interval=settings.ocr_poll_interval,
        )

    def _is_configured(self) -> bool:
        return (
            self._config.provider == "azure_vision_read"
            and bool(self._config.endpoint and self._config.api_key)
        )

    async def extract_text_from_url(self, url: str) -> Optional[str]:
        if not self._is_configured():
            return None

        try:
            async with httpx.AsyncClient(timeout=self._config.timeout, follow_redirects=True) as client:
                download = await client.get(url)
                if download.status_code >= 400:
                    logger.warning(
                        "OCR download failed",
                        status=download.status_code,
                        url=url[:120],
                    )
                    return None

                content_type = download.headers.get("content-type", "application/octet-stream")
                image_bytes = download.content

            return await self._extract_text_from_bytes(image_bytes, content_type)
        except Exception as e:
            logger.warning("OCR download error", error=str(e))
            return None

    async def _extract_text_from_bytes(self, data: bytes, content_type: str) -> Optional[str]:
        if not self._is_configured():
            return None

        analyze_url = f"{self._config.endpoint}/vision/v3.2/read/analyze"
        headers = {
            "Ocp-Apim-Subscription-Key": self._config.api_key,
            "Content-Type": content_type or "application/octet-stream",
        }

        try:
            async with httpx.AsyncClient(timeout=self._config.timeout) as client:
                response = await client.post(analyze_url, headers=headers, content=data)
                if response.status_code >= 400:
                    logger.warning(
                        "OCR analyze failed",
                        status=response.status_code,
                        response=response.text[:200],
                    )
                    return None

                operation_url = response.headers.get("Operation-Location")
                if not operation_url:
                    logger.warning("OCR missing Operation-Location header")
                    return None

                # Polling
                start = time.time()
                while True:
                    result = await client.get(
                        operation_url,
                        headers={"Ocp-Apim-Subscription-Key": self._config.api_key},
                    )
                    if result.status_code >= 400:
                        logger.warning(
                            "OCR poll failed",
                            status=result.status_code,
                            response=result.text[:200],
                        )
                        return None

                    data = result.json()
                    status = data.get("status")
                    if status == "succeeded":
                        return _extract_lines_from_azure_result(data)
                    if status == "failed":
                        logger.warning("OCR processing failed")
                        return None

                    if time.time() - start > self._config.timeout:
                        logger.warning("OCR polling timed out")
                        return None

                    await asyncio.sleep(self._config.poll_interval)

        except Exception as e:
            logger.warning("OCR error", error=str(e))
            return None


def _extract_lines_from_azure_result(data: dict) -> Optional[str]:
    try:
        analyze = data.get("analyzeResult", {})
        read_results = analyze.get("readResults", [])
        lines: list[str] = []
        for page in read_results:
            for line in page.get("lines", []):
                text = line.get("text")
                if text:
                    lines.append(text.strip())
        return "\n".join(lines).strip() if lines else None
    except Exception:
        return None


_ocr_instance: Optional[OCRService] = None


def get_ocr_service() -> OCRService:
    global _ocr_instance
    if _ocr_instance is None:
        _ocr_instance = OCRService()
    return _ocr_instance
