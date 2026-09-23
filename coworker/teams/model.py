"""智能体团队的工作项模型 — 状态、参与者（Actors）、关联（Links）、错误类型。
Work-item model for agent teams — states, actors, links, errors.

看板并非记录数据库（database of record）：它是仅追加团队事件日志的投影视图（参见 teams.store）。
这些是投影所折叠成的数据形态，以及动词操作所执行的规则。
故意保持极简 — 没有敏捷冲刺（sprint）、工作量估算、优先级或自定义字段；
任何需要这些功能的人都可以通过连接器接入真正的工单跟踪系统。
The board is not a database of record: it is a projection of the append-only team
event log (see teams.store). These are the shapes the projection folds into, and the
rules the verbs enforce. Deliberately minimal — no sprints, estimates, priorities, or
custom fields; anyone needing those graduates to a real tracker via connectors.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ItemState(str, Enum):
    """看板工作项的状态枚举。
    State enumeration of a board work item."""
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    REVIEW = "review"
    DONE = "done"
    CANCELED = "canceled"


# 状态机的合法流转边。没有 draft/proposed 草稿状态（2026-08-16 决策）：
# 计划提议存在于对话中（计划审批流），看板只包含已被接受的工作 —
# 工作项创建时即为 `open`，而开始工作的控制点是分配 ASSIGNMENT（一种被授予但可撤销的授权），
# 而非针对每个工作项的重复审批。review→done 是验证把关点；canceled→open 是重新打开。
# Legal edges of the state machine. There is NO draft/proposed state (decided
# 2026-08-16): a plan proposal lives in the conversation (plan-approval flow) and
# the board only ever contains accepted work — items are created `open`, and the
# control point for work starting is ASSIGNMENT (a granted, revocable authority),
# not a per-item approval. review→done stays the verification gate; canceled→open
# is reopen.
EDGES: dict[ItemState, set[ItemState]] = {
    ItemState.OPEN: {ItemState.IN_PROGRESS, ItemState.CANCELED},
    ItemState.IN_PROGRESS: {ItemState.BLOCKED, ItemState.REVIEW, ItemState.CANCELED},
    ItemState.BLOCKED: {ItemState.IN_PROGRESS, ItemState.CANCELED},
    ItemState.REVIEW: {ItemState.DONE, ItemState.IN_PROGRESS, ItemState.CANCELED},
    ItemState.DONE: set(),
    ItemState.CANCELED: {ItemState.OPEN},
}

# Worker 角色可将其自身工作项流转到的目标状态。
# Worker 从不审批、从有关闭：done 是 Lead 在 review 时的裁决，cancel 是 Lead/用户在看板层面的决策。
# Targets a worker may move its OWN item to. Workers never approve, never close:
# done is the lead's verdict at review, cancel is a lead/user board decision.
WORKER_TARGETS = {ItemState.IN_PROGRESS, ItemState.BLOCKED, ItemState.REVIEW}


class Role(str, Enum):
    """团队参与者的角色分类。
    Role classification of team actors."""
    USER = "user"
    LEAD = "lead"
    WORKER = "worker"
    SYSTEM = "system"


@dataclass(frozen=True)
class Actor:
    """看板的交互主体。`id` 是智能体实例 ID（人类用户为 "user"）；
    role 决定动词执行权限 — 以数据形式确立的能力防火墙。

    Who is speaking to the board. `id` is the agent instance id ("user" for the
    human); role decides verb authority — the capability firebreak in data form."""

    id: str
    role: Role
    persona: str = ""
    model: str = ""
    session_id: str = ""


# link(src, "parent", dst): dst 是 src 的父工作项 / dst is src's parent
# link(src, "blocks", dst): src 阻塞了 dst / src blocks dst
LINK_KINDS = ("parent", "blocks")

# `note` 是任何观察记录 — 日志不仅用于排查调查。`raw` 是原始捕获（日志摘录、命令输出）；
# 默认读取会跳过 raw 除非明确要求，大型载荷应存为独立文件并在条目中引用。
# `note` is any observation — the journal is not only for investigations. `raw` is
# a capture (log excerpt, command output); reads skip raw unless asked, and large
# payloads belong in a file the entry references.
JOURNAL_KINDS = ("finding", "evidence", "decision", "note", "raw")

# 日志条目正文是摘录/总结，绝不是巨型二进制大对象：超大的载荷会拖慢每次读取（和重放）。
# 完整的捕获作为条目所指向的文件存在。
# An entry body is an excerpt/summary, never a blob: oversized payloads make every
# read (and replay) drag. Full captures live as files the entry points at.
JOURNAL_BODY_LIMIT = 16_000


def space_for_workspace(workspace: str | Path) -> str:
    """空间键绑定到项目/工作区（看板是空间的视图）。
    解析后的绝对路径是唯一无歧义的本地键；显示名称为其目录基准名。

    Spaces are keyed to the project/workspace (boards are views over a space).
    The resolved path is the one unambiguous local key; a display name is its
    basename."""
    return str(Path(workspace).expanduser().resolve())


class BoardError(Exception):
    """看板拒绝执行的操作异常 — 非法流转、缺失项、非法输入。
    A verb call the board refuses — illegal transition, missing item, bad input."""


class BoardNotFoundError(BoardError):
    """请求的看板对象不存在，或对当前操作主体不可见。
    A requested board object is missing or is not visible to the actor."""


class AuthorityError(BoardError):
    """操作主体的角色不允许对该项执行此操作动词。
    The actor's role does not permit this verb on this item."""


class ChainError(Exception):
    """哈希链校验失败 — 日志已被带外窜改。
    Hash-chain verification failed — the log was modified out of band."""
