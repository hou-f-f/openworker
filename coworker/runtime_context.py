"""[中文] 有边界且不含机密信息的运行时发现。无子进程且不读取配置文件。
[English] Bounded, secret-free runtime discovery. No subprocesses or config-file reads."""
from __future__ import annotations

import shutil
from pathlib import Path

import aisuite as ai


def capture(workspace, roots) -> dict:
    root = Path(workspace) if workspace else None

    def present(name):
        if root is None:
            return False
        candidate = root
        for part in Path(name).parts:
            candidate /= part
            if candidate.is_symlink():
                return False
        return candidate.exists()

    return {
        "workspace": str(root) if root else None,
        "working_folders": [{"path": str(path), "writable": writable} for path, writable in roots],
        "tools_available": {name: shutil.which(name) is not None for name in ("git", "python3", "node", "npm", "uv")},
        "project_entries": {
            name: present(name)
            for name in (".venv", "node_modules", "web/node_modules", "pyproject.toml", "package.json", "web/package.json", "tests", "web/e2e")
        },
        "configuration_values": "not exposed",
    }


def runtime_context_tool(permissions):
    def runtime_context() -> dict:
        """[中文] 发现当前会话的工作文件夹、工具可用性及项目环境存在情况，
        无需读取 .env、凭据、shell 配置文件或用户主目录缓存。在进行环境/工具检查前使用此工具。
        事实不等于访问权限授予。对于测试，请使用项目配置的运行器；不要输出配置或凭据。
        数据库端点和凭据故意不予返回。

        [English] Discover this session's working folders, tool availability and project
        environment presence without reading .env, credentials, shell profiles or home
        caches. Use this before environment/tooling checks. Facts are not access grants.
        For tests, use the project's configured runner; do not print configuration or
        credentials. Database endpoints and credentials are intentionally not returned.
        """
        return capture(permissions.workspace_root, permissions._resolved_roots())

    return ai.tool(runtime_context, metadata=ai.ToolMetadata(category="runtime", risk_level="low"))
