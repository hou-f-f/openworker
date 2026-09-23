"""[中文] MCPManager —— 基于官方 `mcp` SDK 构建的轻量级异步 MCP 客户端。

原生异步实现（无 `nest_asyncio`，无第二事件循环）：每个服务器运行在一个专用的
asyncio task 中，该 task 打开传输通道 + `ClientSession`，保持它们存活直至系统关闭，
并在*同一个* task 中关闭它们 —— 这是必需的，因为 SDK 的传输实现使用了 anyio 取消作用域，
必须在单个 task 中进入和退出。工具调用可以从同一个事件循环上的任何 task 中被 await 等待，这是安全的。

来自（同步）ToolRegistry 的工具执行通过 `run_coroutine_threadsafe` 桥接回此处 —— 详见 `coworker/mcp/tools.py`。

[English]
MCPManager — our own thin async MCP client over the official `mcp` SDK.

Async-native (no `nest_asyncio`, no second event loop): each server runs in a dedicated
asyncio task that opens the transport + `ClientSession`, keeps them alive until shutdown,
then closes them in the *same* task — required because the SDK's transports use anyio cancel
scopes that must be entered and exited on one task. Tool calls are awaited from any task on
the same loop, which is safe.

Tool execution from the (sync) ToolRegistry bridges back here via
`run_coroutine_threadsafe` — see `coworker/mcp/tools.py`.
"""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import AsyncExitStack
from typing import Any, IO, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from .config import MCPServerDef


_STDERR_TAIL_LINES = 20
_STDERR_TAIL_CHARS = 1500


def _read_tail(errfile: Optional[IO[str]]) -> Optional[str]:
    """[中文] 捕获的 stderr 文件的最后几行 —— 崩溃证据，而非完整日志。 / [English] Last few lines of a captured stderr file — the crash evidence, not the log."""
    if errfile is None:
        return None
    try:
        errfile.seek(0)
        text = errfile.read()
    except (OSError, ValueError):
        return None
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return None
    return "\n".join(lines[-_STDERR_TAIL_LINES:])[-_STDERR_TAIL_CHARS:]


class _Conn:
    def __init__(self, session: ClientSession, tools: list[Any]) -> None:
        self.session = session
        self.tools = tools  # list[mcp.types.Tool]
        self.shutdown = asyncio.Event()


class MCPManager:
    """[中文] 管理按服务器名称键控的持久 MCP 连接；按需延迟连接。 / [English] Owns persistent MCP connections keyed by server name; lazy-connects on demand."""

    def __init__(self, secrets: Any = None) -> None:
        self._conns: dict[str, _Conn] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._stderr_tails: dict[str, str] = {}
        self._lock = asyncio.Lock()
        # [中文] 用于 OAuth 服务器令牌持久化的 SecretStore（mcp/oauth.py）；延迟默认值以便无 secrets 时库/CLI 模式仍可工作。
        # [English] SecretStore for OAuth servers' token persistence (mcp/oauth.py); lazy default
        # so library/CLI construction without secrets keeps working.
        self._secrets = secrets

    async def ensure(self, server: MCPServerDef, *, interactive: bool = False) -> _Conn:
        """[中文] 返回 `server` 的活动连接，必要时进行（一次）连接建立。

        `interactive=True`（仅在显式连接操作时）允许 OAuth 服务器运行浏览器登录流程；
        默认情况下拒绝该行为 —— 已存储的 token 和静默刷新仍然有效，但坚持要求重新授权的服务器会抛出
        InteractiveAuthRequired，而不是强行劫持用户的浏览器。

        [English]
        Return a live connection for `server`, connecting (once) if needed.

        `interactive=True` (explicit connect actions only) lets an OAuth server run
        the browser sign-in flow; the default refuses it — stored tokens and silent
        refresh still work, but a server that insists on re-authorization raises
        InteractiveAuthRequired instead of hijacking the user's browser.
        """
        async with self._lock:
            existing = self._conns.get(server.name)
            if existing is not None:
                return existing
            ready: asyncio.Future = asyncio.get_running_loop().create_future()
            self._tasks[server.name] = asyncio.create_task(
                self._serve(server, ready, interactive=interactive)
            )
            conn = await ready  # [中文] 传递连接错误 / [English] propagates connection errors
            self._conns[server.name] = conn
            return conn

    async def tools(self, server: MCPServerDef) -> list[Any]:
        return (await self.ensure(server)).tools

    async def verify(self, server: MCPServerDef, *, interactive: bool = False) -> _Conn:
        """[中文] 针对显式“测试连接”操作的真实健康检查。`ensure` 只是原样返回缓存的连接，
        这曾导致对已连接服务器的测试成为静默空操作，无法检测到已宕机的服务器（owner-hit 2026-08-21）。
        此处对缓存连接进行实际请求往返（tools/list，同时刷新工具集）；死连接会被拆除并全新重连。

        [English]
        A REAL health check for explicit Test actions. `ensure` returns a cached
        connection untouched, which made Test-on-Live a silent no-op that could not
        detect a dead server (owner-hit 2026-08-21). Here a cached connection is
        round-tripped (tools/list, refreshing the tool set); a dead one is torn
        down and reconnected fresh.
        """
        conn = self._conns.get(server.name)
        if conn is not None:
            try:
                listed = await asyncio.wait_for(conn.session.list_tools(), timeout=20)
                conn.tools = list(listed.tools)
                return conn
            except Exception:
                conn.shutdown.set()
                task = self._tasks.pop(server.name, None)
                if task is not None:
                    try:
                        await asyncio.wait_for(asyncio.shield(task), timeout=5)
                    except Exception:
                        task.cancel()
                self._conns.pop(server.name, None)  # [中文] _serve 也会 pop；双重保险 / [English] _serve pops too; belt and braces
        return await self.ensure(server, interactive=interactive)

    def last_stderr(self, name: str) -> Optional[str]:
        """[中文] `name` 最近一次启动失败时的 stderr 末尾内容（如有）。 / [English] Stderr tail from the most recent failed startup of `name`, if any."""
        return self._stderr_tails.get(name)

    async def call(
        self, name: str, tool: str, arguments: Optional[dict[str, Any]]
    ) -> Any:
        conn = self._conns.get(name)
        if conn is None:
            raise RuntimeError(f"MCP server not connected: {name}")
        result = await conn.session.call_tool(tool, arguments or {})
        return _result_payload(result)

    async def aclose(self) -> None:
        for conn in self._conns.values():
            conn.shutdown.set()
        for task in list(self._tasks.values()):
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except (asyncio.TimeoutError, Exception):
                task.cancel()
        self._conns.clear()
        self._tasks.clear()

    # -- [中文] 单服务器生命周期管理（单个 task 拥有进入与退出） / [English] per-server lifecycle (one task owns enter+exit) --------
    async def _serve(
        self, server: MCPServerDef, ready: asyncio.Future, *, interactive: bool = False
    ) -> None:
        errfile = None
        try:
            async with AsyncExitStack() as stack:
                if server.transport == "http":
                    if not server.url:
                        raise ValueError(
                            f"MCP server '{server.name}' is http but has no url"
                        )
                    auth = None
                    if server.auth == "oauth":
                        from ..secrets import SecretStore
                        from .oauth import build_auth

                        if self._secrets is None:
                            self._secrets = SecretStore()
                        auth = build_auth(
                            server.name,
                            server.url,
                            self._secrets,
                            interactive=interactive,
                        )
                    read, write, *_ = await stack.enter_async_context(
                        streamablehttp_client(
                            server.url, headers=server.headers or None, auth=auth
                        )
                    )
                else:
                    if not server.command:
                        raise ValueError(
                            f"MCP server '{server.name}' is stdio but has no command"
                        )
                    params = StdioServerParameters(
                        command=server.command,
                        args=server.args,
                        env=server.env or None,
                        cwd=server.cwd,
                    )
                    # [中文] 捕获子进程的 stderr，使启动崩溃留下 UI 可展示的证据（此处 SDK 需要真实的文件描述符）。
                    # [English] Capture the child's stderr so a startup crash leaves evidence
                    # the UI can show (the SDK needs a real file descriptor here).
                    errfile = tempfile.TemporaryFile(
                        mode="w+", encoding="utf-8", errors="replace"
                    )
                    read, write = await stack.enter_async_context(
                        stdio_client(params, errlog=errfile)
                    )
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                listed = await session.list_tools()
                conn = _Conn(session, list(listed.tools))
                self._stderr_tails.pop(server.name, None)
                if not ready.done():
                    ready.set_result(conn)
                await conn.shutdown.wait()
        except Exception as exc:  # [中文] 连接 / 初始化失败 / [English] connection / init failure
            tail = _read_tail(errfile)
            if tail:
                self._stderr_tails[server.name] = tail
            if not ready.done():
                ready.set_exception(exc)
        finally:
            if errfile is not None:
                try:
                    errfile.close()
                except OSError:
                    pass
            self._conns.pop(server.name, None)
            self._tasks.pop(server.name, None)


def _result_payload(result: Any) -> Any:
    """[中文] 将 CallToolResult 扁平化为引擎可以为模型序列化的结构。 / [English] Flatten a CallToolResult into something the engine can serialize for the model."""
    texts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            texts.append(text)
        else:  # [中文] 非文本内容（图像/资源） —— 进行格式化描述 / [English] non-text content (image/resource) — describe it
            texts.append(f"[{getattr(block, 'type', 'content')}]")
    body = "\n".join(texts)
    if getattr(result, "isError", False):
        return {"error": body or "MCP tool error"}
    structured = getattr(result, "structuredContent", None)
    if structured is not None and not body:
        return structured
    return body
