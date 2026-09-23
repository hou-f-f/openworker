"""[中文] 基于 SQLite 的定时任务持久化存储与运行历史。

任务（task）与运行（run）以 JSON blob 格式存储，辅以少量索引列（next_run, enabled），以便调度器高效查询到期任务。`next_run` 使用 croniter 计算，并严格遵从任务的时区设置。具备线程安全性（check_same_thread=False 配合锁），以支持调度器与 HTTP 请求处理器跨线程并发访问。

[English]
SQLite-backed store for scheduled tasks + run history.

Tasks/runs are stored as JSON blobs with a few indexed columns (next_run, enabled) so the
scheduler can cheaply find what's due. `next_run` is computed with croniter, honoring the
task's timezone. Thread-safe (check_same_thread=False + a lock) since the scheduler and the
request handlers touch it from different threads.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from .models import ScheduledTask, TaskRun


def compute_next_run(
    task: ScheduledTask, *, after: Optional[float] = None
) -> Optional[float]:
    """[中文] 下次触发时间（UNIX 时间戳秒），若任务已耗尽或单次任务已过期则返回 None。

    [English]
    Next fire time (epoch seconds), or None if the task is exhausted/one-shot-past."""
    sched = task.schedule
    now = after if after is not None else _epoch_now()
    if sched.kind == "once":
        if not sched.fire_at:
            return None
        try:
            dt = datetime.fromisoformat(sched.fire_at)
        except ValueError:
            return None
        tz = _tz(sched.timezone)
        if dt.tzinfo is None and tz is not None:
            dt = dt.replace(tzinfo=tz)
        # Naive local dt: datetime.timestamp() interprets it in the machine's zone and is
        # DST-aware for the actual fire DATE (via the C library), so a "once" task set in
        # summer for a winter date fires at the right wall-clock instead of an hour off.
        ts = dt.timestamp()
        return ts if (task.run_count == 0 and ts > now) else None
    # cron
    from croniter import croniter

    if not sched.cron or not croniter.is_valid(sched.cron):
        return None
    if task.max_runs is not None and task.run_count >= task.max_runs:
        return None
    tz = _tz(sched.timezone)
    # Local: a naive base makes croniter compute in local wall-clock and .timestamp() apply
    # the correct DST offset per occurrence. A named zone anchors the base in that zone.
    base = datetime.fromtimestamp(now) if tz is None else datetime.fromtimestamp(now, tz=tz)
    return croniter(sched.cron, base).get_next(datetime).timestamp()


def _tz(name: str):
    """[中文] 将调度时区名称解析为支持夏令时（DST）的 tzinfo 对象；若是机器本地时区则返回 None。使用 None（而非固定偏移量 tzinfo）是深思熟虑的设计：naive datetime 允许 .timestamp() / C 标准库在实际触发日期计算准确的本地夏令时。若写死 `datetime.now().astimezone()` 会固化计算时的当前偏移量，导致跨夏令时边界时触发时间偏差一小时。未知的 IANA 时区名称回退为本地（None）而非抛出异常。

    [English]
    Resolve a schedule timezone to a DST-aware tzinfo, or None for the machine's local
    zone. None (not a fixed-offset tzinfo) is deliberate: naive datetimes let .timestamp()/
    the C library apply local DST at the fire date. A frozen `datetime.now().astimezone()`
    offset baked in whatever offset was in effect at compute time and misfired across a DST
    boundary. An unknown IANA name falls back to local (None) rather than raising."""
    if not name or name.lower() == "local":
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def _epoch_now() -> float:
    return datetime.now(timezone.utc).timestamp()


# [中文] SQLite 驱动的任务与运行历史存储管理器
# [English] SQLite-backed scheduled tasks and run history store manager
class TaskStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init()

    # [中文] 初始化 SQLite 表结构与索引
    # [English] Initialize SQLite schema and indices
    def _init(self) -> None:
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    next_run REAL,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_runs (
                    run_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runs_task ON task_runs(task_id, started_at DESC);
                """)
            self._conn.commit()

    # -- tasks ------------------------------------------------------------------
    # [中文] 保存或更新任务记录，自动更新修改时间与下次触发时间
    # [English] Save or update task record, recalculating update time and next run
    def save(self, task: ScheduledTask) -> ScheduledTask:
        task.updated_at = _epoch_now()
        task.next_run = compute_next_run(task) if task.enabled else None
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO scheduled_tasks (id, enabled, next_run, data) VALUES (?, ?, ?, ?)",
                (
                    task.id,
                    1 if task.enabled else 0,
                    task.next_run,
                    json.dumps(task.to_dict()),
                ),
            )
            self._conn.commit()
        return task

    # [中文] 根据任务 ID 查询任务记录
    # [English] Query task record by task ID
    def get(self, task_id: str) -> Optional[ScheduledTask]:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM scheduled_tasks WHERE id=?", (task_id,)
            ).fetchone()
        return ScheduledTask.from_dict(json.loads(row["data"])) if row else None

    # [中文] 查询所有任务记录，按下次触发时间升序排列
    # [English] List all task records, ordered by next_run ascending
    def list(self) -> list[ScheduledTask]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM scheduled_tasks ORDER BY next_run IS NULL, next_run"
            ).fetchall()
        return [ScheduledTask.from_dict(json.loads(r["data"])) for r in rows]

    # [中文] 删除任务及其关联的所有运行历史记录
    # [English] Delete task and all its associated run records
    def delete(self, task_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM scheduled_tasks WHERE id=?", (task_id,)
            )
            self._conn.execute("DELETE FROM task_runs WHERE task_id=?", (task_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # [中文] 查询当前已到期且处于启用状态的任务列表
    # [English] Query enabled tasks that are due at or before now
    def due(self, *, now: Optional[float] = None) -> list[ScheduledTask]:
        now = now if now is not None else _epoch_now()
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM scheduled_tasks WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=? ORDER BY next_run",
                (now,),
            ).fetchall()
        return [ScheduledTask.from_dict(json.loads(r["data"])) for r in rows]

    # -- runs -------------------------------------------------------------------
    # [中文] 保存或更新单次任务运行记录
    # [English] Save or update a single task run record
    def add_run(self, run: TaskRun) -> TaskRun:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO task_runs (run_id, task_id, started_at, data) VALUES (?, ?, ?, ?)",
                (run.run_id, run.task_id, run.started_at, json.dumps(run.to_dict())),
            )
            self._conn.commit()
        return run

    # [中文] 根据运行 ID 查询运行记录
    # [English] Find run record by run ID
    def find_run(self, run_id: str) -> Optional[TaskRun]:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM task_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return TaskRun.from_dict(json.loads(row["data"])) if row else None

    def task_for_run_session(self, session_id: str) -> Optional[ScheduledTask]:
        """[中文] 查询某个运行会话（'__run__<run_id>'）所属的任务，未找到则返回 None。常驻作用域授权（§25）据此识别当前审批归属于哪个自动化任务。

        [English]
        The owning task of a run session ('__run__<run_id>'), or None. How standing
        scoped approvals resolve which automation a live approval belongs to (§25)."""
        if not session_id.startswith("__run__"):
            return None
        run = self.find_run(session_id[len("__run__") :])
        return self.get(run.task_id) if run else None

    # [中文] 查询指定任务的历史运行记录列表，按启动时间倒序排列
    # [English] List run history for a task, ordered by start time descending
    def runs(self, task_id: str, *, limit: int = 50) -> list[TaskRun]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM task_runs WHERE task_id=? ORDER BY started_at DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        return [TaskRun.from_dict(json.loads(r["data"])) for r in rows]

    # [中文] 关闭数据库连接
    # [English] Close database connection
    def close(self) -> None:
        with self._lock:
            self._conn.close()
