"""[中文] 记忆设置 —— 开关选项与用户常驻规则。

设置层状态，故意独立于记忆数据表之外（MEMORY-SPEC §2, §4.3, §6）：

- ``enabled``：关闭意味着构建引擎时不包含记忆工具、不注入记忆块、也不包含记忆指引。现有记忆保留但保持非活跃状态。在构建时读取；正在运行的会话按照其启动时的模式运行完毕。
- ``user_rules``：用户在“设置”中输入的单一文本块。原样逐字注入到自动记忆上方；发生冲突时以规则为准。**智能体绝不能编写、修改或删除此内容** —— 任何工具都无法触碰它；唯一的写入源是来自管理器的“设置”用户界面。

[English]
Memory settings — the on/off switch and the user's standing rules.

Settings-level state, deliberately outside the memory table (MEMORY-SPEC §2, §4.3, §6):

- ``enabled``: off means engines are built with no memory tools, no memories block, and
  no memory guidance. Existing memories are kept but inert. Read at build time; running
  sessions finish under the mode they started with.
- ``user_rules``: one text blob the user typed into Settings. Injected verbatim above
  auto memories; on conflict the rule wins. **The agent never writes, edits, or deletes
  this** — no tool touches it; the only writer is the Settings UI via the manager.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

# [中文] 用户规则是有界的设置字段，而非文档库：容量足以容纳真实的规则列表，又足够小以防止误粘贴（或恶意客户端）导致未来每个系统 Prompt 膨胀。
# [English] User Rules is a bounded settings field, not a document store: big enough for any
# real rule list, small enough that a paste-accident (or a hostile client) can't
# bloat every future system prompt.
MAX_USER_RULES_CHARS = 20_000


# [中文] 记忆设置存储类：管理记忆开关与用户常驻规则的本地持久化
# [English] Memory settings store: manages local persistence of memory toggle and user rules
class MemorySettingsStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    # [中文] 从磁盘加载配置字典
    # [English] Load settings dictionary from disk
    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    # [中文] 保存配置字典到磁盘
    # [English] Save settings dictionary to disk
    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # [中文] 记忆功能是否启用，默认开启（spec §5.4）
    # [English] Whether memory is enabled, defaults to True (spec §5.4)
    @property
    def enabled(self) -> bool:
        return bool(self._load().get("enabled", True))  # on by default (spec §5.4)

    # [中文] 获取用户设置的常驻规则文本
    # [English] Get user's standing rules text
    @property
    def user_rules(self) -> str:
        rules = self._load().get("user_rules", "")
        return rules if isinstance(rules, str) else ""

    # [中文] 更新记忆配置（启用状态与用户规则文本）
    # [English] Update memory settings (enabled flag and user rules text)
    def set(
        self, *, enabled: Optional[bool] = None, user_rules: Optional[str] = None
    ) -> dict:
        with self._lock:
            data = self._load()
            if enabled is not None:
                data["enabled"] = bool(enabled)
            if user_rules is not None:
                data["user_rules"] = str(user_rules)[:MAX_USER_RULES_CHARS]
            self._save(data)
        return {"enabled": self.enabled, "user_rules": self.user_rules}

    # [中文] 获取当前记忆配置的快照字典
    # [English] Get current memory settings snapshot dictionary
    def snapshot(self) -> dict:
        return {"enabled": self.enabled, "user_rules": self.user_rules}


def format_user_rules(rules: str) -> str:
    """[中文] 系统 Prompt 中用户规则的格式化块。规则为空则返回空字符串。

    [English]
    The system-prompt block for user rules. Empty rules -> empty string."""
    text = (rules or "").strip()
    if not text:
        return ""
    return (
        "User rules (written by the user in Settings; always follow these — on any "
        f"conflict they outrank learned memories):\n{text}"
    )
