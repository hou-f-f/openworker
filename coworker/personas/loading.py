"""[中文] 第三方角色加载与安装时能力授权同意机制。

角色可以从本地目录或 Git URL 加载。因为角色不包含可执行代码（它仅引用经审查的目录能力、连接器和 MCP 服务器），
所以“安装”一个角色是一个轻量级的信任事件：
我们计算其将能够执行的操作的**同意摘要（Consent Summary）**（工具、风险等级、连接器、MCP、聊天消息、推荐权限模式），
并在用户批准同意后才启用该角色。加载操作绝不会写入风险覆盖规则或提升任何模式。

[English] Third-party persona loading + install-time capability consent.

A persona is loaded from a local directory or a git URL. Because a persona ships no executable
code (it only references vetted catalog capabilities, connectors, and MCP servers), "installing"
one is a light trust event: we compute a **consent summary** of what it will be able to do
(tools, risk classes, connectors, MCP, messaging, recommended mode) and the user approves that
before the persona is enabled. Loading never writes risk overrides or elevates any mode.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Optional

from .manifest import PersonaManifest


def consent_summary(m: PersonaManifest) -> dict:
    """[中文] 角色将被允许执行的能力摘要 —— 在安装时展示给用户以便其审批同意。
    [English] What a persona will be able to do — shown at install for the user to approve."""
    from ..catalog import risk_summary

    return {
        "id": m.id,
        "name": m.name,
        "description": m.description,
        "tools": list(m.tools),
        "risk": sorted(rc.value for rc in risk_summary(m.tools)),
        # [中文] "all" | [连接器 ID 列表] | [] —— 同意界面显示实际名称，而不是简陋的 "使用连接器" 标志。
        # [English] "all" | [connector ids] | [] — the consent screen shows the actual names,
        # never a bare "uses connectors" bit (OPE-93).
        "connectors": "all" if m.connectors is True else list(m.connectors or ()),
        "mcp": list(m.mcp),
        "messaging": m.can_chat,  # [中文] 源自 connectors (规范 §11) / [English] derived from connectors (spec §11)
        # [中文] "lead" 角色可创建和指导 worker 员工 —— 同意界面明确展示此项。
        # [English] "lead" personas can create and direct worker coworkers — the consent
        # screen says that plainly (capability firebreak as a manifest fact).
        "team": m.team,
        "recommended_mode": m.default_permission_mode,
        "models": list(m.models),
        "recommended_models": list(m.models),  # [中文] 旧名称别名，保留一个版本 / [English] old name, one release
        # [中文] 附带理由与等级的推荐连接器/MCP —— 同意屏幕展示这些以便用户知道员工期望使用的服务。
        # [English] Recommended connectors/MCP with reasons + tiers — the consent screen shows
        # these so the user knows what the coworker hopes to use (sharing v1).
        "recommends": [
            {"kind": r.kind, "ref": r.ref, "reason": r.reason, "tier": r.tier}
            for r in m.recommends
        ],
        "version": m.version,
        "source": m.source,
        "builtin": m.builtin,
    }


def capability_set(m: PersonaManifest) -> set[str]:
    """[中文] 角色的扁平能力集合 —— 用于判定版本更新是否扩充了能力（扩充能力需要用户重新同意；相同或缩减的更新保持已启用状态）。
    [English] The persona's capability surface as a flat comparable set — used to decide
    whether an update GREW capabilities (which requires re-consent; a same-or-smaller
    update keeps the user's enabled state)."""
    caps = {f"tool:{t}" for t in m.tools}
    caps |= {f"mcp:{s}" for s in m.mcp}
    # [中文] 按连接器细分的能力 (OPE-93)：添加连接器的更新必须扩大集合并重新触发同意。
    # [English] Per-connector caps (OPE-93): an update that ADDS a connector must grow the set and
    # re-trigger consent — the old single "connectors" bit hid exactly that change.
    if m.connectors is True:
        caps.add("connectors:all")
    else:
        caps |= {f"connector:{c}" for c in m.connectors or ()}
    if m.can_chat:
        caps.add("messaging")
    # [中文] 将单独角色变为 lead/worker 的更新必须重新同意 —— 团队能力改变了它可以指挥谁或被谁指挥。
    # [English] An update that turns a solo persona into a lead/worker must re-consent —
    # team capability changes who the coworker can direct or be directed by.
    if m.team:
        caps.add(f"team:{m.team}")
    return caps


def git_clone(
    url: str, dest: Path
) -> None:  # pragma: no cover - exercised via injection
    """[中文] 浅克隆角色仓库。可注入以便测试无需访问网络。
    [English] Shallow-clone a persona repo. Injectable so tests don't touch the network."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth", "1", url, str(dest)],
        check=True,
        capture_output=True,
    )


def cache_dir_for(url: str, base: Path) -> Path:
    """[中文] Git URL 的稳定缓存目录（清理后的末尾路径段 + 短哈希）。
    [English] A stable cache directory for a git URL (sanitized last path segment + short hash)."""
    import hashlib

    slug = url.rstrip("/").split("/")[-1].removesuffix(".git") or "persona"
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in slug)
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return base / f"{slug}-{digest}"


def clone_persona_repo(
    url: str, base: Path, *, clone: Callable[[str, Path], None] = git_clone
) -> Path:
    """[中文] 在 ``base`` 下克隆（或复用）角色仓库并返回其目录路径。
    [English] Clone (or reuse) a persona repo under ``base`` and return its directory."""
    dest = cache_dir_for(url, base)
    if not dest.is_dir():
        clone(url, dest)
    return dest
