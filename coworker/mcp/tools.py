"""[中文] 将 MCP 工具转换为可直接供 ToolRegistry 使用的同步可调用对象。

每个 MCP 工具被包装为一个同步 callable（以此满足注册表的 `execute` 契约，
引擎已通过 `asyncio.to_thread` 运行它）。该 callable 通过 `run_coroutine_threadsafe`
桥接回服务器事件循环上的实时异步会话。我们附加了 `ToolMetadata`（category="mcp"，按配置指定 `requires_approval`）
以便 PermissionEngine 进行权限把关，并直接从 MCP `inputSchema` 构建显式的 OpenAI schema 以保证保真度。

[English]
Turn MCP tools into ToolRegistry-ready callables.

Each MCP tool becomes a sync callable (so it fits the registry's `execute` contract, which
the engine already runs via `asyncio.to_thread`). The callable bridges back to the live
async session on the server loop via `run_coroutine_threadsafe`. We attach `ToolMetadata`
(category="mcp", `requires_approval` per config) so the PermissionEngine gates it, and an
explicit OpenAI schema built straight from the MCP `inputSchema` for fidelity.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Awaitable, Callable

import aisuite as ai

from .config import MCPServerDef

CallAsync = Callable[[str, dict[str, Any]], Awaitable[Any]]

_NAME_OK = re.compile(r"[^a-zA-Z0-9_-]")
_MAX_NAME = 64  # OpenAI function-name limit


def tool_name(server: str, tool: str) -> str:
    """[中文] `mcp__<server>__<tool>`，已按照 OpenAI 的 `[A-Za-z0-9_-]{1,64}` 命名规则净化。 / [English] `mcp__<server>__<tool>`, sanitized to OpenAI's `[A-Za-z0-9_-]{1,64}` rule."""
    base = f"mcp__{_NAME_OK.sub('_', server)}__{_NAME_OK.sub('_', tool)}"
    if len(base) > _MAX_NAME:
        base = base[:_MAX_NAME]
    return base


def _openai_schema(name: str, mcp_tool: Any) -> dict[str, Any]:
    params = getattr(mcp_tool, "inputSchema", None) or {
        "type": "object",
        "properties": {},
    }
    description = (getattr(mcp_tool, "description", None) or "")[:1024]
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": params},
    }


def _filtered(mcp_tools: list[Any], server: MCPServerDef) -> list[Any]:
    out = mcp_tools
    if server.include_tools is not None:
        allow = set(server.include_tools)
        out = [t for t in out if t.name in allow]
    if server.exclude_tools:
        block = set(server.exclude_tools)
        out = [t for t in out if t.name not in block]
    return out


def build_callables(
    server: MCPServerDef,
    mcp_tools: list[Any],
    call_async: CallAsync,
    loop: asyncio.AbstractEventLoop,
    *,
    timeout: float = 120.0,
) -> list[Callable[..., Any]]:
    """[中文] 将某个服务器的（已过滤）MCP 工具包装为注册表就绪的同步可调用对象。

    [English]
    Wrap a server's (filtered) MCP tools as registry-ready callables.
    """
    callables: list[Callable[..., Any]] = []
    for mcp_tool in _filtered(mcp_tools, server):
        name = tool_name(server.name, mcp_tool.name)
        remote = mcp_tool.name

        def _invoke(_remote: str = remote, **kwargs: Any) -> Any:
            future = asyncio.run_coroutine_threadsafe(call_async(_remote, kwargs), loop)
            return future.result(timeout)

        # [中文] 我们显式附加 schema + metadata（而非通过 `ai.tool`，后者会尝试从该 `**kwargs` 包装器中推导 schema）：注册表会读取这两个属性。
        # [English] We attach the schema + metadata explicitly (rather than via `ai.tool`, which would
        # try to derive a schema from this `**kwargs` wrapper): the registry reads both attrs.
        _invoke.__name__ = name
        _invoke.__doc__ = (
            getattr(mcp_tool, "description", None)
            or f"MCP tool {remote} from {server.name}"
        )
        _invoke.__aisuite_tool_metadata__ = ai.ToolMetadata(
            name=name,
            category="mcp",
            risk_level="medium",
            capabilities=[server.name],
            requires_approval=server.requires_approval,
        )
        _invoke.__coworker_schema__ = _openai_schema(name, mcp_tool)
        # [中文] OPE-136 发现项 4：该调用实际前往何处，用于审批卡片的作用域 chip 展示。
        # 来源于服务器 DEF（用户编写的配置），绝不采信服务器自身宣称的内容。http → 远程主机；stdio → 本地进程。
        # [English] OPE-136 finding 4: where this call actually goes, for the approval card's
        # scope chip. From the server DEF (user-authored config), never from anything
        # the server itself claims. http → the remote host; stdio → a local process.
        _invoke.__coworker_mcp_destination__ = {
            "transport": server.transport,
            "host": _server_host(server),
        }
        callables.append(_invoke)
    return callables


def _server_host(server: MCPServerDef) -> str:
    """[中文] HTTP 服务器调用所到达的主机名（小写），对于 stdio 或无法解析的 URL 返回 ""。 / [English] The hostname an HTTP server's calls reach (lowercased), "" for stdio/unparseable."""
    if not server.url:
        return ""
    try:
        from urllib.parse import urlparse

        return (urlparse(server.url).hostname or "").lower()
    except ValueError:  # [中文] pragma: no cover - urlparse 极少抛出异常，但失败时返回 "" / [English] pragma: no cover - urlparse rarely raises, but fail to ""
        return ""
