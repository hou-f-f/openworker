"""[中文] 用户本地风险覆盖 — 放宽（或收紧）工具的风险等级 — 以及自 OPE-136 起引入的每工具信任（TRUST）规则。

``rules`` 通过通配符模式放宽或收紧第三方（插件）工具的风险等级；最具体的规则生效。
MCP 工具不能被重新分类（``risk.classify`` 中的下限约束）；其被认可的杠杆是 ``trust`` 规则：
*免除该工具的审批卡片* — 仅此而已。受信任的工具仍保持 EXTERNAL 类别：只读模式仍会拒绝它，
自动批准审核员仍会评判它，审计日志也仍会记录它。一个存储库、两种规则类型、一个加载器 —
故意不拆分为第二个文件（架构评审拒绝了将并行信任存储作为又一套标记系统）。

**不可违背的铁律：该存储库属于用户本地，绝不能被 Persona/扩展包写入。**
Persona 可以声明其所需的工具，但唯有用户决定信任它们的程度 — 因此 Persona 加载路径绝不触及此文件（参见 ``PERMISSIONS-AND-INBOX.md``）。

[English]
User-local risk overrides — relax (or tighten) a tool's risk class — and, since
OPE-136, per-tool TRUST rules.

``rules`` relax or tighten a third-party (plugin) tool's risk class by glob; the most
specific rule wins. MCP tools cannot be reclassified (the floor in ``risk.classify``);
their sanctioned lever is a ``trust`` rule instead: *waive the approval card for this
tool* — nothing else. A trusted tool stays EXTERNAL: read-only modes still deny it, the
Auto-approve reviewer still judges it, and the audit trail still records it. One store,
two rule types, one loader — deliberately NOT a second file (the architecture review
rejected a parallel trust store as yet another labeling system).

**Inviolable rule: this store is user-local and is NEVER written by a persona/package.** A
persona can declare what tools it wants, but only the user decides how much to trust them — so
the persona-loading path never touches this file (see ``PERMISSIONS-AND-INBOX.md``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Callable, Optional

from .risk import RiskClass


@dataclass
class _Rule:
    pattern: str
    risk: RiskClass


def _specificity(pattern: str) -> int:
    """[中文] 字面量（非通配符）字符越多 = 越具体；精确模式优于任何通配符。
    [English] More literal (non-wildcard) characters = more specific; an exact pattern beats any glob."""
    literal = sum(1 for c in pattern if c not in "*?[]")
    exact = 0 if any(c in pattern for c in "*?[") else 1000
    return literal + exact


class RiskOverrideStore:
    def __init__(self, path: Optional[str | Path] = None) -> None:
        self.path = Path(path) if path else None
        # [中文] 加载时被拒绝的规则及其原因 — 呈现给用户，而不是静默地产生与文件配置不一致的权限。
        # [English]
        # Rules refused at load with the reason why — surfaced to the user instead of
        # silently shaping permissions differently than their file says.
        self.rejected: list[tuple[str, str]] = []  # (pattern, reason)
        # [中文] OPE-136 信任规则：精确工具名称（卡片写入精确名称 — 按钮仅授予卡片所展示的精确范围；通配符保留为手动编辑的高级路径）。
        # [English]
        # OPE-136 trust rules: exact tool names (the card writes exact names — a button
        # grants precisely what its card showed; globs stay a hand-editing power path).
        self._trust: list[str] = []
        self._rules: list[_Rule] = self._load()

    def _load(self) -> list[_Rule]:
        if not (self.path and self.path.is_file()):
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        # [中文] 信任条目：{"pattern": "..."} 字典（写入形式）或纯字符串。
        # [English]
        # Trust entries: {"pattern": "..."} dicts (the written form) or bare strings.
        seen: set[str] = set()
        for entry in data.get("trust", []) or []:
            pattern = (
                str(entry.get("pattern", "")) if isinstance(entry, dict) else str(entry)
            )
            if pattern and pattern not in seen:
                seen.add(pattern)
                self._trust.append(pattern)
        rules = []
        for r in data.get("rules", []):
            try:
                rule = _Rule(str(r["pattern"]), RiskClass(str(r["risk"])))
            except (KeyError, ValueError):
                continue  # skip malformed rules rather than failing the whole store
            # [中文] OPE-136：显式针对 MCP 的规则不得将工具降至 EXTERNAL 以下 —
            # risk.classify 中的底线无论如何都会静默忽略它，而在文件中读起来是一回事但实际表现是另一回事的规则，
            # 比直接拒绝规则更糟糕。（碰巧匹配 mcp__ 名称的泛型通配符正常加载；分类底线会中和对这些工具的放宽）。
            # [English]
            # OPE-136: an explicitly MCP-targeting rule may not sink a tool below
            # EXTERNAL — the floor in risk.classify would silently ignore it anyway,
            # and a rule that reads one way in the file but acts another is worse than
            # a refused rule. (Generic globs that merely HAPPEN to match mcp__ names
            # load normally; the classify floor neutralizes the loosening for those.)
            if rule.pattern.startswith("mcp__") and rule.risk in (
                RiskClass.READ,
                RiskClass.EGRESS,
            ):
                self.rejected.append(
                    (
                        rule.pattern,
                        "MCP tools cannot be reclassified below external "
                        "(OPE-136) — use a trust rule to stop the asking",
                    )
                )
                continue
            rules.append(rule)
        return rules

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "rules": [
                        {"pattern": r.pattern, "risk": r.risk.value}
                        for r in self._rules
                    ],
                    "trust": [{"pattern": p} for p in self._trust],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def set_rule(self, pattern: str, risk: RiskClass | str) -> None:
        """[中文] 添加/替换用户覆盖规则（日常路径通过审批 UI 写入此内容）。

        拒绝 `_load` 所拒绝的内容（OPE-136）：低于 EXTERNAL 的显式针对 MCP 的规则若现在写入，
        在下次加载时将被静默丢弃 — 仅在一个会话中有效随后消失的规则是一个陷阱，因此绝不写入。

        [English] Add/replace a user override (the everyday path writes this from the approval UI).

        Refuses what `_load` refuses (OPE-136): an explicitly MCP-targeting rule below
        EXTERNAL would be written now and silently dropped on the next load — a rule
        that works for one session and then vanishes is a trap, so it never lands."""
        risk = RiskClass(risk) if not isinstance(risk, RiskClass) else risk
        if pattern.startswith("mcp__") and risk in (RiskClass.READ, RiskClass.EGRESS):
            raise ValueError(
                "MCP tools cannot be reclassified below external (OPE-136) — "
                "use a trust rule to stop the asking"
            )
        self._rules = [r for r in self._rules if r.pattern != pattern]
        self._rules.append(_Rule(pattern, risk))
        self.save()

    def resolve(self, tool_name: str) -> Optional[RiskClass]:
        best: Optional[RiskClass] = None
        best_score = -1
        for r in self._rules:
            if fnmatchcase(tool_name, r.pattern):
                score = _specificity(r.pattern)
                if score > best_score:
                    best, best_score = r.risk, score
        return best

    def resolver(self) -> Callable[[str], Optional[RiskClass]]:
        """[中文] 适用于 ``PermissionEngine.risk_overrides`` / ``risk.classify`` 的可调用对象。
        [English] A callable for ``PermissionEngine.risk_overrides`` / ``risk.classify``."""
        return self.resolve

    # -- [中文] OPE-136 信任规则（免除卡片审批；绝不重新分类） / [English] OPE-136 trust rules (waive the card; never reclassify) ---------------------
    def trusted(self, tool_name: str) -> bool:
        """[中文] 是否有长期信任规则覆盖此工具（像风险规则一样使用通配符匹配）。
        [English] Whether a standing trust rule covers this tool (glob-matched, like risk rules)."""
        return any(fnmatchcase(tool_name, p) for p in self._trust)

    def set_trust(self, pattern: str) -> None:
        """[中文] 生成信任规则（审批卡片的“总是允许此工具”写入精确名称 — 按钮仅授予卡片所示权限，不作泛化）。
        [English] Mint a trust rule (the approval card's "Always allow this tool" writes an
        EXACT name — a button grants precisely what its card showed, nothing wider)."""
        if not pattern:
            return
        if pattern not in self._trust:
            self._trust.append(pattern)
            self.save()

    def revoke_trust(self, pattern: str) -> None:
        before = len(self._trust)
        self._trust = [p for p in self._trust if p != pattern]
        if len(self._trust) != before:
            self.save()

    def trust_patterns(self) -> list[str]:
        return list(self._trust)
