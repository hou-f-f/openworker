"""[中文] 看板加入令牌（Board join tokens）—— 为外部看板客户端提供身份认证。

令牌在服务端绑定一个参与者（ACTOR）和一个角色（ROLE）：外部工作框架（例如另一个智能体 CLI、无界面的 OpenWorker、或来自另一台机器的 `ocw` 命令行）提供此令牌，服务端据此解析出其身份 —— 客户端本身从不自行声明身份，worker 令牌也绝不能谎称自己是 lead。权限判断随后由 store 统一掌管，与应用内智能体完全相同：令牌即身份，store 即关卡。

存储仅保存哈希值（sha256）：明文仅在签发时展示一次，绝不持久化存储，因此即便注册表文件泄漏也不会泄露有效凭据。吊销按令牌进行，通过前缀检索匹配。

[English]
Board join tokens — identity for external board clients.

A token binds an ACTOR and a ROLE server-side: an external harness (another agent
CLI, a headless OpenWorker, the `ocw` CLI from a second machine) presents the token
and the server resolves who it is — the client never states its own identity, and a
worker token cannot claim to be the lead. Authority then falls to the store, same
as for in-app agents: the token is identity, the store is the gate.

Storage is hash-only (sha256): the plaintext is shown once at mint and never
persisted, so the registry file leaking doesn't leak the credentials. Revocation is
per-token, keyed by the display prefix.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .model import Actor, Role

_TOKEN_PREFIX = "owb_"  # OpenWorker board — greppable in configs, meaningless to guess


# [中文] 看板令牌管理器：负责令牌的签发（mint）、解析（resolve）、列举（entries）与吊销（revoke）
# [English] Board token manager: minting, resolving, listing, and revoking external board tokens
class BoardTokens:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.Lock()

    def mint(self, actor: str, role: str = "worker", *, label: str = "") -> str:
        """[中文] 为某个参与者身份创建令牌；仅在生成时返回一次明文。

        [English]
        Create a token for one actor identity; returns the plaintext ONCE."""
        actor = (actor or "").strip()
        if not actor:
            raise ValueError("actor is required")
        Role(role)  # validate early — a bad role should fail at mint, not at use
        token = _TOKEN_PREFIX + secrets.token_urlsafe(32)
        with self._lock:
            entries = self._load()
            entries[_digest(token)] = {
                "actor": actor,
                "role": role,
                "label": label,
                "prefix": token[:12],
                "created_ts": datetime.now(timezone.utc).isoformat(),
            }
            self._save(entries)
        return token

    # [中文] 解析令牌明文对应的 Actor 身份（参与者 ID 及角色）；若未找到或失效则返回 None
    # [English] Resolve plaintext token into Actor identity (actor id and role); return None if invalid
    def resolve(self, token: str) -> Optional[Actor]:
        if not token:
            return None
        with self._lock:
            entry = self._load().get(_digest(token))
        if entry is None:
            return None
        return Actor(id=entry["actor"], role=Role(entry["role"]))

    # [中文] 获取所有已签发令牌的元数据列表（不含明文令牌）
    # [English] List metadata of all minted tokens (excluding plaintext tokens)
    def entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(self._load().values(), key=lambda e: e["created_ts"])

    def revoke(self, prefix: str) -> int:
        """[中文] 吊销所有前缀匹配的令牌；返回被吊销的令牌数量。

        [English]
        Revoke every token whose display prefix matches; returns the count."""
        prefix = (prefix or "").strip()
        if not prefix:
            return 0
        with self._lock:
            entries = self._load()
            keep = {
                key: entry
                for key, entry in entries.items()
                if not entry["prefix"].startswith(prefix)
            }
            removed = len(entries) - len(keep)
            if removed:
                self._save(keep)
        return removed

    # [中文] 从磁盘加载令牌哈希字典
    # [English] Load token hash dictionary from disk
    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    # [中文] 原子写入令牌哈希字典到磁盘
    # [English] Atomically write token hash dictionary to disk
    def _save(self, entries: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries, indent=2))
        tmp.replace(self.path)


# [中文] 计算令牌的 SHA-256 哈希值
# [English] Compute SHA-256 digest of token
def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
