"""[中文] `git_log` —— 最近提交历史记录（只读）。

aisuite 自带的 git 工具套件提供了 `git_status`/`git_diff`；本模块增加了提交历史记录，
以便智能体在修改文件之前先查看该文件是如何演变至今的。只读操作；此处不提供 commit/push 操作
（提示词禁止在没有用户明确要求的情况下提交代码，且即便执行也会通过 run_shell 进行）。

`git_log` — recent commit history for context (read-only).

aisuite's git toolkit gives `git_status`/`git_diff`; this adds history so the agent can see how
a file came to be the way it is before changing it. Read-only; no commit/push here (the prompt
forbids those without explicit ask, and they'd go through run_shell anyway).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Optional

import aisuite as ai

_SEP = "\x1f"

_SCHEMA = {
    "type": "function",
    "function": {
        "name": "git_log",
        "description": (
            "Recent git commit history (hash, author, date, subject). Optionally scope to a path. "
            "Use it to understand how code evolved before editing. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Optional file/dir to scope history to.",
                },
                "max_count": {
                    "type": "integer",
                    "description": "How many commits (default 20, max 200).",
                },
            },
        },
    },
}


def git_tools(workspace: str) -> list:
    """[中文] 返回针对指定工作区的 Git 提交历史工具集。
    Return the Git commit history tools for the specified workspace.
    """
    root = str(Path(workspace).resolve())

    def git_log(path: Optional[str] = None, max_count: int = 20) -> dict[str, Any]:
        n = max_count if isinstance(max_count, int) and max_count > 0 else 20
        n = min(n, 200)
        cmd = [
            "git",
            "-C",
            root,
            "log",
            f"-n{n}",
            f"--pretty=format:%h{_SEP}%an{_SEP}%ad{_SEP}%s",
            "--date=short",
        ]
        if path:
            cmd += ["--", path]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except Exception as exc:
            return {"error": f"git log failed: {exc}"}
        if out.returncode != 0:
            return {"error": (out.stderr or "git log failed").strip()[:300]}
        commits = []
        for line in out.stdout.splitlines():
            parts = line.split(_SEP)
            if len(parts) == 4:
                commits.append(
                    {
                        "hash": parts[0],
                        "author": parts[1],
                        "date": parts[2],
                        "subject": parts[3],
                    }
                )
        return {"count": len(commits), "commits": commits}

    git_log.__name__ = "git_log"
    git_log.__doc__ = _SCHEMA["function"]["description"]
    git_log.__aisuite_tool_metadata__ = ai.ToolMetadata(
        name="git_log",
        category="git",
        risk_level="low",
        capabilities=["git"],
        requires_approval=False,
    )
    git_log.__coworker_schema__ = _SCHEMA
    return [git_log]
