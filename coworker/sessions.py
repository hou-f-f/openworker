"""[中文] 会话记录 — 单次对话的元数据 + 消息列表。

存储位于 `coworker.conversations.ConversationStore`：按项目建立键的 SQLite 索引，
每个对话的消息存储在仅追加的 `.jsonl` 文件中。

[English]
Session record — the metadata + messages for one conversation.

Storage lives in `coworker.conversations.ConversationStore`: a SQLite index keyed by
project, with each conversation's messages in an append-only `.jsonl` file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SessionRecord:
    session_id: str
    workspace: str
    model: str
    mode: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    title: Optional[str] = None
    agent: str = "code"
    message_count: int = 0
    updated_at: Optional[str] = None
    # [中文] 除主要草稿目录之外添加到会话的文件夹，每个形如 {path, writable, label}。
    # 主要草稿目录在引擎构建时重新置备，因此仅持久化这些额外文件夹。
    # [English]
    # Folders added to the session beyond its primary scratch dir, each {path, writable, label}.
    # The primary scratch is re-provisioned at engine build, so only these extras are persisted.
    extra_roots: list[dict[str, Any]] = field(default_factory=list)
    # [中文] 在此会话中授予的“总是允许”审批（{tools: [...], commands: [...]}）—
    # 设计上限定于会话范围，但会话生命周期长于进程，因此这些授权也必须持久保存
    # （2026-07-22 负责人踩坑：每次重启都会丢失授权）。
    # [English]
    # "Always allow" approvals granted in this session ({tools: [...], commands: [...]}) —
    # session-scoped by design, but the session outlives the process, so they must too
    # (owner-hit 2026-07-22: grants forgotten on every restart).
    grants: dict[str, Any] = field(default_factory=dict)
    pinned: bool = False
    archived: bool = False
    # [中文] 组织下的设备群（2026-09-02）：在机器上启动此会话的已验证登录身份（由控制器盖戳，首次写入生效）。
    # "" = 本地/桌面或自动化启动。
    # [English]
    # Fleet under the org (2026-09-02): the verified login that STARTED this session on a
    # machine (controller-stamped, first writer wins). "" = local/desktop or automated.
    actor: str = ""
    # [中文] 会话来源（非用户直接启动时，§31）：机器标识符 + 显示标签（例如 origin="slack", origin_label="#general · T0ABCD"）。
    # 在派生时设置一次。
    # [English]
    # Where the session came from, when not user-started (§31): machine key + display label
    # (e.g. origin="slack", origin_label="#general · T0ABCD"). Set once at spawn.
    origin: Optional[str] = None
    origin_label: Optional[str] = None
    # [中文] 自动压缩状态（OPE-27）：CompactionState.as_dict()，从未压缩时为 {}。
    # 进行持久化，以便重新加载的会话保留其已压缩的出站视图。
    # [English]
    # Auto-compaction state (OPE-27): CompactionState.as_dict(), {} when never compacted.
    # Persisted so a reloaded session keeps its compacted outbound view.
    compaction: dict[str, Any] = field(default_factory=dict)
    # [中文] 第二十轮演进：显式的每会话项目绑定，{} = 从工作区派生。键为 "memory" / "board"，值为 project_names 中的名称。
    # [English]
    # Twentieth pass: explicit per-session project bindings, {} = derive from the
    # workspace. Keys "memory" / "board", values = names in project_names.
    bindings: dict[str, Any] = field(default_factory=dict)
    # [中文] Agent 团队：普通会话为 {}。Worker 为 {team_id, role: "worker", actor, lead_session, space}。
    # Lead 在人员编制门控创建团队时获得其条目。驱动工具绑定（看板 actor 标识）+ 侧边栏可展开条目。
    # [English]
    # Agent teams: {} for plain sessions. Workers: {team_id, role: "worker", actor,
    # lead_session, space}. Leads gain their entry when the staffing gate creates the
    # team. Drives tool binding (board actor identity) + the sidebar's expandable entry.
    team: dict[str, Any] = field(default_factory=dict)
    # [中文] Token 计数（跨机器连接器规范 §5）：保存时从 Assistant 消息的 `usage` sidecar 折叠计算的每个模型总计 —
    # {model: {input, output, cache_read, cache_write, turns}}。不包含任何金额计算；预算控制将在后续版本支持。
    # [English]
    # Token counting (connectors-across-machines spec §5): per-model totals folded from
    # the assistant messages' `usage` sidecars at save time — {model: {input, output,
    # cache_read, cache_write, turns}}. No dollars anywhere; budgets are a later pass.
    usage: dict[str, Any] = field(default_factory=dict)
    # [中文] 由配置触发启动（连接器规范 §10）：{config_id, event, name, instructions, clone, worktree, owner_repo, number}。
    # `instructions` 仅为此会话加入用户的长期规则；`worktree` 在归档或删除时被移除（保留 clone）。对于所有其他会话为 {}。
    # [English]
    # Started by a configuration (connectors spec §10): {config_id, event, name,
    # instructions, clone, worktree, owner_repo, number}. `instructions` join the
    # user's standing rules for this session only; `worktree` is removed on archive
    # or delete (the clone stays). {} for every other session.
    spawn: dict[str, Any] = field(default_factory=dict)


USAGE_FIELDS = ("input", "output", "cache_read", "cache_write")


def usage_totals(messages: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """[中文] 将 Assistant 消息的 `usage` sidecar 折叠为每个模型的总量（规范 §5）。
    从对话记录中确定性计算，因此重新保存绝不会重复计数。
    [English] Fold the assistant messages' `usage` sidecars into per-model totals (spec §5).
    Deterministic from the transcript, so a re-save never double counts."""
    out: dict[str, dict[str, int]] = {}
    for m in messages or []:
        if m.get("role") != "assistant":
            continue
        u = m.get("usage")
        if not isinstance(u, dict):
            continue
        key = str(u.get("model") or "unknown")
        row = out.setdefault(key, {f: 0 for f in USAGE_FIELDS} | {"turns": 0})
        for f in USAGE_FIELDS:
            try:
                row[f] += max(int(u.get(f) or 0), 0)
            except (TypeError, ValueError):
                pass
        row["turns"] += 1
    return out
