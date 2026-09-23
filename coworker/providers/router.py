"""[中文] ProviderRouter —— 一个统一的 `ProviderClient`，它根据模型字符串中的 `provider:` 前缀
分发到具体的提供商客户端，该客户端从 SecretStore 配置中延迟构建并进行缓存。

这是 `SessionManager` 传递给每个引擎的唯一提供商客户端，因此 `complete()/stream()`
（每次调用都会接收完整的模型字符串）能够自行完成路由：例如 `ollama:llama3.3` →
Ollama 客户端（Ollama 兼容 OpenAI 的 `/v1`），纯 `gpt-5.5` → 默认提供商（OpenAI）。
在委托调用之前前缀会被剔除，因为底层 SDK 需要纯粹的模型名称。

配置变更（新增 API 密钥、新的 Ollama 地址）调用 `invalidate()` 来废弃已缓存的客户端，
从而让现有引擎无需重建即可应用最新配置。

ProviderRouter — one `ProviderClient` that dispatches by the `provider:` prefix of a model
string to a per-provider client, built lazily from its SecretStore profile and cached.

This is the single provider the `SessionManager` hands to every engine, so `complete()/stream()`
(which already receive the full model string per-call) route themselves: `ollama:llama3.3` →
the Ollama client (Ollama's OpenAI-compatible `/v1`), bare `gpt-5.5` → the default (OpenAI). The
prefix is stripped before delegating, since the underlying SDKs want the bare model name.

Config changes (a new key, a new Ollama URL) call `invalidate()` to drop cached clients, so
existing engines pick up the change without a rebuild.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

from .base import ProviderClient
from .capabilities import capabilities_for
from .registry import build_provider_client, get_descriptor


class ProviderRouter(ProviderClient):
    def __init__(
        self,
        secrets: Any = None,
        *,
        default_provider: str = "openai",
        on_use: Any = None,
    ) -> None:
        self._secrets = secrets
        self._default = default_provider
        self._clients: dict[str, ProviderClient] = {}
        self._lock = threading.Lock()
        # [中文] 当分发补全请求时触发的可选 callable(provider_name) 回调 —— 驱动“设置”面板中的“上次使用”行。
        # 尽最大努力执行：其失败绝不会影响模型调用。
        # Optional callable(provider_name) fired when a completion is dispatched — drives the
        # Settings pane's "Last used" line. Best-effort: its failures never break a model call.
        self._on_use = on_use

    def _note_use(self, model: str) -> None:
        if self._on_use is None:
            return
        try:
            self._on_use(self._provider_name(model))
        except Exception:
            pass

    # -- routing ----------------------------------------------------------------
    def _provider_name(self, model: str) -> str:
        """[中文] 获取模型对应的提供商：如果是已知提供商，则为 `prefix:rest` 中的 `prefix`，
        否则为默认提供商。（若冒号前不是已知的提供商 —— 极少见 —— 则回退默认处理）。

        The provider for a model: the `prefix` of `prefix:rest` if it's a known provider,
        else the default. (A colon that isn't a known provider — unlikely — falls through.)
        """
        if ":" in model:
            prefix = model.split(":", 1)[0]
            if get_descriptor(prefix) is not None:
                return prefix
        return self._default

    def _client_for(self, model: str) -> ProviderClient:
        name = self._provider_name(model)
        with self._lock:
            client = self._clients.get(name)
            if client is None:
                profile = {}
                if self._secrets is not None:
                    profile = self._secrets.get(f"provider:{name}") or {}
                client = build_provider_client(name, profile, self._secrets)
                self._clients[name] = client
            return client

    @staticmethod
    def _bare(model: str) -> str:
        """[中文] 剔除已知的提供商前缀；底层 SDK 需要纯粹的模型名称。
        若模型字符串的第一段不是提供商（例如 `qwen2.5-coder:32b` —— 冒号后是版本标签而非前缀），
        则原样返回，避免将冒号误认为提供商分隔符。

        Strip a KNOWN provider prefix; the underlying SDK wants the bare model name. A model
        whose first segment isn't a provider (e.g. `qwen2.5-coder:32b` — a version tag, not a
        prefix) is returned unchanged, so the colon isn't mistaken for a provider separator.
        """
        if ":" in model:
            prefix, rest = model.split(":", 1)
            if get_descriptor(prefix) is not None:
                return rest
        return model

    def invalidate(self, name: Optional[str] = None) -> None:
        """[中文] 废弃已缓存的客户端，以便下一次调用使用最新配置重新构建。
        Drop cached client(s) so the next call rebuilds with fresh config."""
        with self._lock:
            if name is None:
                self._clients.clear()
            else:
                self._clients.pop(name, None)

    # -- ProviderClient ---------------------------------------------------------
    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        **settings: Any,
    ):
        self._note_use(model)
        return self._client_for(model).complete(
            model=self._bare(model), messages=messages, tools=tools, **settings
        )

    def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        **settings: Any,
    ):
        self._note_use(model)
        return self._client_for(model).stream(
            model=self._bare(model), messages=messages, tools=tools, **settings
        )

    def capabilities(self, model: str):
        return capabilities_for(model)
