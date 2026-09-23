"""[中文] CLI 命令行入口点。

公开命令行接口：`openworker join <link>` 与 `openworker up`（两个日常核心命令），`openworker machine <command>`（状态、密钥、日志、系统服务、离开退出），`openworker version` 与帮助信息。终端 UI（`openworker tui` 或指定技能名称）在正式作为独立产品交互面成熟之前暂不列出。

[English]
CLI entry point.

Public surface: `openworker join <link>` and `openworker up` (the two everyday commands),
`openworker machine <command>` (status, keys, logs, service, leave), `openworker version`,
and help. The terminal UI (`openworker tui`, or a skill name as before) is unlisted until
it has been tested as a product surface.
"""

from __future__ import annotations

import argparse
import os
import uuid
from pathlib import Path
from typing import Optional

from .config import load_config
from .conversations import ConversationStore
from .memory import MemorySettingsStore, SQLiteMemoryStore
from .permissions import Mode
from .secrets import state_dir


HELP = """\
usage: openworker <command>

OpenWorker — an open-source AI coworker you govern.

commands:
  join <link>   enroll this computer as a machine, then serve
                (the link comes from the app: Settings > Machines > Add a machine;
                 give the controller's address instead to approve a code there)
  up            serve again with the stored identity
  machine       manage this machine
                  status    show enrollment and the sealing-key fingerprint (--json)
                  keys      manage provider keys stored on this machine
                  logs      show the service's log (-f to follow)
                  service   run `up` as a background service (systemd, launchd)
                  leave     forget this machine's enrollment and identity
  version       print the version

Run `openworker <command> --help` for details.
Desktop app and docs: https://openworker.com
"""

# [中文] 当在错误的层级输入命令时的映射字典：提示命令的实际位置，而不是直接穿透到终端 UI 当作未知技能报错。
# [English] Typed at the wrong level: say where the command lives, rather than letting it fall
# through to the terminal UI as an unknown skill.
_AT_TOP = {"join": "join", "up": "up"}
_UNDER_MACHINE = {
    "status": "status", "keys": "keys", "secrets": "keys", "logs": "logs",
    "service": "service", "leave": "leave",
}


# [中文] 当用户输入了旧版或层级错误的命令时，提示正确的命令层级并退出
# [English] Notify user of the correct command location when typed at the wrong level and exit
def _moved(typed: str, now: str) -> None:
    import sys

    print(f"error: `openworker {typed}` is `openworker {now}`.", file=sys.stderr)
    raise SystemExit(2)


# [中文] CLI 主入口函数：解析顶层子命令（join, up, machine, version, tui 等）并分发执行
# [English] Main CLI entrypoint: parse top-level subcommands and dispatch execution
def main(argv: Optional[list[str]] = None) -> None:
    import sys

    from .remote.joiner import MACHINE_COMMANDS, TOP_COMMANDS

    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(HELP, end="")
        return
    if args[0] in ("version", "--version", "-V"):
        from .remote.channel import app_version

        print(f"openworker {app_version()}")
        return
    if args[0] in TOP_COMMANDS:
        from .remote.joiner import cli as remote_cli

        raise SystemExit(remote_cli(args, prog="openworker", only=TOP_COMMANDS))
    if args[0] == "machine":
        from .remote.joiner import cli as remote_cli

        rest = args[1:] or ["--help"]
        if rest[0] in _AT_TOP:
            _moved(f"machine {rest[0]}", _AT_TOP[rest[0]])
        raise SystemExit(remote_cli(rest, prog="openworker machine", only=MACHINE_COMMANDS))
    if args[0] in _UNDER_MACHINE:
        _moved(args[0], f"machine {_UNDER_MACHINE[args[0]]}")
    if args[0] == "auth":
        _moved("auth join <address>", "join <address>")
    if args[0] == "tui":
        args = args[1:]

    cfg = load_config()
    parser = argparse.ArgumentParser(
        prog="openworker tui", description="Terminal UI (unlisted)."
    )
    parser.add_argument(
        "skill", nargs="?", default="code", help="skill to launch (default: code)"
    )
    parser.add_argument("--cwd", default=".", help="workspace directory")
    parser.add_argument(
        "--model", default=cfg.model, help="model id, e.g. openai gpt-5.5"
    )
    parser.add_argument(
        "--mode",
        default=cfg.mode,
        choices=["plan", "interactive", "auto", "bypass-approvals", "auto-approve"],
        help="permission mode",
    )
    parser.add_argument("--resume", default=None, help="resume a session id")
    args = parser.parse_args(args)

    workspace = Path(args.cwd).expanduser().resolve()
    # [中文] 与 GUI/服务端共享的统一全局状态目录（所有会话统一归整处）
    # [English] Unified global store shared with the GUI/server (one place for all conversations).
    data_dir = state_dir()
    # [中文] 与 GUI 管理的开关和用户规则保持一致（MEMORY-SPEC §4.3/§6）。
    # 存储始终接入：关闭意味着“停止学习新事实”，但已保存的事实依然可用。
    # [English]
    # Same on/off switch and user rules the GUI manages (MEMORY-SPEC §4.3/§6). The
    # store is always wired: off means "stop learning", so saved facts stay usable.
    memory_settings = MemorySettingsStore(data_dir / "memory-settings.json")
    memory_store = SQLiteMemoryStore(data_dir / "coworker.db")
    session_store = ConversationStore(data_dir)
    session_store.touch_workspace(os.path.realpath(str(workspace)))

    resume_messages = None
    session_id = args.resume or uuid.uuid4().hex[:12]
    model, mode = args.model, args.mode
    if args.resume:
        record = session_store.load(args.resume)
        if record is not None:
            resume_messages = record.messages
            model, mode = record.model, record.mode

    from .tui.app import CoworkerApp

    app = CoworkerApp(
        workspace=workspace,
        model=model,
        mode=Mode(mode),
        memory_store=memory_store,
        memory_off=not memory_settings.enabled,
        user_rules=memory_settings.user_rules,
        session_store=session_store,
        session_id=session_id,
        resume_messages=resume_messages,
    )
    app.run()


if __name__ == "__main__":
    main()
