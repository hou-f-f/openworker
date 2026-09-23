"""[中文] 自动化数据模型 —— 定时调度任务本身是一个持久化实体（参见 docs/AUTOMATION-SCHEDULING.md）。每次触发都会针对任务指令启动一次全新的 Run，并记录在任务自有的会话线程及工作目录中。

[English]
Automation data model — a scheduled task is its own persistent entity (see
docs/AUTOMATION-SCHEDULING.md). Each fire is a fresh Run of the task's instructions, recorded
in the task's own thread + working folder.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

# [中文] 按 cron 星期几索引：0 和 7 代表周日，1 代表周一……6 代表周六。必须以周日开头。
# [English] Indexed by cron day-of-week: 0 and 7 are Sunday, 1 is Monday … 6 is Saturday. Must start at Sunday.
_DOW = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def _now() -> float:
    return time.time()


# -- standing scoped approvals (UX-DECISIONS §25) --------------------------------
# [中文] 常驻作用域授权（UX-DECISIONS §25）：
# `always_allowed_tools` 条目要么是纯工具名（旧版，对该工具的任何参数均予放行），
# 要么是 "tool target"（以单个空格分隔，工具名本身从不包含空格）—— 将授权绑定到某个确切目标（频道地址、接收者等）。
# 规则保存在任务记录中，因此撤销仅针对特定自动化任务，删除任务也会一同清理规则。
# [English]
# An `always_allowed_tools` entry is either a bare tool name (legacy, allows the tool
# against any argument) or "tool target" — one space, tool names never contain spaces —
# binding the allowance to one exact target (channel address, recipient, …). Rules live
# on the task record so revocation is per-automation and deletion takes them along.


# [中文] 构造授权规则条目："tool target" 或纯 "tool"
# [English] Construct rule entry: "tool target" or bare "tool"
def rule_entry(tool: str, target: Optional[str] = None) -> str:
    return f"{tool} {target}" if target else tool


# [中文] 解析授权规则条目，拆分为工具名和目标标识
# [English] Parse rule entry into tool name and target identifier
def rule_parts(entry: str) -> tuple[str, Optional[str]]:
    tool, _, target = entry.strip().partition(" ")
    return tool, (target.strip() or None)


def grant_entries(permissions: Any) -> list[str]:
    """[中文] 校验拟授权的 permissions 列表，收敛为实际可授予的规则条目。仅 access: "write" 的条目可成为授权；工具必须声明目标参数（按设计排除了执行/破坏性工具），且目标必须非空。读权限仅用于披露（展示在许可卡片上，绝不持久化）。其他内容一律丢弃（fail-closed 安全闭合）。

    [English]
    Validate a proposed `permissions` list (from the create-tool schema or the GUI
    create payload) down to the entries actually grantable. Only `access: "write"` items
    become grants; the tool must declare a target argument (which excludes exec/destructive
    tools by construction) and the target must be non-empty. Reads are disclosure-only —
    rendered on the consent card, never stored. Anything else is dropped, fail-closed.
    """
    from ..connectors.tool_defs import rule_eligible

    entries: list[str] = []
    for item in permissions or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("access", "")).lower() != "write":
            continue
        tool = str(item.get("tool", "")).strip()
        target = str(item.get("target", "")).strip()
        if not tool or not target or not rule_eligible(tool):
            continue
        entry = rule_entry(tool, target)
        if entry not in entries:
            entries.append(entry)
    return entries


def _human_time(hour: int, minute: int) -> str:
    ampm = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    return f"{h12}:{minute:02d} {ampm}"


# [中文] 调度配置数据模型：支持 cron 或单次（once）执行
# [English] Schedule configuration dataclass: supports cron or one-time execution
@dataclass
class Schedule:
    kind: str  # "cron" | "once"
    cron: Optional[str] = None
    fire_at: Optional[str] = None  # ISO datetime for one-time
    timezone: str = (
        "local"  # 'local' = the machine's clock (a local-first tool default)
    )

    def human(self) -> str:
        """[中文] 尽力而为的人类可读描述标签（例如 'Every day at ~7:10 PM'）；若无法解析则回退到原始 cron 表达式。

        [English]
        Best-effort human label ('Every day at ~7:10 PM'); falls back to the raw cron."""
        if self.kind == "once":
            return f"Once at {self.fire_at}"
        parts = (self.cron or "").split()
        if len(parts) != 5:
            return self.cron or "?"
        minute, hour, dom, month, dow = parts
        try:
            t = _human_time(int(hour), int(minute))
        except ValueError:
            return self.cron  # non-trivial cron (ranges/steps) — show as-is
        if dom == "*" and dow == "*":
            return f"Every day at ~{t}"
        if dom == "*" and dow.isdigit():
            return f"Every {_DOW[int(dow) % 7]} at ~{t}"
        if dom.isdigit() and dow == "*":
            return f"Monthly on day {dom} at ~{t}"
        return self.cron

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "cron": self.cron,
            "fire_at": self.fire_at,
            "timezone": self.timezone,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Schedule":
        return cls(
            kind=d.get("kind", "cron"),
            cron=d.get("cron"),
            fire_at=d.get("fire_at"),
            timezone=d.get("timezone", "local"),
        )


# [中文] 定时调度任务数据模型：包含调度、指令、工作区、模型及常驻授权规则
# [English] Scheduled task dataclass: schedule, instructions, workspace, model, and standing approval rules
@dataclass
class ScheduledTask:
    title: str
    instructions: str
    schedule: Schedule
    workspace: str
    origin_surface: str = "cowork"  # where it was launched from (a reference)
    origin_session_id: str = ""
    agent: str = "cowork"
    id: str = field(default_factory=lambda: "task-" + uuid.uuid4().hex[:10])
    task_session_id: str = ""  # the task's OWN thread (set to f"__task__{id}")
    model: Optional[str] = None
    notify_on_completion: bool = True
    notify_target: Optional[str] = None  # extra messaging target ("telegram:123")
    always_allowed_tools: list[str] = field(default_factory=list)
    always_allowed_commands: list[str] = field(default_factory=list)
    enabled: bool = True
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    next_run: Optional[float] = None  # epoch seconds; computed by the store
    last_run: Optional[float] = None
    last_status: Optional[str] = None
    run_count: int = 0
    max_runs: Optional[int] = None
    # Sidebar unread tracking (UX-023): runs started after this mark count as
    # "unseen"; opening the automation's detail advances it. 0.0 = never opened.
    seen_runs_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.task_session_id:
            self.task_session_id = f"__task__{self.id}"

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["schedule"] = self.schedule.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ScheduledTask":
        d = dict(d)
        d["schedule"] = Schedule.from_dict(d.get("schedule") or {})
        return cls(**d)

    # -- standing rules (§25) --------------------------------------------------
    def standing_rules(self) -> dict[str, set[str]]:
        """[中文] 目标绑定的常驻授权条目，组织为 {tool: {targets}} 映射 —— 权限引擎将其与声明的目标参数进行匹配。

        [English]
        Target-bound entries as {tool: {targets}} — the shape the permission engine
        matches against the declared target argument."""
        out: dict[str, set[str]] = {}
        for entry in self.always_allowed_tools:
            tool, target = rule_parts(entry)
            if tool and target:
                out.setdefault(tool, set()).add(target)
        return out

    def name_allowed_tools(self) -> set[str]:
        """[中文] 兼容旧版的纯工具名条目（无目标绑定）—— 保持向后兼容的行为。

        [English]
        Legacy name-only entries (no target binding) — back-compatible behavior."""
        return {
            tool
            for tool, target in map(rule_parts, self.always_allowed_tools)
            if tool and target is None
        }

    # [中文] 为指定工具和目标添加常驻授权规则
    # [English] Add standing approval rule for specified tool and target
    def add_rule(self, tool: str, target: str) -> bool:
        entry = rule_entry(tool, target)
        if not tool or not target or entry in self.always_allowed_tools:
            return False
        self.always_allowed_tools.append(entry)
        return True

    # [中文] 撤销特定的常驻授权规则条目
    # [English] Revoke a specific standing approval rule entry
    def revoke_rule(self, entry: str) -> bool:
        if entry in self.always_allowed_tools:
            self.always_allowed_tools.remove(entry)
            return True
        return False

    def public(self) -> dict[str, Any]:
        """[中文] 面向 API/UI 的公开状态结构（不截断 instructions；绝不包含任何机密）。

        [English]
        Status shape for the API/UI (no instructions truncation; never any secret)."""
        return {
            "id": self.id,
            "title": self.title,
            "instructions": self.instructions,
            "schedule": self.schedule.human(),
            "schedule_raw": self.schedule.to_dict(),
            "workspace": self.workspace,
            "agent": self.agent,
            "enabled": self.enabled,
            "next_run": self.next_run,
            "last_run": self.last_run,
            "last_status": self.last_status,
            "run_count": self.run_count,
            "notify_on_completion": self.notify_on_completion,
            # UX-023: lets the detail freeze the pre-open mark for its "new" pills.
            "seen_runs_at": self.seen_runs_at,
            # Structured for the task page's revoke list; `entry` is the revoke handle.
            "always_allowed": [
                {"entry": e, "tool": t, "target": tg}
                for e, (t, tg) in (
                    (e, rule_parts(e)) for e in sorted(set(self.always_allowed_tools))
                )
            ],
        }


# [中文] 单次任务运行记录数据模型：每次定时或手动触发生成的执行记录与独立会话
# [English] Task run dataclass: execution record and independent session generated per trigger
@dataclass
class TaskRun:
    task_id: str
    run_id: str = field(default_factory=lambda: "run-" + uuid.uuid4().hex[:10])
    started_at: float = field(default_factory=_now)
    finished_at: Optional[float] = None
    status: str = "running"  # running | ok | error | skipped
    result_text: Optional[str] = None
    artifacts: list[str] = field(default_factory=list)
    error: Optional[str] = None
    trigger: str = "schedule"  # schedule | manual | catchup
    session_id: str = ""  # the run's own conversation thread — persisted + continuable

    def __post_init__(self) -> None:
        if not self.session_id:
            self.session_id = f"__run__{self.run_id}"

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "TaskRun":
        return cls(**d)
