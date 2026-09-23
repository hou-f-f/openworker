"""[中文] `explore` 工具 —— 拥有独立上下文窗口的只读代码探索调研子智能体。

泛化宽泛的问题（如“重试逻辑在哪里处理？”）会因为读取几十个文件而大量消耗主会话的上下文容量。
`explore` 工具在同一个工作区上启动一个子 TurnEngine，配备只读工具和全新的上下文；
最终仅将其生成的调研报告返回给调用方。

子智能体在 plan 模式下运行 —— 无论子智能体决定做什么，PermissionEngine 都会硬性拦截所有写入/命令执行操作 ——
且无需审批人，因此永远不需要往返等待批准。这也使得 `explore` 能够带有低风险元数据，
进而允许智能体在单轮回复中并发发起多个调研任务。防止递归：子智能体的工具注册表中不包含 `explore` 工具。

The `explore` tool — a read-only research subagent with its own context window.

Broad questions ("where is retry logic handled?") burn the main session's context on
dozens of file reads. `explore` spawns a child TurnEngine over the same workspace with
read-only tools and a fresh context; only its final report returns to the caller.

The child runs in plan mode — the PermissionEngine hard-blocks writes/shell no matter
what the child decides — with no approver, so it never needs an approval round-trip.
That's what lets `explore` carry low-risk metadata, which in turn makes several explores
in one assistant turn eligible for the engine's parallel execution. No recursion: the
child registry has no `explore` tool.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

import aisuite as ai

from ..engine import TurnEngine
from ..events import EventType
from ..permissions import Mode, PermissionEngine
from ..tools import ToolRegistry
from .files import file_tools
from .git import git_tools
from .search import search_tools

EXPLORER_INSTRUCTIONS = """You are a read-only code explorer working inside the user's workspace. \
Answer the research task you're given by searching and reading the code (`grep`, `read_file`, \
`list_files`, `git_log`, `git_status`, `git_diff`). You cannot write files or run commands.

Your final message is your report — it goes back to the agent that spawned you, not to the \
user. Make it self-contained: answer the task directly, reference code as path:line, quote the \
key snippets, and note anything surprising you found along the way. If you couldn't find \
something, say what you searched so the caller doesn't repeat the same searches."""

_CHILD_MAX_ITERATIONS = 10


def build_explorer_engine(
    *,
    workspace: str | Path,
    provider: Any,
    model: str,
    model_settings: Optional[dict[str, Any]] = None,
    max_iterations: int = _CHILD_MAX_ITERATIONS,
) -> TurnEngine:
    """[中文] 构建一个具备代码智能体只读工具集和全新独立上下文的子引擎。
    A child engine with the Code agent's read-only tools and a fresh context."""
    ws = str(Path(workspace).resolve())
    registry = ToolRegistry()
    # [中文] 代码智能体工具集的只读切片，包含相同的工具套件替换项
    # （用我们自己的 grep 替换 search_files，用窗口化的 read_file 替换 read_file/read_file_lines）。
    # Read-only slice of the Code agent's toolset, with the same toolkit replacements
    # (our grep for search_files, our windowed read_file for read_file/read_file_lines).
    replaced = {"search_files", "read_file", "read_file_lines"}
    registry.register_all(
        [
            t
            for t in ai.toolkits.files(root=ws)  # [中文] 不启用 allow_write → 仅 list/read / no allow_write → list/read only
            if getattr(t, "__name__", "") not in replaced
        ]
    )
    registry.register_all(file_tools(ws))
    registry.register_all(ai.toolkits.git(root=ws))  # git_status, git_diff
    registry.register_all(git_tools(ws))  # git_log
    registry.register_all(search_tools(ws))  # grep
    permissions = PermissionEngine(workspace_root=Path(ws), mode=Mode.PLAN)
    return TurnEngine(
        provider=provider,
        registry=registry,
        permissions=permissions,
        model=model,
        instructions=EXPLORER_INSTRUCTIONS,
        max_iterations=max_iterations,
        model_settings=model_settings,
    )


def explorer_tools(
    *,
    workspace: str | Path,
    provider: Any,
    model: str,
    model_settings: Optional[dict[str, Any]] = None,
) -> list:
    def explore(task: str) -> dict:
        """[中文] 将宽泛的只读调研任务委托给具有全新独立上下文窗口的子智能体。
        子智能体搜索并阅读工作区中的代码，最终仅返回其调研报告 —— 中间读取的文件内容绝不会污染你的上下文。
        适用于涉及多个文件的问题（例如“X 在何处处理？”、“流程 Y 是如何工作的？”）；
        若针对单个已知文件，直接自行读取即可。
        在一次回复中同时请求的多个独立探索调用将并行执行。
        请精确陈述任务并指明报告应包含哪些内容。

        Args:
            task (str): 调研问题、约束条件以及报告的预期形式。

        Delegate a broad, read-only research task to a subagent with its own fresh
        context window. It searches and reads the workspace, then returns only its final
        report — the intermediate file reads never touch your context. Use it for
        multi-file questions ("where is X handled?", "how does the Y flow work?"); for a
        single known file, just read it yourself. Independent explore calls run in
        parallel when requested together. State the task precisely and say what the
        report should include.

        Args:
            task (str): The research question, with any constraints and the expected
                shape of the report.
        """
        engine = build_explorer_engine(
            workspace=workspace,
            provider=provider,
            model=model,
            model_settings=model_settings,
        )

        async def _run() -> tuple[str, str]:
            report, status = "", "unknown"
            async for event in engine.run(task):
                if event.type == EventType.ASSISTANT_MESSAGE and event.data.get("text"):
                    report = event.data["text"]
                elif event.type == EventType.TURN_END:
                    status = event.data.get("status", "unknown")
                elif event.type == EventType.ERROR:
                    return report, f"error: {event.data.get('error', '')}"
            return report, status

        # [中文] 工具在工作线程中执行（没有正在运行的事件循环），因此 asyncio.run 是安全的。
        # Tools execute in a worker thread (no running loop), so asyncio.run is safe.
        report, status = asyncio.run(_run())
        if not report:
            return {"error": f"explorer produced no report (status: {status})"}
        result: dict[str, Any] = {"report": report}
        if status != "completed":
            result["note"] = (
                f"explorer stopped early ({status}); the report may be partial"
            )
        return result

    return [
        ai.tool(
            explore,
            metadata=ai.ToolMetadata(
                category="search",
                risk_level="low",
                capabilities=["search"],
                requires_approval=False,
            ),
        )
    ]
