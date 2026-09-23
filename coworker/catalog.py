"""[中文] 经过审查的工具目录 — Persona 所引用的稳定 ``id → capability`` 层。

*Capability* 将一组工具（现有的 ``tools/`` 工厂）打包在一个稳定的 id 之后，
外加该能力所需的会话上下文（``requires``）以及其可能产生的风险类别（``risk``，用于第 2 阶段安装许可授权屏幕）。
``expand(ids, context)`` 将 Persona 的 ``tools:`` 列表转换为具体的可调用工具，跳过上下文先决条件未满足的能力
（例如没有 executor 就不能使用 shell）— 匹配过去手动装配工具的各个 Agent 工厂。

目录是**平台专有且封闭的**：第三方通过我们在目录中增加经过审查的能力以及通过 MCP 来获得扩展，绝不能自行添加条目。
MCP 工具*不*在目录中（参见 ``PERMISSIONS-AND-INBOX.md``）。

[English]
Vetted tool catalog — the stable ``id → capability`` layer a persona references.

A *capability* bundles a group of tools (the existing ``tools/`` factories) behind a stable
id, plus what session context it needs (``requires``) and the risk classes it can produce
(``risk``, used by the Phase 2 install-consent screen). ``expand(ids, context)`` turns a
persona's ``tools:`` list into concrete callables, skipping capabilities whose context
prerequisites aren't met (e.g. no shell without an executor) — matching the per-agent
factories that used to assemble tools by hand.

The catalog is **platform-owned and closed**: third parties get breadth from us adding
vetted capabilities here and from MCP, never by adding entries. MCP tools are *not* in the
catalog (see ``PERMISSIONS-AND-INBOX.md``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import aisuite as ai

from .agents.base import AgentContext
from .risk import RiskClass
from .tools.files import file_tools
from .tools.git import git_tools
from .tools.search import search_tools
from .tools.shell import shell_tools
from .tools.todo import todo_tools

# [中文] 一项能力可能要求的上下文先决条件，映射为作用于 AgentContext 的谓词。
# [English] Context prerequisites a capability may require, mapped to a predicate over AgentContext.
_REQUIREMENTS: dict[str, Callable[[AgentContext], bool]] = {
    "workspace": lambda c: c.workspace is not None,
    "executor": lambda c: c.executor is not None,
    "todo": lambda c: c.todo is not None,
}


@dataclass(frozen=True)
class Capability:
    id: str
    name: str  # [中文] 人类可读标签（授权屏幕） / [English] human label (consent screen)
    description: str
    build: Callable[[AgentContext], list]
    requires: tuple[str, ...] = ()
    risk: tuple[RiskClass, ...] = (RiskClass.READ,)

    def available(self, context: AgentContext) -> bool:
        return all(_REQUIREMENTS[r](context) for r in self.requires)


# -- [中文] 能力构建器 / [English] capability builders --------------------------------------------------------
# [中文] 这些构建器精确重现了 Code 和 Cowork agent 工厂过去手动装配的内容。
# [English]
# These reproduce, exactly, what the Code and Cowork agent factories assembled by hand.

# [中文] OPE-186 改动 2：aisuite 的文件工具各自仅有一行通用描述
# （"Write a UTF-8 text file under the configured root."），因此模型无法得知何时应重写文件、何时应编辑文件。
# 在一次长期的多任务会话中，Kimi K3 选择了 1,812 次 write_file，而针对性编辑仅有 609 次，并且它写入的每一个完整文件内容
# 都在后续的每一轮对话中一直携带。将指导信息构建到工具描述中是通常的解决手段；这些描述即用于此目的。
# [English]
# OPE-186 change 2: aisuite's file tools describe themselves in one generic line each
# ("Write a UTF-8 text file under the configured root."), so nothing tells the model when
# to rewrite a file and when to edit it. In one long multi-task session Kimi K3 chose
# write_file 1,812 times against 609 targeted edits, and every whole-file body it wrote
# rode along in the conversation on every later turn. Building the guidance into the
# tool description is the usual remedy; these descriptions do that.
EDIT_TOOL_GUIDANCE: dict[str, str] = {
    "write_file": (
        "Create a NEW file, or replace an existing file's ENTIRE content when most of it "
        "changes. For a small or medium change to an existing file do not rewrite it: read "
        "it, then use replace_in_file (exact text swap) or apply_patch (multi-line edit). "
        "The whole `content` you pass stays in the conversation on every later turn, so "
        "whole-file rewrites of large files are expensive."
    ),
    "replace_in_file": (
        "Edit an existing file in place: replace `old` (an exact, unique text fragment, "
        "whitespace included) with `new`. Prefer this over write_file for changing part of "
        "a file; read the file first so `old` matches exactly. Set expected_replacements "
        "when the fragment appears more than once."
    ),
    "apply_patch": (
        "Apply a Codex-style patch (*** Begin Patch / *** Update File: path / @@ hunks of "
        "' ' context, '-' removed and '+' added lines / *** End Patch) for targeted "
        "multi-line edits to one or more existing files, or to add or delete files. Prefer "
        "this over write_file for edits that touch several places."
    ),
    "apply_unified_diff": (
        "Apply a standard unified diff (the format of `diff -u` or `git diff`) to existing "
        "files. Use it when you already have the change as a diff."
    ),
}


def _describe_edit_tools(tools: list) -> list:
    """[中文] 为文件工具添加描述，说明何时使用哪种工具（注册表根据 docstring 构建模型看到的 schema）。
    [English] Give the file tools descriptions that say WHEN to use each (the registry builds the
    schema the model sees from the docstring)."""
    for t in tools:
        text = EDIT_TOOL_GUIDANCE.get(getattr(t, "__name__", ""))
        if text:
            try:
                t.__doc__ = text
            except (AttributeError, TypeError):
                pass
    return tools


def _code_files(context: AgentContext) -> list:
    """[中文] 代码库导向的文件工具：带行号/窗口化的 `read_file`。我们的 `grep` 和窗口化 `read_file`
    替代了 aisuite 较慢的 `search_files` / `read_file`/`read_file_lines`。
    多根目录感知（通用草稿目录 universal scratch）：有了会话根目录，写入/读取也能触达草稿目录和授权目录；工作区保持为相对路径锚点。

    [English] Repo-oriented files: line-numbered/windowed `read_file`. Our `grep` and windowed
    `read_file` replace aisuite's slower `search_files` / `read_file`/`read_file_lines`.
    Multi-root aware (universal scratch): with session roots, writes/reads reach the
    scratch and granted dirs too; the workspace stays the relative-path anchor.
    """
    ws = str(context.workspace)
    replaced = {"search_files", "read_file", "read_file_lines"}
    file_kwargs = (
        {"roots": context.roots} if context.roots else {"root": ws, "allow_write": True}
    )
    files = _describe_edit_tools(
        [
            t
            for t in ai.toolkits.files(**file_kwargs)
            if getattr(t, "__name__", "") not in replaced
        ]
    )
    return [*files, *file_tools(ws, roots=context.roots)]


def _files(context: AgentContext) -> list:
    """[中文] 知识工作文件工具：多根目录感知（跨会话根目录读取/写入）。
    全局统一读取器（2026-08-20 负责人裁定）：窗口化、带行号的 `read_file` 替代 aisuite 的
    `read_file`/`read_file_lines`，我们的 `grep` 替代慢速的 `search_files` — 与 Code 所用集合完全相同。

    [English] Knowledge-work files: multi-root aware (reads/writes across the session's roots).
    One reader everywhere (owner ruling 2026-08-20): the windowed, line-numbered
    `read_file` replaces aisuite's `read_file`/`read_file_lines`, and our `grep`
    replaces the slow `search_files` — same set Code uses.
    """
    ws = str(context.workspace)
    file_kwargs = (
        {"roots": context.roots} if context.roots else {"root": ws, "allow_write": True}
    )
    replaced = {"search_files", "read_file", "read_file_lines"}
    files = _describe_edit_tools(
        [
            t
            for t in ai.toolkits.files(**file_kwargs)
            if getattr(t, "__name__", "") not in replaced
        ]
    )
    return [*files, *file_tools(ws, roots=context.roots)]


def _git(context: AgentContext) -> list:
    ws = str(context.workspace)
    # [中文] git_status, git_diff, git_log
    # [English] git_status, git_diff, git_log
    return [*ai.toolkits.git(root=ws), *git_tools(ws)]


def _search(context: AgentContext) -> list:
    # [中文] grep（ripgrep，感知 .gitignore） / [English] grep (ripgrep, .gitignore-aware)
    return search_tools(str(context.workspace))


def _shell(context: AgentContext) -> list:
    # [中文] run_shell + 后台任务工具 / [English] run_shell + background task tools
    return shell_tools(context.executor)


def _todo(context: AgentContext) -> list:
    # [中文] todo_write（驱动前端进度面板） / [English] todo_write (drives the Progress panel)
    return todo_tools(context.todo)


_CAPS: list[Capability] = [
    Capability(
        id="code_files",
        name="Code files",
        description="Read & edit files in a single repo workspace (line-numbered reads).",
        build=_code_files,
        requires=("workspace",),
        risk=(RiskClass.READ, RiskClass.WRITE_LOCAL),
    ),
    Capability(
        id="files",
        name="Files",
        description="Read & edit files across the session's workspace folders.",
        build=_files,
        requires=("workspace",),
        risk=(RiskClass.READ, RiskClass.WRITE_LOCAL),
    ),
    Capability(
        id="git",
        name="Git",
        description="Inspect git state and history (status, diff, log).",
        build=_git,
        requires=("workspace",),
        risk=(RiskClass.READ,),
    ),
    Capability(
        id="search",
        name="Search",
        description="Fast code/content search (grep).",
        build=_search,
        requires=("workspace",),
        risk=(RiskClass.READ,),
    ),
    Capability(
        id="shell",
        name="Shell",
        description="Run shell commands in a persistent session.",
        build=_shell,
        requires=("executor",),
        risk=(RiskClass.EXEC,),
    ),
    Capability(
        id="todo",
        name="Task list",
        description="Maintain a visible task/progress list.",
        build=_todo,
        requires=("todo",),
        risk=(RiskClass.READ,),
    ),
]

CATALOG: dict[str, Capability] = {c.id: c for c in _CAPS}


def capability(cap_id: str) -> Capability:
    cap = CATALOG.get(cap_id)
    if cap is None:
        raise KeyError(f"Unknown capability id: {cap_id!r}")
    return cap


def expand(ids: list[str], context: AgentContext) -> list:
    """[中文] 将 Persona 的 ``tools:`` id 列表展开为此上下文的具体工具可调用对象。
    未满足上下文先决条件的能力将被跳过（没有 executor 就没有 shell，没有 workspace 就没有 files）—
    这与旧的手写工厂完全一致。

    [English] Expand a persona's ``tools:`` id list into concrete tool callables for this context.
    Capabilities whose context prerequisites aren't met are skipped (no shell without an
    executor, no files without a workspace) — exactly like the old hand-written factories.
    """
    tools: list = []
    for cap_id in ids:
        cap = capability(cap_id)
        if cap.available(context):
            tools.extend(cap.build(context))
    return tools


def risk_summary(ids: list[str]) -> set[RiskClass]:
    """[中文] 工具列表可能产生的风险类别的并集 — 用于安装许可授权屏幕。
    [English] The union of risk classes a tool list can produce — for the install-consent screen."""
    out: set[RiskClass] = set()
    for cap_id in ids:
        out.update(capability(cap_id).risk)
    return out
