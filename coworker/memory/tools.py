"""记忆工具 — 智能体显式操作记忆的路径。
Memory tools — the agent's explicit paths into memory.

`remember` 保存新事实；`memory_update` / `memory_forget` 根据已知记忆块中显示的 [#id] 修订或废弃记忆，
使得更正能够替换陈旧事实，而不是在旁边堆积重复。
`memory_read` 按 ID 获取完整正文 — 索引模式的检索半边（MEMORY-SPEC §7）；始终注册，在完整模式下无害。

`on_saved` 是保存通知钩子（规范 §5.1）：管理器传入一个回调，将会话事件 `memory_saved` 推送到界面，
以便在转录本中内联渲染“我会记住这一点 — … [撤销 Undo]”。
它也对 `memory_update` 触发 —“更新而非重复”的规则意味着许多保存实际上是对现有记忆的编辑，
这些之前是不可见的（2026-07-28 踩坑），通知携带修改前的文本，以便撤销操作可以恢复原状。回调中的异常绝不会导致写入失败。
The agent's explicit paths into memory.

`remember` saves a new fact; `memory_update` / `memory_forget` revise or retire one by
the [#id] shown in the known-memories block, so corrections replace stale facts instead
of piling up next to them. `memory_read` fetches full bodies by id — the retrieval half
of index mode (MEMORY-SPEC §7); registered always, harmless in full mode.

`on_saved` is the save-notice hook (spec §5.1): the manager passes a callback that pushes
a memory_saved event to the session's surface so it can render "I'll remember that — …
[Undo]" inline in the transcript. It fires for `memory_update` too — the
update-don't-duplicate rule means many saves arrive as edits to an existing memory, and
those were invisible (owner-hit 2026-07-28) — carrying the previous text so Undo can put
it back. Failures in the callback never fail the write.
"""

from __future__ import annotations

from typing import Callable, Optional

import aisuite as ai

from .base import MemoryItem, MemoryStore, Scope

_SCOPES = {s.value for s in Scope}

_META = dict(category="memory", risk_level="low", capabilities=["remember"])


def memory_tools(
    store: MemoryStore,
    *,
    workspace: Optional[str],
    on_saved: Optional[Callable[[MemoryItem, Optional[str]], None]] = None,
    saving_enabled: Optional[Callable[[], bool]] = None,
) -> list:
    """智能体的记忆工具集。
    The agent's memory tools.

    `saving_enabled` 是每次写入时检查的实时可调用对象，因此“设置”中的开关可立即适用于已经在运行的会话 —
    双向均生效（2026-07-28 踩坑：关闭时持续保存，开启后持续拒绝）。
    工具注册在构建时固定，因此写入工具始终处于注册状态并在保存关闭时拒绝写入；
    `memory_read` 永远不受门禁限制（关闭保存 = 停止学习，而非遗忘）。
    `saving_enabled` is a LIVE callable checked on each write, so the Settings switch
    applies to conversations already running — in BOTH directions (owner-hit
    2026-07-28: off kept saving, then on kept refusing). The registry is fixed at
    build, so the write tools are always registered and refuse when saving is off;
    `memory_read` never gates (off = stop learning, not amnesia).
    """

    def _saving_off() -> bool:
        return saving_enabled is not None and not saving_enabled()

    _OFF_ERROR = (
        "Saving memories is turned off in the user's Settings (they can turn it back "
        "on in Settings ▸ Memory). Nothing was saved — tell the user plainly instead "
        "of implying you remembered it."
    )

    def _announce(item: MemoryItem, previous: Optional[str]) -> None:
        """向用户呈现写入通知（§5.1）。尽力而为：通知不值得让已经成功的写入操作失败。
        Surface the write to the user (§5.1). Best-effort: the notice is never worth
        failing a write that already succeeded."""
        if on_saved is None:
            return
        try:
            on_saved(item, previous)
        except Exception:
            pass
    def remember(content: str, summary: str = "", scope: str = "workspace") -> dict:
        """保存持久化记忆（事实或偏好），以便在未来的会话中召回。
        先检查已知记忆列表：如果已有条目覆盖了此内容，请使用 memory_update 而不是保存近乎重复的新条目。

        Save a durable memory (a fact or preference) to recall in future sessions.
        Check the known-memories list first: if one already covers this, use
        memory_update instead of saving a near-duplicate.

        Args:
            content (str): 要记住的事项，附带原因。 / The thing to remember, with the why.
            summary (str): 紧凑列表中显示的单行要点（最多 15 个词）。 / One-line gist (15 words max) shown in compact listings.
            scope (str): "global"（关于用户的事实 — 随处适用）或 "workspace"（仅关于当前项目的事实）。 / "global" (facts about the user — applies everywhere) or "workspace" (facts about this project only).
        """
        if _saving_off():
            return {"saved": False, "error": _OFF_ERROR}
        chosen = Scope(scope) if scope in _SCOPES else Scope.WORKSPACE
        if chosen is Scope.SESSION:  # 废弃作用域（规范 §3）：绝不保存到该作用域 / dead scope (spec §3): never save to it
            chosen = Scope.WORKSPACE
        item = store.add(
            content,
            scope=chosen,
            summary=summary.strip() or None,
            workspace=workspace if chosen is Scope.WORKSPACE else None,
        )
        _announce(item, None)
        return {"id": item.id, "scope": item.scope.value, "saved": True}

    def memory_read(memory_ids: list[int]) -> dict:
        """按 ID 读取记忆的完整内容（当已知记忆列表仅显示单行摘要，且你在行动前需要详细信息时使用）。

        Read the full content of memories by id (use when the known-memories list
        shows only a one-line summary and you need the details before acting).

        Args:
            memory_ids (list[int]): 要获取的记忆 [#id] 列表。 / The [#id]s to fetch.
        """
        found, missing = [], []
        for mid in memory_ids:
            item = store.get(int(mid))
            if item is None:
                missing.append(int(mid))
            else:
                found.append(
                    {"id": item.id, "scope": item.scope.value, "content": item.content}
                )
        result: dict = {"memories": found}
        if missing:
            result["missing"] = missing
        return result

    def memory_update(memory_id: int, content: str, summary: str = "") -> dict:
        """使用更正或完善的内容重写现有记忆。

        Rewrite an existing memory with corrected or refined content.

        Args:
            memory_id (int): 记忆 ID，来自已知记忆列表中的 [#id]。 / The memory's id, from the [#id] in the known-memories list.
            content (str): 完整更正后的记忆正文（替换旧文本）。 / The full corrected memory text (replaces the old text).
            summary (str): 更正后的单行要点（最多 15 个词）。 / Corrected one-line gist (15 words max).
        """
        if _saving_off():
            return {"updated": False, "error": _OFF_ERROR}
        # 在写入前捕获，以便用户的“撤销”操作可以还原先前的措辞。
        # Captured BEFORE the write so the user's Undo can restore the old wording.
        existing = store.get(memory_id)
        previous = existing.content if existing is not None else None
        item = store.update(memory_id, content, summary=summary.strip() or None)
        if item is None:
            return {"updated": False, "error": f"no memory with id {memory_id}"}
        _announce(item, previous)
        return {"updated": True, "id": item.id}

    def memory_forget(memory_id: int) -> dict:
        """删除被证实错误或不再成立的记忆。

        Delete a memory that turned out to be wrong or is no longer true.

        Args:
            memory_id (int): 记忆 ID，来自已知记忆列表中的 [#id]。 / The memory's id, from the [#id] in the known-memories list.
        """
        if _saving_off():
            return {"deleted": False, "error": _OFF_ERROR}
        if store.delete(memory_id):
            return {"deleted": True, "id": memory_id}
        return {"deleted": False, "error": f"no memory with id {memory_id}"}

    return [
        ai.tool(fn, metadata=ai.ToolMetadata(**_META))
        for fn in (remember, memory_read, memory_update, memory_forget)
    ]
