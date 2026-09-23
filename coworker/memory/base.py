"""持久化记忆 — 适配器接口与作用域。
Persistent memory — adapter interface + scopes.

记忆是位于短暂对话状态之上的长久保留层：持久事实、偏好偏好、任务说明、总结摘要。
作用域包括：全局（用户级）、工作区（项目级）、会话级。
后端为适配器模式（目前为 `SQLiteMemoryStore`，后续支持 `PostgresMemoryStore`）。
Memory is the long-lived layer above transient conversation state: durable facts,
preferences, task notes, summaries. Scopes: global (user-wide), workspace (per project),
session. Backends are adapters (`SQLiteMemoryStore` now, `PostgresMemoryStore` later).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Scope(str, Enum):
    """记忆项的作用域。
    Scope of memory items."""
    GLOBAL = "global"
    WORKSPACE = "workspace"
    SESSION = "session"


@dataclass
class MemoryItem:
    """持久化记忆条目数据类。
    Persistent memory item data class."""
    id: int
    scope: Scope
    content: str
    key: Optional[str] = None
    summary: Optional[str] = None
    workspace: Optional[str] = None
    session_id: Optional[str] = None
    created_at: Optional[str] = None


class MemoryStore(ABC):
    """记忆存储库的抽象基类。
    Abstract base class for memory stores."""

    @abstractmethod
    def add(
        self,
        content: str,
        *,
        scope: Scope = Scope.WORKSPACE,
        key: Optional[str] = None,
        summary: Optional[str] = None,
        workspace: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> MemoryItem: ...

    @abstractmethod
    def get(self, item_id: int) -> Optional[MemoryItem]: ...

    @abstractmethod
    def list(
        self,
        *,
        scope: Optional[Scope] = None,
        workspace: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> list[MemoryItem]: ...

    @abstractmethod
    def update(
        self, item_id: int, content: str, *, summary: Optional[str] = None
    ) -> Optional[MemoryItem]: ...

    @abstractmethod
    def delete(self, item_id: int) -> bool: ...

    @abstractmethod
    def delete_all(self, *, scope: Optional[Scope] = None) -> int: ...


# MEMORY-SPEC §7: 低于此渲染大小时，完整注入每一条记忆；高于此时，
# 该块切换到索引模式（最新的几条完整呈现，其余呈现为单行摘要，正文通过 memory_read 按需拉取）。
# 约 2k tokens：典型记忆条目为 20-40 tokens，因此这仅在超过约 50-100 条记忆时触发 —
# 且受支持的最弱运行环境（拥有 8k 上下文窗口的本地模型）绑定了此上限。
# MEMORY-SPEC §7: below this rendered size, every memory is injected in full; above it,
# the block flips to index mode (newest few in full, one-line summaries for the rest,
# bodies fetched on demand via memory_read). ~2k tokens: a typical memory is 20-40
# tokens, so this only trips past ~50-100 memories — and the weakest supported setup
# (a local model with an 8k context) binds the ceiling.
INDEX_THRESHOLD_CHARS = 8_000
# 在索引模式下，最新的 N 条保持完整展示：最近的事实具有超乎寻常的相关度，这在最关键的地方降低了两阶段调用的开销。
# In index mode the newest N stay in full: recent facts are disproportionately relevant,
# which softens the two-step recall cost where it matters most.
INDEX_FULL_NEWEST = 10

_INDEX_NOTE = (
    "(Some memories above show only a one-line summary. Call memory_read with the "
    "[#id]s before acting on anything a summary hints at.)"
)


def _index_line(item: MemoryItem) -> str:
    """单行渲染：保存的摘要，或针对在摘要功能引入前写入的历史行（无需数据迁移）截断首行。
    One-line rendering: the saved summary, or a truncated first line for rows
    written before summaries existed (no data migration)."""
    text = (item.summary or "").strip()
    if not text:
        text = item.content.strip().splitlines()[0] if item.content.strip() else ""
        if len(text) > 80:
            text = text[:77] + "..."
    return f"- [#{item.id}] {text}"


def format_memories(items: list[MemoryItem]) -> str:
    """完整渲染记忆以注入系统提示词。展示 ID 以便智能体可以修订记忆（`memory_update`）或废弃它（`memory_forget`）。
    Render memories in full for injection into the system prompt. Ids are shown so
    the agent can revise a memory (`memory_update`) or retire it (`memory_forget`)."""
    if not items:
        return ""
    lines = [f"- [#{item.id}] {item.content}" for item in items]
    return "Known memories (from earlier sessions):\n" + "\n".join(lines)


def format_memory_index(
    items: list[MemoryItem], *, full_newest: int = INDEX_FULL_NEWEST
) -> str:
    """索引模式渲染：最新的 `full_newest` 条保持完整展示，其余呈现单行摘要，
    加上供 `memory_read` 使用的“行动前请先拉取详情”提示说明。

    Index rendering: newest `full_newest` in full, one-line summaries for the rest,
    plus the fetch-before-acting note for memory_read."""
    if not items:
        return ""
    newest = {item.id for item in sorted(items, key=lambda i: i.id)[-full_newest:]}
    lines = [
        f"- [#{item.id}] {item.content}" if item.id in newest else _index_line(item)
        for item in items
    ]
    return (
        "Known memories (from earlier sessions):\n"
        + "\n".join(lines)
        + f"\n{_INDEX_NOTE}"
    )


def render_memory_block(
    items: list[MemoryItem], *, threshold_chars: int = INDEX_THRESHOLD_CHARS
) -> str:
    """注入的记忆文本块。在空间允许时为完整模式；当完整渲染超过字符阈值时自动且无缝切换到索引模式（MEMORY-SPEC §7）。
    在每次引擎构建时评估一次 — 会话在其整个生命周期中始终处于确定的一种模式下。

    The injected memories block. Full mode while it's affordable; automatically and
    invisibly flips to index mode when the full rendering exceeds the threshold
    (MEMORY-SPEC §7). Evaluated once per engine build — a session is always in exactly
    one mode for its whole life."""
    full = format_memories(items)
    if len(full) <= threshold_chars:
        return full
    return format_memory_index(items)
