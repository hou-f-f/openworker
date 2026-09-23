"""[中文] 自唤醒（Self-wake）— 允许长期运行的 Agent 挂起并在触发器到达时被重新唤醒调用的工具。

将常驻运行的 Agent 转换为挂起/恢复模型（事件驱动，几乎零空闲成本）：会话进入休眠，
当唤醒到期时运行时环境重新调用它。这里支持两种触发器：**定时器**（`sleep_for` 相对等待，`sleep_until` 绝对时间）
和**任务完成触发**（针对后台任务的 `wake_on`）。该模块拥有唤醒记录及到期/完成逻辑；调度器 tick 消费 ``due()`` /
``complete_job()`` 并恢复会话（与自动化调度器共享 — 参见 ``PERMISSIONS-AND-INBOX.md``）。

[English]
Self-wake — tools that let a long-running agent suspend and be re-invoked on a trigger.

Converts an always-on agent into suspend/resume (event-driven, ~zero idle cost): the session
sleeps and the runtime re-invokes it when a wake is due. Two triggers here: a **timer**
(`sleep_for` relative, `sleep_until` absolute) and **on-completion** (`wake_on` a
backgrounded job). This module
owns the wake records + the due/complete logic; the scheduler tick consumes ``due()`` /
``complete_job()`` and resumes the session (shares the automation scheduler — see
``PERMISSIONS-AND-INBOX.md``).
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

KIND_TIMER = "timer"
KIND_COMPLETION = "completion"
KIND_EVENT = "event"  # [中文] 当命名的连接器/Webhook 事件触发时唤醒（第 3 阶段） / [English] wake when a named connector/webhook event fires (Phase 3)

STATE_PENDING = "pending"
STATE_DUE = "due"
STATE_FIRED = "fired"
STATE_CANCELLED = "cancelled"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Wake:
    id: str
    session_id: str
    kind: str
    state: str = STATE_PENDING
    fire_at: Optional[str] = None  # [中文] ISO 格式时间字符串，用于定时器唤醒 / [English] ISO, for timer wakes
    job_id: Optional[str] = None  # [中文] 用于任务完成唤醒 / [English] for completion wakes
    event_key: Optional[str] = None  # [中文] 用于事件触发唤醒 / [English] for on-event wakes
    note: str = ""
    created_at: str = field(default_factory=lambda: _now().isoformat())
    cancellation_reason: str = ""
    context_delivered: bool = False


class WakeStore:
    def __init__(self, path: Optional[str | Path] = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._wakes: dict[str, Wake] = {}
        self.stopped_sessions: set[str] = set()
        if self.path and self.path.is_file():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            self.stopped_sessions = set(saved.get("stopped_sessions", []))
            for raw in saved.get("wakes", []):
                w = Wake(**raw)
                self._wakes[w.id] = w
            # [中文] 旧版本会累积定时器。每个会话仅保留最新的挂起 sleep，同时保留被替换的记录以供审计。
            # [English]
            # Older versions accumulated timers. Keep only the newest pending
            # sleep per session while retaining the replaced records for audit.
            newest = {}
            migrated = False
            for w in sorted(self._wakes.values(), key=lambda w: w.created_at):
                if w.kind != KIND_TIMER or w.state not in (STATE_PENDING, STATE_DUE):
                    continue
                old = newest.get(w.session_id)
                if old:
                    old.state = STATE_CANCELLED
                    old.cancellation_reason = "replaced by a newer sleep"
                    old.context_delivered = True
                    migrated = True
                newest[w.session_id] = w
            if migrated:
                self._save()

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {"wakes": [asdict(w) for w in self._wakes.values()],
                 "stopped_sessions": sorted(self.stopped_sessions)},
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def add_timer(self, session_id: str, fire_at: datetime, *, note: str = "") -> Wake:
        w = Wake(
            uuid.uuid4().hex,
            session_id,
            KIND_TIMER,
            fire_at=fire_at.isoformat(),
            note=note,
        )
        with self._lock:
            for old in self._wakes.values():
                if (old.session_id == session_id and old.kind == KIND_TIMER
                        and old.state in (STATE_PENDING, STATE_DUE)):
                    old.state = STATE_CANCELLED
                    old.cancellation_reason = "replaced by a newer sleep"
                    old.context_delivered = True
            self._wakes[w.id] = w
            self._save()
        return w

    def add_completion(self, session_id: str, job_id: str, *, note: str = "") -> Wake:
        w = Wake(
            uuid.uuid4().hex, session_id, KIND_COMPLETION, job_id=job_id, note=note
        )
        with self._lock:
            self._wakes[w.id] = w
            self._save()
        return w

    def add_event(self, session_id: str, event_key: str, *, note: str = "") -> Wake:
        w = Wake(
            uuid.uuid4().hex, session_id, KIND_EVENT, event_key=event_key, note=note
        )
        with self._lock:
            self._wakes[w.id] = w
            self._save()
        return w

    def due(self, now: Optional[datetime] = None) -> list[Wake]:
        """[中文] 触发时间已过的定时器唤醒，加上被标记为到期的任务完成/事件唤醒。
        [English] Timer wakes whose fire time has passed, plus completion/event wakes marked due."""
        now = now or _now()
        out = []
        with self._lock:
            snapshot = list(self._wakes.values())
        for w in snapshot:
            if w.state != STATE_PENDING and w.state != STATE_DUE:
                continue
            if (
                w.kind == KIND_TIMER
                and w.fire_at
                and datetime.fromisoformat(w.fire_at) <= now
            ):
                out.append(w)
            elif w.kind in (KIND_COMPLETION, KIND_EVENT) and w.state == STATE_DUE:
                out.append(w)
        return out

    def complete_job(self, job_id: str) -> list[Wake]:
        """[中文] 将针对 ``job_id`` 的完成唤醒标记为到期（任务已退出）。返回这些唤醒。
        [English] Mark completion wakes for ``job_id`` as due (the job exited). Returns them."""
        return self._mark_due(
            lambda w: w.kind == KIND_COMPLETION and w.job_id == job_id
        )

    def fire_event(self, event_key: str) -> list[Wake]:
        """[中文] 将针对 ``event_key`` 的事件唤醒标记为到期（连接器/Webhook 触发）。返回这些唤醒。
        [English] Mark on-event wakes for ``event_key`` as due (a connector/webhook fired). Returns them."""
        return self._mark_due(
            lambda w: w.kind == KIND_EVENT and w.event_key == event_key
        )

    def _mark_due(self, pred) -> list[Wake]:
        fired = []
        with self._lock:
            for w in self._wakes.values():
                if w.state == STATE_PENDING and pred(w):
                    w.state = STATE_DUE
                    fired.append(w)
            if fired:
                self._save()
        return fired

    def mark_fired(self, wake_id: str) -> None:
        with self._lock:
            w = self._wakes.get(wake_id)
            if w is not None and w.state in (STATE_PENDING, STATE_DUE):
                w.state = STATE_FIRED
                self._save()

    def cancel_sleep(self, session_id: str, reason: str) -> None:
        """[中文] 持久保留已取消的提醒，直到入站轮次将其记录下来。
        [English] Retain cancelled reminders durably until an incoming turn records them."""
        with self._lock:
            changed = False
            for w in self._wakes.values():
                if (w.session_id == session_id and w.kind == KIND_TIMER
                        and w.state in (STATE_PENDING, STATE_DUE)):
                    w.state = STATE_CANCELLED
                    w.cancellation_reason = reason
                    changed = True
            if changed:
                self._save()

    def set_stopped(self, session_id: str, stopped: bool) -> None:
        with self._lock:
            if stopped:
                self.stopped_sessions.add(session_id)
            else:
                self.stopped_sessions.discard(session_id)
            self._save()

    def cancelled_context(self, session_id: str) -> list[Wake]:
        with self._lock:
            return [
                w for w in self._wakes.values()
                if w.session_id == session_id and w.state == STATE_CANCELLED
                and not w.context_delivered
            ]

    def acknowledge(self, wake_ids: list[str]) -> None:
        """[中文] 仅在持久化入站消息收据存在之后调用。
        [English] Called only after a durable incoming-message receipt exists."""
        with self._lock:
            changed = False
            for wake_id in wake_ids:
                w = self._wakes.get(wake_id)
                if w is not None and not w.context_delivered:
                    if w.state in (STATE_PENDING, STATE_DUE):
                        w.state = STATE_FIRED
                    w.context_delivered = True
                    changed = True
            if changed:
                self._save()

    def pending(self, session_id: Optional[str] = None) -> list[Wake]:
        with self._lock:
            return [
                w
                for w in self._wakes.values()
                if w.state in (STATE_PENDING, STATE_DUE)
                and (session_id is None or w.session_id == session_id)
            ]


def selfwake_tools(store: WakeStore, session_id: str) -> list:
    """[中文] Agent 用来调度自身恢复运行的工具集合。
    [English] Tools an agent calls to schedule its own resumption."""

    def sleep_for(seconds: int, note: str = "") -> dict:
        """[中文] 挂起并在 `seconds` 秒后唤醒此会话（相对等待：“5 分钟后再检查”为 sleep_for(300)）。
        用于显式的定时检查，而非常规团队轮询：看板决策会自动唤醒结束了本轮运行的 Lead。
        替换先前的 sleep。更早的看板/用户活动会取消它，并将其可选提醒便签带入后续活动。无需时钟计算。

        [English] Suspend and wake this session after `seconds` (a relative wait: "check again in
        5 minutes" is sleep_for(300)). Use for an explicit timed check, not routine
        team polling: board decisions already wake a lead that finishes its turn.
        Replaces the previous sleep. Earlier board/user activity cancels
        it and carries your optional reminder note forward. No clock arithmetic needed."""
        secs = int(seconds)
        if secs <= 0:
            raise ValueError("sleep_for needs a positive number of seconds")
        w = store.add_timer(session_id, _now() + timedelta(seconds=secs), note=note)
        return {"ok": True, "wake_id": w.id, "fire_at": w.fire_at}

    def sleep_until(when_iso: str, note: str = "") -> dict:
        """[中文] 挂起并在 ISO-8601 时间戳处唤醒此会话（感知时区；无时区的时间戳被视为 UTC）—
        用于绝对时间（“明天 09:00”）。如果需要今天的日期或时区，请先调用 `current_time`；对于相对等待，
        请改用 sleep_for。替换先前的 sleep，并在更早的看板/用户活动中取消，将可选提醒便签带入该活动中。
        这是一个空闲签到截止时间，而非持久预约。

        [English] Suspend and wake this session at an ISO-8601 timestamp (timezone-aware; bare
        timestamps are read as UTC) — for an absolute time ("at 09:00 tomorrow"). Call
        `current_time` first if you need today's date or the timezone; for a relative wait
        use sleep_for instead. Replaces the previous sleep and cancels on earlier
        board/user activity, carrying the optional reminder note into that activity.
        This is an idle check-in deadline, not a persistent appointment."""
        when = datetime.fromisoformat(when_iso)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        w = store.add_timer(session_id, when, note=note)
        return {"ok": True, "wake_id": w.id, "fire_at": w.fire_at}

    def wake_on(job_id: str, note: str = "") -> dict:
        """[中文] 当后台任务（`job_id`）完成时挂起并唤醒此会话。
        [English] Suspend and wake this session when a backgrounded job (`job_id`) completes."""
        w = store.add_completion(session_id, job_id, note=note)
        return {"ok": True, "wake_id": w.id, "job_id": job_id}

    def wake_on_event(event_key: str, note: str = "") -> dict:
        """[中文] 当命名事件（`event_key`）触发时挂起并唤醒此会话 — 例如 Ops Agent 监控的连接器/Webhook 信号。
        [English] Suspend and wake this session when a named event (`event_key`) fires — e.g. a
        connector/webhook signal an Ops agent watches for."""
        w = store.add_event(session_id, event_key, note=note)
        return {"ok": True, "wake_id": w.id, "event_key": event_key}

    tools = [sleep_for, sleep_until, wake_on, wake_on_event]
    for tool in tools:
        tool.__coworker_yields_turn__ = True
    return tools
