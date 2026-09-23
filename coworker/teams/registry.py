"""[中文] 团队注册表 —— 记录哪些会话组成一个团队：一个 lead、其下属 workers 以及他们共享的看板。

团队在人员编制卡片确认时（"Create team & start"）创建：worker 会话作为磁盘上的持久化状态预先生成（预生成并不等同于首轮运行 —— 未分配任务的 worker 消耗零 token；其首次模型轮次仅在第一个任务指派落地时触发）。注册表是唤醒机制每 tick 轮询的花名册，也是按角色成员资格划分过期待办摘要（staleness digests）的作用域纽带。

[English]
Team registry — which sessions form a team: one lead, its workers, their board.

A team is created at the staffing gate ("Create team & start"): worker sessions are
PRE-SPAWNED as durable state on disk (spawn ≠ first turn — an unassigned worker costs
zero tokens; its first model turn fires when the first assignment lands). The registry
is the roster the wake plumbing walks each tick, and the tie that scopes staleness
digests by role membership.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# [中文] 团队成员 Worker 数据模型
# [English] Team worker member dataclass
@dataclass
class TeamWorker:
    # [中文] Lead 赋予的名称 —— 看板参与者 ID、被指派者标识、@提及目标
    # [English] the lead-given NAME — board actor id, assignee handle, @mention target
    actor: str
    persona: str
    session_id: str
    model: str = ""
    # [中文] Lead 配置该角色的原因 —— 会展示在队友的名册中
    # [English] why the lead staffed it — surfaces in teammates' rosters
    reason: str = ""
    # [中文] 人工明确批准的人员配置指导文本，而非实时转向指令
    # [English] exact human-approved staffing text, not live steering
    approval_guidance: str = ""


# [中文] 团队拓扑与运行状态数据模型
# [English] Team topology and runtime status dataclass
@dataclass
class Team:
    team_id: str
    space: str
    lead_session: str
    lead_actor: str
    workers: list[TeamWorker] = field(default_factory=list)
    chat_enabled: bool = False
    # [中文] 启用群聊时的 ChatStore group_id
    # [English] ChatStore group_id when chat is enabled
    chat_group: str = ""
    # [中文] 预算/用户暂停：唤醒关卡会跳过已暂停的团队
    # [English] budget/user pause: the wake gate skips a paused team
    paused: bool = False
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    # [中文] 滚动预算关卡：本小时内的自动唤醒次数（换小时后重置）。
    # [English] Rolling budget gate: automatic wakes this hour (reset when the hour rolls).
    wake_hour: str = ""
    wakes_this_hour: int = 0


# [中文] 团队注册表管理器，持久化团队拓扑及预算状态到 JSON 文件
# [English] Team registry manager, persisting team topology and budget state to JSON file
class TeamRegistry:
    def __init__(self, path: Optional[str | Path] = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._teams: dict[str, Team] = {}
        if self.path and self.path.is_file():
            for raw in json.loads(self.path.read_text(encoding="utf-8")).get(
                "teams", []
            ):
                workers = [TeamWorker(**w) for w in raw.pop("workers", [])]
                team = Team(**{**raw, "workers": []})
                team.workers = workers
                self._teams[team.team_id] = team

    # [中文] 持久化团队列表到磁盘
    # [English] Persist teams list to disk
    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"teams": [asdict(t) for t in self._teams.values()]}, indent=2
            ),
            encoding="utf-8",
        )

    # [中文] 创建并记录新团队
    # [English] Create and register a new team
    def create(
        self,
        *,
        space: str,
        lead_session: str,
        lead_actor: str,
        workers: list[TeamWorker],
        chat_enabled: bool = False,
        chat_group: str = "",
    ) -> Team:
        team = Team(
            team_id=uuid.uuid4().hex[:12],
            space=space,
            lead_session=lead_session,
            lead_actor=lead_actor,
            workers=workers,
            chat_enabled=chat_enabled,
            chat_group=chat_group,
        )
        with self._lock:
            self._teams[team.team_id] = team
            self._save()
        return team

    # [中文] 获取所有已注册的团队
    # [English] Get all registered teams
    def all(self) -> list[Team]:
        return list(self._teams.values())

    # [中文] 根据团队 ID 获取团队
    # [English] Get team by team ID
    def get(self, team_id: str) -> Optional[Team]:
        return self._teams.get(team_id)

    # [中文] 根据 Lead 会话 ID 查找所属团队
    # [English] Find team by lead session ID
    def for_lead_session(self, session_id: str) -> Optional[Team]:
        for team in self._teams.values():
            if team.lead_session == session_id:
                return team
        return None

    # [中文] 根据 Worker 会话 ID 查找所属团队及 Worker 记录
    # [English] Find team and worker record by worker session ID
    def for_worker_session(self, session_id: str) -> Optional[tuple[Team, TeamWorker]]:
        for team in self._teams.values():
            for worker in team.workers:
                if worker.session_id == session_id:
                    return team, worker
        return None

    # [中文] 暂停或恢复指定团队
    # [English] Pause or resume a specific team
    def set_paused(self, team_id: str, paused: bool) -> None:
        with self._lock:
            team = self._teams.get(team_id)
            if team is not None:
                team.paused = paused
                self._save()

    def count_wake(self, team_id: str, *, cap: int) -> bool:
        """[中文] 唤醒关卡处的预算门禁：在团队当前滚动小时内计入一次自动唤醒；返回 False 表示超出上限（调用方跳过唤醒，该团队在跨小时前被视为因预算而暂停）。失控循环总是在轮次之间（BETWEEN turns）被终止，绝不会在飞行中途突兀中断。

        [English]
        The budget gate at the wake gate: count one automatic wake against the
        team's rolling hour; False = over cap (the caller skips the wake and the
        team reads as paused-for-budget until the hour rolls). A runaway loop
        stops BETWEEN turns, never mid-flight."""
        hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
        with self._lock:
            team = self._teams.get(team_id)
            if team is None:
                return False
            if team.wake_hour != hour:
                team.wake_hour, team.wakes_this_hour = hour, 0
            if team.wakes_this_hour >= cap:
                self._save()
                return False
            team.wakes_this_hour += 1
            self._save()
            return True
