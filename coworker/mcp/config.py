"""[中文] MCP 服务器配置 —— 标准的 `mcpServers` JSON，采用全局 + 工作区分层机制。

全局配置：  ~/.config/coworker/mcp.json
工作区配置：<workspace>/.coworker/mcp.json   （在名称冲突时覆盖全局，
            但仅在用户信任该工作区后生效 —— 与代码仓库 `allowed_commands` 的安全门控一致）

格式与 Claude Desktop / Cursor / Codex 粘贴兼容。command/args/env/url/headers 中的 `${VAR}`
引用在加载时通过 SecretStore 解析（包含系统环境变量 + 本地 `.env`）。REST 编辑操作的目标是**全局**配置文件。

[English]
MCP server config — the standard `mcpServers` JSON, layered global + workspace.

Global:    ~/.config/coworker/mcp.json
Workspace: <workspace>/.coworker/mcp.json   (overrides global on name clash,
           but only after the user trusts that workspace — same gate as
           repository `allowed_commands`)

Paste-compatible with Claude Desktop / Cursor / Codex. `${VAR}` refs in command/args/env/
url/headers are resolved at load time via the SecretStore (env + local `.env`). REST edits
target the **global** file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..secrets import SecretStore, state_dir

_HTTP_TYPES = {"http", "https", "sse", "streamable-http", "streamable_http"}


@dataclass
class MCPServerDef:
    name: str
    transport: str  # "stdio" | "http"
    command: Optional[str] = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    url: Optional[str] = None
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    include_tools: Optional[list[str]] = None
    exclude_tools: Optional[list[str]] = None
    requires_approval: bool = True
    # [中文] "oauth" → 具备动态客户端注册（DCR）的浏览器端 OAuth 2.1 + PKCE（参见 mcp/oauth.py）。
    # 仅适用于 HTTP 传输；令牌保存在 SecretStore 中，绝不保存在本配置文件中。
    # [English] "oauth" → browser OAuth 2.1 + PKCE with Dynamic Client Registration (mcp/oauth.py).
    # HTTP transport only; tokens live in the SecretStore, never in this file.
    auth: Optional[str] = None


def global_mcp_path() -> Path:
    return state_dir() / "mcp.json"


def _read(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _config_paths(
    workspace: Optional[str | Path], *, workspace_trusted: bool
) -> list[Path]:
    """[中文] 待合并的配置文件路径列表。工作区 MCP 属于可执行文件来源（stdio 进程派生），
    因此不受信任的仓库的 `.coworker/mcp.json` 绝不会被读取 —— 仅凭克隆代码绝不能定义在会话开启时运行的进程。

    [English]
    Config files to merge. Workspace MCP is executable provenance (stdio spawn),
    so an untrusted repo's `.coworker/mcp.json` is never read — cloning alone must
    not be enough to define processes that run at session open.
    """
    paths = [global_mcp_path()]
    if workspace and workspace_trusted:
        paths.append(Path(workspace).expanduser() / ".coworker" / "mcp.json")
    return paths


def _parse(name: str, raw: dict[str, Any], secrets: SecretStore) -> MCPServerDef:
    raw = secrets.resolve(raw)  # [中文] 在构建 server def 之前在各处解析 ${VAR} / [English] resolve ${VAR} everywhere before building the def
    declared = str(raw.get("type", "")).lower()
    is_http = declared in _HTTP_TYPES or bool(raw.get("url"))
    return MCPServerDef(
        name=name,
        transport="http" if is_http else "stdio",
        command=raw.get("command"),
        args=list(raw.get("args", []) or []),
        env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
        cwd=raw.get("cwd"),
        url=raw.get("url"),
        headers={str(k): str(v) for k, v in (raw.get("headers") or {}).items()},
        enabled=bool(raw.get("enabled", True)),
        include_tools=raw.get("include_tools"),
        exclude_tools=raw.get("exclude_tools"),
        requires_approval=bool(raw.get("requires_approval", True)),
        auth=(str(raw["auth"]).lower() if raw.get("auth") else None),
    )


def load_mcp_servers(
    workspace: Optional[str | Path] = None,
    *,
    secrets: Optional[SecretStore] = None,
    workspace_trusted: bool = False,
) -> list[MCPServerDef]:
    """[中文] 将全局与（受信任时的）工作区 `mcpServers` 合并为解析后的服务器定义列表。

    仅有受信任的工作区才会参与合并 —— 这与代码仓库 ``allowed_commands`` 属于相同的许可边界 ——
    并且**在命名冲突时全局配置胜出**，因此即使是受信任的代码仓库也无法通过复用全局名称来静默重定义全局服务器。
    工作区定义中的 ``${VAR}`` 引用从用户的环境变量中解析，这之所以被允许完全是因为该工作区已被信任；
    不受信任的工作区绝不会被读取。

    [English]
    Merge global + (when trusted) workspace `mcpServers` into parsed server defs.

    Only trusted workspaces contribute — the same consent boundary as repository
    ``allowed_commands`` — and **global wins on name clash**, so even a trusted repo
    cannot silently redefine a global server by reusing its name. ``${VAR}`` refs in
    a workspace def are resolved from the user's env, which is acceptable only because
    the workspace is trusted; untrusted workspaces are never read.
    """
    secrets = secrets or SecretStore()
    merged: dict[str, dict[str, Any]] = {}
    for path in _config_paths(workspace, workspace_trusted=workspace_trusted):
        for name, raw in (_read(path).get("mcpServers") or {}).items():
            if isinstance(raw, dict):
                merged.setdefault(name, raw)  # [中文] 先加载全局 → 冲突时全局配置生效 / [English] global first → global wins on clash
    return [_parse(name, raw, secrets) for name, raw in merged.items()]


# -- [中文] 原始全局文件变更操作（REST 接口） / [English] raw global-file mutation (REST) -----------
def read_global() -> dict[str, dict[str, Any]]:
    """[中文] 来自全局配置文件的原始 `mcpServers` 映射表（未解析 `${VAR}`）。 / [English] Raw `mcpServers` map from the global file (no `${VAR}` resolution)."""
    return dict(_read(global_mcp_path()).get("mcpServers") or {})


def _write_global(servers: dict[str, dict[str, Any]]) -> None:
    path = global_mcp_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"mcpServers": servers}, indent=2), encoding="utf-8")
    tmp.replace(path)


def put_global_server(name: str, config: dict[str, Any]) -> None:
    servers = read_global()
    servers[name] = config
    _write_global(servers)


def patch_global_server(name: str, changes: dict[str, Any]) -> bool:
    servers = read_global()
    if name not in servers:
        return False
    merged = {**servers[name], **changes}
    # [中文] None 值用于删除该键（在 merge patch 中没有其他删除键的方法）—— 由 OPE-136 信任迁移用于丢弃 `requires_approval`。
    # [English] A None value DELETES the key (there is no other way to remove one through a
    # merge patch) — used by the OPE-136 trust migration to drop `requires_approval`.
    servers[name] = {k: v for k, v in merged.items() if v is not None}
    _write_global(servers)
    return True


def delete_global_server(name: str) -> bool:
    servers = read_global()
    if name not in servers:
        return False
    del servers[name]
    _write_global(servers)
    return True
