"""[中文] Web 搜索 —— 无需 API Key 的 DuckDuckGo 默认提供方 + 可配置的第三方提供方。

[English]
Web search — a keyless DuckDuckGo default + configurable third-party providers.
"""

from __future__ import annotations

from .providers import (
    BraveProvider,
    DuckDuckGoProvider,
    SearchResult,
    TavilyProvider,
    WebSearchProvider,
    build_provider,
    provider_names,
)
from .fetch import make_web_fetch_tool
from .tool import make_web_search_tool, provider_name, resolve_provider

__all__ = [
    "SearchResult",
    "WebSearchProvider",
    "DuckDuckGoProvider",
    "TavilyProvider",
    "BraveProvider",
    "build_provider",
    "provider_names",
    "make_web_search_tool",
    "make_web_fetch_tool",
    "provider_name",
    "resolve_provider",
]
