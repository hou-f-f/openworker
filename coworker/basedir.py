"""[中文] OPENWORKER_BASE_DIR — 机器绝不越过的基准目录。

2026-09-02 负责人裁定（规范 §Fly sandboxes, "Base directory"）：当设置了该环境变量时，
状态目录、每个草稿工作区以及用户添加的每个文件夹 — 键入的远程路径、另存为项目目标、额外根目录 —
都必须解析在它**之下**；外部的任何路径都会被明确拒绝并报错。在托管沙箱中，它是卷挂载点（/data）。
在用户自带的机器上它是可选的；未设置表示当前行为，即无限制。

这是路径纪律规范。后续在工具级别将“不得越过基准”机械化实施的沙箱（sandbox-refactor-design.md）
只会改变底层机制，而不会改变用户所看到的行为。

[English]
OPENWORKER_BASE_DIR — one directory the box never looks beyond.

Owner ruling 2026-09-02 (spec §Fly sandboxes, "Base directory"): when the
variable is set, the state dir, every scratch workspace, and every folder a
user adds — typed remote paths, save-as-project targets, extra roots — must
resolve UNDER it; anything outside is refused with a plain error. On a
managed sandbox it is the volume mount (/data). On a machine the user
brought it is optional; unset means today's behaviour, no restriction.

This is the path discipline. The tool-level sandbox that later makes
"nothing beyond base" mechanical (sandbox-refactor-design.md) then changes a
mechanism, not what users see.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


class OutsideBaseDir(ValueError):
    """[中文] 在配置了 OPENWORKER_BASE_DIR 的机器上超出该基准目录的路径。
    [English] A path outside OPENWORKER_BASE_DIR on a box that has one."""


def base_dir() -> Optional[Path]:
    raw = os.environ.get("OPENWORKER_BASE_DIR", "").strip()
    return Path(raw).expanduser() if raw else None


def ensure_under_base(path: str | os.PathLike, what: str = "folder") -> Path:
    """[中文] 展开并解析 `path`；如果设置了 base 且路径不在其内部，则抛出 OutsideBaseDir。
    符号链接会被先行解析，以防止超出 base 的符号链接漏过。无论哪种情况均返回解析后的路径。

    [English] Expand and resolve `path`; raise OutsideBaseDir when a base is set and
    the path is not inside it. Symlinks are resolved first so a link out of
    the base does not slip through. Returns the resolved path either way."""
    resolved = Path(path).expanduser().resolve()
    base = base_dir()
    if base is None:
        return resolved
    root = base.resolve()
    if resolved != root and not resolved.is_relative_to(root):
        raise OutsideBaseDir(
            f"this machine only works under {root} — pick a {what} inside it"
        )
    return resolved
