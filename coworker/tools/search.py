"""[中文] 快速代码搜索 (`grep`) —— 优先使用 ripgrep，不可用时回退到 Python 的目录遍历。

ripgrep 会自动遵循 `.gitignore`，因此自动跳过 `node_modules`/`target`/`dist` 等目录；
回退实现也会跳过一组硬编码的庞大目录。只读操作，受工作区范围限定。返回 `file:line:text`。

Fast code search (`grep`) — ripgrep when available, a Python walk otherwise.

ripgrep respects `.gitignore`, so it skips `node_modules`/`target`/`dist` automatically; the
fallback skips a hardcoded set of heavy dirs. Read-only, workspace-scoped. Returns file:line:text.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

import aisuite as ai

# [中文] 各操作系统的应用程序数据目录。这些并非构建产生的噪音文件：在 macOS 14+ 上，仅仅是 *进入*
# ~/Library/Application Support（其他应用程序的沙盒容器）就会触发系统的应用数据 TCC 权限保护，
# macOS 会弹出“想要访问来自其他应用的数据”—— 这是一个令人生疑且用户从未请求过的弹窗，
# 只要工作区设在家目录就可能触发。此处设置永不遍历；若工作区位于这些目录之一，仍然可以正常搜索，
# 因为该防御规则是在遍历过程中匹配遇到的目录“名称”。
# Per-OS application data directories. These are not build noise: on macOS 14+ merely
# *descending* into ~/Library/Application Support (other apps' containers) trips the App
# Data TCC protection and macOS shows "would like to access data from other apps" — an
# alarming prompt the user never asked for, reachable whenever the workspace is a home
# directory. Never traversed; a workspace under one of these is still searched normally,
# because the guard matches directory NAMES encountered during a walk.
OS_DATA_DIRS = {
    "Library",  # macOS
    "AppData",  # Windows
    "Application Data",  # Windows (legacy junction)
}

_IGNORE_DIRS = {
    ".git",
    "node_modules",
    "target",
    "dist",
    "build",
    ".venv",
    "venv",
    "__pycache__",
    ".next",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".idea",
} | OS_DATA_DIRS

_SCHEMA = {
    "type": "function",
    "function": {
        "name": "grep",
        "description": (
            "Search the workspace for a regular-expression pattern and return matching lines as "
            "file:line:text. Fast and .gitignore-aware (skips node_modules, build dirs, etc.). "
            "Prefer this over reading files blindly to locate code. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regular expression to search for.",
                },
                "path": {
                    "type": "string",
                    "description": "Subdirectory to search (default: whole workspace).",
                },
                "glob": {
                    "type": "string",
                    "description": "Optional filename glob filter, e.g. '*.py'.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max matches (default 100, max 1000).",
                },
            },
            "required": ["pattern"],
        },
    },
}


def search_tools(workspace: str) -> list:
    """[中文] 返回工作区范围内的搜索工具集（grep）。
    Return the workspace-scoped search tools (grep).
    """
    root = Path(workspace).resolve()

    def grep(
        pattern: str,
        path: str = ".",
        glob: Optional[str] = None,
        max_results: int = 100,
    ) -> dict[str, Any]:
        n = max_results if isinstance(max_results, int) and max_results > 0 else 100
        n = min(n, 1000)
        base = (root / (path or ".")).resolve()
        try:
            base.relative_to(root)  # [中文] 限制搜索范围在工作区内部 / keep searches inside the workspace
        except ValueError:
            return {"error": "path escapes the workspace"}

        rg = shutil.which("rg")
        if rg:
            cmd = [
                rg,
                "--line-number",
                "--no-heading",
                "--color=never",
                # [中文] 始终输出文件名（即使只针对单个目标文件），并使用 NUL 空字符分隔文件名与 `line:text`，
                # 这样解析器就永远无需猜测路径在何处结束 —— 否则 Windows 盘符冒号（如 C:\ws\a.py）
                # 或匹配文本内部的冒号会被错误地当作字段分隔符（参见 issue #17）。
                # 如果没有 --with-filename，单个文件搜索将既不输出路径也不输出 NUL，解析器就会丢弃该结果。
                # Always print the filename (even for a single-file target) and
                # NUL-separate it from `line:text`, so parsing never has to guess
                # where the path ends — a Windows drive-letter colon (C:\ws\a.py),
                # or a colon inside the matched text, would otherwise be taken as a
                # field separator (issue #17). Without --with-filename a single-file
                # search emits no path and no NUL, and the parser would drop it.
                "--with-filename",
                "--null",
                "--max-count",
                str(n),
                "-e",
                pattern,
            ]
            if glob:
                cmd += ["--glob", glob]
            # [中文] 不要完全依赖工作区的 .gitignore：Python 回退搜索同样总是忽略这些生成/依赖目录。
            # 排除规则放在最后，因为 ripgrep 在冲突时以靠后的 glob 规则为准。
            # Do not rely solely on a workspace's .gitignore: the Python fallback
            # always omits these generated/dependency directories too. Exclusions come
            # last because ripgrep resolves conflicting globs with the later one winning.
            for ignored in sorted(_IGNORE_DIRS):
                cmd += ["--glob", f"!**/{ignored}/**"]
            cmd.append(str(base))
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            except Exception as exc:
                return {"error": f"grep failed: {exc}"}
            if out.returncode not in (0, 1):  # [中文] 1 表示未找到匹配项 / 1 = no matches
                return {"error": (out.stderr or "ripgrep error").strip()[:300]}
            return {"engine": "ripgrep", **_parse_rg(out.stdout, root, n)}

        return {"engine": "python", **_py_grep(root, base, pattern, glob, n)}

    grep.__name__ = "grep"
    grep.__doc__ = _SCHEMA["function"]["description"]
    grep.__aisuite_tool_metadata__ = ai.ToolMetadata(
        name="grep",
        category="search",
        risk_level="low",
        capabilities=["search"],
        requires_approval=False,
    )
    grep.__coworker_schema__ = _SCHEMA
    return [grep]


def _rel(path: str, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(root))
    except (ValueError, OSError):
        return path


def _parse_rg(stdout: str, root: Path, n: int) -> dict[str, Any]:
    # [中文] `rg --null` 将每个匹配项输出为 `<path>\0<line>:<text>`。
    # 首先通过 NUL 字节拆分出路径，意味着 Windows 盘符中的冒号（C:\...）
    # 绝不会与行号/文本分隔符混淆 —— 旧版 `split(":", 2)` 会把 `C:\ws\a.py:12:def f()`
    # 错误解析为 file="C", line="\ws\a.py", text="12:def f()"。
    # `rg --null` emits each match as `<path>\0<line>:<text>`. Splitting the path off
    # on the NUL byte first means the colon inside a Windows drive letter (C:\...) is
    # never confused with the line/text separators — the old `split(":", 2)` parsed
    # `C:\ws\a.py:12:def f()` as file="C", line="\ws\a.py", text="12:def f()".
    matches: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        path, sep, rest = line.partition("\0")
        if not sep:
            continue
        ln, _, txt = rest.partition(":")
        matches.append(
            {
                "file": _rel(path, root),
                "line": int(ln) if ln.isdigit() else 0,
                "text": txt[:300],
            }
        )
        if len(matches) >= n:
            break
    return {"count": len(matches), "matches": matches}


def _py_grep(
    root: Path, base: Path, pattern: str, glob: Optional[str], n: int
) -> dict[str, Any]:
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return {"error": f"invalid regex: {exc}", "count": 0, "matches": []}
    matches: list[dict[str, Any]] = []
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS]
        for fn in files:
            if glob and not fnmatch.fnmatch(fn, glob):
                continue
            fp = Path(dirpath) / fn
            try:
                with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
                    for i, line in enumerate(fh, 1):
                        if rx.search(line):
                            matches.append(
                                {
                                    "file": _rel(str(fp), root),
                                    "line": i,
                                    "text": line.rstrip()[:300],
                                }
                            )
                            if len(matches) >= n:
                                return {"count": len(matches), "matches": matches}
            except OSError:
                continue
    return {"count": len(matches), "matches": matches}
