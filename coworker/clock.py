"""[中文] 作为工具的时钟功能。

当前时间故意不包含在每轮交互的上下文块中（OPE-192）：该块会被粘合到提供方已缓存的消息上，
而一个会自动变化的数值会在每一轮重写该消息，从而导致整个已缓存的会话被抛弃 —— 实测这可能耗费长会话成本的 77%。
工具调用仅花费少量 token，且仅在模型确实需要时间时调用（截止日期、“多久之前”、用于 `sleep_until` 的绝对唤醒时间等）。
相对等待（`sleep_for`）和定时器唤醒（唤醒消息自带触发时间）完全不需要读取时钟。

[English]
The clock, as a tool.

The current time is deliberately NOT in the per-turn context block (OPE-192): that block
is glued onto a message the provider has already cached, and a value that changes by
itself rewrites the message on every turn, which throws the whole cached conversation
away — measured at up to 77% of a long session's cost. A tool call costs a
few tokens and only when the model actually needs the time (a deadline, "how long ago",
an absolute wake time for `sleep_until`). Relative waits (`sleep_for`) and timer wakes
(the wake message carries its fire time) need no clock reading at all.
"""

from __future__ import annotations

from datetime import datetime, timezone


def current_time() -> dict:
    """[中文] 当前日期与时间。当任务依赖时钟时调用此工具 —— 截止日期、“多久之前”、备忘录日期或 sleep_until 的唤醒时间。
    环境中的“Today's date”是会话启动时的快照，可能会过时；此工具返回实时时间。
    返回带有 UTC 偏移量和时区名称的本地时间、同一时刻的 UTC 时间以及星期几。

    [English]
    The current date and time. Call this when a task depends on the clock — a
    deadline, "how long ago", the date for a note, or the wake time for sleep_until.
    The "Today's date" in your environment is a session-start snapshot and may be stale;
    this is live. Returns the local time with its UTC offset and timezone name, the
    same instant in UTC, and the weekday."""
    now = datetime.now().astimezone()
    utc = now.astimezone(timezone.utc)
    return {
        "local": now.isoformat(timespec="seconds"),
        "timezone": now.tzname() or "",
        "utc": utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "weekday": now.strftime("%A"),
    }


def clock_tools() -> list:
    return [current_time]
