"""[中文] 调度循环 —— 在常驻服务器中持续运行。

策略机制：
1. **宕机单次补跑（run-once-catch-up）**：对于服务停机期间错过的任务，在启动时触发一次，随后恢复正常定时；
2. **重叠跳过（skip-on-overlap）**：若上一轮任务仍在运行中，则跳过本次触发，不发生堆叠。
实际的执行逻辑通过注入 `runner(task, trigger) -> TaskRun` 实现，从而与底层执行引擎/管理器保持彻底解耦。

[English]
The scheduler loop — runs in the always-on server.

Policy (agreed): **run-once-catch-up** for runs missed while down (due tasks fire once on
startup, then resume), and **skip-on-overlap** (don't stack a run if the previous is still
going). The actual execution is injected as `runner(task, trigger) -> TaskRun` so this stays
independent of the engine/manager.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from .models import ScheduledTask, TaskRun
from .store import TaskStore

logger = logging.getLogger("coworker.automation")

Runner = Callable[[ScheduledTask, str], Awaitable[TaskRun]]


# [中文] 定时任务调度器：负责定时循环轮询、到期任务触发、重叠保护与任务状态更新
# [English] Automation task scheduler: periodic tick loop, due task triggering, overlap guard, and status updating
class Scheduler:
    def __init__(
        self,
        store: TaskStore,
        runner: Runner,
        *,
        tick_seconds: float = 30.0,
        extra_tick: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self.store = store
        self.runner = runner
        self.tick_seconds = tick_seconds
        # [中文] 每个周期执行的额外协程（自唤醒恢复：恢复到期需要唤醒的会话）。
        # [English] An extra per-tick coroutine (self-wake resumption: resume sessions whose wakes are due).
        self.extra_tick = extra_tick
        self._task: Optional[asyncio.Task] = None
        self._running_ids: set[str] = set()  # overlap guard
        self._spawned: set[asyncio.Task] = set()  # keep spawned runs referenced

    # [中文] 启动后台调度轮询协程
    # [English] Start background scheduler loop coroutine
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    # [中文] 停止调度循环并取消所有正在运行的任务
    # [English] Stop scheduler loop and cancel all running spawned tasks
    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # In-flight runs died with the loop before they were spawned; keep that shutdown
        # contract now that they're independent tasks (a suspended run must not outlive us).
        for spawned in list(self._spawned):
            spawned.cancel()
            try:
                await spawned
            except asyncio.CancelledError:
                pass
        self._spawned.clear()

    # [中文] 调度核心循环：启动时先执行一次 catchup 补跑，之后按 tick_seconds 间隔轮询
    # [English] Scheduler core loop: run catchup once on startup, then tick every tick_seconds
    async def _loop(self) -> None:
        # First pass = run-once-catch-up for anything missed while the server was down.
        try:
            await self._tick(trigger="catchup")
        except Exception:
            logger.exception("scheduler catch-up failed")
        while True:
            await asyncio.sleep(self.tick_seconds)
            try:
                await self._tick(trigger="schedule")
            except Exception:
                logger.exception("scheduler tick failed")

    # [中文] 单次轮询检查：拉取到期任务并异步派生执行，随后执行额外的周期性协程（如 self-wake 自唤醒）
    # [English] Single tick check: pull due tasks, spawn execution, then run extra tick coroutines (like self-wake)
    async def _tick(self, *, trigger: str) -> None:
        for task in self.store.due():
            # Spawn, don't await: a run can suspend on a parked approval (standing
            # scoped approvals, §25) and one blocked automation must never stall the
            # scheduler loop, other due tasks, or self-wake resumption. The overlap
            # guard must be claimed *here*, before the spawn: this due() snapshot
            # goes stale, and if the in-flight run finishes before a spawned
            # duplicate gets its first step, a guard checked inside the spawn is
            # already clear — the task runs twice.
            if not self._claim(task.id):
                continue
            spawned = asyncio.create_task(self._run_claimed(task, trigger=trigger))
            self._spawned.add(spawned)
            spawned.add_done_callback(self._spawned.discard)
        if self.extra_tick is not None:
            try:
                await self.extra_tick()
            except Exception:
                logger.exception("scheduler extra_tick (wake resume) failed")

    # [中文] 重叠保护认领：若任务已在运行则返回 False，否则计入运行集合并返回 True
    # [English] Overlap guard claim: return False if already running, otherwise add to running set and return True
    def _claim(self, task_id: str) -> bool:
        if task_id in self._running_ids:  # skip-on-overlap
            logger.info("skipping %s — previous run still going", task_id)
            return False
        self._running_ids.add(task_id)
        return True

    # [中文] 手动触发执行指定任务（受重叠保护限制）
    # [English] Manually trigger execution of a task (subject to overlap guard)
    async def run_task(self, task: ScheduledTask, *, trigger: str) -> Optional[TaskRun]:
        if not self._claim(task.id):
            return None
        return await self._run_claimed(task, trigger=trigger)

    # [中文] 执行已成功认领的任务，记录运行日志并更新任务统计与下次执行时间
    # [English] Execute claimed task, record run history, and advance task counts and next_run
    async def _run_claimed(
        self, task: ScheduledTask, *, trigger: str
    ) -> Optional[TaskRun]:
        try:
            run = await self.runner(task, trigger)
        except Exception as exc:
            logger.exception("task %s run failed", task.id)
            run = TaskRun(
                task_id=task.id, status="error", error=str(exc), trigger=trigger
            )
            self.store.add_run(run)
        finally:
            self._running_ids.discard(task.id)
        # advance the task (run_count/last_run) → save recomputes next_run.
        fresh = self.store.get(task.id)
        if fresh is not None:
            fresh.run_count += 1
            fresh.last_run = run.started_at if run else None
            fresh.last_status = run.status if run else "error"
            self.store.save(fresh)
        return run
