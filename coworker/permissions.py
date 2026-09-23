"""[中文] 权限引擎 —— 针对提议的每次工具调用决定 允许（allow）/ 拒绝（deny）/ 询问用户（ask-user）。

运行模式：Plan（只读探索，计划模式）· Interactive（自动允许读，对写操作/命令询问用户，默认交互模式）·
Auto（自动允许，但仍受路径范围限制）。通过参数匹配模式（根目录限定路径、命令前缀）以及会话白名单进一步细化。
权限引擎仅负责*决策*；轮次引擎将 `needs_user` 决策路由到前端界面进行人工审批并记录结果。

Permission engine — decides allow / deny / ask-user for each proposed tool call.

Modes: Plan (read-only) · Interactive (auto reads, ask on writes/commands) · Auto
(allow, still path-scoped). Refined by argument patterns (path-under-root, command
prefixes) and a session allowlist. The engine only *decides*; the turn engine routes
`needs_user` decisions to a surface for approval and records the outcome.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

# [中文] 无法安全评估其“内容”的语法结构，包含这些结构的命令永远无法凭前缀规则自动放行：
# 命令/进程替换、重定向（可能写入白名单未审查的位置）以及变量展开（变量值在视线外设置）。
# Constructs whose *contents* we cannot evaluate, so a command carrying one is never
# eligible for prefix auto-run: command/process substitution, redirection (writes anywhere
# the allowlist never vetted), and variable expansion (the value was set out of view).
_OPAQUE_CONSTRUCTS = ("`", "$(", "$", ">", "<", "(")

# [中文] 将多个命令串联为单字符串的分隔符。各子命令将独立对照白名单进行检查 ——
# 旧逻辑直接拒绝整个复合命令，这不仅误拒了无害的 `git status && git diff`，而且（因为 `-exec` 无需分隔符）还会在 `find` 前缀下误放行 `find . -exec rm {} +`。
# Separators that chain several commands into one string. Each part is checked independently
# against the allowlist — the old behaviour rejected the whole command outright, which both
# refused harmless `git status && git diff` and (because `-exec` needs no separator) still
# auto-allowed `find . -exec rm {} +` under a `find` prefix.
_SEPARATORS = ("&&", "||", ";", "|&", "|", "&", "\n", "\r")

# [中文] 运行其参数中所命名的*另一个*程序的程序。外部程序的前缀规则绝不能为内部程序作担保，因此这些命令始终回退到人工审批。
# Programs that run *another* program named in their arguments. A prefix rule on the outer
# program can never vouch for the inner one, so these always fall through to approval.
_ARG_EXECUTORS = {
    "xargs", "env", "nohup", "nice", "stdbuf", "timeout", "watch", "sudo", "doas",
    "ssh", "docker", "podman", "kubectl", "npx", "pnpx", "bunx", "uvx",
}
# [中文] 携带内联执行代码的解释器，例如 `python -c "..."`、`node -e "..."`。
# Interpreters carrying inline code, e.g. `python -c "..."`, `node -e "..."`.
_INLINE_CODE_FLAGS = {"-c", "-e", "--eval", "--command", "-Command", "-EncodedCommand"}
_INTERPRETERS = {
    "sh", "bash", "zsh", "dash", "ksh", "fish", "powershell", "pwsh", "cmd",
    "python", "python3", "node", "deno", "bun", "ruby", "perl", "php",
}
# [中文] 将搜索/列举工具转变为执行或删除工具的高危参数标志。
# Flags that turn a search/list tool into an execution or deletion tool.
_DANGEROUS_FLAGS = {"-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprintf"}


def _split_commands(command: str) -> list[str]:
    """[中文] 按分隔符拆分复合命令。优先使用较长的分隔符拆分，避免将 `&&` 错误识别为两个 `&`。
    纯文本级拆分 —— 不考虑带引号的分隔符，这是有意设计的：过度拆分只会增加需要验证的子命令数量，绝不会减少。

    Split a compound command on its separators. Longest separators first so `&&` isn't
    read as two `&`. Purely textual — quoted separators are not respected, which is
    deliberate: over-splitting only ever produces MORE parts to justify, never fewer."""
    parts = [command]
    for sep in _SEPARATORS:
        parts = [chunk for part in parts for chunk in part.split(sep)]
    return [p.strip() for p in parts if p.strip()]


def _is_prefix_eligible(argv: list[str]) -> bool:
    """[中文] 若解析后的命令执行了前缀规则未审查的代码（例如参数中指定的另一程序、内联脚本源码或执行/删除高危标志），
    则无法被前缀规则担保，返回 False。

    False when a parsed command can never be vouched for by a prefix rule, because it
    runs code the rule never saw: another program named in its arguments, inline source, or
    an execution/deletion flag."""
    if not argv:
        return False
    program = Path(argv[0]).name.lower()
    program = program[:-4] if program.endswith(".exe") else program
    if program in _ARG_EXECUTORS:
        return False
    if program in _INTERPRETERS and any(a in _INLINE_CODE_FLAGS for a in argv[1:]):
        return False
    if any(a.lower() in _DANGEROUS_FLAGS for a in argv[1:]):
        return False
    return True


# [中文] 授予生命周期超出当前会话范围权限的工具：智能体将在后续对话中遵从的指令，或者在后续独立运行的定时任务 (OPE-117)。
# 审查器绝不能放行这些工具 —— 与延迟执行文件遵循相同的底线原则：其影响发生在此次授权对话结束之后，届时承担后果的人并不在场。
# `create_scheduled_task` 在自身注释中声明了契约（“人类通过批准此门控调用授予了权限”）；此处正是确保该契约成立。
#
# 包含 `update_` 是因为它能修改已被用户批准任务的指令与执行计划，同时保留其既有授权；
# 包含 `delete_` 是因为篡改用户亲自建立的常规配置属于同类危害（逆向破坏）。
# 缩小更新范围与扩大更新范围一并受底线保护：区分两者需要判定主观意图，而底线存在的初衷正是为了避免主观臆测。
#
# Tools granting authority that OUTLIVES this session: instructions the agent will follow
# in later conversations, or a task that runs on its own afterwards (OPE-117). The reviewer
# never clears these — the same floor as deferred-execution files, for the same reason: the
# effect lands after the conversation that authorised it has ended, so the person who bears
# it is not in the room. `create_scheduled_task` states the contract in its own comment
# ("the human granted them by approving this gated call"); this makes that true again.
#
# `update_` is included because it can rewrite the instructions and schedule of a task the
# user already approved while keeping its existing grants; `delete_` because tampering with
# standing configuration the user personally set up is the same class of harm, in reverse.
# Narrowing an update is floored along with broadening it: telling the two apart means
# judging intent, which is exactly what a floor exists to avoid.
PERSISTENT_AUTHORITY_TOOLS = {
    "save_skill",
    "create_scheduled_task",
    "update_scheduled_task",
    "delete_scheduled_task",
}


def protected_paths() -> list[Path]:
    """[中文] 管理权限系统自身的关键文件。智能体的一切操作均不得写入这些文件 —— 无论在何种模式下，通过何种工具。
    此处阻断的提权路径是：批准一条看似普通的命令，它悄悄追加到规则文件中，使得所有未来的会话都拥有更宽松的权限。
    这种攻击在默认的交互模式下即可能发生，因此绝不能将其仅仅作为沙箱或某一模式的特性；这是一条硬性底线。

    Files that govern the permission system itself. Nothing the agent does may write
    these — in any mode, through any tool. The escalation this blocks is: approve one
    ordinary-looking command, it quietly appends to the rule file, every future session is
    more permissive. That happens in the DEFAULT interactive mode, so this cannot be a
    property of a sandbox or of any one mode; it is a floor."""
    from .secrets import state_dir

    base = state_dir()
    return [
        base / "config.toml",
        base / "risk_overrides.json",
        base / "workspace_trust.json",
        base / "unattended.json",
        base / "coworker.db",  # session records carry the saved "always allow" grants
        base / "secrets.json",
        base / "inbox_routing.json",
    ]


# [中文] 工作区内部在后续看似无害的操作中会自动执行的文件。在此处编辑等同于延迟执行命令：
# 写入 `.git/hooks/pre-commit` 然后运行 `git commit` 便会触发其执行。
# 它们保持可写状态，但绝不能绕过人类 —— 任何自动批准路径都无法直接放行它们。
# Files INSIDE a workspace that execute on a later, innocuous-looking action. An edit here
# is a deferred command: writing `.git/hooks/pre-commit` and then running `git commit` runs
# it. They stay writable, but never WITHOUT a human — no auto-approve path may clear them.
_PROTECTED_IN_PROJECT = (
    ".git/hooks/",
    ".github/workflows/",
    ".gitlab-ci.yml",
    ".vscode/tasks.json",
    ".coworker/",  # workspace policy + skills the agent would otherwise self-grant
)


def _is_protected_in_project(candidate: Path) -> bool:
    posix = candidate.as_posix()
    return any(
        (f"/{marker}" in posix or posix.startswith(marker))
        if marker.endswith("/")
        else posix.endswith("/" + marker)
        for marker in _PROTECTED_IN_PROJECT
    )


def _host_of(url_or_domain: str) -> str:
    """[中文] URL 的小写主机名，或原样的纯域名。若无有效主机名则返回 `''`。同时支持 `https://docs.python.org/x` 与 `docs.python.org`。
    The lowercased host of a URL, or a bare domain as-is. `''` when there's nothing
    usable. Accepts both `https://docs.python.org/x` and `docs.python.org`."""
    s = (url_or_domain or "").strip().lower()
    if not s:
        return ""
    if "://" in s:
        return urlsplit(s).hostname or ""
    return urlsplit("//" + s).hostname or s


# [中文] 当写工具的目标路径是顶层单字段时，指明该参数字段名。补丁/差异工具的路径包含在数据块内部 —— 在 `write_paths` 中提取。
# The argument that names a write tool's target path, when it's a single top-level field.
# Patch/diff tools carry their paths inside the blob instead — extracted in `write_paths`.
_PATH_ARG: dict[str, str] = {"write_file": "path", "replace_in_file": "path"}
# apply_patch (Codex format) file headers, and unified-diff `+++ b/<path>` headers.
_APPLY_PATCH_FILE = re.compile(
    r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE
)
_APPLY_PATCH_MOVE = re.compile(r"^\*\*\* Move to: (.+)$", re.MULTILINE)
_UNIFIED_DIFF_FILE = re.compile(r"^\+\+\+ (?:b/)?(.+?)\s*$", re.MULTILINE)


def write_paths(tool_name: str, arguments: dict[str, Any]) -> tuple[list[str], bool]:
    """[中文] 写工具拟触及的每个文件系统路径，用于根目录范围限定检查。

    返回 ``(paths, located)``。当无法确定路径时（未知的写工具，或不含可解析文件头的补丁/差异块），``located`` 为 False ——
    调用方此时必须遵循“默认关闭 / fail closed”原则直接转入人工审批，而不是跳过范围检查，防止未界定范围的写操作在 auto/custom 模式下悄悄放行。

    Every filesystem path a write tool would touch, for root scoping.

    Returns ``(paths, located)``. ``located`` is False when the path can't be determined
    (an unknown write tool, or a patch/diff blob with no parseable file header) — the caller
    must then fail closed rather than skip scoping, so an unscoped write can't slip through
    auto/custom mode.
    """
    arg = _PATH_ARG.get(tool_name)
    if arg is not None:
        value = arguments.get(arg)
        return ([str(value)], True) if value else ([], False)
    if tool_name == "apply_patch":
        blob = str(arguments.get("patch", ""))
        paths = _APPLY_PATCH_FILE.findall(blob) + _APPLY_PATCH_MOVE.findall(blob)
        return ([p.strip() for p in paths], bool(paths))
    if tool_name == "apply_unified_diff":
        blob = str(arguments.get("diff", ""))
        paths = [p for p in _UNIFIED_DIFF_FILE.findall(blob) if p and p != "/dev/null"]
        return (paths, bool(paths))
    # Unknown write tool (e.g. one promoted to write via a user override): we cannot locate
    # its path, so it cannot be auto-scoped.
    return ([], False)

from .risk import (  # re-exported for back-compat (manager.py imports WRITE_TOOLS)
    SHELL_TOOL,
    WRITE_TOOLS,
    RiskClass,
    RiskOverrides,
    classify,
    is_consequential,
)


# [中文] 对话记录中关于自动批准（Auto-Approve）的完整说明（所有者文案 2026-08-24）。
# 在会话首次进入 Auto-Approve 时作为 `mode_notice` 消息持久化存储 —— 由服务端生成，确保其恰好出现一次并能跨重启保留。
# The transcript's full Auto-Approve explainer (owner copy 2026-08-24). Persisted as a
# `mode_notice` message the FIRST time a session enters Auto-Approve — server-authored so
# it appears exactly once, in place, and survives reloads (the old client-side banner
# re-announced on every restart).
AUTO_APPROVE_NOTICE = (
    "Auto-approve uses a model to let routine actions through without asking; anything "
    "it isn't sure about still comes to you. It cuts interruptions but still carries "
    "some risk i.e. a command it allows still reaches anything you can. These are model "
    "judgments, and not guarantees."
)

# [中文] 单行模式切换标记的人类可读标签（例如 “Ask for approval is on.”）。
# Human labels for the one-line persisted switch markers ("Ask for approval is on.").
MODE_LABELS = {
    "discuss": "Discuss",
    "plan": "Plan",
    "interactive": "Ask for approval",
    "auto": "Bypass approvals",
    "bypass-approvals": "Bypass approvals",
    "auto-approve": "Auto-approve",
}


class Mode(str, Enum):
    # [中文] DISCUSS：纯只读对话，无任何文件编辑，无计划工作流
    DISCUSS = "discuss"  # read-only conversation: no edits, no planning workflow
    # [中文] PLAN：只读 + 计划契约（探索 → propose_plan 提议计划 → 获批后执行）
    PLAN = (
        "plan"  # read-only + the planning contract (explore → propose_plan → execute)
    )
    # [中文] INTERACTIVE：交互模式，询问用户审批（默认模式）
    INTERACTIVE = "interactive"  # ask for approval (default)
    # [中文] BYPASS_APPROVALS：跳过常规审批，拥有完全权限（硬性安全底线依然生效）
    # Renamed from "auto" (spec §1.5, 2026-08-12): "bypass" names the action — switching a
    # safety system off — and can't be confused with AUTO_APPROVE in a picker. Deliberately
    # NOT "bypass-ALL-approvals": Phase 1's floors (settings files, out-of-root writes,
    # `.git/hooks`) still hold in this mode, so "all" would be a false promise.
    BYPASS_APPROVALS = "bypass-approvals"  # full access (minus the hard floors)
    # [中文] AUTO_APPROVE：智能体审查器自动审批模式（大模型预审查，明确允许的放行，存疑的交由人类）
    # Interactive, but an LLM reviewer judges each would-be approval card first: clear
    # allows run without a prompt, everything else still reaches the human. The reviewer
    # can only turn "ask" into "allow", never "blocked" into "allow" (spec §1.2). With no
    # reviewer plugged into the engine this mode behaves exactly like INTERACTIVE.
    AUTO_APPROVE = "auto-approve"
    # [中文] CUSTOM：自定义模式（交互模式 + 自动放行配置中指定的 `auto_allow` 工具）
    CUSTOM = "custom"  # interactive + auto-allow the config's `auto_allow` tools

    @classmethod
    def _missing_(cls, value: object) -> "Mode | None":
        # Legacy spelling from configs, saved sessions, and older UIs.
        if value == "auto":
            return cls.BYPASS_APPROVALS
        return None


# [中文] 强制只读的模式集合。DISCUSS 与 PLAN 共享相同的安全门控；区别仅在于意图 —— PLAN 额外引导智能体生成计划审批。
# Modes whose enforcement is read-only. DISCUSS and PLAN share the same gate; they differ
# only in intent — PLAN additionally drives the agent toward a propose_plan approval.
READ_ONLY_MODES = frozenset({Mode.DISCUSS, Mode.PLAN})


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    needs_user: bool = False  # True → surface should prompt the user for approval
    # [中文] True → 此请求必须由“人类”审批：Auto-Approve 审查器不得介入且无法放行它。
    # 适用于核心目的就是让人类看到的操作 —— 项目内受保护的延迟执行文件（git hooks、CI 配置）以及无法定位目标路径的写操作。
    # True → this ask is reserved for a HUMAN: the Auto-Approve reviewer must not be
    # consulted and cannot clear it. Set on decisions whose entire point is that a person
    # sees them — protected in-project files that execute later (git hooks, CI configs:
    # "never WITHOUT a human — no auto-approve path may clear them") and writes whose path
    # could not be located for scoping (an allow would bypass root scoping unverified).
    human_only: bool = False
    # [中文] 当任务级常规规则允许了调用（"tool → target"）时设置，以便引擎审计具体规则且工具卡片可以声明 (§25)。
    # Set when a task-scoped standing rule allowed the call ("tool → target") so the
    # engine can audit the exact rule and the tool card can say so (§25).
    rule: str = ""


def standing_rule_candidate(
    tool_name: str,
    arguments: dict[str, Any],
    metadata: Any = None,
    overrides: Optional[RiskOverrides] = None,
) -> Optional[str]:
    """[中文] 当且仅当本次调用符合任务级常规规则资格时返回目标值 (UX-DECISIONS §25)：
    仅限外部风险操作（绝不适用于 exec/write-local —— shell 永远需要询问），工具必须声明目标参数，且调用中必须实际指定了目标。
    否则返回 None —— 不符合条件的调用保持弹出审批卡片。

    The target value iff this call is eligible for a task-scoped standing rule
    (UX-DECISIONS §25): external-risk only (never exec/write-local — shell asks forever),
    the tool must declare a target argument, and the call must actually name a target.
    Returns None otherwise — ineligible calls keep parking approvals as today."""
    from .connectors.tool_defs import standing_target_for

    if classify(tool_name, metadata, overrides) is not RiskClass.EXTERNAL:
        return None
    return standing_target_for(tool_name, arguments or {})


@dataclass
class PermissionEngine:
    workspace_root: Path
    mode: Mode = Mode.INTERACTIVE
    allowed_commands: list[str] = field(default_factory=list)
    auto_allow_tools: set[str] = field(default_factory=set)
    session_allow_tools: set[str] = field(default_factory=set)
    session_allow_commands: set[str] = field(default_factory=set)
    # [中文] OPE-136 运行级授权（“本次请求允许 / Allow for this request”）：仅在“当前运行剩余阶段”覆盖的工具名称。
    # 设计为纯内存保存 —— 当运行完成或中断时引擎清空该集合；进程重启结束运行使得集合重置为空是正确的行为，而非损失。
    # 仅为 EXTERNAL（外部风险）工具生成（在 manager._grant_offered 中服务端校验）；
    # 与会话级授权不同，此类授权专为连接器与 MCP（循环/重试/分页调用）场景而设计。
    # OPE-136 run grants ("Allow for this request"): tool names covered for the
    # REMAINDER OF THE CURRENT RUN only. In-memory by design — the engine clears the
    # set when the run finishes or is interrupted, and a process restart ending the
    # run makes the empty set correct, not a loss. Minted only for EXTERNAL-risk
    # tools (server-validated in manager._grant_offered); unlike the session grant
    # this one exists FOR connectors and MCP — the loop/retry/pagination shapes.
    run_allow_tools: set[str] = field(default_factory=set)
    # [中文] 免提示自动运行的出站域名：用户配置中的 `allowed_domains` 加上通过“始终允许此域名”生成的 `session_allow_domains`。
    # 按精确主机名或子域后缀进行匹配（见 `_domain_allowed`）。
    # Egress domains that auto-run without a prompt: `allowed_domains` from user config, plus
    # `session_allow_domains` minted by "Always allow this domain". Matched by exact host or
    # subdomain suffix (see `_domain_allowed`).
    allowed_domains: list[str] = field(default_factory=list)
    session_allow_domains: set[str] = field(default_factory=set)
    # [中文] 全会话只读授权（所有者要求 2026-08-11）：自动放行保守分类器（coworker/readonly.py）接受的 shell 命令。由用户在每个会话中主动选择。
    # Session-wide read-only grant (owner ask 2026-08-11): auto-allow shell commands the
    # conservative classifier (coworker/readonly.py) accepts. User-elected per session.
    session_readonly: bool = False
    # [中文] 任务级常规规则 (§25)：{tool: {allowed targets}}，从所属 ScheduledTask 的目标配置中初始化。
    # 按引用保留并在每次检查时重新读取，因此运行中途生成的规则（“每次均允许”）立即对运行中的下一次调用生效。
    # Task-scoped standing rules (§25): {tool: {allowed targets}}, seeded from the owning
    # ScheduledTask's target-shaped entries. Kept by reference and re-read every check, so a
    # rule minted mid-run ("Allow every time") applies to the run's next call too.
    task_rules: dict[str, set[str]] = field(default_factory=dict)
    # [中文] 话题授权（规范 §11.4）：当前会话可以无需询问直接答复的来源话题（Threads）。
    # 每个条目会展开为平台回复工具的任务规则（管理器的 `_grant_thread_rules`）；与会话授权一起持久化，在重新构建时重新应用。
    # Thread grants (spec §11.4): the origin threads this session may answer without
    # asking. Each entry expands into task_rules for the platform's reply tools (the
    # manager's `_grant_thread_rules`); persisted with the session's grants and
    # re-applied on rebuild, so a subscribed session tagged in a thread keeps its
    # grant across restarts (mention-spawned sessions also re-derive from the thread map).
    thread_grants: set[str] = field(default_factory=set)
    # [中文] 用户本地风险覆盖解析器（第 2 阶段）。None → 使用基础分类。
    # User-local risk override resolver (Phase 2). None → use the base classification.
    risk_overrides: Optional[RiskOverrides] = None
    # [中文] OPE-136 持久信任规则：工具名称 → 用户是否建立了长期的“不要询问”规则？(RiskOverrideStore.trusted)。
    # 仅免除审批卡片，仅在 AUTO_APPROVE 之外生效 —— 绝不改变风险分类、模式门控或审计日志。None → 无信任规则。
    # OPE-136 durable trust: tool name → has the user minted a standing "don't ask" rule?
    # (RiskOverrideStore.trusted). Waives only the card, only outside AUTO_APPROVE —
    # never the class, the mode gates, or the audit trail. None → no trust rules.
    trust_overrides: Optional[Callable[[str], bool]] = None
    # [中文] 写入端（RiskOverrideStore.set_trust）—— ApprovalOutcome.ALWAYS_TRUST 如何落盘。
    # 作为注入的可调用对象保留，使本模块绝不直接导入 store 模块。
    # The write half (RiskOverrideStore.set_trust) — how ApprovalOutcome.ALWAYS_TRUST
    # lands on disk. Kept as an injected callable so this module never imports the store.
    grant_trust: Optional[Callable[[str], None]] = None
    # [中文] 共享的、可能变动的根目录列表（类似 RootDir 对象或字典）。缺省时以单个 `workspace_root` 作为唯一可写根目录（向后兼容）。
    # 按引用保留并在每次检查时重新读取，因此在运行时添加/移除文件夹立即可生效而无需重新构建引擎。
    # Shared, possibly-mutable list of roots (RootDir-like / dicts). When omitted, the single
    # `workspace_root` is the sole writable root (back-compat). Kept by reference and re-read on
    # every check, so runtime add/remove of folders takes effect without rebuilding the engine.
    roots: Optional[list] = None

    def __post_init__(self) -> None:
        self.workspace_root = Path(self.workspace_root).expanduser().resolve()
        self.auto_allow_tools = set(self.auto_allow_tools)
        if self.roots is None:
            self.roots = [{"path": self.workspace_root, "writable": True}]

    def _resolved_roots(self) -> list[tuple[Path, bool]]:
        out: list[tuple[Path, bool]] = []
        for r in self.roots or []:
            if isinstance(r, dict):
                p, w = r["path"], bool(r.get("writable", False))
            elif isinstance(r, (str, Path)):
                p, w = r, True
            else:  # duck-typed RootDir-like
                p, w = getattr(r, "path"), bool(getattr(r, "writable", False))
            out.append((Path(p).expanduser().resolve(), w))
        return out

    def evaluate(
        self, tool_name: str, arguments: dict[str, Any], metadata: Any = None
    ) -> Decision:
        arguments = arguments or {}
        is_connector = getattr(metadata, "category", "") == "connector"
        risk = classify(tool_name, metadata, self.risk_overrides)
        is_write = risk is RiskClass.WRITE_LOCAL
        is_shell = risk is RiskClass.EXEC
        is_egress = risk is RiskClass.EGRESS
        # [中文] 持久授权工具因其名称而具有重大影响（consequential）：其风险分类可能读作 READ（基础表中无记录），
        # 但授予长期有效权限属于副作用 —— 只读模式必须“拒绝”它们，而非弹出授权卡片。
        # Persistent-authority tools are consequential BY NAME: their risk class can
        # read as READ (no base-table/catalog entry), but granting standing authority is
        # a side effect — read-only modes must DENY them, not offer a grant card. The
        # OPE-117 comment below always promised "read-only modes still hard-deny above
        # this"; the OPE-136 gate-order pin caught that the class-based check alone
        # didn't deliver it (save_skill in Discuss reached the human-only card).
        consequential = (
            is_consequential(risk) or tool_name in PERSISTENT_AUTHORITY_TOOLS
        )

        # [中文] 自我保护安全底线 —— 先于运行模式、白名单和所有自动放行路径运行，因为其要阻止的提权攻击正是在默认模式下发生。
        # 以下任何判定均无法触及这些文件，且流程中人类的任何点击也无法授予该权限：放宽限制必须通过带外方式直接编辑文件。
        # SELF-PROTECTION FLOOR — runs before mode, allowlists and every auto-approve path,
        # because the escalation it blocks happens in the DEFAULT mode. No verdict below can
        # reach these files, and no human click in the flow can grant it either: loosening
        # requires editing the files out-of-band.
        if is_write or is_shell:
            hit = self._touches_protected(tool_name, arguments, is_shell)
            if hit is not None:
                return Decision(
                    False,
                    f"refusing to modify OpenWorker's own settings: {hit}",
                    needs_user=False,
                )

        # [中文] Discuss / plan 模式：强制只读。
        # Discuss / plan modes: read-only.
        if self.mode in READ_ONLY_MODES and consequential:
            return Decision(
                False, f"{self.mode.value} mode is read-only", needs_user=False
            )

        # [中文] 写操作的路径范围限定（适用于所有模式）：写操作触及的每个路径均必须落在可写根目录内。
        # 无法定位目标路径的写操作无法进行范围检查，因此执行“默认关闭 / fail closed”转向人工审批，而不是未经范围检查直接在 auto/custom 模式下放行。
        # Path scoping for writes (all modes): every path the write touches must land in a
        # writable root. A write whose path can't be located is not scoped-able, so it fails
        # closed to approval rather than slipping through auto/custom unscoped.
        needs_human_for_protected = False
        if is_write:
            paths, located = write_paths(tool_name, arguments)
            if not located:
                return Decision(
                    False,
                    "cannot determine the write path to scope",
                    needs_user=True,
                    human_only=True,  # an unscopable write must reach a person, not the reviewer
                )
            for path in paths:
                if not self._under_writable_root(path):
                    return Decision(
                        False, f"path is not in a writable directory: {path}"
                    )
                # [中文] 项目内部在后续操作中执行的文件（git hooks、CI 配置）允许编辑，但绝不能经由自动审批路径放行 —— 必须由人类亲自审核。
                # In-project files that run on a later action (git hooks, CI configs) may be
                # edited, but never by an auto-approve path — a human must see it.
                if _is_protected_in_project(self._candidate(path)):
                    needs_human_for_protected = True

        # [中文] 超出会话生命周期的授权必须升级至人类，超越审查器和以下的所有白名单 (OPE-117)。
        # 特意置于非重大影响检查之前：这些工具在当前虽然属于重大影响，但必须防止元数据遗漏导致关闭此底线。只读模式在上方已硬性拒绝。
        # Authority outliving the session reaches a person, over the reviewer and over
        # every allowlist below (OPE-117). Placed ahead of the non-consequential return on
        # purpose: these tools are consequential today, but a metadata slip must not be
        # able to switch the floor off. Read-only modes still hard-deny above this.
        if tool_name in PERSISTENT_AUTHORITY_TOOLS:
            return Decision(
                False,
                "this outlives the session — approval required",
                needs_user=True,
                human_only=True,
            )

        # [中文] 无重大影响（低风险）工具总是直接允许运行。
        # Non-consequential tools always run.
        if not consequential:
            return Decision(True, "low risk")

        # [中文] 项目内受保护的目标（git hooks、CI 配置）跳过以下所有自动放行路径 —— 包括 auto 模式与会话/配置白名单 —— 并强制向人类提问。
        # A protected in-project target (git hooks, CI config) skips every auto-approve path
        # below — including auto mode and the session/config allowlists — and asks.
        if needs_human_for_protected:
            return Decision(
                False,
                "this file runs automatically later — approval required",
                needs_user=True,
                human_only=True,  # deferred-execution files: a human sees every one (§ floor)
            )

        # [中文] 完全访问模式（Bypass）。
        # Full access.
        if self.mode is Mode.BYPASS_APPROVALS:
            return Decision(True, "full access")

        # [中文] 交互 / 自定义 / 自动审批模式：各类白名单校验。
        # 在 AUTO_APPROVE 模式下，会话临时授权（“始终允许此...”点击）特意*不*直接自动放行 (规范 §1.5)：
        # 带外设置的长期策略 —— 通过 `_command_allowed` / 配置项 `allowed_domains` 检查的用户设置白名单 —— 可以跳过审查器，
        # 但在对话流程中的临时点击不行。域名授权仅按主机名匹配，对可能潜藏泄露数据的路径和查询字符串不可知；命令授权则是按完全文本回放；
        # 这两类均正是审查器应当仔细甄别的内容。跳过的检查将返回 `needs_user`，从而路由给审查器。
        # interactive / custom / auto-approve: allowlists.
        #
        # In AUTO_APPROVE, session grants ("always allow this …" clicks) deliberately do
        # NOT auto-allow (spec §1.5): out-of-band standing policy — the user-settings
        # allowlists checked via `_command_allowed` / config `allowed_domains` — may skip
        # the judge, but an in-flow click may not. A domain grant matches on host only and
        # is blind to the path and query string (where exfiltration rides), and command
        # grants replay as exact text; both are precisely what the reviewer should see.
        # The skipped checks return `needs_user` instead, which routes to the reviewer.
        honor_session_grants = self.mode is not Mode.AUTO_APPROVE
        if is_shell:
            command = str(arguments.get("command", ""))
            if self._command_allowed(command):
                return Decision(True, "command on allowlist")
            if (
                honor_session_grants
                and command
                and command in self.session_allow_commands
            ):
                return Decision(True, "command allowed for session")
            # [中文] 同样属于会话授权，因此适用 §1.5：在 Auto-Approve 模式下由审查器裁决，而非由分类器直接放行。
            # Also a session grant, so §1.5 applies: in Auto-Approve the reviewer judges
            # these rather than the classifier waving them through.
            if honor_session_grants and self.session_readonly and command:
                from .readonly import is_readonly_command, read_targets

                # [中文] 分类器审查命令“做什么”；根目录集审查命令“读什么” (OPE-130)。
                # 若无后半部分检查，用户理解为“别再为我的项目文件弹窗”的授权，可能会涵盖 ~/.aws/credentials、其他代码仓库的历史以及 OpenWorker 自身的密钥文件 —— 自我保护底线无法拦截这些读取，因为底线防范的是写而非读。
                # The classifier vets what a command DOES; the roots vet what it READS
                # (OPE-130). Without the second half, a grant the user reads as "stop
                # asking about my project files" also covers ~/.aws/credentials, another
                # repo's history, and OpenWorker's own secrets file — none of which the
                # self-protection floor catches, since that guards writes, not reads.
                if is_readonly_command(command) and all(
                    self._under_root(t) for t in read_targets(command)
                ):
                    return Decision(True, "read-only command (session grant)")
        if is_egress:
            url = str(arguments.get("url", ""))
            if self._domain_allowed(url, include_session=honor_session_grants):
                return Decision(True, "domain on allowlist")
        if (
            honor_session_grants
            and tool_name in self.session_allow_tools
            and not is_connector
        ):
            return Decision(True, "tool allowed for session")
        # [中文] 运行级授权（OPE-136 “本次请求允许”）：相同的检查点，但生命周期更短 —— 并且不排除连接器，因为 EXTERNAL 正是其存在的原因。
        # Run grant (OPE-136 "Allow for this request"): same checkpoint, shorter life —
        # and no connector exclusion, because EXTERNAL is exactly who it exists for.
        # §1.5 still applies: an in-flow click never skips the Auto-Approve judge.
        if honor_session_grants and tool_name in self.run_allow_tools:
            return Decision(True, "tool allowed for this request")

        # [中文] OPE-136：MCP 信任规则仅在审批卡片起决定作用的模式下免除卡片。
        # 两个来源，一个分支：用户从卡片创建的单工具信任规则（“始终允许此工具” → risk_overrides.json），
        # 或者是传统的服务级 `requires_approval: false`（不再改变分类 —— risk.classify 中的 MCP 底线保持这些工具为 EXTERNAL）。
        # 上述所有底线依然有效：只读模式在此行前已被拒绝，持久授权与受保护文件底线在此行前已返回，Bypass 模式也已返回。
        # 在 AUTO_APPROVE 中特意不予免除：v1 保持 §1.5 的保守原则 —— 审查器负责裁决受信任的 MCP 调用（回退到 needs_user 路由到审查器）；
        # 只有手工编写的配置白名单能够跳过审查器。
        # OPE-136: MCP trust waives only the card, in the one mode where the card is the
        # deciding voice. Two sources, one branch: a per-tool trust RULE the user minted
        # from the card ("Always allow this tool" → risk_overrides.json), or the legacy
        # server-level `requires_approval: false` (which no longer reclassifies — the MCP
        # floor in risk.classify keeps these tools EXTERNAL). Everything above still
        # applied: read-only modes denied before this line, the persistent-authority and
        # protected-file floors returned before it, and Bypass already returned.
        # Deliberately NOT honored in AUTO_APPROVE: v1 keeps §1.5 conservative — the
        # reviewer judges trusted MCP calls (falling through to needs_user routes
        # there); only hand-authored config allowlists skip the judge.
        if (
            getattr(metadata, "category", "") == "mcp"
            and self.mode is not Mode.AUTO_APPROVE
        ):
            if self.trust_overrides is not None and self.trust_overrides(tool_name):
                return Decision(True, "trusted MCP tool (user trust rule)")
            if not bool(getattr(metadata, "requires_approval", True)):
                return Decision(True, "trusted MCP tool (server marked don't-ask)")

        # [中文] 任务级常规规则 (§25)：工具 + 精确目标，属于自动化流程。
        # 特意不受上述连接器排除限制 —— 精确目标绑定正是让自动放行连接器工具安全的原因。
        # 绝不适用于执行风险（候选提取仅限外部风险），并且是在模式之上的叠加：只读模式在此点之前已返回。
        # Task-scoped standing rules (§25): tool + exact target, owned by the automation.
        # Deliberately NOT subject to the connector exclusion above — the exact-target
        # binding is what makes auto-allowing a connector tool safe. Never for exec risk
        # (candidate extraction is external-risk-only), and additive on top of the mode:
        # read-only modes already returned before this point.
        if tool_name in self.task_rules:
            target = standing_rule_candidate(
                tool_name, arguments, metadata, self.risk_overrides
            )
            if target and target in self.task_rules[tool_name]:
                rule = f"{tool_name} → {target}"
                return Decision(True, f"allowed by standing rule: {rule}", rule=rule)

        # [中文] 自定义模式（Custom）：自动批准配置文件中指定的工具。
        # Custom mode auto-approves the configured tools.
        if self.mode is Mode.CUSTOM and tool_name in self.auto_allow_tools:
            return Decision(True, "auto-allowed by config")

        # [中文] 其他情况：需要用户审批。
        # Otherwise: ask the user.
        return Decision(False, "requires approval", needs_user=True)

    # -- session memory ---------------------------------------------------------
    def allow_tool_for_session(self, tool_name: str) -> None:
        self.session_allow_tools.add(tool_name)

    def allow_tool_for_run(self, tool_name: str) -> None:
        self.run_allow_tools.add(tool_name)

    def clear_run_allowances(self) -> None:
        """[中文] 运行边界即为授权的到期时刻：当某次运行完成或被中断时引擎调用此方法，使得“本次请求允许”绝不会超出用户当时正在关注的答复轮次。
        The run boundary IS the grant's expiry: the engine calls this when a run
        finishes or is interrupted, so "Allow for this request" never outlives the
        answer the user was watching."""
        self.run_allow_tools.clear()

    def grant_trust_for_tool(self, tool_name: str) -> None:
        """[中文] OPE-136 持久信任：持久化单工具“不要询问”规则（可跨会话存续）。
        当未连接存储时（测试中的临时引擎）回退到会话级授权 —— 卡片的承诺降级为会话范围而非直接失效。

        OPE-136 durable trust: persist a per-tool "don't ask" rule (survives sessions).
        Falls back to the session grant when no store is wired (ephemeral engines in
        tests) — the card's promise degrades to session scope rather than to nothing."""
        if self.grant_trust is not None:
            self.grant_trust(tool_name)
        else:
            self.session_allow_tools.add(tool_name)

    def allow_command_for_session(self, command: str) -> None:
        if command:
            self.session_allow_commands.add(command)

    def allow_readonly_for_session(self) -> None:
        self.session_readonly = True

    def allow_domain_for_session(self, url_or_domain: str) -> None:
        """[中文] 为当前会话记录出站目标（“始终允许此域名”）。
        生成时去除前导 `www.` (§1.9)：在每个用户的心智模型中 `bbc.com` 和 `www.bbc.com` 属于同一站点，
        且 `_domain_allowed` 中的后缀匹配已将 `www.bbc.com` 视为 `bbc.com` 的子域。
        仅进行纯字面拼写处理 —— 绝不执行 eTLD+1 或任何更宽泛的规范化，否则会暗中扩大授权范围。

        Remember an egress destination for this session ("Always allow this domain").

        A leading `www.` is stripped at minting (§1.9): `bbc.com` and `www.bbc.com` are one
        site in every user's mental model, and the suffix match in `_domain_allowed` already
        treats `www.bbc.com` as a subdomain of `bbc.com`. Pure spelling only — never eTLD+1
        or any broader normalisation, which would silently widen the grant."""
        host = _host_of(url_or_domain)
        if host.startswith("www."):
            host = host[4:]
        if host:
            self.session_allow_domains.add(host)

    # -- helpers ----------------------------------------------------------------
    def _candidate(self, path: str) -> Path:
        # Relative paths resolve against the primary (workspace_root); absolute/`~` taken as-is.
        p = Path(path).expanduser()
        return p.resolve() if p.is_absolute() else (self.workspace_root / p).resolve()

    def _under_root(self, path: str) -> bool:
        candidate = self._candidate(path)
        for rp, _ in self._resolved_roots():
            try:
                candidate.relative_to(rp)
                return True
            except ValueError:
                continue
        return False

    def _under_writable_root(self, path: str) -> bool:
        candidate = self._candidate(path)
        for rp, writable in self._resolved_roots():
            if not writable:
                continue
            try:
                candidate.relative_to(rp)
                return True
            except ValueError:
                continue
        return False

    def _touches_protected(
        self, tool_name: str, arguments: dict[str, Any], is_shell: bool
    ) -> Optional[str]:
        """[中文] 本次调用拟修改的受保护配置路径，若无则返回 None。

        对于写操作，我们解析出真实目标路径。对于 shell，我们只能检查命令文本 —— 解析器深度，
        因此它防范意外与随意的尝试，而非蓄意的对抗（后者需要 OS 操作系统沙箱）。但无论如何成本很低且值得具备。

        Shell 匹配仅针对“全路径”，绝不仅是纯文件名：在命令中匹配任意 `secrets.json` 会拒绝仅仅提及该名称的无关工作。
        只要命令指名了真实的配置文件路径，无论读还是写一律拒绝 —— 仅凭文本无法分辨二者，对这些文件采取保守策略是正确的。

        The protected settings path this call would modify, or None.

        For writes we resolve the real target. For shell we can only inspect the command
        text — parser depth, so it stops accidents and casual attempts, not a determined
        adversary (that needs the OS sandbox). Cheap and worth having regardless.

        Shell matching is on the FULL path only, never a bare filename: matching
        `secrets.json` anywhere in a command would refuse unrelated work that merely
        mentions the name. A command naming the real settings path is refused whether it
        reads or writes — we cannot tell which from text, and the conservative direction is
        the right one for these files.
        """
        targets = [str(p) for p in protected_paths()]
        if is_shell:
            command = str(arguments.get("command", ""))
            if not command:
                return None
            lowered = command.replace("\\", "/").lower()
            for target in targets:
                if target.replace("\\", "/").lower() in lowered:
                    return target
            return None
        paths, located = write_paths(tool_name, arguments)
        if not located:
            return None  # unlocatable writes are already failed closed by the caller
        resolved = {str(self._candidate(p)) for p in paths}
        for target in targets:
            if str(Path(target).resolve()) in resolved:
                return target
        return None

    def _domain_allowed(self, url: str, *, include_session: bool = True) -> bool:
        """[中文] 当 URL 的主机名属于允许的出站目标时为 True —— 精确匹配或是允许域名的子域名
        （例如 `docs.python.org` 匹配 `python.org`，但 `evil-python.org` 绝不匹配 `python.org`）。

        `include_session=False` (AUTO_APPROVE 模式) 仅检查用户设置列表：会话中途点击的“始终允许此域名”在审查器面前不生效。

        True when the URL's host is an allowed egress destination — an exact match or a
        subdomain of an allowed domain (so `docs.python.org` matches `python.org`, but
        `evil-python.org` never matches `python.org`).

        `include_session=False` (AUTO_APPROVE mode) checks the user-settings list only:
        mid-session "always allow this domain" clicks don't bypass the reviewer there."""
        host = _host_of(url)
        if not host:
            return False
        allowed = {d for d in (_host_of(x) for x in self.allowed_domains) if d}
        if include_session:
            allowed |= self.session_allow_domains
        for dom in allowed:
            if host == dom or host.endswith("." + dom):
                return True
        return False

    def _command_allowed(self, command: str) -> bool:
        """[中文] 仅当（可能复合的）命令的“每一部分”均独立被白名单项覆盖时才为 True。

        白名单项无需审批直接自动运行，而前缀规则仅能为其匹配到的词汇作担保 —— 其后的一切内容均未受审查。
        因此本方法执行两项工作：确保未受审查的尾部只能是普通参数，然后匹配命令头部。

        - 无法安全评估其内容的语法结构（命令替换、重定向、变量展开）将直接取消整个命令的免审资格。
        - 复合命令会被拆分并分别独立检查其各部分，因此 `git status && git diff` 在两者均被允许时可运行，
          而 `git status && rm -rf ~` 则不行。
        - 执行其参数中命名代码的部分（`xargs`、`sh -c`、`find -exec`、`-delete`）永远不具备前缀免审资格：
          `find` 规则绝不能自动放行 `find . -exec rm {} +`。
        - 匹配基于解析后的单词分词而非纯文本，因此 `git status` 覆盖 `git status -s`，
          但绝不覆盖 `git statusfoo` 或纯 `git`。

        True only when EVERY part of a (possibly compound) command is independently
        covered by an allowlist entry.

        An allowlist entry auto-runs without approval, and a prefix rule can only vouch for
        the words it matched — everything after is unexamined. So this does two jobs:
        guarantee the unexamined tail can only be arguments, then match the beginning.

        - Constructs whose contents we can't evaluate (substitution, redirection, variable
          expansion) disqualify the whole command.
        - Compound commands are split and each part checked on its own, so
          `git status && git diff` runs when both are allowed, while
          `git status && rm -rf ~` does not.
        - Parts that run code named in their arguments (`xargs`, `sh -c`, `find -exec`,
          `-delete`) are never prefix-eligible: a `find` rule must not auto-run
          `find . -exec rm {} +`.
        - Matching is on parsed words, not text, so `git status` covers `git status -s` but
          never `git statusfoo` or a bare `git`.
        """
        if not command.strip():
            return False
        if any(tok in command for tok in _OPAQUE_CONSTRUCTS):
            return False
        parts = _split_commands(command)
        if not parts:
            return False
        prefixes: list[list[str]] = []
        for allowed in self.allowed_commands:
            try:
                prefix = shlex.split(allowed)
            except ValueError:
                continue
            if prefix:
                prefixes.append(prefix)
        if not prefixes:
            return False
        for part in parts:
            try:
                argv = shlex.split(part)
            except ValueError:
                return False  # unbalanced quotes etc. — treat as not-allowlisted
            if not argv or not _is_prefix_eligible(argv):
                return False
            if not any(argv[: len(p)] == p for p in prefixes):
                return False
        return True
