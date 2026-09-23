"""使用 uvicorn 启动服务器。供桌面 GUI sidecar 和 `openworker-server` 使用。
Launch the server with uvicorn. Used by the desktop GUI sidecar and `openworker-server`."""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

from ..config import load_config
from ..permissions import Mode
from ..secrets import state_dir, write_private_text
from .app import _WS_MAX_FRAME_BYTES, create_app
from .manager import SessionManager


def _exit_when_orphaned() -> None:
    """当作为桌面 sidecar 启动时（`COWORKER_EXIT_WITH_PARENT=1`），若父进程退出则自动终止 — 即使是突发强杀（例如 Tauri 开发观察器重启应用或崩溃），这种情况下会跳过 shell 的优雅子进程终止。独立的 `openworker-server` 运行不受影响。

    GUI 在 `COWORKER_PARENT_PID` 中传入它自己的 PID。监控该显式 PID（而非 getppid）使得这在 PyInstaller onefile 下正常工作，因为在这种情况下本进程是 GUI 的*孙进程* — bootloader 位于两者之间，因此 getppid() 指向 bootloader，当 GUI 退出时孤儿重绑定检查永远不会触发（这是之前每次应用退出泄露一对服务器进程的 Bug）。

    POSIX: 使用 kill(pid, 0) 轮询 PID。Windows: 完全没有父进程重绑定语义，因此在进程句柄上阻塞，并在其发出信号时（即父进程已退出）立即退出。

    When launched as a desktop sidecar (`COWORKER_EXIT_WITH_PARENT=1`), exit if the parent
    process dies — even on an abrupt kill (e.g. the Tauri dev watcher restarting the app, or a
    crash) that skips the shell's graceful child-kill. Standalone `openworker-server` runs are
    unaffected.

    The GUI passes its own PID in `COWORKER_PARENT_PID`. Watching that explicit PID (not
    getppid) is what makes this work under PyInstaller onefile, where this process is a
    *grandchild* of the GUI — the bootloader sits in between, so getppid() points at the
    bootloader and a re-parenting check never fires when the GUI dies (the bug that leaked
    a server pair on every app quit).

    POSIX: poll the PID with kill(pid, 0). Windows: no re-parenting semantics at all, so
    block on a process handle and exit the moment it signals (i.e. the parent exited).
    """
    if os.environ.get("COWORKER_EXIT_WITH_PARENT") != "1":
        return
    import threading

    try:
        parent = int(os.environ.get("COWORKER_PARENT_PID") or 0)
    except ValueError:
        parent = 0
    parent = parent or os.getppid()  # 独立后备方案：直接生成我们的父进程 / standalone fallback: our direct spawner

    if sys.platform == "win32":
        _watch_parent_windows(parent)
        return

    import time

    original_ppid = os.getppid()

    def watch() -> None:
        while True:
            time.sleep(1.5)
            try:
                os.kill(parent, 0)  # 仅存活探测；信号 0 不传递任何内容 / liveness probe only; signal 0 delivers nothing
            except ProcessLookupError:
                os._exit(0)
            except PermissionError:
                pass  # 存活，但归他人所有（不应发生）— 继续等待 / alive, but owned by someone else (shouldn't happen) — keep waiting
            # 次要信号：直接父进程已死亡（覆盖 PID 重用极端边界情况）。
            # Secondary signal: our direct parent died (covers PID-reuse edge cases).
            if os.getppid() != original_ppid:
                os._exit(0)

    threading.Thread(target=watch, daemon=True).start()


def _watch_parent_windows(parent: int) -> None:
    """在父进程句柄上阻塞；仅在父进程实际终止时退出。

    尽力而为 — 任何失败都会保留父进程的 RunEvent::ExitRequested 杀死动作作为主要清理路径。之前踩过的两个正确性关键点：
      - `OpenProcess` 返回 64 位 HANDLE；ctypes 默认返回类型为 32 位 int，会将句柄截断为垃圾数据。必须声明 restype/argtypes 使句柄有效。
      - 仅在 WAIT_OBJECT_0（父进程真正死亡）时调用 `os._exit`。无效句柄会立即返回 WAIT_FAILED — 将其视为“父进程已死”会在启动几秒后杀死状态完全正常的服务器（正是我们之前遇到的假死现象）。

    Block on a handle to the parent process; exit only when it actually terminates.

    Best-effort — any failure leaves the parent's RunEvent::ExitRequested kill as the primary
    cleanup path. Two correctness points that bit us before:
      - `OpenProcess` returns a 64-bit HANDLE; ctypes defaults the return type to a 32-bit int,
        which truncates the handle to garbage. Declare restype/argtypes so the handle is valid.
      - Only `os._exit` on WAIT_OBJECT_0 (the parent genuinely died). A bad handle yields
        WAIT_FAILED immediately — treating that as "parent died" would kill a perfectly healthy
        server seconds after startup (exactly the freeze we saw)."""
    import ctypes
    import threading
    from ctypes import wintypes

    SYNCHRONIZE = 0x0010_0000
    INFINITE = 0xFFFF_FFFF
    WAIT_OBJECT_0 = 0x0000_0000

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

    handle = kernel32.OpenProcess(SYNCHRONIZE, False, parent)
    if not handle:
        return

    def watch() -> None:
        if kernel32.WaitForSingleObject(handle, INFINITE) == WAIT_OBJECT_0:
            os._exit(0)

    threading.Thread(target=watch, daemon=True).start()


def build_app(workspace: str | None, model: str, mode: str):
    """构建 FastAPI 应用实例。
    Build the FastAPI application instance."""
    manager = SessionManager(
        workspace=Path(workspace).expanduser().resolve() if workspace else None,
        data_dir=state_dir(),
        model=model,
        mode=Mode(mode),
    )
    return create_app(manager)


def _ensure_ca_bundle() -> None:
    """如果解释器未配置 CA 证书包，将 SSL 指向 certifi 的 CA 证书包。macOS 框架 Python 自带环境缺少适用于 `aiohttp` 的可用系统信任库（它构建没有 CA 的 `ssl` 上下文），因此 Slack Socket-Mode 客户端会报 CERTIFICATE_VERIFY_FAILED 失败。`httpx`/`requests` 已经捆绑了 certifi；aiohttp 支持 SSL_CERT_FILE 环境变量，因此在启动时设置一次。

    Point SSL at certifi's CA bundle if the interpreter has none configured. macOS framework
    Python ships without a usable system trust store for `aiohttp` (it builds an `ssl` context with
    no CAs), so the Slack Socket-Mode client fails with CERTIFICATE_VERIFY_FAILED. `httpx`/`requests`
    bundle certifi already; aiohttp honours the SSL_CERT_FILE env var, so set it once at startup.
    """
    if os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import certifi

        os.environ["SSL_CERT_FILE"] = certifi.where()
    except Exception:
        pass


def _ensure_api_token(port: int) -> Path | None:
    """设置启动认证令牌；独立/开发令牌使用仅用户可见、端口特定的文件。
    Set launch auth; standalone/dev tokens use a user-only, port-specific file."""
    if os.environ.get("COWORKER_API_TOKEN"):
        return None  # Tauri 提供了内存令牌；绝不持久化 / Tauri supplied an in-memory token; never persist it.
    token = secrets.token_hex(32)
    os.environ["COWORKER_API_TOKEN"] = token
    return write_private_text(
        state_dir() / f"sidecar-{port}.token", token + "\n"
    )


_ENGINE_LOCK = None


def _warn_if_state_shared() -> None:
    """桌面 sidecar 运行在随机端口上，正是为了能与同一状态目录上手动运行的 `openworker-server` 共存，因此该入口点仅对第二个引擎发出警告（statelock.py）。`openworker up` 则完全拒绝；设置 COWORKER_STATE_LOCK=strict 可让该服务器也直接拒绝。

    The desktop sidecar runs on a random port precisely so it can coexist with
    a hand-run `openworker-server` on the same state dir, so this entrypoint only
    WARNS about a second engine (statelock.py). `openworker up` refuses outright;
    set COWORKER_STATE_LOCK=strict to make this server refuse too."""
    global _ENGINE_LOCK
    from ..statelock import EngineBusy, acquire

    strict = os.environ.get("COWORKER_STATE_LOCK") == "strict"
    try:
        _ENGINE_LOCK = acquire(state_dir(), timeout=10.0 if strict else 0.0)
    except EngineBusy as exc:
        if strict:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(3)
        print(f"warning: {exc}", file=sys.stderr)


def main(argv=None) -> None:
    _ensure_ca_bundle()
    cfg = load_config()  # 全局配置提供默认值 / global config supplies defaults
    parser = argparse.ArgumentParser(prog="openworker-server")
    parser.add_argument("--cwd", default=None, help="optional seed/default workspace")
    parser.add_argument("--model", default=cfg.model)
    parser.add_argument(
        "--mode",
        default=cfg.mode,
        choices=["discuss", "plan", "interactive", "auto", "bypass-approvals", "auto-approve"],
    )
    parser.add_argument("--host", default=cfg.host)
    parser.add_argument("--port", type=int, default=cfg.port)
    args = parser.parse_args(argv)

    # 发布实际绑定的端口，以便回环 URL（托管 OAuth 回调）以该进程为目标，而非 config.port。
    # 桌面 shell 在随机空闲端口上运行 sidecar（以便与 8765 端口上手动运行的服务器共存），
    # 因此托管连接重定向必须遵循实际端口，而非默认的 8765。
    # Publish the ACTUAL bound port so loopback URLs (the managed-OAuth callback)
    # target this process, not config.port. The desktop shell runs the sidecar on
    # a random free port (to coexist with a hand-run server on 8765), so the
    # managed-connect redirect must follow the real port, not the 8765 default.
    os.environ["COWORKER_PORT"] = str(args.port)
    generated_token_path = _ensure_api_token(args.port)
    try:
        import uvicorn

        _exit_when_orphaned()
        _warn_if_state_shared()
        app = build_app(args.cwd, args.model, args.mode)
        uvicorn.run(
            app, host=args.host, port=args.port, ws_max_size=_WS_MAX_FRAME_BYTES
        )
    finally:
        if generated_token_path is not None:
            generated_token_path.unlink(missing_ok=True)
            os.environ.pop("COWORKER_API_TOKEN", None)


if __name__ == "__main__":
    main()
