"""[中文] 每个状态目录仅限一个引擎。

两个引擎同时写入同一个状态目录会静默地相互损坏：SQLite 行在缓存句柄后消失、
看板出现两个写入者、会话双重唤醒。我们在实际发布中遇到的真实故障是一台虚拟机上有两个 systemd 用户单元，
两者都在运行 `openworker up`（第一次机器测试遗留的陈旧 `openworker.service` 与手动编写的单元并存）—
每一次 "kill the stray" 都在 5 秒后被 `Restart=always` 还原。
该锁让第二个引擎识别此情况并主动退出，而不是直接运行。

`acquire()` 在返回的句柄（实际中即进程生命周期）的存续期间持有对 `<state>/engine.lock` 的建议锁（advisory lock），
并在文件中记录持有者的 pid 以便在拒绝消息中显示。POSIX 使用 flock；Windows 在第一个字节上使用 msvcrt.locking。
在两者均不存在的环境下尽力而为：锁降级为“总是获取成功”，而不是阻断启动。

[English]
One engine per state directory.

Two engines writing one state dir corrupt each other quietly: SQLite rows
vanish behind a cached handle, boards get two writers, sessions double-wake.
The failure we actually shipped was two systemd user units on one VM, both
running `openworker up` (a stale `openworker.service` from the first machine
test beside the hand-written unit) — every "kill the stray" was undone by
`Restart=always` five seconds later. This lock makes the second engine say so
and stop, instead of running.

`acquire()` holds an advisory lock on `<state>/engine.lock` for the life of
the returned handle (the process, in practice) and records the holder's pid
in the file for the refusal message. POSIX uses flock; Windows uses
msvcrt.locking on the first byte. Best effort where neither exists: the
lock degrades to "always acquired" rather than blocking a launch.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Optional

LOCK_NAME = "engine.lock"


class EngineBusy(RuntimeError):
    def __init__(self, state: Path, holder_pid: Optional[int]) -> None:
        self.state = Path(state)
        self.holder_pid = holder_pid
        who = f"pid {holder_pid}" if holder_pid else "another process"
        super().__init__(
            f"another engine ({who}) already holds the state dir {self.state} — "
            "two engines on one state dir corrupt it. Stop the other one first "
            "(on a systemd box: `systemctl --user list-units 'openworker*'`)."
        )


class EngineLock:
    """[中文] 由 `acquire()` 返回的句柄；请保持对它的引用。`release()` 仅供测试使用。
    [English] Handle returned by `acquire()`; keep it referenced. `release()` is for tests."""

    def __init__(self, path: Path, fh) -> None:
        self.path = path
        self._fh = fh

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


def _try_lock(fh) -> bool:
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (OSError, ImportError):
        return False


def holder_pid(state: Path) -> Optional[int]:
    """[中文] 当前持有者记录的 pid（如果有，仅供展示参考）。
    [English] Pid recorded by the current holder, if any (informational only)."""
    try:
        text = (Path(state) / LOCK_NAME).read_text().strip()
        return int(text) if text else None
    except (OSError, ValueError):
        return None


def acquire(state: Path, *, timeout: float = 0.0) -> EngineLock:
    """[中文] 获取 `state` 目录的引擎锁，最多等待 `timeout` 秒以等待即将退出的前驱进程
    （例如 supervisor 在旧进程仍在销毁退出时重启了我们）。如果锁持续被占用，则抛出 `EngineBusy`。

    [English] Take the engine lock for `state`, waiting up to `timeout` seconds for a
    dying predecessor (a supervisor restarting us while the old process is
    still tearing down). Raises `EngineBusy` when it stays held."""
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    path = state / LOCK_NAME
    # [中文] "a+" 模式绝不会截断：持有者的 pid 保持可读以便输出消息。
    # [English] "a+" never truncates: the holder's pid stays readable for the message.
    fh = open(path, "a+")
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if _try_lock(fh):
            break
        if time.monotonic() >= deadline:
            pid = holder_pid(state)
            fh.close()
            raise EngineBusy(state, pid if pid != os.getpid() else None)
        time.sleep(0.2)
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
    except OSError:
        pass
    return EngineLock(path, fh)
