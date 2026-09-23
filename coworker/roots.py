"""【工作区根目录模型】会话获准访问的目录集合（Workspace Roots）。

一个 Cowork 会话拥有一个专属的临时 scratch 目录（作为第 0 个主根目录，默认可写，是默认保存文件的位置），
并且可以通过用户授权获得对额外目录的访问权限（每个目录可独立配置只读或读写）。
同一个 `list[RootDir]` 对象通过内存引用在 PermissionEngine（权限范围裁决）、
文件工具箱（路径解析与边界校验）以及上下文注入器（向模型告知当前可用目录）之间共享，
使得运行时动态增减授权目录能够被所有组件实时感知。第 0 个元素始终为主目录。

Workspace roots — the directories a session is allowed to touch.

A Cowork session is "orphan": it owns a per-conversation **scratch** dir (the primary root,
writable, the default save location) and may gain access to additional folders, each chosen
read-only or read-write. The same `list[RootDir]` object is shared by reference across the
PermissionEngine (scoping), the file toolkit (resolution), and the context injector (so the
agent is told which dirs it has), so Slice C can mutate it in place at runtime and all three
see the change. Index 0 is always the primary.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass
class RootDir:
    """【授权根目录条目】封装目录的绝对路径、是否可写及展示标签。

    Authorized directory entry encapsulating absolute path, writability, and label.
    """
    path: Path
    writable: bool = False
    label: str = ""  # 展示名称；默认使用目录的 basename / display name; defaults to the dir's basename

    def __post_init__(self) -> None:
        self.path = Path(self.path).expanduser().resolve()
        if not self.label:
            self.label = self.path.name or str(self.path)

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "writable": self.writable, "label": self.label}


def normalize_roots(roots: Iterable[Any] | None) -> list[RootDir]:
    """【规范化根目录列表】将混合类型列表转为统一的 RootDir 对象列表。
    裸字符串/Path 默认视为只读；传入 dict 或 RootDir 可显式赋予写权限。

    Coerce a mixed list (RootDir | dict{path,writable,label} | str/Path) into RootDirs.
    Bare str/Path entries are treated as read-only; pass dicts/RootDirs to grant write.
    """
    out: list[RootDir] = []
    for r in roots or []:
        if isinstance(r, RootDir):
            out.append(r)
        elif isinstance(r, dict):
            out.append(
                RootDir(
                    path=r["path"],
                    writable=bool(r.get("writable", False)),
                    label=r.get("label", ""),
                )
            )
        elif isinstance(r, (str, Path)):
            out.append(RootDir(path=r, writable=False))
        else:  # duck-typed object with .path/.writable
            out.append(
                RootDir(
                    path=getattr(r, "path"),
                    writable=bool(getattr(r, "writable", False)),
                )
            )
    return out


def render_context(roots: list[RootDir]) -> str:
    """【渲染目录上下文】生成包含当前轮次可用目录清单的 `<system-context>` 块内容。若无目录则返回空。

    The `<system-context>` body listing the dirs available this turn. Empty when no roots."""
    if not roots:
        return ""
    lines = ["Available directories (you may use file/shell tools within these):"]
    has_side_scratch = any(i > 0 and r.label == "scratch" for i, r in enumerate(roots))
    for i, r in enumerate(roots):
        access = "read-write" if r.writable else "read-only"
        if i == 0 and r.label == "scratch":
            tag = " — primary scratch, the default place to save files"
        elif i == 0:
            tag = " — the session's workspace (relative paths resolve here)"
        elif r.label == "scratch":
            tag = (
                " — your scratch directory: temporary files, and artifacts you don't "
                "want to leave inside the workspace"
            )
        else:
            tag = ""
        lines.append(f"- {r.path} [{access}]{tag}")
    if has_side_scratch:
        lines.append(
            "Relative paths resolve against the workspace; pass an absolute path to use "
            "another directory. Writes are only allowed in read-write directories. Put "
            "reports, analyses, and other non-repo deliverables in the scratch directory "
            "(they appear in the user's Artifacts panel) — write into the workspace only "
            "for changes that belong in it."
        )
    else:
        lines.append(
            "Relative paths resolve against the primary directory; pass an absolute path to use "
            "another directory. Writes are only allowed in read-write directories. If the user "
            "cares where a deliverable lands, ask; otherwise save it in the primary scratch."
        )
    return "\n".join(lines)
