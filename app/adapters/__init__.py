"""플랫폼 어댑터"""
from typing import Type

from app.adapters.base import BaseAdapter
from app.adapters.freshchat import FreshchatAdapter
# from app.adapters.zendesk import ZendeskAdapter  # Phase 2

ADAPTERS: dict[str, Type[BaseAdapter]] = {
    "freshchat": FreshchatAdapter,
    # "zendesk": ZendeskAdapter,  # Phase 2
}


def get_adapter(platform: str, config: dict) -> BaseAdapter:
    """플랫폼에 맞는 어댑터 반환"""
    adapter_class = ADAPTERS.get(platform)
    if not adapter_class:
        raise ValueError(f"Unsupported platform: {platform}")
    return adapter_class(config)
