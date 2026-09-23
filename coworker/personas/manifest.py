"""[中文] Persona Manifest（角色清单）—— 解析与验证角色定义。

格式：YAML Frontmatter（身份声明 + 能力声明），后跟作为系统提示词的 Markdown 正文。
`persona ⊇ skill` —— 与 SKILL.md 相同的 frontmatter-markdown 结构，拥有更丰富的结构化字段。
解析过程十分严格：无效的清单会抛出 ``ManifestError``，而不是静默生成损坏的角色（第三方角色必须尽早大声报错）。

[English] Persona manifest — parse + validate a persona definition.

Format: YAML frontmatter (identity + capability declaration) followed by a markdown body that
is the system prompt. `persona ⊇ skill` — the same frontmatter-markdown shape as SKILL.md, with
more structured fields. Parsing is strict: an invalid manifest raises ``ManifestError`` rather
than silently producing a broken persona (a third-party persona must fail loudly).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

# [中文] 角色 ID 会成为受管安装区域下的目录名（以及注册表键），因此限制在所有操作系统上都是文件系统安全的 slug：无路径分隔符或 `..`（防路径穿越），无 Windows 非法字符 `:*?"<>|`，且有长度限制。
# [English] Persona ids become directory names under the managed install area (and registry keys), so
# they are restricted to a filesystem-safe slug on every OS: no path separators or `..`
# (traversal), no `:*?"<>|` (invalid on Windows), bounded length.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

VALID_FAMILIES = {"code", "knowledge"}  # [中文] 遗留键，在 parse() 中做垫片兼容 / [English] legacy key, shimmed in parse()
VALID_TEAM = {"lead", "worker"}
# [中文] "auto" 保留作为 "bypass-approvals" 的旧式拼写 / [English] "auto" kept as the legacy spelling of "bypass-approvals" (Mode._missing_).
VALID_MODES = {"discuss", "plan", "interactive", "custom", "auto", "bypass-approvals", "auto-approve"}
VALID_REC_KINDS = {"connector", "mcp"}
VALID_REC_TIERS = {"core", "optional"}
VALID_GROUPS = {"general", "security"}


class ManifestError(ValueError):
    """[中文] 角色清单格式错误或引用了未知的能力/枚举值。
    [English] A persona manifest is malformed or references unknown capabilities/values."""


@dataclass
class Recommendation:
    """[中文] 角色推荐的外部连接，展示在每个会话的连接抽屉中。``ref`` 为连接器 ID 或 MCP 服务器名称；``reason`` 为解锁的价值；``tier`` 标识推荐级别。
    [English] A connection a persona recommends, surfaced in the per-session connections drawer. ``ref`` is a
    connector id or an MCP server name; ``reason`` is the value it unlocks; ``tier`` ranks it. Not
    validated against shipped connectors — a persona may recommend one we don't ship yet.
    """

    kind: str  # "connector" | "mcp"
    ref: str
    reason: str = ""
    tier: str = "optional"  # "core" | "optional"


@dataclass
class PersonaManifest:
    """[中文] 角色清单数据模型，完整描述一个 AI 员工的身份、能力与配置。
    [English] Persona manifest data model, completely describing an AI employee's identity, capabilities, and configuration."""
    id: str
    name: str
    system_prompt: str
    icon: str = ""
    tagline: str = ""
    description: str = ""
    tools: list[str] = field(default_factory=list)
    # [中文] 工作区/工具集特性 (workspace-scratch-design.md —— 替代旧的 family/workspace 对)。
    # requires_folder: 用户选取的主文件夹门禁。
    # subagents: 允许只读探索智能体扇出。
    # scheduling: 计划任务 + 自唤醒。
    # [English] Workspace/toolset traits (workspace-scratch-design.md — replaces the old
    # family/workspace pair). requires_folder: the composer/engine gate on a
    # user-picked primary folder. subagents: explorer fan-out. scheduling:
    # scheduled tasks + self-wake (defaults to the opposite of requires_folder
    # when the manifest is silent — folder personas fan out instead).
    requires_folder: bool = False
    subagents: bool = False
    scheduling: bool = True
    # `messaging:` is parsed and IGNORED (spec §11, 2026-09-05): posting to a chat
    # platform is decided by `connectors:` alone. Removed next release.
    messaging: bool = False
    # [中文] 连接器授权 (OPE-93)：False = 无，元组 = 连接器 ID 白名单（会话暴露 声明 ∩ 已连接 的交集），True = 每个已连接的连接器。
    # [English] Connector grant (OPE-93): False = none, a tuple = allowlist of connector ids
    # (session exposes declared ∩ connected), True = every connected connector — the
    # `all` sentinel, reserved for built-in general personas. Coarser grants leaked
    # undeclared tools (browser, email) into security sessions; undeclared = absent.
    connectors: bool | tuple[str, ...] = False
    # [中文] 团队身份："lead" = 协调团队；"worker" = 专为在主导者下工作构建；None = 仅单机独立。独立角色绝无团队编制资格。
    # [English] Team identity (agent-teams design, third/fourth pass): "lead" = coordinates a
    # team (gets the board coordination verbs + gates; consent copy says "can create
    # and direct worker coworkers"); "worker" = purpose-built to work under a lead
    # (board worker verbs, no ask_user-shaped prompt); None = solo-only. Solo
    # personas are NOT team-eligible — team-awareness changes who the prompt talks
    # to, so staffing fails closed on personas without the trait.
    team: Optional[str] = None
    approval_guidance: str = ""
    default_permission_mode: str = "interactive"
    # [中文] `models:` 该员工可运行的有序模型 ID 列表。机器能运行的第一项作为默认值；输入框仅显示此列表；为空表示任意模型。
    # [English] `models:` (connectors-across-machines spec §4): the ORDERED list of model ids this
    # coworker may run on. First entry a machine can run = its default there; the
    # composer's picker shows only the list; empty = any model (the machine default).
    # `recommended_models` is the old name — read as an alias for one release.
    models: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    mcp: list[str] = field(default_factory=list)
    # [中文] 共享版本字符串 ("1", "1.2", "2026-08"…)。纯信息来源。
    # [English] Sharing v1 (OPE-7): the author's version string ("1", "1.2", "2026-08"…). Purely
    # informational provenance — with folder/git distribution there is no authoritative
    # update channel, so this drives the "replaces vN" note on re-install, nothing more.
    version: str = ""
    recommends: list[Recommendation] = field(default_factory=list)
    # [中文] 分发决策：ships:false 的员工存在于代码库中但不会打包进正式发布版本。
    # [English] Distribution decision, not a maturity claim (owner, 2026-08-21): ships:false
    # coworkers exist in the codebase but are absent from release builds — internal
    # builds opt them in via OPENWORKER_UNSHIPPED=1.
    ships: bool = True
    # [中文] 设置页分组 ("general" | "security")。
    # [English] Settings-page grouping ("general" | "security"). Cosmetic — grouping never
    # gates behavior, so a third-party persona claiming "security" is harmless.
    group: str = "general"
    builtin: bool = False
    source: Optional[str] = (
        None  # [中文] 加载来源路径/URL / [English] where it was loaded from (path / url), for provenance
    )

    @property
    def recommended_models(self) -> list[str]:
        """[中文] `models` 的旧名称别名。 / [English] Old name for `models` — alias for one release, then removed."""
        return self.models

    @property
    def can_chat(self) -> bool:
        """[中文] 该角色是否可在聊天平台发帖 —— 源自 `connectors:`。
        [English] Whether this persona may post to a chat platform — derived from `connectors:`
        (every connected connector, or a declared chat platform), never from `messaging:`."""
        if self.connectors is True:
            return True
        return bool(set(self.connectors or ()) & {"slack", "telegram"})

    def to_agent(self):
        """[中文] 实例化运行时 Agent（提示词 + 目录扩展的工具 + 特性）。
        [English] Materialize the runtime Agent (prompt + catalog-expanded tools + traits)."""
        from ..agents.base import Agent
        from ..catalog import expand

        tool_ids = list(self.tools)
        factory = (lambda ctx: expand(tool_ids, ctx)) if tool_ids else None
        return Agent(
            name=self.id,
            title=self.name,
            system_prompt=self.system_prompt,
            tool_factory=factory,
            requires_folder=self.requires_folder,
            subagents=self.subagents,
            scheduling=self.scheduling,
            messaging=self.can_chat,
            connectors=self.connectors,
            team=self.team,
            approval_guidance=self.approval_guidance,
        )


def _connectors(
    persona_id: str,
    raw: Any,
    recommends: list[Recommendation],
    builtin: bool,
) -> bool | tuple[str, ...]:
    """[中文] 解析连接器授权 (OPE-93)。在遇到任何歧义时默认失败关闭。

    - list → 显式白名单（正常情况）。
    - "all" → 每一个已连接的连接器；仅限内置通用角色 —— 共享包声明它正是白名单为了防止的信任违规，因此第三方加载会拒绝它。
    - legacy `true`（预白名单清单）→ 清单已推荐的连接器引用（作者意图）；无推荐则无授权。
    - 推荐必须在授权范围内：员工无法使用的推荐是作者疏忽，在加载时暴露而不是在用户同意屏幕上暴露。

    [English] Parse the connector grant (OPE-93). Fail closed at every ambiguity.

    - list → explicit allowlist (the normal case).
    - "all" → every connected connector; reserved for BUILT-IN general personas — a
      shared bundle claiming it is exactly the trust violation the allowlist exists
      to prevent, so third-party loads reject it.
    - legacy `true` (pre-allowlist manifests) → the connector refs the manifest already
      recommends (author intent); no recommends → no grant.
    - recommends must stay within the grant: a recommendation the coworker can't use is
      author drift, surfaced at load rather than at the user's consent screen.
    """
    if raw is None or raw is False:
        declared: bool | tuple[str, ...] = False
    elif raw is True:
        refs = {r.ref for r in recommends if r.kind == "connector"}
        declared = tuple(sorted(refs)) if refs else False
    elif isinstance(raw, str):
        if raw.strip().lower() != "all":
            raise ManifestError(
                f"{persona_id}: `connectors` must be a list of connector ids or 'all'"
            )
        if not builtin:
            raise ManifestError(
                f"{persona_id}: `connectors: all` is reserved for built-in coworkers — "
                "declare the specific connectors this coworker uses"
            )
        declared = True
    elif isinstance(raw, list):
        declared = tuple(
            dict.fromkeys(s for s in (str(x).strip() for x in raw) if s)
        )
    else:
        raise ManifestError(
            f"{persona_id}: `connectors` must be a list of connector ids or 'all'"
        )

    if declared is not True:
        granted = set(declared or ())
        for r in recommends:
            if r.kind == "connector" and r.ref not in granted:
                raise ManifestError(
                    f"{persona_id}: recommends connector '{r.ref}' but does not declare "
                    "it in `connectors` — a recommendation must stay within the grant"
                )
    return declared


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """[中文] 从 Markdown 文本中分离出 YAML frontmatter 映射和正文内容。
    [English] Split YAML frontmatter mapping and body content from markdown text."""
    if not text.startswith("---"):
        raise ManifestError("manifest must start with a YAML frontmatter block (---)")
    end = text.find("\n---", 3)
    if end == -1:
        raise ManifestError("unterminated frontmatter block (missing closing ---)")
    raw = text[3:end]
    body = text[end + 4 :].lstrip("\n")
    try:
        meta = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:  # pragma: no cover - exercised via parse error path
        raise ManifestError(f"invalid YAML frontmatter: {e}") from e
    if not isinstance(meta, dict):
        raise ManifestError("frontmatter must be a mapping of key: value")
    return meta, body


def _slugify(stem: str) -> str:
    """[中文] 将文件名词干规范化为角色 ID 字符集（仅用于从文件名派生的 ID；显式 `id:` 必须已经有效）。
    [English] Normalize a filename stem into the persona-id charset (used only for ids derived
    from filenames; explicit `id:` values must already be valid)."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", stem.strip().lower()).strip("-_")[:64]
    return slug if _ID_RE.match(slug) else ""


def _strlist(meta: dict, key: str) -> list[str]:
    val = meta.get(key, [])
    if val is None:
        return []
    if isinstance(val, str):
        return [v.strip() for v in val.split(",") if v.strip()]
    if isinstance(val, list):
        return [str(v).strip() for v in val if str(v).strip()]
    raise ManifestError(f"`{key}` must be a list or comma-separated string")


def _models(persona_id: str, meta: dict) -> list[str]:
    """[中文] 解析 `models:`，包含旧名称 `recommended_models` 的别名兼容。
    [English] `models:` with the `recommended_models` alias (one release). Ids are kept verbatim
    and de-duplicated in order; a manifest naming both keys must agree."""
    new = _strlist(meta, "models")
    old = _strlist(meta, "recommended_models")
    if old and new and old != new:
        raise ManifestError(
            f"persona {persona_id!r}: `models` and `recommended_models` (deprecated) differ"
        )
    if old and not new:
        logging.getLogger(__name__).info(
            "persona %s uses `recommended_models` — rename it to `models`", persona_id
        )
    return list(dict.fromkeys(new or old))


def _recommends(persona_id: str, meta: dict) -> list[Recommendation]:
    raw = meta.get("recommends")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ManifestError(f"persona {persona_id!r}: `recommends` must be a list")
    out: list[Recommendation] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ManifestError(
                f"persona {persona_id!r}: each `recommends` item must be a mapping"
            )
        if "connector" in item:
            kind, ref = "connector", str(item.get("connector") or "").strip()
        elif "mcp" in item:
            kind, ref = "mcp", str(item.get("mcp") or "").strip()
        else:
            raise ManifestError(
                f"persona {persona_id!r}: each `recommends` item needs a `connector:` or `mcp:` key"
            )
        if not ref:
            raise ManifestError(
                f"persona {persona_id!r}: a `recommends` item has an empty {kind}"
            )
        tier = str(item.get("tier", "optional")).strip().lower()
        if tier not in VALID_REC_TIERS:
            raise ManifestError(
                f"persona {persona_id!r}: recommend tier must be one of {sorted(VALID_REC_TIERS)}"
            )
        out.append(
            Recommendation(
                kind=kind,
                ref=ref,
                reason=str(item.get("reason", "")).strip(),
                tier=tier,
            )
        )
    return out


def parse_manifest(
    text: str,
    *,
    fallback_id: Optional[str] = None,
    builtin: bool = False,
    source: Optional[str] = None,
) -> PersonaManifest:
    """[中文] 解析角色清单 Markdown 文本为 PersonaManifest 对象。
    [English] Parse persona manifest markdown text into a PersonaManifest object."""
    meta, body = _split_frontmatter(text)

    explicit_id = str(meta.get("id") or "").strip()
    if explicit_id:
        persona_id = explicit_id
        if not _ID_RE.match(persona_id):
            raise ManifestError(
                f"persona id {persona_id!r} is invalid: lowercase letters, digits, '-' or '_' "
                "only, starting with a letter/digit, max 64 chars (ids become directory names)"
            )
    else:
        # [中文] 从文件名派生：规范化为 ID 字符集而不是报错，因此无显式 ID 的 `My Persona.md` 仍能安装为 `my-persona`。
        # [English] Derived from the filename: normalize it into the id charset instead of erroring,
        # so `My Persona.md` without an explicit id still installs (as `my-persona`).
        persona_id = _slugify(str(fallback_id or ""))
        if not persona_id:
            raise ManifestError(
                "manifest needs an `id` (or a filename to derive one from)"
            )
    if not body.strip():
        raise ManifestError(f"persona {persona_id!r} has no body (the system prompt)")

    # [中文] 工作区/工具集特性 (workspace-scratch-design.md)。遗留兼容：旧 bundle 声明 `family: code|knowledge`，在缺少新键时将 `family: code` 映射到文件夹门禁模式。
    # [English] Workspace/toolset traits (workspace-scratch-design.md). Legacy shim: pre-trait
    # bundles declared `family: code|knowledge` (and a dead `workspace:` enum, ignored
    # here) — when the new keys are absent, `family: code` maps to the folder-gated
    # profile so an old bundle keeps its gate. New keys always win.
    legacy_family = str(meta.get("family", "")).strip().lower()
    if legacy_family and legacy_family not in VALID_FAMILIES:
        raise ManifestError(
            f"persona {persona_id!r}: family (legacy) must be one of {sorted(VALID_FAMILIES)}"
        )
    legacy_code = legacy_family == "code"
    requires_folder = bool(meta.get("requires_folder", legacy_code))
    subagents = bool(meta.get("subagents", legacy_code))
    # [中文] 文件夹型角色扇出到探索智能体而非计划任务 —— 默认配置遵循此划分。
    # [English] Folder personas fan out to explorers instead of scheduling — the silent default
    # mirrors that split; either can be declared explicitly.
    scheduling = bool(meta.get("scheduling", not requires_folder))

    mode = str(meta.get("default_permission_mode", "interactive")).strip().lower()
    if mode not in VALID_MODES:
        raise ManifestError(
            f"persona {persona_id!r}: default_permission_mode must be one of {sorted(VALID_MODES)}"
        )

    group = str(meta.get("group", "general") or "general").strip().lower()
    if group not in VALID_GROUPS:
        raise ManifestError(
            f"persona {persona_id!r}: group must be one of {sorted(VALID_GROUPS)}"
        )

    team_raw = str(meta.get("team", "") or "").strip().lower()
    if team_raw and team_raw not in VALID_TEAM:
        raise ManifestError(
            f"persona {persona_id!r}: team must be one of {sorted(VALID_TEAM)}"
            " (omit for a solo coworker)"
        )

    approval_guidance = meta.get("approval_guidance", "")
    if not isinstance(approval_guidance, str) or len(approval_guidance) > 2400:
        raise ManifestError("approval_guidance must be text of at most 2400 characters")

    tools = _strlist(meta, "tools")
    _validate_tools(persona_id, tools)
    recommends = _recommends(persona_id, meta)
    connectors = _connectors(persona_id, meta.get("connectors"), recommends, builtin)

    return PersonaManifest(
        id=persona_id,
        name=str(meta.get("name") or persona_id).strip(),
        system_prompt=body.strip(),
        icon=str(meta.get("icon", "")).strip(),
        tagline=str(meta.get("tagline", "")).strip(),
        description=str(meta.get("description", "")).strip(),
        tools=tools,
        requires_folder=requires_folder,
        subagents=subagents,
        scheduling=scheduling,
        messaging=bool(meta.get("messaging", False)),
        connectors=connectors,
        team=team_raw or None,
        approval_guidance=approval_guidance,
        default_permission_mode=mode,
        models=_models(persona_id, meta),
        skills=_strlist(meta, "skills"),
        mcp=_strlist(meta, "mcp"),
        version=str(meta.get("version", "") or "").strip(),
        recommends=recommends,
        ships=bool(meta.get("ships", True)),
        group=group,
        builtin=builtin,
        source=source,
    )


def _validate_tools(persona_id: str, tools: list[str]) -> None:
    # [中文] 在此处导入以避免模块加载循环（catalog 导入 agents.base）。
    # [English] Imported here to avoid a module-load cycle (catalog imports agents.base).
    from ..catalog import CATALOG

    unknown = [t for t in tools if t not in CATALOG]
    if unknown:
        raise ManifestError(
            f"persona {persona_id!r} references unknown tool capabilities: {unknown}. "
            f"Known: {sorted(CATALOG)}"
        )


def load_manifest_file(path: str | Path, *, builtin: bool = False) -> PersonaManifest:
    """[中文] 从文件路径加载并解析 PersonaManifest。
    [English] Load and parse PersonaManifest from a file path."""
    p = Path(path)
    return parse_manifest(
        p.read_text(encoding="utf-8"),
        fallback_id=p.stem,
        builtin=builtin,
        source=str(p),
    )
