"""[中文] 配置 — 分层 TOML：内置默认值 < 全局 < 每个工作区。

全局：    <state-dir>/config.toml   （参见 `secrets.state_dir`；平台原生路径）
工作区：  <workspace>/.coworker/config.toml   （覆盖全局配置）

工作区命令许可仅在用户信任该精确规范工作区路径后生效。
其他权限授予保持仅限全局。

[English]
Configuration — layered TOML: built-in defaults < global < per-workspace.

Global:    <state-dir>/config.toml   (see `secrets.state_dir`; platform-native)
Workspace: <workspace>/.coworker/config.toml   (overrides global)

Workspace command allowances apply only after the user trusts that exact canonical
workspace path. Other permission grants remain global-only.
"""

from __future__ import annotations

import os

try:
    import tomllib  # stdlib since 3.11
except ModuleNotFoundError:  # 3.10, the floor requires-python declares
    import tomli as tomllib  # type: ignore[no-redef]
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .secrets import state_dir

# [中文] 无需批准提示即可自动运行的命令。通常没有绝对安全的可执行程序：
# 名义上只读的程序也可能读取工作区外的机密信息、展开环境变量、加载受项目控制的配置/插件，
# 或执行外部辅助命令（例如 `find -exec` 和 pytest 收集）。因此保持内置列表为空。
# 用户可以在其拥有的用户全局配置中显式选择命令前缀，以授予该权限。
# [English]
# Commands auto-run WITHOUT an approval prompt. There is no generally safe executable:
# nominally read-only programs can read secrets outside the workspace, expand environment
# variables, load project-controlled config/plugins, or execute helpers (for example
# `find -exec` and pytest collection). Keep the built-in list empty. A user may explicitly
# opt into command prefixes in their user-owned global config, accepting that authority.
DEFAULT_ALLOWED_COMMANDS: list[str] = []


# [中文] 推理努力程度级别（OPE-176），参照 Anthropic 的术语；每个 provider 将级别映射到其协议所接受的格式（coworker/providers/effort.py）。
# [English]
# Reasoning-effort levels (OPE-176), mirroring Anthropic's vocabulary; each provider
# maps a level to what its wire accepts (coworker/providers/effort.py).
EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")


@dataclass
class Config:
    model: str = "gpt-5.6-sol"
    mode: str = "interactive"
    max_iterations: int = 150
    # [中文] 发送给 Provider 作为 `max_tokens` 的每次回复输出 token 上限（思考/思考过程、可见文本和工具调用参数均计入其中）。
    # 未设置 = 各 Provider 自身的默认值（Anthropic 和 OpenAI 兼容端点为 32,000；Bedrock 为 4,096）。
    # Anthropic 在高推理努力下建议 ~64,000。环境变量覆盖项：COWORKER_MAX_OUTPUT_TOKENS。
    # 显式传入的 `build_engine(model_settings=...)` 优先级最高。
    # [English]
    # Per-reply output-token ceiling sent to the provider as `max_tokens` (thinking,
    # visible text and tool-call arguments all count against it). Unset = each
    # provider's own default (32,000 for Anthropic and OpenAI-compatible endpoints;
    # Bedrock 4,096). Anthropic recommends ~64,000 at high effort. Environment override:
    # COWORKER_MAX_OUTPUT_TOKENS. Explicit `build_engine(model_settings=...)` wins.
    max_output_tokens: Optional[int] = None
    # [中文] 模型每次回复应深度思考的程度：EFFORT_LEVELS 之一。未设置 = 完全不发送 effort 参数
    # （Anthropic API 默认为 high；Together 对 Kimi K3 默认为 max），因此现有请求保持不变。
    # 整个会话期间保持不变 — 在对话中途更改会重置 Prompt 缓存。环境变量覆盖项：COWORKER_REASONING_EFFORT。
    # [English]
    # How hard the model should think per reply: one of EFFORT_LEVELS. Unset = send no
    # effort parameter at all (Anthropic's API default is high; Together's default for
    # Kimi K3 is max), so existing requests are unchanged. Held constant for a whole
    # session — changing it mid-conversation restarts the prompt cache. Environment
    # override: COWORKER_REASONING_EFFORT.
    reasoning_effort: Optional[str] = None
    # [中文] OPE-186：在工具结果进入对话前限制每个结果的大小。序列化形式超过此字节数的结果将存储为
    # head + 标记 + tail，全文保存在模型可读取的溢出文件中。未设置 = 10,000；0 = 关闭。
    # 环境变量覆盖项：COWORKER_TOOL_RESULT_MAX_BYTES。
    # [English]
    # OPE-186: bound every tool result before it enters the conversation. A result whose
    # serialised form exceeds this many bytes is stored as head + marker + tail, with the
    # full text in a spill file the model can read. Unset = 10,000;
    # 0 = off. Environment override: COWORKER_TOOL_RESULT_MAX_BYTES.
    tool_result_max_bytes: Optional[int] = None
    # [中文] OPE-186 改动 3：自动压缩触发的上限（token 数）。引擎在 min(模型窗口的 80%, 该上限) 处进行压缩；
    # 内置上限为 250,000，这在 1M 窗口模型上的 445 次长会话中仅触发了一次。调低该值（例如 60000）可更早地总结长会话。
    # 环境变量覆盖项：COWORKER_COMPACTION_CAP_TOKENS。
    # [English]
    # OPE-186 change 3: cap on the auto-compaction trigger, in tokens. The engine compacts
    # at min(80% of the model's window, this cap); the built-in cap is 250,000, which on a
    # 1M-window model fired once in 445 long sessions. Lower it (e.g. 60000) to
    # summarise long sessions earlier. Environment override: COWORKER_COMPACTION_CAP_TOKENS.
    compaction_cap_tokens: Optional[int] = None
    # [中文] OPE-189：总结器（summariser）调用的输出上限（token 数）。未设置 = 16,000。
    # 在推理模型上，该预算与模型的思考过程共享，因此过低的值意味着总结内容本身永远无法写入。
    # 环境变量覆盖项：COWORKER_COMPACTION_SUMMARY_MAX_TOKENS。
    # [English]
    # OPE-189: output ceiling for the summariser call, in tokens. Unset = 16,000. On a
    # reasoning model the budget is shared with the model's thinking, so a low value means
    # the summary itself never gets written. Environment override:
    # COWORKER_COMPACTION_SUMMARY_MAX_TOKENS.
    compaction_summary_max_tokens: Optional[int] = None
    allowed_commands: list[str] = field(
        default_factory=lambda: list(DEFAULT_ALLOWED_COMMANDS)
    )
    # [中文] 在 "custom" 权限模式下，这些工具会被自动批准（例如文件编辑），而其他所有工具仍会提示确认。
    # [English]
    # In "custom" permission mode, these tools are auto-approved (e.g. file edits)
    # while everything else still asks.
    auto_allow: list[str] = field(default_factory=list)
    # [中文] `web_fetch` 无需批准提示即可访问的外网目标（精确主机或子域名）。默认为空 — 首次访问任何主机都会提示。
    # 高级用户可选项，类似于 `allowed_commands`；仅限用户全局，因此代码仓库无法扩大 Agent 的网络访问范围。
    # [English]
    # Egress destinations `web_fetch` may reach WITHOUT an approval prompt (exact host or
    # subdomain). Empty by default — the first fetch to any host asks. A power-user opt-in,
    # like `allowed_commands`; user-global only, so a repo can't widen the agent's network reach.
    allowed_domains: list[str] = field(default_factory=list)
    # [中文] 自动批准模式的特性开关（规范 §1.5）：为 true 时，会话会获得一个 LLM 审核员（reviewer），
    # 在 Mode.AUTO_APPROVE 下裁决潜在的审批卡片。默认关闭；仅限用户全局 — 克隆的代码仓库绝不能为自身放宽审核限制。
    # [English]
    # Auto-Approve mode's feature flag (spec §1.5): when true, sessions get an LLM reviewer
    # that judges would-be approval cards in Mode.AUTO_APPROVE. Off by default; user-global
    # only — a cloned repo must not be able to hand itself a looser reviewer.
    auto_approve: bool = False
    # [中文] 影子评估（规范 Part 6 步骤 3）：审核员记录它在人类做决策时对每个审批卡片“本会”作出的决定。
    # 裁决结果记录在审计日志中，与人类的结果并列，不改变其他任何行为 — 这就是如何在真实会话中衡量发布门槛
    # （零误批准；提示减少 ≥30%）。开启时每张卡片消耗一次模型调用。默认关闭；仅限用户全局。
    # [English]
    # Shadow evaluation (spec Part 6 step 3): the reviewer records what it WOULD have
    # decided on every approval card while the human still decides. Verdicts land in the
    # audit log next to the human's outcome and nothing else changes — this is how the ship
    # gates (zero false-allows; ≥30% fewer prompts) get measured on real sessions. Costs
    # one model call per card while on. Off by default; user-global only.
    auto_approve_shadow: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    # [中文] Web 搜索提供商："duckduckgo"（无需 Key 的默认项） | "tavily" | "brave"（需要 Key）。
    # [English]
    # Web search provider: "duckduckgo" (keyless default) | "tavily" | "brave" (need a key).
    web_search_provider: str = "duckduckgo"
    # [中文] OpenWorker Cloud（登录 + 托管连接器）。使用配置，绝非硬编码常量：
    # 开发/预发/自带 VPC (BYO-VPC) 部署可将其指向自己的实例。
    # [English]
    # OpenWorker Cloud (sign-in + managed connectors). Config, never constants:
    # dev/staging/BYO-VPC deployments point these at their own instances.
    cloud_base_url: str = "https://api.openworker.com"
    # [中文] Auth0 租户 + API audience 为已注册的标识符，而非品牌名称：
    # 租户名称永远不可重命名，且 audience 必须匹配在 Auth0 中注册的 API 标识符 — 两者均故意保留旧值。
    # [English]
    # Auth0 tenant + API audience are registered identifiers, not branding: the
    # tenant name can never be renamed, and the audience must match the API
    # identifier registered in Auth0 — both keep the legacy value on purpose.
    cloud_auth_domain: str = "opencoworker.us.auth0.com"
    cloud_client_id: str = "g1l4Q1lhYWmyS03qPSf4KEJGrgq02Qam"
    cloud_audience: str = "https://api.opencoworker.app"
    # [中文] 托管中继 WebSocket 端点（Slack/GitHub 入站）。默认指向生产中继，
    # 使得全新安装开箱即可中继 — 过去空默认值曾在每台未手动编辑 config.toml 的机器上表现为“已连接但中继关闭”。
    # 覆盖为空值 ⇒ 禁用中继（手动 Socket Mode 仍然可用）；开发/BYO 部署可指向其他位置。
    # [English]
    # Managed relay WebSocket endpoint (Slack/GitHub inbound). Defaults to the
    # PRODUCTION relay so a fresh install relays out of the box — an empty
    # default shipped once as "connected but relay OFF" on every machine
    # without a hand-edited config.toml. Empty override ⇒ relay disabled
    # (manual Socket Mode still works); dev/BYO deployments point elsewhere.
    cloud_relay_ws_url: str = (
        "wss://l4z1paxb83.execute-api.us-east-1.amazonaws.com/ocw-connect"
    )
    # [中文] 联合视图代理所连接的托管机器服务（规范：“已登录桌面上的联合视图”）。
    # 覆盖为空值 ⇒ 云端机器交互界面完全关闭；开发/BYO 部署可指向其他位置。
    # [English]
    # Hosted machines service the union view proxies to (spec: "Union view on
    # the signed-in desktop"). Empty override ⇒ the cloud machines surface is
    # off entirely; dev/BYO deployments point elsewhere.
    cloud_machines_base: str = "https://machines.openworker.com"


_FIELDS = {
    "model",
    "mode",
    "max_iterations",
    "max_output_tokens",
    "reasoning_effort",
    "tool_result_max_bytes",
    "compaction_cap_tokens",
    "compaction_summary_max_tokens",
    "allowed_commands",
    "auto_allow",
    "allowed_domains",
    "auto_approve",
    "auto_approve_shadow",
    "host",
    "port",
    "web_search_provider",
    "cloud_base_url",
    "cloud_auth_domain",
    "cloud_client_id",
    "cloud_audience",
    "cloud_relay_ws_url",
    "cloud_machines_base",
}

# [中文] 这些字段会改变无需提示即可执行的重大后果操作，因此常规的工作区覆盖传递永远不会应用它们。
# `allowed_commands` 仅针对规范信任的工作区单独添加；`auto_allow` 和 `allowed_domains` 保持仅限用户全局
# （代码仓库绝不能扩大 Agent 的命令或网络访问范围）。
# [English]
# These fields change what consequential actions can run without a prompt, so the normal
# workspace override pass never applies them. `allowed_commands` is added separately only
# for a canonically trusted workspace; `auto_allow` and `allowed_domains` remain user-global
# only (a repo must not be able to widen the agent's command or network reach).
_GLOBAL_ONLY_FIELDS = {
    "allowed_commands",
    "auto_allow",
    "allowed_domains",
    "auto_approve",
    "auto_approve_shadow",
}
_WORKSPACE_FIELDS = _FIELDS - _GLOBAL_ONLY_FIELDS


def global_config_path() -> Path:
    return state_dir() / "config.toml"


def _read(path: Path) -> dict[str, Any]:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def workspace_allowed_commands(workspace: str | Path) -> list[str]:
    """[中文] 代码仓库配置请求的命令前缀；在工作区获得信任之前仅具建议性。
    [English] Command prefixes requested by repository config; advisory until workspace trust."""
    path = Path(workspace).expanduser() / ".coworker" / "config.toml"
    value = _read(path).get("allowed_commands", [])
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(v.strip() for v in value if isinstance(v, str) and v.strip()))


MAX_OUTPUT_TOKENS_ENV = "COWORKER_MAX_OUTPUT_TOKENS"
REASONING_EFFORT_ENV = "COWORKER_REASONING_EFFORT"
TOOL_RESULT_MAX_BYTES_ENV = "COWORKER_TOOL_RESULT_MAX_BYTES"
COMPACTION_CAP_TOKENS_ENV = "COWORKER_COMPACTION_CAP_TOKENS"
COMPACTION_SUMMARY_MAX_TOKENS_ENV = "COWORKER_COMPACTION_SUMMARY_MAX_TOKENS"


def _nonnegative_int(value: Any, source: str) -> Optional[int]:
    """[中文] `tool_result_max_bytes`：>= 0 的整数（0 表示关闭限制）；布尔值会被拒绝。
    [English] `tool_result_max_bytes`: an integer >= 0 (0 turns bounding off); bools rejected."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(
            f"tool_result_max_bytes must be an integer >= 0, got {value!r} ({source})"
        )
    return value


def _effort_level(value: Any, source: str) -> Optional[str]:
    """[中文] `reasoning_effort` 必须是 EFFORT_LEVELS 之一（不区分大小写）；空值 = 未设置。
    [English] `reasoning_effort` must be one of EFFORT_LEVELS (case-insensitive); empty = unset."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text not in EFFORT_LEVELS:
        raise ValueError(
            f"reasoning_effort must be one of {', '.join(EFFORT_LEVELS)}, got {value!r} ({source})"
        )
    return text


def _positive_int(value: Any, source: str) -> Optional[int]:
    """[中文] `max_output_tokens` 必须为正整数（Python 中 bool 是 int 的子类，TOML `true` 否则会作为 1 通过）。
    [English] `max_output_tokens` must be a positive integer (bools are ints in Python and
    TOML `true` would otherwise pass as 1)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(
            f"max_output_tokens must be a positive integer, got {value!r} ({source})"
        )
    return value


def load_config(
    workspace: Optional[str | Path] = None,
    *,
    global_path: Optional[Path] = None,
    workspace_trusted: bool = False,
) -> Config:
    cfg = Config()

    g = Path(global_path) if global_path is not None else global_config_path()
    if g.is_file():
        for key, value in _read(g).items():
            if key in _FIELDS:
                setattr(cfg, key, value)
    if workspace:
        w = Path(workspace).expanduser() / ".coworker" / "config.toml"
        if w.is_file():
            for key, value in _read(w).items():
                if key in _WORKSPACE_FIELDS:
                    setattr(cfg, key, value)
            if workspace_trusted:
                cfg.allowed_commands = list(
                    dict.fromkeys(
                        [*cfg.allowed_commands, *workspace_allowed_commands(workspace)]
                    )
                )
    cfg.max_output_tokens = _positive_int(cfg.max_output_tokens, "config.toml")
    raw = (os.environ.get(MAX_OUTPUT_TOKENS_ENV) or "").strip()
    if raw:
        try:
            parsed: Any = int(raw)
        except ValueError:
            parsed = raw
        cfg.max_output_tokens = _positive_int(parsed, MAX_OUTPUT_TOKENS_ENV)
    cfg.reasoning_effort = _effort_level(cfg.reasoning_effort, "config.toml")
    raw_effort = os.environ.get(REASONING_EFFORT_ENV)
    if raw_effort is not None and raw_effort.strip():
        cfg.reasoning_effort = _effort_level(raw_effort, REASONING_EFFORT_ENV)
    cfg.tool_result_max_bytes = _nonnegative_int(cfg.tool_result_max_bytes, "config.toml")
    raw_cap = (os.environ.get(TOOL_RESULT_MAX_BYTES_ENV) or "").strip()
    if raw_cap:
        try:
            parsed_cap: Any = int(raw_cap)
        except ValueError:
            parsed_cap = raw_cap
        cfg.tool_result_max_bytes = _nonnegative_int(parsed_cap, TOOL_RESULT_MAX_BYTES_ENV)
    if cfg.compaction_cap_tokens is not None and (
        isinstance(cfg.compaction_cap_tokens, bool)
        or not isinstance(cfg.compaction_cap_tokens, int)
        or cfg.compaction_cap_tokens <= 0
    ):
        raise ValueError(
            f"compaction_cap_tokens must be a positive integer, got {cfg.compaction_cap_tokens!r} (config.toml)"
        )
    raw_comp = (os.environ.get(COMPACTION_CAP_TOKENS_ENV) or "").strip()
    if raw_comp:
        try:
            parsed_comp = int(raw_comp)
        except ValueError:
            parsed_comp = 0
        if parsed_comp <= 0:
            raise ValueError(
                f"compaction_cap_tokens must be a positive integer, got {raw_comp!r} ({COMPACTION_CAP_TOKENS_ENV})"
            )
        cfg.compaction_cap_tokens = parsed_comp
    cfg.compaction_summary_max_tokens = _positive_int_setting(
        cfg.compaction_summary_max_tokens,
        "compaction_summary_max_tokens",
        COMPACTION_SUMMARY_MAX_TOKENS_ENV,
    )
    return cfg


def _positive_int_setting(
    value: Any, name: str, env_var: str
) -> Optional[int]:
    """[中文] 由 `env_var` 覆盖的 config.toml 值；两者都必须是正整数。
    [English] A config.toml value overridden by `env_var`; both must be positive integers."""
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
    ):
        raise ValueError(
            f"{name} must be a positive integer, got {value!r} (config.toml)"
        )
    raw = (os.environ.get(env_var) or "").strip()
    if not raw:
        return value
    try:
        parsed = int(raw)
    except ValueError:
        parsed = 0
    if parsed <= 0:
        raise ValueError(
            f"{name} must be a positive integer, got {raw!r} ({env_var})"
        )
    return parsed
