"""[中文] 处于 `Executor` 抽象边界之后的持久化 Shell。

`LocalExecutor` 维护一个长寿命的 shell 进程，因此跨多次 `run_shell` 调用时，`cd`、`export`、已激活的虚拟环境 venv 等状态能够持续保留（不同于每次重新调用 `subprocess.run`）。
`Executor` 抽象接口为未来引入 `ContainerExecutor`/`VMExecutor`（容器/虚拟机沙箱化隔离）预留了防范层，无需修改上层引擎。

底层 Shell 为操作系统原生：POSIX 系统下为 `/bin/bash`，Windows 系统下为 `powershell.exe`（`-Command -` 交互式 REPL 模式）。
每个后端拥有各自的标记/退出码协议与中断机制，但 `Executor` 契约（以及解析出的 `{marker} {exit_code} {cwd}` 尾部数据行）完全一致。

安全防护包含：权限门控（高危工具 → 必须人工审批）+ 单命令超时限制 + 尽最大努力的非交互式运行环境保障。
超时的命令将被中断（在 POSIX 上向对应前台子进程发送 SIGINT，在 Windows 上向子进程组发送 Ctrl-Break）；shell 进程本身保留以维持会话状态。

后台任务（带有 `run_in_background` 参数的 `run_shell`）会获得独立的脱钩进程 —— 而不是持久化 shell 自身 ——
使得开发服务器可以在后台运行，同时当前会话继续处理其他工作。
它们特意不会被 `close()` 杀掉（超时恢复路径会调用 close()）；仅在进程自行退出或通过 `shell_task_kill` 时终止。

Persistent shell behind an `Executor` boundary.

`LocalExecutor` keeps one long-lived shell process, so `cd`, `export`, activated venvs,
etc. persist across `run_shell` calls (unlike a per-call `subprocess.run`). The `Executor`
interface is the hedge for a future `ContainerExecutor`/`VMExecutor` (sandboxing) without
touching the engine.

The shell is OS-native: `/bin/bash` on POSIX, `powershell.exe` (`-Command -` REPL) on
Windows. Each backend has its own marker/exit-code protocol and interrupt mechanism, but
the `Executor` contract (and the parsed `{marker} {exit_code} {cwd}` trailer) is identical.

Safety here is permission-gating (high-risk tool → approval) + per-command timeout +
best-effort non-interactive enforcement. A timed-out command is interrupted (SIGINT to the
foreground child on POSIX, Ctrl-Break to the child group on Windows); the shell survives so
session state is preserved.

Background tasks (`run_shell` with `run_in_background`) get their own detached process —
NOT the persistent shell — so a dev server can run while the session keeps working. They
are deliberately not killed by `close()` (which the timeout-recovery path calls); they end
when they exit or via `shell_task_kill`.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

import aisuite as ai

_IS_WINDOWS = sys.platform == "win32"

# [中文] 前台超时范围：默认足够支持安装/构建/测试运行，并设置上限以防止模型请求的超时时间将轮次卡死超过十分钟。
# Foreground timeout bounds: long enough for installs/builds/test runs by default, capped so
# a model-requested timeout can't wedge the turn for more than ten minutes.
_DEFAULT_TIMEOUT = 120.0
_MAX_TIMEOUT = 600.0

# [中文] 环境变量默认值，阻止命令因等待用户输入提示而阻塞。
# Env defaults that discourage commands from blocking on a prompt.
_NONINTERACTIVE_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "DEBIAN_FRONTEND": "noninteractive",
    "PYTHONUNBUFFERED": "1",
    "PIP_NO_INPUT": "1",
}


class Executor(ABC):
    @abstractmethod
    def run(self, command: str, timeout: Optional[float] = None) -> dict[str, Any]: ...

    def run_background(self, command: str) -> dict[str, Any]:
        return {"error": "background execution is not supported by this executor"}

    def background_output(self, task_id: str) -> dict[str, Any]:
        return {"error": "background execution is not supported by this executor"}

    def background_kill(self, task_id: str) -> dict[str, Any]:
        return {"error": "background execution is not supported by this executor"}

    def interrupt(self) -> None:  # pragma: no cover - default no-op
        pass

    def close(self) -> None:  # pragma: no cover - default no-op
        pass


class _BackgroundTask:
    """[中文] 单个独立的后台命令：拥有独立进程（非持久化 shell）、将输出排空到缓冲区的读取线程以及增量读取游标。
    One detached background command: its own process (not the persistent shell), a
    reader thread draining output into a buffer, and an incremental-read cursor."""

    def __init__(self, task_id: str, command: str, cwd: str, env: dict[str, str]):
        self.id = task_id
        self.command = command
        if _IS_WINDOWS:
            argv = ["powershell.exe", "-NoProfile", "-Command", command]
            spawn_kwargs: dict[str, Any] = {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
            }
        else:
            argv = ["/bin/bash", "-c", command]
            spawn_kwargs = {"start_new_session": True}
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            text=True,
            bufsize=1,
            env=env,
            **spawn_kwargs,
        )
        self._lock = threading.Lock()
        self._lines: list[str] = []
        self._cursor = 0
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            with self._lock:
                self._lines.append(line)

    def read_new(self) -> str:
        with self._lock:
            new = "".join(self._lines[self._cursor :])
            self._cursor = len(self._lines)
        return new

    def kill(self) -> None:
        if self.proc.poll() is not None:
            return
        if _IS_WINDOWS:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                    capture_output=True,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass


class LocalExecutor(Executor):
    def __init__(
        self,
        *,
        cwd: str | Path,
        env: Optional[dict[str, str]] = None,
        shell_path: Optional[str] = None,
        default_timeout: float = _DEFAULT_TIMEOUT,
        # [中文] 仅作为内存安全网（OPE-186）：模型可见的内容受到引擎工具结果上限（开头 + 标记 + 结尾，完整文本存放在溢出文件中）的限制，
        # 该机制需要保留完整输出来溢出到文件。此处的尾部保留上限现在仅用于防止失控的命令耗尽内存；
        # 它过去曾是 20,000 字符，并会悄悄丢弃开头的输出。
        # Memory safety net only (OPE-186): what the MODEL sees is bounded by the engine's
        # tool-result cap (head + marker + tail, full text in a spill file), which needs
        # the whole output to spill. This tail-keep cap now only stops a runaway command
        # from filling memory; it used to be 20,000 and silently dropped the beginning.
        max_output_chars: int = 2_000_000,
    ) -> None:
        self.cwd = str(Path(cwd).expanduser().resolve())
        self.default_timeout = default_timeout
        self.max_output_chars = max_output_chars
        self._marker = f"__COWORKER_DONE_{uuid.uuid4().hex}__"
        self._is_windows = _IS_WINDOWS
        self._bg_tasks: dict[str, _BackgroundTask] = {}
        self._bg_counter = 0
        # [中文] 由 interrupt_now() 设置（用户点击停止）—— run() 的读取循环将其视为提前截止时间，
        # 因此执行中的前台命令会在一个周期内被终止。
        # Set by interrupt_now() (user Stop) — run()'s read loop treats it like an
        # early deadline, so the in-flight foreground command dies within one tick.
        self._abort = threading.Event()

        # [中文] 按操作系统选择原生 Shell。POSIX 逐行驱动 bash；Windows 以 `-Command -` 模式驱动 PowerShell，
        # 这是一个真正的标准输入 REPL（增量执行，且工作目录和环境变量跨命令持久保留）。
        # Pick a native shell per-OS. POSIX drives bash line-by-line; Windows drives
        # PowerShell in `-Command -` mode, which is a true stdin REPL (executes
        # incrementally, and cwd/env persist across commands).
        if shell_path is None:
            shell_path = "powershell.exe" if self._is_windows else "/bin/bash"
        self._shell_path = shell_path
        self._env = {**os.environ, **_NONINTERACTIVE_ENV, **(env or {})}
        # [中文] 托管的固定版本工具（toolchain.install）放置在同一个稳定的 bin 目录下；
        # 预先将其置于 PATH 中 —— 即使此时该目录下尚未安装任何工具 —— 意味着用户在会话中途批准安装的工具
        # 可以在当前 Shell 中立即按名称执行，无需重启 Shell。
        # 附加在最后：用户自己的工具副本始终优先生效。
        # Managed pinned tools (toolchain.install) land under one stable bin dir; putting
        # it on PATH up front — even before anything is installed there — means a tool the
        # user approves mid-session works in THIS shell immediately, by name, no respawn.
        # Appended last: the user's own copies always win.
        from .. import toolchain

        path = self._env.get("PATH", "")
        managed_bin = str(toolchain.bin_dir())
        if managed_bin not in path.split(os.pathsep):
            self._env["PATH"] = f"{path}{os.pathsep}{managed_bin}" if path else managed_bin
        self._spawn()

    def _spawn(self) -> None:
        """[中文] 启动（或重启）Shell 进程及其读取器。可用于自我修复：
        如果某条命令超时且 Shell 被强制关闭，下一次 `run` 会在此处基于最后已知的工作目录（cwd）重新生成
        （Shell 内部的环境变量/局部变量会丢失，但会话得以继续）。

        Start (or restart) the shell process and its reader. Reused for self-healing:
        if a command times out and the shell is hard-closed, the next `run` respawns here
        in the last known `cwd` (in-shell env/vars are lost, but the session continues).
        """
        if self._is_windows:
            argv = [
                self._shell_path,
                "-NoProfile",
                "-NoLogo",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                "-",
            ]
            # [中文] 新建进程组，以便超时时可以向子进程（且仅子进程）发送 Ctrl-Break，而不影响我们自己的进程。
            # New process group so a timeout can deliver Ctrl-Break to the child (and only
            # the child), without signaling our own process.
            spawn_kwargs: dict[str, Any] = {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
            }
        else:
            argv = [self._shell_path]
            spawn_kwargs = {"start_new_session": True}

        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=self.cwd,
            text=True,
            bufsize=1,
            env=self._env,
            **spawn_kwargs,
        )
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        if self._is_windows and self._proc.stdin is not None:
            # [中文] 静默 REPL 提示符，避免其污染捕获到的命令输出。
            # Silence the REPL prompt so it never pollutes captured command output.
            self._proc.stdin.write("function prompt { '' }\n")
            self._proc.stdin.flush()

    def _read_loop(self) -> None:
        try:
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                self._queue.put(line)
        finally:
            self._queue.put(None)  # [中文] EOF 结束哨兵 / EOF sentinel

    def run(self, command: str, timeout: Optional[float] = None) -> dict[str, Any]:
        if self._proc.poll() is not None:
            # [中文] Shell 已退出（例如在上一个命令超时后被强制关闭）。重新生成 Shell，
            # 以便会话自我修复，而不是卡死后续所有命令。
            # Shell exited (e.g. hard-closed after a prior command's timeout). Respawn so
            # the session self-heals rather than wedging every future command.
            self._spawn()
        if self._proc.stdin is None:
            return self._result(
                command, None, "", timed_out=False, error="shell not running"
            )

        timeout = timeout or self.default_timeout
        self._abort.clear()
        # [中文] 运行命令，然后输出包含退出代码和当前工作目录的标记行。
        # Run the command, then emit a marker line with exit code + cwd.
        self._proc.stdin.write(command + "\n")
        self._proc.stdin.write(self._trailer())
        self._proc.stdin.flush()

        deadline = time.monotonic() + timeout
        interrupted = False
        timed_out = False
        aborted = False
        exit_code: Optional[int] = None
        lines: list[str] = []

        while True:
            if self._abort.is_set():
                # [中文] 用户停止：在当前时钟周期复用截止时间路径（在 POSIX 上中断并重新同步，
                # 在 Windows 上果断终止 Shell），而不是干等超时。
                # User Stop: reuse the deadline path this tick (interrupt-and-resync on
                # POSIX, decisive shell kill on Windows) instead of waiting out the timeout.
                aborted = True
                deadline = time.monotonic()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if self._is_windows:
                    # [中文] PowerShell 没有可靠的“中断单个命令并保留 REPL”原语，
                    # 因此不要尝试重新同步 —— 直接彻底终止整个 Shell 进程树。
                    # 下一次 run() 将在最后的工作目录中重新生成（会话继续）。
                    # PowerShell has no reliable "interrupt one command, keep the REPL"
                    # primitive, so don't try to resync — kill the shell tree decisively.
                    # The next run() respawns in the last cwd (session continues).
                    timed_out = True
                    self.close()
                    break
                if not interrupted:
                    # [中文] 第一次截止时间：中断正在运行的命令并继续读取，直到其标记行到达，
                    # 从而使输出流与下一条命令保持同步。SIGINT 会让命令退出，尾部 printf 则输出标记。
                    # First deadline: interrupt the running command and keep reading
                    # until ITS marker arrives, so the stream stays in sync for the
                    # next command. SIGINT makes the command exit and the trailer
                    # printf emit the marker.
                    interrupted = True
                    timed_out = True
                    self._interrupt()
                    deadline = time.monotonic() + 3.0  # [中文] 等待标记重新同步的宽限期 / grace to resync on the marker
                    continue
                # [中文] 宽限期已过且仍未收到标记：Shell 已卡死。强制终止它，
                # 以免后续命令发生状态失步（Shell 内部会话状态丢失）。
                # Grace expired and still no marker: the shell is wedged. Hard-kill
                # so future commands don't desync (session state is lost).
                self.close()
                break
            try:
                item = self._queue.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if item is None:
                break  # [中文] Shell 进程已死亡 / shell died
            if self._marker in item:
                exit_code = _parse_exit_code(item, self._marker)
                cwd = _parse_cwd(item, self._marker)
                if cwd:
                    self.cwd = cwd
                break
            lines.append(item)

        output = "".join(lines)
        truncated = len(output) > self.max_output_chars
        if truncated:
            # [中文] 保留尾部：构建工具和测试运行器通常将最终判定结果输出在末尾。
            # Keep the TAIL: builds and test runners put the verdict at the end.
            output = output[-self.max_output_chars :]
        return self._result(
            command,
            exit_code,
            output,
            timed_out=timed_out,
            truncated=truncated,
            error="interrupted by user" if aborted else None,
        )

    def interrupt_now(self) -> None:
        """[中文] 用户停止：让正在执行的前台 `run()` 在下一个读取周期（≤0.5秒）内退出。
        线程安全；在没有命令运行时为空操作。后台任务不受影响 —— 它们是显式的后台长驻任务。

        User Stop: make an in-flight foreground `run()` bail on its next read tick
        (≤0.5s). Thread-safe; a no-op when nothing is running. Background tasks are
        left alone — they're explicitly fire-and-forget."""
        self._abort.set()

    # -- background tasks ---------------------------------------------------------
    def run_background(self, command: str) -> dict[str, Any]:
        self._bg_counter += 1
        task_id = f"bg-{self._bg_counter}"
        try:
            task = _BackgroundTask(task_id, command, self.cwd, self._env)
        except OSError as exc:
            return {"error": f"failed to start background task: {exc}"}
        self._bg_tasks[task_id] = task
        return {
            "task_id": task_id,
            "command": command,
            "status": "running",
            "note": "use shell_task_output to read its output, shell_task_kill to stop it",
        }

    def background_output(self, task_id: str) -> dict[str, Any]:
        task = self._bg_tasks.get(task_id)
        if task is None:
            return {"error": f"unknown task: {task_id}"}
        output = task.read_new()
        truncated = len(output) > self.max_output_chars
        if truncated:
            output = output[-self.max_output_chars :]
        exit_code = task.proc.poll()
        return {
            "task_id": task_id,
            "status": "running" if exit_code is None else "exited",
            "exit_code": exit_code,
            "output": output,
            "truncated": truncated,
        }

    def background_kill(self, task_id: str) -> dict[str, Any]:
        task = self._bg_tasks.get(task_id)
        if task is None:
            return {"error": f"unknown task: {task_id}"}
        task.kill()
        try:
            task.proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
        return {
            "task_id": task_id,
            "status": "running" if task.proc.poll() is None else "killed",
            "exit_code": task.proc.poll(),
        }

    def _trailer(self) -> str:
        """[中文] 追加在每条用户命令后面的尾随命令。输出单行 `<marker> <exit> <cwd>`，
        由 `_parse_exit_code` / `_parse_cwd` 解析。读取 *上一条* 命令的退出状态，
        因此必须紧随其后作为独立语句运行。

        Command appended after each user command. Emits one line `<marker> <exit> <cwd>`
        parsed by `_parse_exit_code` / `_parse_cwd`. Reads the exit status of the *preceding*
        command, so it must run as its own statement right after it."""
        if self._is_windows:
            # [中文] PowerShell：`$?` 是表示成功的布尔值；`$LASTEXITCODE` 是最后一个原生程序的退出码。
            # 成功 → 0；否则使用该程序的退出码，回退默认值为 1。
            # PowerShell: `$?` is the success bool; `$LASTEXITCODE` is the exit code of the
            # last native program. Success → 0; else the program's code, falling back to 1.
            return (
                f'"`n{self._marker} '
                f"$(if ($?) {{0}} else {{ if ($LASTEXITCODE) {{$LASTEXITCODE}} else {{1}} }}) "
                f'$($PWD.Path)"\n'
            )
        return f'printf "\\n%s %s %s\\n" "{self._marker}" "$?" "$PWD"\n'

    def _interrupt(self) -> None:
        # [中文] 中断正在运行的命令，而不是 Shell 本身，从而使会话得以保留；
        # 排队的尾随命令随后输出标记，输出流重新恢复同步。
        # Interrupt the running command, not the shell itself, so the session survives; the
        # queued trailer then emits the marker and the stream resyncs.
        if self._is_windows:
            # [中文] 向子进程的进程组发送 Ctrl-Break（尽最大努力）。如果标记从未恢复同步，
            # run() 的宽限超时将强制关闭该 Shell。
            # Ctrl-Break to the child's process group (best-effort). If the marker never
            # resyncs, run()'s grace timeout hard-closes the shell.
            try:
                self._proc.send_signal(signal.CTRL_BREAK_EVENT)
            except (OSError, ValueError):
                pass
            return
        try:
            found = subprocess.run(
                ["pgrep", "-P", str(self._proc.pid)],
                capture_output=True,
                text=True,
            )
            for pid in found.stdout.split():
                try:
                    os.kill(int(pid), signal.SIGINT)
                except (ProcessLookupError, ValueError, OSError):
                    pass
        except (FileNotFoundError, OSError):
            pass

    def interrupt(self) -> None:
        self._interrupt()

    def close(self) -> None:
        if self._is_windows:
            # [中文] 终止整个进程树 —— 超时的命令可能生成了子进程，若仅对 Shell 执行 `terminate()` 会导致子进程孤儿化。
            # 然后等待回收进程，以便 `poll()` 可靠地报告退出状态，下一次 run() 的重新生成检查依赖于此。
            # Kill the whole tree — a timed-out command may have spawned children that
            # `terminate()` (the shell only) would orphan. Then reap so `poll()` reliably
            # reports the exit, which the next run()'s respawn check depends on.
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self._proc.pid)],
                    capture_output=True,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                self._proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass
            return
        try:
            self._proc.terminate()
        except (ProcessLookupError, OSError):
            pass

    def _result(
        self, command, exit_code, output, *, timed_out, truncated=False, error=None
    ):
        result = {
            "command": command,
            "cwd": self.cwd,
            "exit_code": exit_code,
            "output": output,
            "timed_out": timed_out,
            "truncated": truncated,
        }
        if error:
            result["error"] = error
        return result


def _parse_exit_code(line: str, marker: str) -> Optional[int]:
    parts = line.strip().split()
    try:
        return int(parts[parts.index(marker) + 1])
    except (ValueError, IndexError):
        return None


def _parse_cwd(line: str, marker: str) -> Optional[str]:
    parts = line.strip().split()
    try:
        return " ".join(parts[parts.index(marker) + 2 :]) or None
    except (ValueError, IndexError):
        return None


_RUN_SHELL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_shell",
        "description": (
            "Run a shell command in the persistent session (cwd and env persist across "
            "calls). Output longer than the limit keeps the END (where test/build verdicts "
            "are). Set run_in_background for long-running processes like dev servers, then "
            "poll with shell_task_output."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command to run.",
                },
                "description": {
                    "type": "string",
                    "description": (
                        "Short human-readable summary of what the command does (e.g. "
                        "'Install dependencies'), shown in approval prompts and logs."
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": (
                        f"Max seconds to wait (default {int(_DEFAULT_TIMEOUT)}, "
                        f"max {int(_MAX_TIMEOUT)}). Ignored for background tasks."
                    ),
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "Run detached and return a task_id immediately instead of waiting. "
                        "Use for servers, watchers, and very long builds."
                    ),
                },
            },
            "required": ["command"],
        },
    },
}

_TASK_OUTPUT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "shell_task_output",
        "description": (
            "Read NEW output (since the last read) from a background task started with "
            "run_shell run_in_background=true, plus its status and exit code."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task_id returned by run_shell.",
                }
            },
            "required": ["task_id"],
        },
    },
}

_TASK_KILL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "shell_task_kill",
        "description": "Stop a background task started with run_shell run_in_background=true.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task_id returned by run_shell.",
                }
            },
            "required": ["task_id"],
        },
    },
}


def shell_tools(executor: Executor) -> list:
    """[中文] 返回绑定到持久执行器的 Shell 工具集（`run_shell` + 后台任务辅助工具）。

    Return the shell tools (`run_shell` + background-task helpers) bound to a
    persistent executor."""

    def run_shell(
        command: str,
        description: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        run_in_background: bool = False,
    ) -> dict:
        # [中文] 此处故意不使用 `description`：它作为调用参数随附传递，
        # 从而使批准提示和审计日志能够展示意图，而不仅仅是原始命令。
        # `description` is not used here on purpose: it rides along in the call arguments
        # so approval prompts and the audit log can show intent, not just the raw command.
        if run_in_background:
            return executor.run_background(command)
        timeout = None
        if isinstance(timeout_seconds, (int, float)) and timeout_seconds > 0:
            timeout = min(float(timeout_seconds), _MAX_TIMEOUT)
        return executor.run(command, timeout=timeout)

    def shell_task_output(task_id: str) -> dict:
        return executor.background_output(task_id)

    def shell_task_kill(task_id: str) -> dict:
        return executor.background_kill(task_id)

    wrapped_run = ai.tool(
        run_shell,
        metadata=ai.ToolMetadata(
            category="shell",
            risk_level="high",
            capabilities=["run_command"],
            requires_approval=True,
        ),
    )
    wrapped_run.__coworker_schema__ = _RUN_SHELL_SCHEMA
    wrapped_output = ai.tool(
        shell_task_output,
        metadata=ai.ToolMetadata(
            category="shell",
            risk_level="low",
            capabilities=["run_command"],
            requires_approval=False,
        ),
    )
    wrapped_output.__coworker_schema__ = _TASK_OUTPUT_SCHEMA
    wrapped_kill = ai.tool(
        shell_task_kill,
        metadata=ai.ToolMetadata(
            category="shell",
            risk_level="low",
            capabilities=["run_command"],
            requires_approval=False,
        ),
    )
    wrapped_kill.__coworker_schema__ = _TASK_KILL_SCHEMA
    return [wrapped_run, wrapped_output, wrapped_kill]
