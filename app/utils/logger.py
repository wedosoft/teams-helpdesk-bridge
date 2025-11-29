"""구조화된 로깅 설정"""
import logging
import sys

import structlog

from app.config import get_settings


def setup_logging() -> None:
    """structlog 설정"""
    settings = get_settings()

    # 로그 레벨 설정
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # structlog 설정
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer() if settings.log_level == "debug"
            else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # 기본 로깅 설정
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )


def get_logger(name: str = __name__) -> structlog.BoundLogger:
    """로거 인스턴스 반환"""
    return structlog.get_logger(name)
