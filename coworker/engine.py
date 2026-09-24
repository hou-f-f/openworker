"""【TurnEngine — 核心自主 Agent 循环】
异步执行循环，使用 `asyncio.to_thread` 包装阻塞的 Provider 与工具调用，确保循环及消费其事件的前端界面保持响应。
一个用户输入产生的 Turn（执行轮次）会跨越多次模型与工具的交互迭代，直到大模型停止请求工具、触发安全边界机制（Rail）、或被人为中断。
当模型在单轮中同时请求多个工具调用时，低风险工具（读取文件、搜索等）并发并发执行；而高风险操作（写文件、执行 Shell 等）严格保持串行有序执行。

人机审批通过外部注入的异步 `approver` 处理：当权限引擎裁定为 `needs_user` 时，引擎发出 `PERMISSION_REQUIRED` 事件并异步挂起等待人类裁决。

TurnEngine — the owned agent loop.

Async, but with blocking provider/tool calls wrapped in `asyncio.to_thread` so the loop
(and any UI consuming its events) stays responsive. One user turn spans many model↔tool
iterations until the model stops requesting tools, a rail trips, or it's interrupted.
When the model requests several tool calls in one turn, low-risk ones (reads, searches)
execute concurrently; writes/shell stay strictly ordered.

Approvals are handled out-of-band via an injected async `approver`: when the permission
engine says `needs_user`, the engine emits `PERMISSION_REQUIRED` and awaits the approver.
"""

from __future__ import annotations

import logging
from copy import deepcopy

import asyncio
import json
import time
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from . import compaction as _compaction
from . import provenance
from . import session_facts
from . import toolchain as _toolchain
from . import toolresult
from .events import Event, EventType

# §8.4 重试保护门禁：当 Reviewer 审查者连续拒绝达到这么多次后，在当前 Turn 剩余阶段暂停自动审批
# （2→5 + 连续语义，2026-08-24 裁定：之前累积 2 次会导致长任务在一对严苛拒绝后被误降级为纯人工审批）
# §8.4 retry guard: the reviewer pauses for the rest of the turn after this many denials
# IN A ROW (2→5 + streak semantics, owner ruling 2026-08-24 — a cumulative 2 silently
# downgraded long agentic turns to hand-approval after one over-strict pair).
#
# OPE-171：当回复因达到输出 Token 上限被截断（finish_reason 为 "length"）且不包含任何工具调用时，不能算作有效回答——通常是深度思考消耗了全部预算导致正文空白。
# 引擎会提示模型直接采取行动而不是以“已完成”结束当前 Turn，连续重试最多此上限次数；随后结束为 "truncated"，以便调用方区分“正常完成”与“放弃”。
# OPE-171: a reply cut off at the output-token limit (finish_reason "length") that
# carries no tool call is not an answer — typically thinking consumed the whole budget
# and nothing else came back. The engine nudges the model to act instead of ending the
# turn as "completed", at most this many times in a row; then the turn ends as
# "truncated" so callers can tell "finished" from "gave up".
MAX_TRUNCATION_CONTINUATIONS = 2
TRUNCATION_NUDGE = (
    "Your previous reply hit the output-token limit before you took an action or "
    "finished. Do not repeat the long reasoning. Decide the next concrete step and "
    "call a tool now, or give the final answer briefly."
)
# 针对被截断回复模型端看到的占位存根：因部分思考块没有签名（Anthropic 会在重放时拒绝），转录保留原始块，发送给模型时使用此存根替代
# What the provider sees in place of the cut-off reply: its partial thinking block has
# no signature (Anthropic rejects it on replay) and its content is empty or a fragment;
# the transcript keeps the original, the outbound view sends this.
TRUNCATION_STUB = "(reply cut off at the output-token limit before any action)"
# OPE-192：每轮上下文块是瞬态且易变的（带有动态时钟）。它作为末尾独立消息以该标签开头发送，以便模型提供商将其 Prompt Cache 断点保持在最后一个稳定块上
# OPE-192: the per-turn context block is ephemeral and volatile (it carries a clock). It is
# sent as its own trailing message opening with this tag, so a provider can keep its cache
# breakpoint on the last STABLE block and leave the note outside the cached prefix.
EPHEMERAL_CONTEXT_OPEN = "<system-context>"

_REVIEWER_TRIP = 5
_REVIEWER_PAUSED_TEXT = (
    "Auto-approve is paused for the rest of this turn — the reviewer blocked "
    f"{_REVIEWER_TRIP} actions in a row, so approvals now come to you."
)
from .permissions import Mode, PermissionEngine
from .providers import AssistantTurn, ProviderClient, ToolCall
from .providers.errors import friendly_model_error
from .providers.openai_provider import looks_like_unparsed_tool_call
from .tools import ToolRegistry


logger = logging.getLogger(__name__)

class ApprovalOutcome(str, Enum):
    # 单次批准本次调用
    # Approve this invocation only
    ONCE = "once"
    # 始终允许该工具调用
    # Always allow this tool
    ALWAYS_TOOL = "always_tool"
    # 始终允许此前缀的命令执行
    # Always allow commands matching this prefix
    ALWAYS_COMMAND = "always_command"
    # 始终允许访问此域名
    # Always allow egress to this domain
    ALWAYS_DOMAIN = "always_domain"
    # 会话级授权：放行经分类器核准的只读 Shell 命令 (readonly.py)
    # Session-wide grant for classifier-approved read-only shell commands (readonly.py).
    READONLY_SESSION = "readonly_session"
    # OPE-136 长期信任：持久化保存针对某个 MCP 工具的“无需询问”规则——跨会话生效，可在配置页撤销
    # OPE-136 durable trust: persist a per-tool "don't ask" rule for an MCP tool —
    # survives sessions, revocable on the server's detail page. MCP-only (validated
    # server-side in manager._grant_offered, like every other grant).
    ALWAYS_TRUST = "always_trust"
    # OPE-136 单次运行授权（“在当前请求中允许”）：仅在当前单次运行的剩余阶段放行该工具——保存在内存中，运行结束即清除
    # OPE-136 run grant ("Allow for this request"): cover this exact tool for the
    # remainder of the CURRENT run only — in-memory, cleared at the run boundary,
    # nothing persisted. EXTERNAL-risk tools only (validated server-side).
    THIS_RUN = "this_run"
    # 拒绝执行
    # Deny tool call
    DENY = "deny"
    # 已被后续操作取代废弃
    # Superseded by subsequent action
    SUPERSEDED = "superseded"


def _readonly_ok(arguments: dict) -> bool:
    command = str((arguments or {}).get("command", "") or "")
    if not command:
        return False
    from .readonly import is_readonly_command

    return is_readonly_command(command)


@dataclass
class PermissionRequest:
    tool_name: str
    arguments: dict[str, Any]
    metadata: Any
    reason: str
    tool_call_id: Optional[str] = None  # for durable resume (idempotent inbox item)
    # Where an MCP call actually goes ({transport, host}, from the server DEF at
    # registration) — carried on the request so a PARKED approval shows the same
    # destination evidence as the live card (§35 parity). None for non-MCP tools.
    mcp_destination: Optional[dict] = None
    escalation: Optional[dict] = None
    provenance: str = ""


Approver = Callable[[PermissionRequest], Awaitable[ApprovalOutcome]]


async def _deny_all(_request: PermissionRequest) -> ApprovalOutcome:
    return ApprovalOutcome.DENY


class TurnEngine:
    def __init__(
        self,
        *,
        provider: ProviderClient,
        registry: ToolRegistry,
        permissions: PermissionEngine,
        model: str,
        instructions: Optional[str] = None,
        approver: Optional[Approver] = None,
        max_iterations: int = 12,
        model_settings: Optional[dict[str, Any]] = None,
        messages: Optional[list[dict[str, Any]]] = None,
        audit_sink: Optional[Callable[[dict[str, Any]], None]] = None,
        context_provider: Optional[Callable[[], str]] = None,
        directory_requester: Optional[
            Callable[[dict[str, Any]], "Awaitable[dict[str, Any]]"]
        ] = None,
        plan_approver: Optional[
            Callable[[dict[str, Any]], "Awaitable[dict[str, Any]]"]
        ] = None,
        question_asker: Optional[
            Callable[[dict[str, Any]], "Awaitable[dict[str, Any]]"]
        ] = None,
        tool_requester: Optional[
            Callable[[dict[str, Any]], "Awaitable[dict[str, Any]]"]
        ] = None,
        team_approver: Optional[
            Callable[[dict[str, Any]], "Awaitable[dict[str, Any]]"]
        ] = None,
        items_approver: Optional[
            Callable[[dict[str, Any]], "Awaitable[dict[str, Any]]"]
        ] = None,
        # Handles `request_connector` / `grant_connector` (spec §11.6): emits
        # CONNECTOR_REQUESTED, waits for the human, returns {approved, …}.
        connector_requester: Optional[
            Callable[[dict[str, Any]], "Awaitable[dict[str, Any]]"]
        ] = None,
        # Called (thread-safe, best-effort) when the user stops the turn — e.g. the
        # executor's kill for a running shell command.
        interrupt_hooks: Optional[list[Callable[[], None]]] = None,
        # OPE-186 change 1: bound every tool result before it enters the conversation
        # (head + marker + tail; full text in a spill file). None = the module default
        # (10,000 bytes), 0 = off. See coworker/toolresult.py.
        tool_result_max_bytes: Optional[int] = None,
        tool_result_spill_dir: Optional[Path] = None,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.permissions = permissions
        self.model = model
        self.approver = approver or _deny_all
        self.max_iterations = max_iterations
        self.model_settings = dict(model_settings or {})
        self.messages: list[dict[str, Any]] = list(messages or [])
        self._tool_timings: dict[str, dict[str, float]] = {}
        self.audit_sink = audit_sink
        # 返回一个临时 `<system-context>` 块，仅在发送时追加到最后一条用户消息（绝不持久化）。
        # 我们无法跨 Provider 可靠地在对话中途注入 system 消息，因此动态的逐轮上下文
        # （例如实时目录列表）挂载在最新用户轮次上。无内容时返回 ""。
        # Returns an ephemeral `<system-context>` block appended to the LAST user message at
        # send-time only (never persisted). We can't reliably inject system messages mid-thread
        # across providers, so dynamic per-turn context (e.g. the live directory list) rides on
        # the latest user turn. Returns "" when there's nothing to add.
        self.context_provider = context_provider
        # 处理 `request_directory` 工具：发出 DIRECTORY_REQUESTED 提示，等待用户带外同意/拒绝
        # 某个文件夹授权，将授权应用到当前实时会话，并返回结果。不能提示的界面传 None
        # （该工具随后成为 no-op）。
        # Handles the `request_directory` tool: emits a DIRECTORY_REQUESTED prompt, waits for the
        # user to grant/decline a folder out-of-band, applies the grant to this live session, and
        # returns the outcome. None on surfaces that can't prompt (the tool then no-ops).
        self.directory_requester = directory_requester
        # 处理 `request_tool` 工具：发出 TOOL_REQUESTED，等待用户安装固定版本或拒绝。
        # 不能提示的界面传 None（该工具随后成为 no-op，并明确告知智能体，以便它公开降级处理，
        # 而不是悄悄跳过）。
        # Handles the `request_tool` tool: emits TOOL_REQUESTED, waits for the user to install
        # the pinned build or decline. None on surfaces that can't prompt (the tool then
        # no-ops, and the agent is told so it can fall back openly rather than skip silently).
        self.tool_requester = tool_requester
        # 处理 `propose_plan` 工具：发出 PLAN_PROPOSED，等待用户决定。批准后将实时
        # PermissionEngine 切出 plan 模式（同一会话继续，保留上下文）。不能提示的界面传 None
        # （该工具随后成为 no-op）。
        # Handles the `propose_plan` tool: emits PLAN_PROPOSED, waits for the user's decision.
        # An approving result flips the live PermissionEngine out of plan mode (same session,
        # context kept). None on surfaces that can't prompt (the tool then no-ops).
        self.plan_approver = plan_approver
        # 处理 `propose_team` 工具（人员配备关卡）：发出 TEAM_PROPOSED，等待用户决定；
        # 批准后预先创建工作节点会话，结果携带花名册（actor id）。不能提示的界面传 None。
        # Handles the `propose_team` tool (the staffing gate): emits TEAM_PROPOSED, waits
        # for the user's decision; approval pre-spawns the worker sessions and the result
        # carries the roster (actor ids). None on surfaces that can't prompt.
        self.team_approver = team_approver
        self.connector_requester = connector_requester
        # 处理 `propose_work_items`（任务拆解关卡）：发出 ITEMS_PROPOSED 并等待；
        # 批准后在看板上创建工作项。它按设计与权限模式无关：不同于 propose_plan，
        # 它不携带权限模式语义。propose_plan 是“实现计划”（步骤/文件、退出 plan 模式），
        # 这里是把团队工作拆解到看板上。
        # Handles `propose_work_items` (the decomposition gate): emits ITEMS_PROPOSED,
        # waits; approval creates the items on the board. Mode-independent by design —
        # unlike propose_plan it carries no permission-mode semantics: propose_plan is
        # an IMPLEMENTATION plan (steps/files, plan-mode exit); this is a team
        # decomposition onto the board.
        self.items_approver = items_approver
        # 处理 `ask_user` 工具：把问题转换为 Inbox 项并等待回答（有人值守时可在实时会话内回答，
        # 无人值守时可从 Inbox 回答）。不能提问的界面传 None（该工具随后成为 no-op）。
        # Handles the `ask_user` tool: turns a question into an Inbox item and waits for the answer
        # (answerable inline in a live session or from the Inbox when unattended). None on surfaces
        # that can't ask (the tool then no-ops).
        self.question_asker = question_asker
        # 自动压缩 (OPE-27)：由界面/manager 在构造后设置，从而保持构造函数签名稳定。
        # `compaction_settings` 是实时 getter（设置变更无需重建即可生效）；`is_attended`
        # 控制失败提示（None → 视为无人值守：后台运行绝不因内部账务停住等待）。
        # Auto-compaction (OPE-27) — set post-construction by the surface/manager so the
        # constructor footprint stays put. `compaction_settings` is a live getter (Settings
        # changes apply without a rebuild); `is_attended` gates the failure prompt (None →
        # treat as unattended: never park a background run on internal bookkeeping).
        self.compaction_state: Optional[_compaction.CompactionState] = None
        self.compaction_settings: Optional[Callable[[], dict[str, Any]]] = None
        self.is_attended: Optional[Callable[[], bool]] = None
        # Session facts (spec Part 0 / §2.4) — the known world frozen at session start, plus
        # the per-turn ingestion record. Set post-construction by the surface, same as
        # compaction above, so the constructor footprint stays put. None ⇒ nothing recorded
        # and behaviour is byte-identical; NOTHING consumes it in v1 either way.
        self.session_facts: Optional[session_facts.SessionFacts] = None
        # Auto-Approve reviewer (spec Part 8). Set post-construction; None ⇒ Mode.AUTO_APPROVE
        # behaves exactly like INTERACTIVE. Consulted only on decisions the gate marked
        # needs_user, only in AUTO_APPROVE mode, only when the session is attended (an
        # unset is_attended counts as NOT attended here — automations never set it), and
        # only until _REVIEWER_TRIP denials IN A ROW (§8.4 retry guard). Consecutive, not
        # cumulative: an allow/unsure verdict or an ask_user answer resets the streak —
        # the owner-hit 2026-08-24 was a 2-denial cumulative trip silently downgrading a
        # long agentic turn to hand-approval for everything after one over-strict pair.
        self.reviewer: Optional[Any] = None
        self.reviewer_enabled = True  # standalone injected reviewers; manager supplies live flag
        self.reviewer_settings_epoch = 0
        self.reviewer_settings_key = None
        self._reviewer_denials = 0
        self._reviewer_verdicts: dict[str, Any] = {}
        self._reviewer_input_snapshots: dict[str, Any] = {}
        # (c) 每个有后果调用是如何被放行的，按 tool_call id 记录：
        # {"origin": "reviewer"|"bypass"|"user", "note": <审查器理由>, "grant": <用户结果>}。
        # _record_result 会把它写入 TOOL_FINISHED 事件，也会写入工具消息的 `_display` 伴生字段，
        # 因而静默来源标签在重载后仍保留（所有者裁定 2026-08-24）——仅展示用，Provider 永不可见。
        # (c) How each consequential call got cleared, keyed by tool_call id:
        # {"origin": "reviewer"|"bypass"|"user", "note": <reviewer reasoning>, "grant":
        # <user outcome>}. Consumed by _record_result into the TOOL_FINISHED event AND
        # into the tool message's `_display` sidecar, so the quiet provenance chips
        # survive reload (owner ruling 2026-08-24) — display-only, never provider-visible.
        self._approval_origins: dict[str, dict[str, str]] = {}
        # 影子评估（规范第 6 部分第 3 步）：为 True 且已接入审查器时，审查器记录它
        # “本来会”对每张审批卡片做出的裁决，但实际仍由人决定。即发即弃：卡片绝不延迟，
        # 决策绝不被改写，裁决写入审计日志（stage="reviewer_shadow"，通过 call_id 与人的
        # approval_resolved 行关联）。
        # Shadow evaluation (spec Part 6 step 3): when True and a reviewer is attached, the
        # reviewer records what it WOULD have decided on each approval card while the human
        # still decides. Fire-and-forget — the card is never delayed, no decision is ever
        # touched, and the verdict lands in the audit log (stage="reviewer_shadow", joined
        # to the human's approval_resolved row by call_id).
        self.reviewer_shadow = False
        self._shadow_tasks: set[asyncio.Task] = set()
        # 一次性“仍然允许”授权（§8.4）：只能由人点击拒绝卡片生成，键为精确工具名 +
        # 规范化参数，首次匹配即消耗。稍有不同的再次提议都不会命中，必须回到审查器/卡片流程——
        # 刻意收窄，刻意不是长期规则。
        # One-shot "Allow anyway" grants (§8.4): minted ONLY by a human clicking the deny
        # card, keyed on the exact tool + canonical arguments, consumed on first match. A
        # re-proposal with even slightly different arguments does not match and goes back
        # through the reviewer/card — deliberately narrow, deliberately not standing.
        self._allow_anyway: set[tuple[str, str]] = set()
        # ask_user answers for the reviewer's history (§8.2 — the missing third of the
        # reply-tag feature: render_history prints the tag and the §8.3 instructions say to
        # weigh it lower; this is the extractor that finally delivers the data). Captured at
        # the moment the asker returns — the one point where the engine KNOWS the text came
        # from the human, whichever authenticated surface answered (inline card, Inbox, or a
        # bound channel; the same trust approval clicks already carry). ANSWERS ONLY, never
        # the agent's question: agent-authored text stays out of the judge's view — showing
        # the question too is step 2, evidence-gated on shadow data. Each entry is
        # (anchor, text) where anchor = how many user messages existed at capture, so the
        # merge in `_user_history` stays chronological. Runtime-only on purpose: a restart
        # costs the reviewer context (more cards), never correctness.
        self._ask_replies: list[tuple[int, str, str]] = []  # (anchor, answer, question)
        # 工具审批卡片的额外用户可见字段，会合并进 PERMISSION_REQUIRED 载荷。
        # 例如 web_search 的实时 Provider 名称，让卡片能说明查询实际发往何处（§1.9）。
        # 由界面在构造后设置（引擎自身不知道 Provider 细节）；None 表示无额外字段。
        # 在卡片生成时调用，而不是会话开始时调用，因此会话中途的设置变更可反映出来。
        # Extra user-facing fields for a tool's approval card, merged into the
        # PERMISSION_REQUIRED payload — e.g. web_search's live provider name, so the card
        # can say where queries actually go (§1.9). Set post-construction by the surface
        # (the engine itself knows nothing about providers); None ⇒ no extras. Called at
        # card time, not session start, so a mid-session Settings change shows through.
        self.approval_extras: Optional[
            Callable[[str, dict[str, Any]], dict[str, Any]]
        ] = None
        # Harness-resolved original action for a lead's permission proxy. Never
        # derived from the lead's note or another agent's conversation.
        self.delegated_approval: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None
        self.reviewer_context: Optional[Callable[[], dict[str, Any]]] = None
        self.reviewer_owner_history: Optional[Callable[[], tuple[str, list[dict[str, Any]]]]] = None
        self.reviewer_denial_message: Optional[str] = None
        self._authorized_delegates: dict[str, dict] = {}
        # What the agent itself created this session (OPE-114 §1). The reviewer never sees
        # file contents, so `python scripts/setup.py` is unjudgeable from its text — but the
        # engine knows whether it wrote or downloaded that file moments ago, and says so on
        # the card and in the reviewer's request. Runtime-only, like `_ask_replies`: a
        # restart costs context (more cards), never correctness.
        self._agent_files = provenance.SessionFiles(permissions.workspace_root)
        self._tool_result_max_bytes = (
            toolresult.DEFAULT_TOOL_RESULT_MAX_BYTES
            if tool_result_max_bytes is None
            else int(tool_result_max_bytes)
        )
        self._tool_result_spill_dir = (
            Path(tool_result_spill_dir) if tool_result_spill_dir is not None else None
        )
        # Completed tool calls so far, so a fact can say how many steps back the write was.
        self._step = 0
        self._last_context_tokens: Optional[int] = None
        self.audit_context: dict[str, Any] = {}
        if instructions and not (
            self.messages and self.messages[0].get("role") == "system"
        ):
            self.messages.insert(0, {"role": "system", "content": instructions})
        self._cancel = asyncio.Event()
        # Whether the latest assistant turn hit the output-token limit — decides which
        # diagnosis a mangled (unparseable-args) tool call gets answered with.
        self._turn_truncated = False
        # Consecutive length-truncated, action-free replies nudged this turn (OPE-171).
        self._continuations = 0
        self._warned_context_fallback = False
        # Each pending steering message: (text, optional MessageSource sidecar dict).
        self._steering: list[tuple[str, Optional[dict[str, Any]], Optional[dict[str, Any]]]] = []
        # tool_call.id → the standing rule that auto-allowed it ("tool → target"), so the
        # TOOL_FINISHED event can carry the note to the tool card (§25).
        self._standing_notes: dict[str, str] = {}
        self._interrupt_hooks: list[Callable[[], None]] = list(interrupt_hooks or [])

    # -- external controls ------------------------------------------------------
    def request_interrupt(self) -> None:
        """尽快从任意状态停止当前轮次：流式输出中（生产线程在 chunk 间丢弃流）、
        工具执行中（interrupt hooks 终止运行中的命令）、等待审批/问题/计划中
        （await 以 interrupted 结果返回），或迭代之间（主循环检查点）。
        每个待处理 tool_call 仍会得到一个工具错误结果，确保历史不留下孤儿调用
        （托管模板会拒绝孤儿调用，持久化恢复也会重新提示它们）。

        Stop the turn as soon as possible, from ANY state: mid-stream (the producer
        thread drops the stream between chunks), mid-tool (interrupt hooks kill the
        running command), awaiting an approval/question/plan (the await resolves as
        interrupted), or between iterations (the loop checkpoint). Every pending
        tool_call still gets a tool-error result so the history never carries orphans
        (hosted templates reject them, and durable-resume would re-prompt them)."""
        self._cancel.set()
        for hook in self._interrupt_hooks:
            try:
                hook()
            except Exception:
                pass  # best-effort: a dead executor must not block the stop

    async def _interruptible(self, coro: Any, interrupted: Any) -> Any:
        """等待 `coro`，但如果用户停止当前轮次，则提前以 `interrupted` 返回。
        待处理 task 会被取消，因此稍后才回答的 Inbox 卡片会成为 no-op。

        Await `coro`, but resolve early with `interrupted` if the user stops the
        turn. The pending task is cancelled so an answered-later Inbox card no-ops."""
        task = asyncio.ensure_future(coro)
        cancel_wait = asyncio.ensure_future(self._cancel.wait())
        try:
            done, _ = await asyncio.wait(
                {task, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                return task.result()
            task.cancel()
            return interrupted
        finally:
            cancel_wait.cancel()

    async def _wait_tool(self, tool_call, coro, interrupted):
        started, clock = time.time(), time.monotonic()
        try:
            return await self._interruptible(coro, interrupted)
        finally:
            self._tool_timings.setdefault(tool_call.id, {}).update(waited_started=started, waited_ms=(time.monotonic() - clock) * 1000)

    def _timed_result(self, tool_call, result):
        message = _tool_result_message(tool_call, result)
        if tool_call.id in self._tool_timings:
            message["timing"] = self._tool_timings.pop(tool_call.id)
        return message

    def queue_steering(
        self, text: str, source: Optional[dict[str, Any]] = None,
        activity: Optional[dict[str, Any]] = None,
    ) -> None:
        self._steering.append((text, source, activity))

    # -- 核心执行主循环 (main loop) --------------------------------------------------------------
    async def run(
        self,
        user_input: "str | list",
        *,
        source: Optional[dict[str, Any]] = None,
        display: Optional[str] = None,
        activity: Optional[dict[str, Any]] = None,
    ) -> AsyncIterator[Event]:
        """【执行轮次入口】处理一次用户输入或外部触发事件，产出结构化执行事件流。

        Run a single user turn, yielding structured execution events.
        """
        # `user_input` 可以是纯文本字符串，也可以是多模态 content-parts 数组（文本 + 图片附件）。
        # `source` 是连接器消息的伴生元数据（仅供前端展示）：它保存在持久化用户消息与 TURN_START 事件中，
        # 但在发往大模型前会被剥离。
        # `display` 针对强制运行的技能指令：将用户输入的 "/skill …" 保存在转录中，而 `content` 携带真正发给模型的框架 Prompt。
        # 服务重启可能中断处于 tool_use 与 tool_result 之间的未完结轮次（如挂起的人工审批）。
        # 大模型会拒绝此类悬空的破损历史，因此新 Turn 必须先用合法的存根修复所有孤儿调用，防止会话永久损坏。
        # `user_input` is a string, or OpenAI content-parts (text + image_url) for attachments.
        # `source` (a MessageSource dict) is a display-only sidecar for connector messages: it
        # rides on the persisted user message + the TURN_START event, but is stripped before the
        # message reaches a provider (see `_outbound_messages`). `content` stays the framed text.
        # `display` is the same split for force-run skills (SKILLS-SPEC §4.1 #3): the user's
        # literal "/skill …" line for the transcript, while `content` carries the model-facing
        # framing. `ts` (unix seconds, stamped on every appended message) is the same kind of
        # sidecar.
        # A restart can interrupt a turn between a tool_use and its result (a
        # parked approval is the common case). The provider hard-rejects such
        # a history, so a NEW turn must first close any orphaned calls with an
        # honest stub — otherwise one interruption poisons the session forever.
        self._repair_dangling_tool_calls()
        message: dict[str, Any] = {
            "role": "user",
            "content": user_input,
            "ts": time.time(),
        }
        if source is not None:
            message["source"] = source
        if display is not None:
            message["_display"] = display
        if activity:
            message["_activity"] = activity
        self.messages.append(message)
        self._cancel.clear()
        if self.session_facts is not None:
            self.session_facts.begin_turn()
        # §8.4 重试保护计数器每轮重置：连续拒绝达到上限则将该轮剩余所有操作路由至人工确认
        # §8.4 retry guard resets per user turn: two reviewer denials in one turn route
        # everything else that turn to the human. A fresh user message is a fresh brief.
        self._reviewer_denials = 0
        self._reviewer_verdicts.clear()
        self._reviewer_input_snapshots.clear()
        self._authorized_delegates.clear()
        data: dict[str, Any] = {"input": user_input}
        if source is not None:
            data["source"] = source
        if display is not None:
            data["display"] = display
        # OPE-136 单次运行授权清空：全新轮次从干净状态开始
        # OPE-136 run grants: a fresh run starts with a clean slate (belt — the
        # finally below is the braces; an abandoned generator must not leak a
        # previous answer's "Allow for this request" into this one).
        self.permissions.clear_run_allowances()
        yield Event(EventType.TURN_START, data)
        try:
            async for event in self._loop():
                yield event
        finally:
            # 运行边界即为授权失效之时：正常完工、Stop 停止、以及生成器意外断开均会触发清理
            # The run boundary IS the grant's expiry — normal finish, Stop, and
            # generator teardown (disconnect) all land here.
            self.permissions.clear_run_allowances()

    def switch_model(self, model: str) -> Optional[str]:
        """在对话中途重新绑定会话模型（roadmap item 3）。
        历史保持规范 OpenAI 形状，各 Provider 在每次调用时自行转换，因此切换只是写字段；
        另加一条持久化 notice 标记切换发生的位置。如果历史里有图片而新模型不可见，
        则给出降级警告（这些图片会以占位符发送，见 `_outbound_messages`）。
        模型未变化（或新会话首次绑定）时返回 None；否则返回 notice 文本。

        Rebind the session's model mid-conversation (roadmap item 3). History is
        canonical OpenAI shape and every provider converts per call, so the switch is just
        the field write — plus a persisted notice marking WHERE it happened, with a
        degradation warning when history carries images the new model can't see (those are
        sent as placeholders — see `_outbound_messages`). Returns the notice text, or None
        when nothing changed (same model, or first bind on a fresh session)."""
        if not model or model == self.model:
            return None
        had_history = any(m.get("role") != "system" for m in self.messages)
        self.model = model
        # The reviewer judges with the session's own model (§1.5: "if it's trusted to
        # drive the agent, it's strong enough to review it"). Bound once at session build,
        # it would otherwise keep the OLD model for the rest of the session after a
        # switch — silently reviewing with a model the user moved away from.
        if self.reviewer is not None:
            self.reviewer.model = model
        if not had_history:
            return None
        from .providers.matrix import model_labels

        text = f"Model switched to {model_labels().get(model, model)}"
        try:
            caps = self.provider.capabilities(model)
        except Exception:
            caps = None
        if (
            caps is not None
            and not getattr(caps, "vision", False)
            and self._history_has_images()
        ):
            text += " — earlier images can't be read by this model"
        self._append_notice("model_switch", text)
        return text

    def _history_has_images(self) -> bool:
        return any(
            isinstance(p, dict) and p.get("type") == "image_url"
            for msg in self.messages
            if isinstance(msg.get("content"), list)
            for p in msg["content"]
        )

    def _tail_is_retriable_error(self) -> bool:
        """当历史尾部是 error notice 时返回 True；会跳过其后的 model_switch notice
        （模型切换不应消耗 retry 机会）。

        True when the history tail is an error notice, looking through any model_switch
        notices appended after it (a switch must not consume the retry)."""
        for message in reversed(self.messages):
            if message.get("role") != "notice":
                return False
            if message.get("kind") == "model_switch":
                continue
            return message.get("kind") == "error"
        return False

    def _append_notice(self, kind: str, text: Optional[str] = None, **fields: Any) -> None:
        """把轮次结束标记（error/interrupted 等）持久化为仅展示用的 `notice` 消息：
        它像转录一样能跨重载保留，但 `_outbound_messages` 会丢弃该 role，确保任何 Provider
        都看不到它。额外 `fields`（例如失败 MCP server 的名称）会作为结构化渲染数据保存在消息上。

        Persist a turn-ending marker (error/interrupted) as a display-only `notice`
        message: it survives reload like the transcript does, but `_outbound_messages`
        drops the role so no provider ever sees it. Extra `fields` (e.g. the failing
        MCP server's name) persist on the message for structured rendering."""
        notice: dict[str, Any] = {"role": "notice", "kind": kind, "ts": time.time()}
        if text:
            notice["text"] = text
        notice.update({k: v for k, v in fields.items() if v is not None})
        self.messages.append(notice)

    async def retry(self) -> AsyncIterator[Event]:
        """Provider 错误后重新运行模型循环，不新增用户消息；失败轮次的输入已经在历史尾部。
        只有尾部是 error notice 时才允许，避免误把已完成轮次重新回答。尾部的 model_switch
        notice 不会破坏门禁；先切模型再 retry 是预期恢复路径（owner-hit 2026-07-23）。

        Re-run the model loop after a provider error — no new user message; the failed
        turn's input is already the tail of history. Guarded on the tail being an error
        notice so a stray retry frame can't re-answer a completed turn. Trailing
        model_switch notices don't break the guard — switching models and THEN retrying
        is the intended recovery path (owner-hit 2026-07-23)."""
        if not self._tail_is_retriable_error():
            return
        self._cancel.clear()
        yield Event(EventType.TURN_START, {"input": ""})
        async for event in self._loop():
            yield event

    async def resume(self) -> AsyncIterator[Event]:
        """继续一个已持久化、停在提示上的轮次，即重启（或引擎被驱逐）后的持久恢复。
        重新处理尾部 assistant 消息中“尚未回答”的工具调用（提示回调会找到已经解决的
        Inbox 项并直接返回；已回答调用会跳过，因此不会重复执行），随后运行模型循环完成该轮。

        Continue a turn that was suspended at a prompt and persisted — durable resume after a
        restart (or engine eviction). Re-process the trailing assistant message's UNANSWERED
        tool-calls (the prompt callbacks find the already-resolved Inbox item and return without
        re-prompting; answered calls are skipped, so nothing double-executes), then run the model
        loop to finish the turn."""
        pending = self._unanswered_trailing_tool_calls()
        if not pending:
            return
        self._cancel.clear()
        self._yield_for_wake = False
        yield Event(EventType.TURN_START, {"input": "(resumed)"})
        async for event in self._handle_tool_calls(pending):
            yield event
        yield Event(EventType.ITERATION_END, {"iteration": 0})
        if not self._cancel.is_set():
            if self._yield_for_wake:
                yield Event(EventType.TURN_END, {"status": "sleeping", "iterations": 0})
                return
            async for event in self._loop():
                yield event

    def _repair_dangling_tool_calls(self) -> None:
        """用存根工具结果关闭历史中每个孤儿 tool_use。

        被中断的轮次（审批中重启、调用与结果之间崩溃）会留下带 tool_calls 但没有结果的
        assistant 消息，Provider 会因此拒绝整段对话。存根会立即插入到出问题的 assistant
        消息后面，保留 id，并如实说明发生了什么；模型可以在需要时重新发起调用。

        Close every orphaned tool_use in history with a stub tool result.

        An interrupted turn (restart mid-approval, crash between call and
        result) leaves an assistant message whose tool_calls have no results;
        providers reject the whole conversation for it. Stubs are inserted
        IMMEDIATELY after the offending assistant message, keep the ids, and
        say honestly what happened — the model may re-issue the call."""
        answered = {
            m.get("tool_call_id") for m in self.messages if m.get("role") == "tool"
        }
        i = 0
        while i < len(self.messages):
            msg = self.messages[i]
            stubs = []
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    if tc.get("id") and tc["id"] not in answered:
                        stubs.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": json.dumps(
                                    {
                                        "error": "tool call interrupted — no result "
                                        "was recorded (the session was restarted). "
                                        "Re-issue the call if it is still needed."
                                    }
                                ),
                                "ts": time.time(),
                            }
                        )
            for offset, stub in enumerate(stubs, start=1):
                self.messages.insert(i + offset, stub)
            i += 1 + len(stubs)

    def _unanswered_trailing_tool_calls(self) -> list[ToolCall]:
        """最后一条 assistant 消息中尚无工具结果的 tool-calls，也就是我们挂起时所在的提示
        （以及其后的任何调用）。从持久化线程中重建。

        The tool-calls of the last assistant message that don't yet have a tool result —
        i.e. the prompt we suspended on (+ any after it). Reconstructed from the persisted thread.
        """
        answered = {
            m.get("tool_call_id") for m in self.messages if m.get("role") == "tool"
        }
        for msg in reversed(self.messages):
            if msg.get("role") == "user":
                return []
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                out: list[ToolCall] = []
                for tc in msg["tool_calls"]:
                    if tc.get("id") in answered:
                        continue
                    fn = tc.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    out.append(
                        ToolCall(id=tc.get("id"), name=fn.get("name"), arguments=args)
                    )
                return out
        return []

    async def _loop(self) -> AsyncIterator[Event]:
        self._yield_for_wake = False
        iterations = 0
        self._continuations = 0
        while True:
            if iterations >= self.max_iterations:
                yield Event(
                    EventType.TURN_END,
                    {"status": "max_iterations_exceeded", "iterations": iterations},
                )
                return
            iterations += 1

            # 自动压缩检查点 (OPE-27)：位于工具轮次之间，以及新轮次第一次模型调用之前。
            # 刻意不向模型发送“收尾”警告。COMPACTING 信号先于可能耗时数秒的摘要器调用发出，
            # 这样界面能展示进度，而不是静默卡住。
            # Auto-compaction checkpoint (OPE-27): between tool turns and before a new
            # turn's first call. Deliberately no "wrap up" warning to the model. The
            # COMPACTING signal precedes the (multi-second) summarizer call so surfaces
            # can show progress instead of a silent stall.
            notice = None
            if self._compaction_due():
                yield Event(EventType.COMPACTING, {})
                notice = await self._compact_now()
            if notice:
                record = self._compaction_record()
                self._append_notice("compacted", notice, compaction=record)
                yield Event(EventType.COMPACTED, {"text": notice, "compaction": record})

            turn: Optional[AssistantTurn] = None
            streamed: list[str] = []
            streamed_reasoning: list[str] = []

            def _partial_turn() -> AssistantTurn:
                # 用户已经看到的部分：文本与思考，不包含工具调用（任何半成形调用要么成为孤儿，
                # 要么会违背停止动作继续执行）。
                # What the user watched arrive — text and thinking, NO tool calls (any
                # half-formed calls would either orphan or execute against the stop).
                return AssistantTurn(
                    text="".join(streamed) or None,
                    reasoning="".join(streamed_reasoning) or None,
                )

            model_started, model_clock = time.time(), time.monotonic()
            try:
                async for chunk in self._astream():
                    if chunk.reasoning_delta:
                        streamed_reasoning.append(chunk.reasoning_delta)
                        yield Event(
                            EventType.REASONING_DELTA, {"text": chunk.reasoning_delta}
                        )
                    if chunk.text_delta:
                        streamed.append(chunk.text_delta)
                        yield Event(
                            EventType.ASSISTANT_DELTA, {"text": chunk.text_delta}
                        )
                    if chunk.turn is not None:
                        turn = chunk.turn
            except Exception as exc:  # provider failure
                # 原始 context-overflow 400（例如估算路径导致压缩预测错误）会进入压缩策略，
                # 而不是直接暴露给用户。重试带有进度保护：每次都会推进边界或放弃，因此持续
                # 溢出的模型最终仍会走到错误路径。
                # A raw context-overflow 400 (compaction mispredicted, e.g. the estimate
                # path) routes into the compaction policy instead of surfacing. The retry
                # is progress-guarded: each pass moves the boundary forward or gives up,
                # so a model that keeps overflowing still terminates in the error path.
                if _compaction.is_context_overflow(exc) and not self._cancel.is_set():
                    yield Event(EventType.COMPACTING, {})
                    notice = await self._compact_now(force=True)
                    if notice:
                        record = self._compaction_record()
                        self._append_notice("compacted", notice, compaction=record)
                        yield Event(
                            EventType.COMPACTED, {"text": notice, "compaction": record}
                        )
                        continue
                # 与下方停止路径保持同一契约：用户已经看到的部分会在失败后保留下来。
                # Same contract as the stop path below: the partial the user watched
                # arrive survives the failure.
                if streamed or streamed_reasoning:
                    self.messages.append(_assistant_message(_partial_turn()))
                friendly = friendly_model_error(self.model, exc)
                payload = {
                    "error": friendly or str(exc),
                    "error_type": type(exc).__name__,
                }
                if friendly:
                    payload["raw"] = str(exc)
                self._append_notice("error", friendly or str(exc))
                yield Event(EventType.ERROR, payload)
                return
            if self._cancel.is_set() and turn is None:
                # 流式输出中途停止：精确保留用户已经看到的内容。
                # Stopped mid-stream: persist exactly what the user watched arrive.
                if streamed or streamed_reasoning:
                    self.messages.append(_assistant_message(_partial_turn()))
                self._append_notice("interrupted")
                yield Event(EventType.INTERRUPTED, {"iterations": iterations})
                return
            if turn is None:
                turn = AssistantTurn()
            if turn.usage is not None:
                # 触发信号：本次往返实际占用上下文窗口的 prompt 侧总量（若 Provider 从未报告，
                # 则退回估算）。
                # The trigger signal: the prompt-side total that actually occupied the
                # window on this round-trip (estimate fallback when never reported).
                self._last_context_tokens = turn.usage.context_tokens

            self._turn_truncated = turn.finish_reason == "length"
            if not self._turn_truncated:
                self._continuations = 0
            _sanitize_mangled_calls(turn)
            self.messages.append(
                _assistant_message(
                    turn,
                    model=self.model,
                    effort_setting=self.model_settings.get("reasoning_effort"),
                )
            )
            self.messages[-1]["timing"] = {"model_started": model_started, "model_ms": (time.monotonic() - model_clock) * 1000}
            payload: dict[str, Any] = {
                "text": turn.text,
                "tool_calls": [tc.name for tc in turn.tool_calls],
            }
            if turn.reasoning:
                payload["reasoning"] = turn.reasoning
            if turn.usage is not None:
                payload["usage"] = {"model": self.model, **turn.usage.as_dict()}
                self._audit_usage(turn.usage)
            if turn.finish_reason:
                # How the reply ended (OPE-173): `stop` / `tool_calls` / `length`.
                payload["finish_reason"] = turn.finish_reason
            if turn.output_limit:
                # The output ceiling the provider sent (OPE-177).
                payload["max_output_tokens"] = turn.output_limit
            effort_record = _effort_record(turn, self.model_settings.get("reasoning_effort"))
            if effort_record:
                payload["reasoning_effort"] = effort_record
            if turn.served_by:
                payload["served_by"] = turn.served_by
            yield Event(EventType.ASSISTANT_MESSAGE, payload)

            if not turn.tool_calls:
                if self._steering:
                    self._inject_steering()
                    continue
                if self._turn_truncated:
                    # OPE-171：达到输出上限而没有任何可执行内容。刚持久化的回复会标记为
                    # stub replay（见 `_outbound_messages`）；随后要求模型采取行动并再次循环，
                    # 除非这种情况已经发生太多次。
                    # OPE-171: cut off at the output limit with nothing actionable. The
                    # reply just persisted is marked for stub replay (see
                    # `_outbound_messages`); the model is asked to act, and the turn
                    # goes round again — unless it has already happened too often.
                    cut = self.messages[-1]
                    if cut.get("role") == "assistant":
                        cut["replay"] = "stub"
                    if self._continuations < MAX_TRUNCATION_CONTINUATIONS:
                        self._continuations += 1
                        self.messages.append(
                            {
                                "role": "user",
                                "content": TRUNCATION_NUDGE,
                                "ts": time.time(),
                                "_display": {
                                    "kind": "continuation",
                                    "reason": "length",
                                    "attempt": self._continuations,
                                },
                            }
                        )
                        yield Event(
                            EventType.CONTINUATION,
                            {
                                "reason": "length",
                                "attempt": self._continuations,
                                "remaining": MAX_TRUNCATION_CONTINUATIONS
                                - self._continuations,
                                "iterations": iterations,
                                "text": (
                                    "Reply cut off at the output-token limit with no "
                                    f"action; asking the model to continue "
                                    f"({self._continuations} of "
                                    f"{MAX_TRUNCATION_CONTINUATIONS})."
                                ),
                            },
                        )
                        continue
                    text = (
                        f"{self.model}'s reply was cut off at the output-token limit with "
                        f"no action {self._continuations + 1} times in a row, so the turn "
                        "was stopped rather than reported as complete. Raise "
                        "max_output_tokens, lower the reasoning effort, or retry."
                    )
                    self._append_notice(
                        "truncated", text, continuations=self._continuations
                    )
                    yield Event(
                        EventType.TURN_END,
                        {
                            "status": "truncated",
                            "iterations": iterations,
                            "continuations": self._continuations,
                            "text": text,
                        },
                    )
                    return
                # 模型试图调用工具，但语法始终没能解析；补救逻辑已经尝试过。若此处以
                # "completed" 结束，就会把半截工具调用当成回答展示，和模型真的决定结束
                # 无法区分；用户只会看到叙述拖进零散标签。这里改为显式失败，走错误路径，
                # 让 GUI 提供 Retry。这通常是模型漂移而非确定性故障，重试同一模型往往可行。
                # The model tried to call a tool and the syntax never parsed — salvage already
                # had its go. Ending as "completed" here would present a half-written call as
                # the answer, which is indistinguishable from the model deciding it was done;
                # the user just sees narration trailing off into stray tags. Fail loudly
                # instead, on the error path so the GUI offers Retry — this is drift, not a
                # deterministic failure, so retrying the same model usually works.
                if looks_like_unparsed_tool_call(turn.text, self.registry.schemas() or None):
                    message = (
                        f"{self.model} replied with a tool call this endpoint couldn't parse, "
                        "so the turn was stopped rather than answered from a partial call. "
                        "Retry, or switch to a larger model — smaller local models drift off "
                        "the tool-call format, especially with many tools in play."
                    )
                    self._append_notice("error", message)
                    yield Event(
                        EventType.ERROR,
                        {"error": message, "error_type": "UnparsedToolCall"},
                    )
                    return
                yield Event(
                    EventType.TURN_END,
                    {"status": "completed", "iterations": iterations},
                )
                return

            async for event in self._handle_tool_calls(turn.tool_calls):
                yield event

            yield Event(EventType.ITERATION_END, {"iteration": iterations})

            if self._cancel.is_set():
                self._append_notice("interrupted")
                yield Event(EventType.INTERRUPTED, {"iterations": iterations})
                return
            if self._yield_for_wake and not self._steering:
                yield Event(EventType.TURN_END, {"status": "sleeping", "iterations": iterations})
                return
            if self._steering:
                self._inject_steering()
                self._yield_for_wake = False

    # -- auto-compaction (OPE-27) ------------------------------------------------
    def _compaction_config(self) -> dict[str, Any]:
        cfg = dict(self.compaction_settings() or {}) if self.compaction_settings else {}
        if not cfg.get("context_window"):
            from .providers.matrix import model_context_windows

            cfg["context_window"] = model_context_windows().get(self.model)
            if not cfg["context_window"] and not self._warned_context_fallback:
                # OPE-170：未列入矩阵的模型会按 128k 猜测值压缩。对 1M 上下文窗口模型来说，
                # 这意味着窗口才用到十分之一就压缩，并且每次重建 prompt cache。
                # 每个 engine 只警告一次。
                # OPE-170: an unlisted model compacts on the 128k guess, which for a
                # 1M-window model means compacting at a tenth of the window and
                # rebuilding the prompt cache each time. Say so once per engine.
                self._warned_context_fallback = True
                logger.warning(
                    "model %s has no context window in the model matrix; assuming "
                    "%d tokens (compaction at %d). Add a matrix row or set "
                    "context_window in the compaction settings.",
                    self.model,
                    _compaction.DEFAULT_CONTEXT_WINDOW,
                    _compaction.trigger_tokens(None),
                )
        cfg.setdefault("threshold_pct", _compaction.DEFAULT_THRESHOLD_PCT)
        cfg.setdefault("cap_tokens", _compaction.DEFAULT_CAP_TOKENS)
        cfg.setdefault("summary_max_tokens", _compaction.SUMMARY_MAX_TOKENS)
        return cfg

    def _compaction_due(self) -> bool:
        """仅执行触发检查；它成本低且无副作用，因此主循环能在提交给较慢的摘要器调用前
        先发出 COMPACTING 信号。

        The trigger check alone — cheap and side-effect free, so the loop can emit
        the COMPACTING signal before committing to the (slow) summarizer call."""
        cfg = self._compaction_config()
        if cfg.get("enabled") is False:
            return False
        signal = self._last_context_tokens or _compaction.estimate_tokens(
            self._outbound_messages()
        )
        return _compaction.should_compact(
            signal,
            cfg.get("context_window"),
            threshold_pct=float(cfg["threshold_pct"]),
            cap_tokens=int(cfg["cap_tokens"]),
        )

    def _compaction_record(self) -> Optional[dict[str, Any]]:
        """刚发生的压缩记录，会持久化在 `compacted` notice 上并携带在 COMPACTED 事件中
        （OPE-170 problem 2）：包括摘要文本、工作状态、规范转录边界、摘要模型，以及是否
        是无摘要裁剪。没有它时，已保存会话只能看出“发生过压缩”，但不知道保留了什么；
        app 的 session record 只保留最新状态，导出的轨迹/运行记录则完全没有。

        The compaction that just happened, as persisted on the `compacted` notice and
        carried on the COMPACTED event (OPE-170 problem 2): summary text, working state,
        the boundary into the canonical transcript, the summarizer model, and whether it
        was the no-summary trim. Without it a saved session only showed THAT compaction
        happened, not what was kept — the app's session record keeps only the latest
        state, and exported trajectories / run records had nothing at all."""
        state = self.compaction_state
        return state.as_dict() if state is not None else None

    async def _compact_now(self, *, force: bool = False) -> Optional[str]:
        """运行压缩策略。调用方以 `_compaction_due()`（或溢出路径的 `force`）作为门禁。
        当 outbound 视图发生变化时返回用户可见 notice 文本，否则返回 None。失败策略按规范：
        两种模式都先重试一次；有人值守则提示 Retry / Trim；无人值守则自动裁剪并继续
        （绝不让运行停在内部账务上）。

        Run the compaction policy. Callers gate on `_compaction_due()` (or `force`,
        the overflow path). Returns the user-facing notice text when the outbound view
        changed, else None. Failure policy per spec: retry once (both modes); attended →
        Retry / Trim prompt; unattended → auto-trim and continue (never park a run on
        bookkeeping)."""
        cfg = self._compaction_config()
        pct = float(cfg["threshold_pct"])
        cap = int(cfg["cap_tokens"])
        window = cfg.get("context_window")
        trigger = _compaction.trigger_tokens(window, threshold_pct=pct, cap_tokens=cap)
        keep = int(_compaction.KEEP_RECENT_FRACTION * trigger)
        # OPE-189：两个预算都随触发阈值缩放，因此为了省 token 而降低阈值时，不会被
        # 一个继续花掉释放空间的块抵消掉。
        # OPE-189: both budgets scale with the trigger, so lowering it to save tokens can't
        # be undone by a block that keeps spending the freed space.
        user_budget = _compaction.user_message_budget(trigger)
        summary_max = int(
            cfg.get("summary_max_tokens") or _compaction.SUMMARY_MAX_TOKENS
        )
        model = str(cfg.get("model") or "") or self.model

        def _build() -> Optional[_compaction.CompactionState]:
            return _compaction.build_state(
                self.messages,
                provider=self.provider,
                model=model,
                keep_tokens=keep,
                prior=self.compaction_state,
                summary_max_tokens=summary_max,
                user_budget_tokens=user_budget,
            )

        state: Optional[_compaction.CompactionState] = None
        failed = False
        for _attempt in range(2):  # first try + the unconditional single retry
            try:
                state = await asyncio.to_thread(_build)
                failed = False
                break
            except Exception:
                failed = True
        if failed and self.question_asker is not None and self.is_attended and self.is_attended():
            while True:
                answer = await self._interruptible(
                    self.question_asker(
                        {
                            "question": (
                                "Context compaction failed — the summarizer couldn't "
                                "condense this session's history. How should I proceed?"
                            ),
                            "options": ["Retry", "Trim oldest 10%"],
                            "allow_text": False,
                            "header": "Compaction",
                        },
                        None,
                    ),
                    interrupted=None,
                )
                if not answer or answer.get("answer") != "Retry":
                    break
                try:
                    state = await asyncio.to_thread(_build)
                    failed = False
                    break
                except Exception:
                    continue
        if state is not None:
            # OPE-186 变更 3：保持被压缩轮次可读。边界之前的逐字转录写入溢出工具结果旁边的文件，
            # 压缩块告诉模型文件在哪里，因此摘要遗漏的细节只需一次读取，而不是彻底丢失。
            # OPE-186 change 3: keep the compacted turns readable. The verbatim transcript
            # up to the boundary goes to a file next to the spilled tool results, and the
            # compacted block tells the model where it is, so a detail the summary dropped
            # costs one read instead of being lost.
            if self._tool_result_spill_dir is not None:
                try:
                    self._tool_result_spill_dir.mkdir(parents=True, exist_ok=True)
                    path = (
                        self._tool_result_spill_dir
                        / f"compacted-transcript-upto-{state.boundary_index:04d}.md"
                    )
                    path.write_text(
                        _compaction.render_transcript(self.messages, state.boundary_index),
                        encoding="utf-8",
                        errors="replace",
                    )
                    state.transcript_path = str(path)
                except OSError:
                    pass
            self.compaction_state = state
            self._last_context_tokens = None  # stale once the outbound view shrank
            return "Context compacted — earlier turns were summarized"
        if failed or force:
            trimmed = _compaction.trim_state(
                self.messages,
                prior=self.compaction_state,
                user_budget_tokens=user_budget,
            )
            if trimmed is not None:
                self.compaction_state = trimmed
                self._last_context_tokens = None
                return "Context trimmed — oldest turns dropped (summary unavailable)"
        return None

    # -- helpers ----------------------------------------------------------------
    async def _astream(self):
        """通过线程 + 队列把 Provider 的阻塞式 stream 生成器桥接到异步循环，
        让文本 delta 可以实时浮出，同时不阻塞事件循环。

        Bridge the provider's blocking stream generator to the async loop via a
        thread + queue, so text deltas surface live without blocking the event loop."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        tools = self.registry.schemas() or None
        model, messages, settings = (
            self.model,
            self._outbound_messages(),
            self.model_settings,
        )
        provider = self.provider

        def produce():
            try:
                for chunk in provider.stream(
                    model=model, messages=messages, tools=tools, **settings
                ):
                    # 用户按下 Stop：在 chunk 间丢弃流（从线程读取 asyncio.Event 标志是安全的；
                    # 我们只读不写）。
                    # User pressed Stop: drop the stream between chunks (reading the
                    # asyncio.Event's flag from a thread is safe; we only read).
                    if self._cancel.is_set():
                        break
                    loop.call_soon_threadsafe(queue.put_nowait, ("chunk", chunk))
            except Exception as exc:  # surfaced to the awaiting consumer
                loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

        loop.run_in_executor(None, produce)
        while True:
            # 让队列读取与 Stop 竞争，避免停滞的流（没有 chunk 到达：首 token 前等待、
            # 卡住的连接）拖住整个轮次。
            # Race the queue against Stop so a stalled stream (no chunks arriving —
            # the pre-first-token wait, a wedged connection) can't hold the turn.
            get_task = asyncio.ensure_future(queue.get())
            cancel_task = asyncio.ensure_future(self._cancel.wait())
            done, _ = await asyncio.wait(
                {get_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
            cancel_task.cancel()
            if get_task not in done:
                get_task.cancel()
                return  # interrupted — the producer exits on its own next chunk
            kind, payload = get_task.result()
            if kind == "chunk":
                yield payload
            elif kind == "error":
                raise payload
            else:
                return

    async def _handle_tool_calls(
        self, tool_calls: list[ToolCall]
    ) -> AsyncIterator[Event]:
        """【处理工具调用】执行大模型在单轮中请求的所有工具调用：
        首先依序进行权限鉴权（交互式审批需逐个向用户展示），随后分流执行。
        低风险操作（文件读取、搜索等）通过并发执行提高吞吐量；高风险操作（写文件、Shell 等）严格按顺序串行执行。

        Run one assistant turn's tool calls: authorize all of them first (sequentially —
        approval prompts are interactive), then execute. Low-risk calls (reads, searches)
        run concurrently; everything else runs one at a time in call order."""
        # 自动审批机制 (Auto-Approve)：在串行鉴权循环之前，并发向 Reviewer 审查者大模型提交所有待审查的工具调用
        # （规范 §8.6 —— 每次请求审查一个动作，并发发起多个；审查 N 个调用的耗时仅相当于单次网络往返，
        # 且裁决结论在物理上绝不会张冠李戴绑定到错误的动作上）。
        # 下方的授权循环保持串行，因为人机审批弹窗具有交互性，必须按调用顺序逐个呈现给用户。
        # Auto-Approve: fire the reviewer for every call that will need it, all at once,
        # BEFORE the sequential authorize loop (spec §8.6 — one action per request, sent
        # concurrently; the wall-clock cost of reviewing N calls is one round-trip, and a
        # verdict physically cannot land on the wrong action). The loop below stays
        # sequential because approval cards are interactive and must reach the human one
        # at a time, in call order.
        await self._preconsult_reviewer(tool_calls)
        cleared: list[ToolCall] = []
        for tool_call in tool_calls:
            if self._cancel.is_set():
                # 收到中断信号：所有剩余调用依然生成应答存根（避免留下破损的孤儿调用）
                # Stopped: every remaining call still gets an answer (no orphans).
                yield self._interrupted_tool(tool_call)
                continue
            yield Event(
                EventType.TOOL_PROPOSED,
                {"name": tool_call.name, "arguments": tool_call.arguments},
            )
            self._audit(tool_call, stage="proposed")
            if _is_mangled(tool_call):
                # 参数无法解析为合法 JSON（大模型返回了 `{"_raw": …}` 降级兜底）。
                # 若直接执行会产生让模型困惑的参数报错，现场容易演变为死循环。在此直接返回准确的诊断提示。
                # The arguments never parsed as JSON (a `{"_raw": …}` fallback from the
                # provider). Executing would produce a bare parameter error the model
                # misreads — seen in the field as an endless "wrong parameter" retry
                # loop. Answer with the ACTUAL diagnosis instead.
                yield self._mangled_tool(tool_call)
                continue
            # 交互式内置系统门禁：直接交由外部专有处理器完成交互确认，跳过通用的工具注册中心执行
            # `request_directory` and `propose_plan` are interactive: the user decides
            # out-of-band and that decision IS the consent, so they skip the
            # permission/registry path.
            if tool_call.name == "request_directory":
                async for event in self._handle_directory_request(tool_call):
                    yield event
                continue
            if tool_call.name == "request_tool":
                async for event in self._handle_tool_request(tool_call):
                    yield event
                continue
            if tool_call.name == "propose_plan":
                async for event in self._handle_plan_proposal(tool_call):
                    yield event
                continue
            if tool_call.name == "propose_team":
                async for event in self._handle_team_proposal(tool_call):
                    yield event
                continue
            if tool_call.name in ("request_connector", "grant_connector"):
                async for event in self._handle_connector_request(tool_call):
                    yield event
                continue
            if tool_call.name == "propose_work_items":
                async for event in self._handle_items_proposal(tool_call):
                    yield event
                continue
            if tool_call.name == "ask_user":
                async for event in self._handle_ask_user(tool_call):
                    yield event
                continue
            allowed = False
            async for item in self._authorize(tool_call):
                if isinstance(item, Event):
                    item.data["tool_call_id"] = tool_call.id
                    yield item
                else:
                    allowed = item
            if allowed:
                cleared.append(tool_call)

        # 区分安全并发调用与严格串行调用
        # Partition cleared calls into concurrent (safe reads) and serial (writes/exec)
        concurrent = (
            [tc for tc in cleared if self._parallel_safe(tc)]
            if len(cleared) > 1
            else []
        )
        serial = [tc for tc in cleared if tc not in concurrent]

        if concurrent:
            # 并发执行低风险只读工具
            # Concurrently execute safe read-only tools
            for tool_call in concurrent:
                yield Event(EventType.TOOL_STARTED, {"name": tool_call.name})
                self._audit(tool_call, stage="started")
            outcomes = await asyncio.gather(
                *[asyncio.to_thread(self._execute_sync, tc) for tc in concurrent]
            )
            for tool_call, (result, status) in zip(concurrent, outcomes):
                yield self._record_result(tool_call, result, status)

        # 串行执行高风险或写操作工具
        # Serially execute high-risk or mutating tools
        for tool_call in serial:
            if self._cancel.is_set():
                yield self._interrupted_tool(tool_call)
                continue
            yield Event(EventType.TOOL_STARTED, {"name": tool_call.name})
            self._audit(tool_call, stage="started")
            result, status = await asyncio.to_thread(self._execute_sync, tool_call)
            yield self._record_result(tool_call, result, status)

    def _mangled_tool(self, tool_call: ToolCall) -> Event:
        """用真实诊断回答一个参数从未成功解析的工具调用。

        两类原因对应两种修复方式；只有错误信息说明发生了哪一种，模型才能选对方案。
        截断（`finish_reason == "length"`）意味着“同样内容，拆小块”；普通坏 JSON 则意味着
        “按声明参数重新发送”。无论哪种，都不把原始文本回放进历史：存下的 `{"_raw": ...}`
        调用会像成功示例一样教模型故意输出 `_raw`（2026-08-15 观察到），还会让垃圾 token
        每轮重复发送。

        Answer a tool call whose arguments never parsed, with the real diagnosis.

        Two causes, two different cures — and the model can only pick the right one if
        the error says which happened. Truncation (`finish_reason == "length"`) means
        "same content, smaller pieces"; plain bad JSON means "re-send with the declared
        parameters". Either way the raw text is NOT replayed into history: a stored
        `{"_raw": …}` call reads as a worked example and teaches the model to emit
        `_raw` on purpose (observed 2026-08-15), on top of re-sending the junk tokens
        every turn."""
        if self._turn_truncated:
            reason = (
                "your tool-call arguments were cut off by the output-token limit before "
                "they finished streaming — the tool never received them. Produce the same "
                "content in smaller pieces: several calls that each write or append a "
                "section, keeping each call's content well under the limit. Do not retry "
                "the identical oversized call."
            )
        else:
            reason = (
                "your tool-call arguments did not parse as a JSON object, so the tool "
                "received nothing. `_raw` is not a parameter — it is the unparsed text of "
                "the failed call. Re-issue the call using the tool's declared parameters."
            )
        self.messages.append(_tool_error_message(tool_call, reason))
        self._audit(tool_call, stage="finished", status="error", reason=reason)
        return Event(
            EventType.TOOL_FINISHED,
            {"name": tool_call.name, "status": "error", "reason": reason},
        )

    def _interrupted_tool(self, tool_call: ToolCall) -> Event:
        """停止路径里给“不再运行”的调用的答复：在历史中写入一个工具错误结果
        （托管聊天模板会拒绝孤儿 tool_calls，持久化恢复也会重新提示它），并为工具卡片
        发出完成事件。

        The stop-path answer for a call that will not run: a tool-error result in the
        history (hosted chat templates reject orphaned tool_calls, and durable-resume
        would otherwise re-prompt it) + the finished event for the tool card."""
        self.messages.append(_tool_error_message(tool_call, "interrupted by user"))
        self._audit(
            tool_call, stage="finished", status="interrupted", reason="user stop"
        )
        return Event(
            EventType.TOOL_FINISHED,
            {"name": tool_call.name, "tool_call_id": tool_call.id, "status": "interrupted", "reason": "stopped"},
        )

    def _parallel_safe(self, tool_call: ToolCall) -> bool:
        # [中文] 仅元数据声明为低风险的工具（读操作、搜索、git 查询）并发运行；写操作、shell 及任何未标注的工具保持严格顺序执行。
        # Only metadata-declared low-risk tools (reads, searches, git queries) run
        # concurrently; writes, shell, and anything unannotated stay strictly ordered.
        spec = self.registry.get(tool_call.name)
        metadata = spec.metadata if spec else None
        return getattr(metadata, "risk_level", "") == "low" and not getattr(
            metadata, "requires_approval", False
        )

    # -- Auto-Approve reviewer (spec Part 8) ----------------------------------------

    def _reviewer_active(self) -> bool:
        """[中文] 仅当以下所有条件均满足时才咨询审查器。任何一项不满足 ⇒ 执行原逻辑（弹出审批卡片）。
        明确要求 attended（有人值守）：未设置 `is_attended` 视为非值守，因此自动化流程（绝不设置该标志）永远不会被审查器审查（§1.5：该模式仅限有人值守）。

        The reviewer is consulted only when ALL of these hold. Any miss ⇒ today's
        behaviour (the card). Attended is required explicitly: `is_attended` unset counts
        as NOT attended, so automations — which never set it — can never be reviewed
        (§1.5: the mode is attended-only)."""
        from .permissions import Mode

        return (
            self.reviewer is not None
            and self.reviewer_enabled
            and self.permissions.mode is Mode.AUTO_APPROVE
            and self.is_attended is not None
            and self.is_attended()
            and self._reviewer_denials < _REVIEWER_TRIP
        )

    def _user_history(self) -> tuple[str, list[dict[str, Any]]]:
        """[中文] (当前请求, 较早未标注来源的用户消息)，机械化提取 (§8.2)。
        绝不包含智能体输出、工具结果或摘要。

        `ask_user` 的回复从 `_ask_replies` 合并进来（在到达时捕获，而非从工具外壳解析），
        标记为 `is_reply`，以便 `render_history` 打印 §8.3 指令已知晓如何权衡的
        "[reply to a question the agent asked]" 标记。回复始终属于历史记录，绝不能成为当前请求 ——
        “ok proceed（好的继续）”绝不能变成评估该操作的核心基准。

        附件通过 `reviewer_text` (§4.4) 折叠为中立标记：审查器仅得知附带了一个文件，
        但绝不会知道文件内容 —— 附件正文属于附带在用户轮次上的外部编写文本。

        (current request, earlier unsourced user messages), extracted
        mechanically (§8.2). Never agent output, never tool results, never a summary.

        `ask_user` answers are merged in from `_ask_replies` (captured as they arrived, not
        parsed out of tool envelopes), tagged `is_reply` so `render_history` prints the
        "[reply to a question the agent asked]" marker the §8.3 instructions already know
        how to weigh. A reply is always HISTORY, never the current request — "ok proceed"
        must not become the headline the action is judged against.

        Attachments collapse to neutral markers via `reviewer_text` (§4.4): the reviewer
        learns a file was attached, never what it says — an attachment body is
        outside-authored text riding a user turn."""
        from .attachments import reviewer_text

        texts: list[str] = []
        for msg in self.messages:
            if msg.get("role") != "user" or msg.get("source"):
                continue
            text = reviewer_text(msg.get("content"))
            if text:
                texts.append(text)
        if not texts:
            return "", [
                {"text": t, "is_reply": True, **({"question": q} if q else {})}
                for _, t, q in self._ask_replies
            ]
        history: list[dict[str, Any]] = []
        for i, t in enumerate(texts[:-1], start=1):
            history.append({"text": t})
            history.extend(
                {"text": r, "is_reply": True, **({"question": q} if q else {})}
                for a, r, q in self._ask_replies
                if a == i
            )
        # 当前轮次捕获的回复（anchor == len(texts)），或锚点消息本身为空/被跳过之后的回复，
        # 放到尾部；这样同一轮刚给出的同意会立刻对审查器评估下一步动作可见。
        # Replies captured during the current turn (anchor == len(texts)) — or after an
        # anchor message that was itself empty/skipped — land at the tail, so a same-turn
        # consent is already visible to the reviewer for the very next action.
        history.extend(
            {"text": r, "is_reply": True, **({"question": q} if q else {})}
            for a, r, q in self._ask_replies
            if a >= len(texts)
        )
        return texts[-1], history

    def _review_inputs(self) -> tuple[str, list[dict[str, Any]], dict]:
        try:
            request, history = self.reviewer_owner_history() if self.reviewer_owner_history else self._user_history()
            context = self.reviewer_context() if self.reviewer_context else {}
            return request, deepcopy(history), deepcopy(context)
        except Exception:
            return "", [], {"context_unavailable": True}

    def _downloaded_target(self, tool_call: ToolCall) -> Optional[Any]:
        """[中文] 本次调用拟运行的由智能体在本会话中下载的文件，若无则为 None。
        “先下载再执行”链条不存在任何被默许的合法形式，因此会绕过审查器和任何命令白名单直接升级给人工确认 (OPE-114 §1)。

        A file this call would run that the agent DOWNLOADED this session, or None.
        Fetch-then-execute has no quiet legitimate form, so it reaches a person over both
        the reviewer and any command allowlist (OPE-114 §1)."""
        match = self._agent_files.match(
            tool_call.name, tool_call.arguments, step=self._step
        )
        return match if match is not None and match.downloaded else None

    def _provenance(self, tool_call: ToolCall) -> str:
        """[中文] 用单行指明该调用拟运行的、由智能体自身创建的文件，若无则为空字符串 "" (§8.2)。
        使用固定词汇 —— 绝不包含文件内容，绝不包含外部编写的文本，从而维持无不受信任内容的原则。

        One line naming a file this call would run that the agent itself created, or ""
        (§8.2). Fixed vocabulary — never file contents, never outside-authored text, so the
        no-untrusted-content rule holds."""
        match = self._agent_files.match(
            tool_call.name, tool_call.arguments, step=self._step
        )
        return match.render() if match else ""

    async def _preconsult_reviewer(self, tool_calls: list[ToolCall]) -> None:
        """[中文] 对每个即将升级的调用并发发起审查器请求，并将裁决暂存以供 `_authorize` 使用。
        每次请求对应一个操作 —— 不存在需要重新配对的裁决列表，因此裁决绝不会落在错误的操作上 (§8.6)。
        跳过门控引擎已做出决定的调用（直接允许或硬性拒绝）：审查器只会看到原本会变成人工审批卡片的调用 (§1.2)。

        Fire one reviewer request per call that will escalate, all concurrently, and
        park the verdicts for `_authorize` to consume. One action per request — there is
        no verdict list to pair back, so a verdict cannot land on the wrong action (§8.6).
        Skips calls the gate already decides (allow or hard-deny): the reviewer only ever
        sees what would otherwise become an approval card (§1.2)."""
        if not self._reviewer_active() or not tool_calls:
            return
        interactive = {"request_directory", "propose_plan", "ask_user"}
        pending: list[ToolCall] = []
        for tool_call in tool_calls:
            # Resolve parked worker calls at authorization time, not speculatively.
            if tool_call.name == "decide_worker_call":
                continue
            if tool_call.name in interactive or tool_call.id in self._reviewer_verdicts:
                continue
            spec = self.registry.get(tool_call.name)
            if spec is None:
                continue
            decision = self.permissions.evaluate(
                tool_call.name, tool_call.arguments, spec.metadata
            )
            # human_only asks never reach the reviewer — same rule as `_authorize`.
            if (
                not decision.allowed
                and decision.needs_user
                and not decision.human_only
                and self._downloaded_target(tool_call) is None
            ):
                pending.append(tool_call)
        if not pending:
            return
        request, history, context = self._review_inputs()
        consulted_reviewer = self.reviewer
        settings_epoch = self.reviewer_settings_epoch
        verdicts = await asyncio.gather(
            *[
                consulted_reviewer.review(
                    request=request,
                    history=history,
                    tool_name=tc.name,
                    arguments=tc.arguments,
                    provenance=self._provenance(tc),
                    **({"action_context": context} if context else {}),
                )
                for tc in pending
            ]
        )
        for tc, verdict in zip(pending, verdicts):
            if (self.reviewer is not consulted_reviewer
                    or self.reviewer_settings_epoch != settings_epoch
                    or not self._reviewer_active()
                    or (request, history, context) != self._review_inputs()):
                verdict = replace(verdict, verdict="unsure", reason="Approval settings changed during review; a human decision is required.")
            self._reviewer_verdicts[tc.id] = verdict
            self._reviewer_input_snapshots[tc.id] = (request, history, context)

    async def _consult_reviewer(self, tool_call: ToolCall) -> Any:
        """返回 `_preconsult_reviewer` 暂存的裁决；没有暂存时现场发起一次单独审查。

        The parked verdict from `_preconsult_reviewer`, or a fresh single call."""
        verdict = self._reviewer_verdicts.pop(tool_call.id, None)
        snapshot = self._reviewer_input_snapshots.pop(tool_call.id, None)
        if verdict is not None:
            if snapshot is not None and snapshot != self._review_inputs():
                from .reviewer import Verdict
                return Verdict("unsure", "Approval context changed since review; a human must decide.")
            return verdict
        request, history, context = self._review_inputs()
        delegated = self._delegated_context(tool_call)
        if delegated:
            from .reviewer import Verdict
            if delegated.get("hard_deny") or delegated.get("human_only"):
                return Verdict("unsure", delegated["reason"])
            verdict = await self.reviewer.review(
                request=request, history=history,
                tool_name=delegated["tool"], arguments=delegated["arguments"],
                provenance=delegated.get("provenance", ""),
                action_context=delegated["context"],
            )
            if delegated != self._delegated_context(tool_call) or (request, history, context) != self._review_inputs():
                return Verdict("unsure", "The worker request or its permissions changed during review. Review the current request.")
            return verdict
        result = await self.reviewer.review(
            request=request,
            history=history,
            tool_name=tool_call.name,
            arguments=tool_call.arguments,
            provenance=self._provenance(tool_call),
            **({"action_context": context} if context else {}),
        )
        if (request, history, context) != self._review_inputs():
            from .reviewer import Verdict
            return Verdict("unsure", "Approval context changed during review; review the current request.")
        return result

    def _delegated_context(self, tool_call: ToolCall) -> dict | None:
        if tool_call.name != "decide_worker_call" or str(tool_call.arguments.get("decision", "")).lower().strip() != "allow":
            return None
        try:
            if self.delegated_approval:
                result = self.delegated_approval(tool_call.arguments)
                if isinstance(result, dict) and result.get("hard_deny"):
                    return deepcopy(result)
                if isinstance(result, dict) and isinstance(result.get("tool"), str) and isinstance(result.get("arguments"), dict) and isinstance(result.get("context"), dict):
                    return deepcopy(result)
        except Exception:
            pass
        return {"hard_deny": True, "reason": "The original worker request cannot be verified."}

    @staticmethod
    def _action_key(tool_name: str, arguments: dict[str, Any] | None) -> tuple[str, str]:
        try:
            canon = json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            canon = str(arguments)
        return (tool_name, canon)

    def approve_action_once(self, tool_name: str, arguments: dict[str, Any] | None) -> None:
        """[中文] 针对此“完全一致”的操作注册一次性的人工批准（§8.4 “仍然允许 / Allow anyway”）。

        当用户点击拒绝卡片上的允许按钮时由服务器调用 —— 这是用户在看到审查器的完整拒绝理由后做出的人工决定。
        当再次提议完全相同的操作（相同的工具、逐字节相同的规范化参数）时，将无需审查器或审批卡片直接运行；
        任何稍有不同的操作仍需经过常规流程。绝非长期有效：首次使用后即刻被消耗。

        Register a one-shot human approval for this EXACT action (§8.4 "Allow anyway").

        Called by the server when the user clicks the deny card — a human decision made
        with the full reviewer reason in front of them. The next proposal of the identical
        action (same tool, byte-identical canonical arguments) runs without the reviewer or
        a card; anything that differs at all still goes through the normal flow. Never
        standing: consumed on first use."""
        self._allow_anyway.add(self._action_key(tool_name, arguments))
        if self.audit_sink is not None:
            try:
                self.audit_sink(
                    {
                        **self.audit_context,
                        "tool": tool_name,
                        "arguments": arguments or {},
                        "stage": "allow_anyway_granted",
                        "status": "granted",
                        "reason": "user approved via the deny card (one-shot, exact action)",
                    }
                )
            except Exception:
                pass

    def _consume_allow_anyway(self, tool_call: ToolCall) -> bool:
        key = self._action_key(tool_call.name, tool_call.arguments)
        if key in self._allow_anyway:
            self._allow_anyway.discard(key)
            return True
        return False

    def _spawn_shadow_review(self, tool_call: ToolCall) -> None:
        """[中文] 影子评估（规范第 6 部分第 3 步）：记录审查器“本来会”对这张审批卡片做出什么裁决，而不改变任何实际行为。
        即发即弃（Fire-and-forget）—— 卡片立即渲染展示；当审查调用返回时，裁决通过 `call_id` 关联并记录到审计日志中与人工的 `approval_resolved` 行一起。
        此处特意没有设计从影子裁决通往实际决策的代码路径。

        Shadow evaluation (spec Part 6 step 3): record what the reviewer WOULD have
        decided about this card, without touching anything. Fire-and-forget — the card
        renders immediately; the verdict lands in the audit log when the call returns,
        joined to the human's `approval_resolved` row by `call_id`. There is deliberately
        no code path from a shadow verdict to a decision."""
        if self.reviewer is None or not self.reviewer_shadow:
            return
        if tool_call.name == "decide_worker_call":
            # 只能审查当前已经解析出的动作。这个代理使用按需实时路径；绝不只把 ID
            # 发给影子裁决器。
            # Only the current, resolved action can be reviewed. This proxy uses
            # the on-demand live path; never send just its ID to the shadow judge.
            return
        request, history, context = self._review_inputs()
        prov = self._provenance(tool_call)

        async def _shadow() -> None:
            try:
                verdict = await self.reviewer.review(
                    request=request,
                    history=history,
                    provenance=prov,
                    tool_name=tool_call.name,
                    arguments=tool_call.arguments,
                    **({"action_context": context} if context else {}),
                )
                self._audit(
                    tool_call,
                    stage="reviewer_shadow",
                    status=verdict.verdict,
                    reason=verdict.reason,
                    call_id=tool_call.id,
                    tokens_in=verdict.tokens_in,
                    tokens_out=verdict.tokens_out,
                    cache_read=verdict.cache_read,
                    cache_write=verdict.cache_write,
                )
            except Exception:
                pass  # shadow must never surface a failure

        task = asyncio.create_task(_shadow())
        self._shadow_tasks.add(task)
        task.add_done_callback(self._shadow_tasks.discard)

    async def drain_shadow_reviews(self) -> None:
        """[中文] 等待进行中的影子评估裁决完成（用于测试和有序关机；绝不在关键热路径上调用）。
        Await in-flight shadow verdicts (tests and orderly shutdown; never the hot path)."""
        if self._shadow_tasks:
            await asyncio.gather(*list(self._shadow_tasks), return_exceptions=True)

    async def _authorize(self, tool_call: ToolCall) -> "AsyncIterator[Event | bool]":
        """[中文] 单个调用的权限鉴权流（TOOL_PROPOSED 由调用方发出）。
        先产出产生的事件，最后产出 True/False（是否允许执行）。被拒绝或未知的调用在此处追加其工具错误消息。

        Permission flow for one call (TOOL_PROPOSED is emitted by the caller). Yields
        its events, then True/False (allowed) last. Denied/unknown calls get their
        tool-error message appended here."""
        from .permissions import standing_rule_candidate

        spec = self.registry.get(tool_call.name)
        metadata = spec.metadata if spec else None

        decision = self.permissions.evaluate(
            tool_call.name, tool_call.arguments, metadata
        )
        delegated = self._delegated_context(tool_call)
        if delegated and delegated.get("hard_deny"):
            decision = replace(decision, allowed=False, needs_user=False, reason=delegated["reason"])
        elif delegated and delegated.get("human_only") and (decision.allowed or decision.needs_user):
            decision = replace(decision, allowed=False, needs_user=True, human_only=True, reason=delegated["reason"])
        allowed = decision.allowed
        reason = decision.reason

        # OPE-114 §1：运行智能体本会话下载的东西，是经典“先获取再执行”链条；
        # 它不存在静默合法形态。因此它会交给人处理，越过审查器和任何原本可能放行的命令白名单
        # （`python` 前缀规则不能为刚从互联网拉下来的脚本背书）。硬拒绝保持不变：
        # 这一底线只会收紧允许，绝不放松阻断。智能体自己写的文件不触发底线——
        # “写这个脚本并运行”是普通工作——但会作为事实交给审查器权衡。
        # OPE-114 §1: running something the agent DOWNLOADED this session is the classic
        # fetch-then-execute chain, and there is no quiet legitimate version of it — so it
        # goes to a person, over both the reviewer and any command allowlist that would
        # otherwise wave it through (a `python` prefix rule must not vouch for a script
        # pulled off the internet a moment ago). A hard deny is left untouched: this floor
        # only ever tightens an allow, never loosens a block. Agent-WRITTEN files are not
        # floored — "write this script and run it" is ordinary work — they travel as a fact
        # for the reviewer to weigh instead.
        provenance_note = self._provenance(tool_call)
        if self._downloaded_target(tool_call) is not None and (
            decision.needs_user or allowed
        ):
            allowed = False
            reason = f"this file was downloaded by the agent this session — {provenance_note}"
            decision = replace(
                decision,
                allowed=False,
                reason=reason,
                needs_user=True,
                human_only=True,
            )

        if allowed and decision.rule:
            # 任务范围的长期规则自动放行了该调用：审计具体规则（§25 不变量：每个自动放行调用
            # 都引用其规则），并记住它，以便工具卡片能显示“由长期规则允许”。
            # A task-scoped standing rule auto-allowed this call: audit the exact rule
            # (§25 invariant — every auto-allowed call cites its rule) and remember it so
            # the tool card can say "allowed by standing rule".
            self._standing_notes[tool_call.id] = decision.rule
            self._audit(
                tool_call, stage="auto_allowed", status="allowed", reason=reason
            )

        # (c) Bypass 模式运行了一个没有其他规则允许的有后果调用：标注来源。
        # "full access" 是 permissions.py bypass 分支的精确 reason 字符串。
        # (c) Bypass mode ran a consequential call no other rule allowed: annotate it.
        # "full access" is the exact reason string of permissions.py's bypass branch.
        if allowed and decision.reason == "full access":
            self._approval_origins[tool_call.id] = {"origin": "bypass"}

        # OPE-136：trusted-MCP 允许基于长期配置无卡运行（用户 trust 规则，或旧 server 标志）；
        # 像其他无卡来源一样审计并显示标签（“有记录，绝不隐形”）。这里前缀匹配
        # permissions.py 的两种 trusted 分支 reason 字符串，并在标签中区分二者：
        # “your trust rule” 指向工具页 Revoke，“server trust” 指向 mcp.json 标志。
        # 曾经一个泛化标签让用户误以为 SERVER 标记了他们自己的规则（owner-hit 2026-08-30）。
        # OPE-136: a trusted-MCP allow ran cardless on standing config (a user trust
        # rule, or the legacy server flag) — audited and chip-annotated like every
        # other cardless origin ("recorded, never invisible"). Prefix-matched against
        # permissions.py's two trusted-branch reason strings — and the chip keeps the
        # two apart: "your trust rule" points at the tool page's Revoke, "server
        # trust" at the mcp.json flag. One generic label made a user believe the
        # SERVER had marked their own rule (owner-hit 2026-08-30).
        if allowed and decision.reason.startswith("trusted MCP tool"):
            origin = (
                "trusted_rule"
                if "user trust rule" in decision.reason
                else "trusted_server"
            )
            self._approval_origins[tool_call.id] = {"origin": origin}
            self._audit(
                tool_call, stage="auto_allowed", status="allowed", reason=reason
            )

        # OPE-136 运行内授权：被覆盖的调用因用户在本次运行中点击“Allow for this request”
        # 而无卡运行——不打扰注意力，但记录上绝不隐形（转录标签 + 审计行，与所有无卡来源一致）。
        # OPE-136 run grant: a covered call ran cardless under the user's in-run
        # "Allow for this request" click — silent to attention, never invisible to
        # the record (transcript chip + audit row, like every cardless origin).
        if allowed and decision.reason == "tool allowed for this request":
            self._approval_origins[tool_call.id] = {"origin": "run_grant"}
            self._audit(
                tool_call, stage="auto_allowed", status="allowed", reason=reason
            )

        if not allowed and decision.needs_user and self._consume_allow_anyway(tool_call):
            # §8.4 "Allow anyway"：人已经从拒绝卡片批准了这个精确动作。一次性——上面已消耗；
            # 不同动作绝不会匹配。
            # §8.4 "Allow anyway": the human already approved this exact action from the
            # deny card. One-shot — consumed above; a different action never matches.
            allowed = True
            reason = "approved by user (allow anyway)"
            self._audit(tool_call, stage="auto_allowed", status="allowed", reason=reason)

        # 组长 DENY 某个工作节点等待中的调用不会运行任何东西：工作节点被告知“不行”并继续，
        # 人仍可直接回复该工作节点。在 Auto-Approve 下，这既不需要审查器也不需要卡片
        # （live 2026-09-17：用户曾被要求“Allow”一个拒绝）。ALLOW 仍会进入下方审查器，
        # Manual 组长仍会两种都询问——该模式意味着“全部展示给我”。
        # A lead DENYING one of its workers' waiting calls runs nothing: the worker is told
        # no and moves on, and the human can still answer the worker directly. Under
        # Auto-Approve that needs neither the reviewer nor a card (live 2026-09-17: the
        # human was asked to "Allow" a denial). An ALLOW still goes to the reviewer below,
        # and a Manual lead still asks for both — that mode means "show me everything".
        if (
            not allowed
            and decision.needs_user
            and not decision.human_only
            and tool_call.name == "decide_worker_call"
            and self.permissions.mode is Mode.AUTO_APPROVE
            and str((tool_call.arguments or {}).get("decision", "")).strip().lower() == "deny"
        ):
            allowed = True
            reason = "a lead's denial of a worker's call runs nothing"
            self._audit(tool_call, stage="auto_allowed", status="allowed", reason=reason)

        consulted_live = False
        unsure_note = ""  # the reviewer's hesitation, when an unsure verdict raised the card
        if (
            not allowed
            and decision.needs_user
            and not decision.human_only
            and self._reviewer_active()
        ):
            # The one thing the reviewer may do: turn "ask the human" into "go ahead" —
            # never "blocked" into "go ahead" (§1.2; hard denies never reach this branch
            # because needs_user is False on them). `human_only` asks (git hooks, CI
            # configs, unscopable writes) skip the reviewer entirely: their floor is that
            # a PERSON sees them, and a verdict here would be that floor's bypass.
            consulted_live = True
            consulted_reviewer = self.reviewer
            settings_epoch = self.reviewer_settings_epoch
            verdict = await self._consult_reviewer(tool_call)
            if (self.reviewer is not consulted_reviewer
                    or self.reviewer_settings_epoch != settings_epoch
                    or not self._reviewer_active()):
                verdict = replace(verdict, verdict="unsure", reason="Approval settings changed during review; a human decision is required.")
            self._audit(
                tool_call,
                stage="reviewer_verdict",
                status=verdict.verdict,
                reason=verdict.reason,
                tokens_in=verdict.tokens_in,
                tokens_out=verdict.tokens_out,
                cache_read=verdict.cache_read,
                cache_write=verdict.cache_write,
            )
            if verdict.verdict == "allow":
                allowed = True
                self._reviewer_denials = 0  # streak semantics: any non-deny resets
                self._approval_origins[tool_call.id] = {
                    "origin": "reviewer", "note": verdict.reason
                }
                reason = f"allowed by reviewer: {verdict.reason}"
            elif verdict.verdict == "deny":
                # §8.4 deny asymmetry — full reason to the USER (event + audit above),
                # terse non-diagnostic refusal to the AGENT. The sanctioned way around a
                # deny is ask the human, never reshape the request.
                from .reviewer import AGENT_DENY_MESSAGE

                self._reviewer_denials += 1
                tripped = self._reviewer_denials == _REVIEWER_TRIP
                if tripped:
                    # (a) The breaker must never trip silently (owner catch 2026-08-24):
                    # persist a notice so reloads see it too.
                    self._append_notice("reviewer_paused", _REVIEWER_PAUSED_TEXT)
                yield Event(
                    EventType.TOOL_FINISHED,
                    {
                        "name": tool_call.name,
                        "status": "denied",
                        "reason": "blocked by the safety reviewer",
                        "reviewer_reason": verdict.reason,
                        "allow_anyway": True,
                        **({"reviewer_paused": _REVIEWER_PAUSED_TEXT} if tripped else {}),
                    },
                )
                deny_msg = _tool_error_message(tool_call, self.reviewer_denial_message or AGENT_DENY_MESSAGE)
                deny_msg["_display"] = {
                    "approval_origin": "reviewer_denied",
                    "approval_note": verdict.reason,
                }
                self.messages.append(deny_msg)
                self._audit(
                    tool_call,
                    stage="finished",
                    status="denied",
                    reason=f"denied by reviewer: {verdict.reason}",
                )
                yield False
                return
            # "unsure" falls through to today's card — the human decides.
            if verdict.verdict == "unsure":
                self._reviewer_denials = 0  # streak semantics: any non-deny resets
                unsure_note = verdict.reason

        if not allowed and decision.needs_user:
            escalation = (
                {"kind": "human_required", "reason": decision.reason} if decision.human_only else
                {"kind": "reviewer_unsure", "reason": unsure_note} if unsure_note else
                {"kind": "reviewer_unavailable", "reason": ""} if self.permissions.mode is Mode.AUTO_APPROVE else None
            )
            # 影子评估：记录审查器会如何评价这张卡片。若实时路径已经咨询过审查器，则跳过
            # （`unsure` 落到卡片时已经以 reviewer_verdict 审计，避免重复花费）。
            # Shadow evaluation: record what the reviewer would have said about this card.
            # Skipped when the live path already consulted it (an `unsure` falling through
            # to the card is already audited as reviewer_verdict — no double spend).
            if not consulted_live:
                self._spawn_shadow_review(tool_call)
            yield Event(
                EventType.PERMISSION_REQUIRED,
                {
                    "name": tool_call.name,
                    "arguments": tool_call.arguments,
                    "reason": decision.reason,
                    "escalation": escalation,
                    # `unsure` 裁决触发了这张卡片：审查器的一句话理由就地回答
                    # “为什么要问我？”（owner ask 2026-08-24）。
                    # An `unsure` verdict raised this card: the reviewer's one-line reason
                    # answers "why am I being asked?" in place (owner ask 2026-08-24).
                    **(
                        {"reviewer_unsure": verdict.reason}
                        if consulted_live and verdict.verdict == "unsure"
                        else {}
                    ),
                    "category": getattr(metadata, "category", ""),
                    # 长期规则可以固定的精确目标；若调用不符合条件（无声明 target 参数 / exec 风险），
                    # 则为 None。界面仅在自动化运行的审批卡上据此提供“Allow every time”。
                    # OPE-114 §1：这是审查器和人都无法仅从命令文本中获知的事实。
                    # The exact target a standing rule could pin, or None when the call
                    # isn't eligible (no declared target arg / exec risk). Surfaces use it
                    # to offer "Allow every time" on automation-run approval cards only.
                    # OPE-114 §1: the fact neither the reviewer nor the human could get
                    # from the command text alone.
                    "provenance": provenance_note,
                    "standing_target": standing_rule_candidate(
                        tool_call.name,
                        tool_call.arguments,
                        metadata,
                        self.permissions.risk_overrides,
                    ),
                    # 当该 shell 命令被分类为只读时为 True；卡片只有这时才提供
                    # “Allow read-only commands for this session”。
                    # True when this shell command classifies as read-only — the card
                    # offers "Allow read-only commands for this session" only then.
                    "readonly_ok": _readonly_ok(tool_call.arguments),
                    # OPE-136 finding 4：MCP 调用实际发往何处，由注册时 (mcp/tools.py)
                    # 根据 server def 盖章；这样卡片范围标签能说“leaves this computer -> host”，
                    # 而不是笼统的“stays on this computer”。非 MCP 工具为 None。
                    # OPE-136 finding 4: where an MCP call actually goes, stamped at
                    # registration (mcp/tools.py) from the server def — so the card's
                    # scope chip can say "leaves this computer → host" instead of the
                    # catch-all "stays on this computer". None for non-MCP tools.
                    **(
                        {"mcp_destination": dest}
                        if (
                            dest := getattr(
                                spec.func, "__coworker_mcp_destination__", None
                            )
                            if spec
                            else None
                        )
                        else {}
                    ),
                    **(
                        self.approval_extras(tool_call.name, tool_call.arguments)
                        if self.approval_extras
                        else {}
                    ),
                },
            )
            self._audit(
                tool_call,
                stage="approval_requested",
                reason=decision.reason,
                call_id=tool_call.id,
            )
            outcome = await self._wait_tool(tool_call,
                self.approver(
                    PermissionRequest(
                        tool_name=tool_call.name,
                        arguments=tool_call.arguments,
                        metadata=metadata,
                        reason=decision.reason,
                        tool_call_id=tool_call.id,
                        escalation=escalation,
                        provenance=provenance_note,
                        mcp_destination=(
                            getattr(spec.func, "__coworker_mcp_destination__", None)
                            if spec
                            else None
                        ),
                    )
                ),
                interrupted=ApprovalOutcome.DENY,
            )
            if outcome is ApprovalOutcome.SUPERSEDED:
                result = {
                    "skipped": True,
                    "reason": "The worker request was already resolved. No further action was taken.",
                }
                self._approval_origins.pop(tool_call.id, None)
                self.messages.append(self._timed_result(tool_call, result))
                self._audit(tool_call, stage="approval_resolved", call_id=tool_call.id, status="superseded", reason=result["reason"])
                self._audit(tool_call, stage="finished", status="skipped", reason=result["reason"])
                yield Event(EventType.TOOL_FINISHED, {
                    "name": tool_call.name, "status": "ok", "result_preview": json.dumps(result),
                    "superseded_worker_call": (tool_call.arguments or {}).get("call_id") if tool_call.name == "decide_worker_call" else None,
                })
                yield False
                return
            if outcome is ApprovalOutcome.DENY:
                allowed, reason = (
                    False,
                    "interrupted by user" if self._cancel.is_set() else "denied by user",
                )
                self._approval_origins[tool_call.id] = {
                    "origin": "user",
                    "grant": "deny",
                    **({"note": unsure_note} if unsure_note else {}),
                }
                self._audit(
                    tool_call,
                    stage="approval_resolved",
                    call_id=tool_call.id,
                    status="denied",
                    approval=outcome.value,
                    reason=reason,
                )
            else:
                if outcome is ApprovalOutcome.ALWAYS_TOOL:
                    self.permissions.allow_tool_for_session(tool_call.name)
                elif outcome is ApprovalOutcome.ALWAYS_COMMAND:
                    self.permissions.allow_command_for_session(
                        str(tool_call.arguments.get("command", ""))
                    )
                elif outcome is ApprovalOutcome.ALWAYS_DOMAIN:
                    self.permissions.allow_domain_for_session(
                        str(tool_call.arguments.get("url", ""))
                    )
                elif outcome is ApprovalOutcome.READONLY_SESSION:
                    self.permissions.allow_readonly_for_session()
                elif outcome is ApprovalOutcome.ALWAYS_TRUST:
                    # 持久化的逐工具信任 (OPE-136 §4)：落入用户本地 override store，
                    # 因此明天的会话也保持安静。
                    # Durable per-tool trust (OPE-136 §4): lands in the user-local
                    # override store, so tomorrow's sessions stay quiet too.
                    self.permissions.grant_trust_for_tool(tool_call.name)
                elif outcome is ApprovalOutcome.THIS_RUN:
                    # 运行内授权：随当前回答结束而失效（在 run() 中清除）。
                    # Run grant: dies with the current answer (cleared in run()).
                    self.permissions.allow_tool_for_run(tool_call.name)
                allowed, reason = True, "approved by user"
                self._approval_origins[tool_call.id] = {
                    "origin": "user",
                    "grant": outcome.value,
                    **({"note": unsure_note} if unsure_note else {}),
                }
                self._audit(
                    tool_call,
                    stage="approval_resolved",
                    call_id=tool_call.id,
                    status="approved",
                    approval=outcome.value,
                    reason=reason,
                )

        if allowed and delegated and delegated != self._delegated_context(tool_call):
            allowed, reason = False, "The worker request or its permissions changed. Request a fresh decision."

        if not allowed:
            if spec is None:
                reason = f"unknown tool: {tool_call.name}"
            err_msg = _tool_error_message(tool_call, reason)
            if tool_call.id in self._tool_timings:
                err_msg["timing"] = self._tool_timings.pop(tool_call.id)
            origin = self._approval_origins.pop(tool_call.id, None)
            if origin:
                err_msg["_display"] = {
                    "approval_origin": origin.get("origin", ""),
                    **({"approval_note": origin["note"]} if origin.get("note") else {}),
                    **({"approval_grant": origin["grant"]} if origin.get("grant") else {}),
                }
            self.messages.append(err_msg)
            yield Event(
                EventType.TOOL_FINISHED,
                {"name": tool_call.name, "status": "denied", "reason": reason},
            )
            self._audit(tool_call, stage="finished", status="denied", reason=reason)
            yield False
            return

        if spec is None:
            self.messages.append(
                _tool_error_message(tool_call, f"unknown tool: {tool_call.name}")
            )
            yield Event(
                EventType.TOOL_FINISHED,
                {"name": tool_call.name, "status": "error", "reason": "unknown tool"},
            )
            yield False
            return

        if delegated:
            self._authorized_delegates[tool_call.id] = delegated
        yield True

    def _execute_sync(self, tool_call: ToolCall) -> tuple[Any, str]:
        """执行一个已授权调用（在工作线程中运行）。

        Execute one authorized call (runs in a worker thread)."""
        started, clock = time.time(), time.monotonic()
        try:
            delegated = self._authorized_delegates.pop(tool_call.id, None)
            if delegated and delegated != self._delegated_context(tool_call):
                return {"error": "The worker request or its permissions changed before execution. Request a fresh decision."}, "error"
            return self.registry.execute(tool_call.name, tool_call.arguments), "ok"
        except Exception as exc:
            return {"error": str(exc), "error_type": type(exc).__name__}, "error"
        finally:
            self._tool_timings.setdefault(tool_call.id, {}).update(tool_started=started, tool_ms=(time.monotonic() - clock) * 1000)

    def _record_result(self, tool_call: ToolCall, result: Any, status: str) -> Event:
        spec = self.registry.get(tool_call.name)
        if (
            status == "ok" and isinstance(result, dict) and result.get("ok")
            and spec and getattr(spec.func, "__coworker_yields_turn__", False)
        ):
            self._yield_for_wake = True
        self._step += 1
        if status == "ok":
            # 只记录成功调用：抛错的写入没有在磁盘上留下可运行目标。
            # Only successful calls: a write that raised left nothing on disk to run.
            self._agent_files.record(
                tool_call.name, tool_call.arguments, result, step=self._step
            )
        # 工具结果里的 `_display` 键是用户可见元数据，智能体绝不能看到（例如 Gmail 隐私过滤器
        # 隐藏了多少命中——模型可能围绕该计数探测）。将它提升为消息 sidecar（类似 `source`），
        # 在 `_outbound_messages` 的每个 Provider 输入中剥离，但为 GUI 工具卡片持久化。
        # A `_display` key on a tool result is user-facing metadata the AGENT must
        # never see (e.g. how many gmail hits the privacy filters hid — a count
        # the model could probe around). Lift it onto the message as a sidecar
        # (like `source`), stripped from every provider feed in
        # `_outbound_messages` but persisted for the GUI's tool card.
        display: Optional[dict[str, Any]] = None
        if isinstance(result, dict) and "_display" in result:
            display = result.get("_display") or None
            result = {k: v for k, v in result.items() if k != "_display"}
        origin = self._approval_origins.pop(tool_call.id, None)
        if origin:
            # 来源信息通过与隐私计数相同的仅展示 sidecar 跨重载保留（owner ruling 2026-08-24）；
            # `_outbound_messages` 会剥离它，因此任何 Provider 都看不到。
            # Provenance survives reload via the same display-only sidecar as the privacy
            # counts (owner ruling 2026-08-24) — `_outbound_messages` strips it, so no
            # provider ever sees it.
            display = {
                **(display or {}),
                "approval_origin": origin.get("origin", ""),
                **({"approval_note": origin["note"]} if origin.get("note") else {}),
                **({"approval_grant": origin["grant"]} if origin.get("grant") else {}),
            }
        # OPE-186 变更 1：模型看到的内容（且后续每轮都会重读的内容）在这里对每个工具只裁剪一次。
        # 上面的 provenance 已记录完整结果。
        # OPE-186 change 1: what the model sees (and re-reads on every later turn) is
        # bounded here, once, for every tool. Provenance above recorded the full result.
        result = toolresult.bound_tool_result(
            result,
            max_bytes=self._tool_result_max_bytes,
            spill_dir=self._tool_result_spill_dir,
            step=self._step,
            tool_name=tool_call.name,
        )
        message = self._timed_result(tool_call, result)
        if display:
            message["_display"] = display
        self.messages.append(message)
        hidden = int((display or {}).get("hidden_by_filters") or 0)
        stripped = int((display or {}).get("hidden_fields") or 0)
        if hidden or stripped:
            # 用户能看到的带外痕迹：规则类别 + 数量，绝不包含内容。
            # The out-of-band trace the user CAN see: rule class + count, never content.
            parts = []
            if hidden:
                parts.append(f"{hidden} result(s) hidden")
            if stripped:
                parts.append(f"{stripped} field value(s) stripped")
            self._audit(
                tool_call,
                stage="filtered",
                status="hidden",
                reason=" · ".join(parts) + " by privacy filters",
            )
        self._audit(
            tool_call,
            stage="finished",
            status=status,
            result=result,
            result_preview=_preview(result),
        )
        self._note_ingestion(tool_call, status)
        rule = self._standing_notes.pop(tool_call.id, "")
        return Event(
            EventType.TOOL_FINISHED,
            {
                "name": tool_call.name,
                "status": status,
                "tool_call_id": tool_call.id,
                "result_preview": _preview(result),
                **({"display": display} if display else {}),
                **({"standing_rule": rule} if rule else {}),
                # (c) 静默来源标签，与 `_display` sidecar 持久化的字段相同。
                # (c) quiet provenance chip — same fields the `_display` sidecar persists.
                **(
                    {
                        "approval_origin": origin.get("origin", ""),
                        **({"approval_note": origin["note"]} if origin.get("note") else {}),
                        **({"approval_grant": origin["grant"]} if origin.get("grant") else {}),
                    }
                    if origin
                    else {}
                ),
            },
        )

    def _note_ingestion(self, tool_call: ToolCall, status: str) -> None:
        """记录外部内容进入了本会话以及来源。只记录事实和来源，绝不记录内容，哪怕是截断内容。

        **v1 中没有任何东西消费它。** 它的存在是为了将来把该事实提供给审查器时
        （v2, `PRV-1`），可以通过回放影子运行回答“这是否会改变裁决？”，而不是重新争论。
        见 `session_facts.py` 与规范 Part 0。

        失败调用会跳过：失败的 fetch 没有带入任何内容。

        Record that outside content entered this session, and from where. The fact and
        the source only — never the content, not even truncated.

        **Nothing consumes this in v1.** It exists so that when the reviewer is eventually
        offered the fact (v2, `PRV-1`), the question "would it have changed a verdict?" can
        be answered by replaying a shadow run instead of re-argued. See
        `session_facts.py` and the spec's Part 0.

        Failed calls are skipped: a fetch that errored brought nothing in.
        """
        if self.session_facts is None or status != "ok":
            return
        spec = self.registry.get(tool_call.name)
        if not session_facts.is_ingesting(spec.metadata if spec else None):
            return
        record = self.session_facts.note(tool_call.name, tool_call.arguments)
        self._audit(tool_call, **record.to_audit())

    def _audit_usage(self, usage: Any) -> None:
        """每次模型往返写一行不含内容的审计记录（规范 §5：逐轮 usage 事件），
        使导出的 token 列真实可信；工具行过去只携带审查器自己的 token。
        stage 为 "usage"，模型放在（已清洗的）args 中。

        One content-blind audit row per model round-trip (spec §5: the per-turn usage
        event), so exported token columns are honest — tool rows only ever carried the
        reviewer's own tokens. Stage "usage", the model in the (sanitized) args."""
        if self.audit_sink is None:
            return
        try:
            self.audit_sink(
                {
                    **self.audit_context,
                    "stage": "usage",
                    "status": "ok",
                    "tool": "",
                    "arguments": {"model": self.model},
                    "tokens_in": int(getattr(usage, "input", 0) or 0),
                    "tokens_out": int(getattr(usage, "output", 0) or 0),
                    "cache_read": int(getattr(usage, "cache_read", 0) or 0),
                    "cache_write": int(getattr(usage, "cache_write", 0) or 0),
                }
            )
        except Exception:
            pass

    def _audit(self, tool_call: ToolCall, **event: Any) -> None:
        if self.audit_sink is None:
            return
        payload = {
            **self.audit_context,
            "tool": tool_call.name,
            "arguments": tool_call.arguments,
            **event,
        }
        try:
            self.audit_sink(payload)
        except Exception:
            pass

    async def _handle_items_proposal(self, tool_call: ToolCall) -> AsyncIterator[Event]:
        """[中文] 任务拆解关卡：发出拟提议的工作项，等待用户决定。
        批准后会在看板上创建这些工作项（服务端内部在审批器中创建），结果中携带它们的 id；
        拒绝则返回修改建议反馈以供重新拆解。

        The decomposition gate: emit the proposed items, await the user's decision.
        Approval creates them on the board (server-side, inside the approver) and the
        result carries their ids; rejection returns feedback for a revised split."""
        from .teams.proposals import validate_work_proposal
        args = tool_call.arguments or {}
        problem = None
        try:
            args = validate_work_proposal(args)
        except ValueError as error:
            problem = str(error)
        if problem:
            result: dict[str, Any] = {
                "approved": False,
                "error": problem,
            }
        elif self.items_approver is None:
            result = {
                "approved": False,
                "error": "item proposals aren't available in this surface",
            }
        else:
            yield Event(
                EventType.ITEMS_PROPOSED,
                {**args, "tool_call_id": tool_call.id},
            )
            self._audit(tool_call, stage="items_proposed")
            result = await self._wait_tool(tool_call,
                self.items_approver(dict(args), tool_call.id),
                interrupted={"approved": False, "error": "interrupted by user"},
            ) or {"approved": False, "error": "no response"}

        status = "ok" if result.get("approved") else "denied"
        self.messages.append(self._timed_result(tool_call, result))
        self._audit(
            tool_call,
            stage="finished",
            status=status,
            result=result,
            result_preview=_preview(result),
        )
        yield Event(
            EventType.TOOL_FINISHED,
            {
                "name": tool_call.name,
                "status": status,
                "result_preview": _preview(result),
                "tool_call_id": tool_call.id,
            },
        )

    async def _handle_connector_request(self, tool_call: ToolCall) -> AsyncIterator[Event]:
        """[中文] `request_connector`（请求人类连接某项服务）和 `grant_connector`（组长请求人类授予其某个工作节点连接器权限）—— 规范 §11.6。
        两者均为人工审批关卡：发出 CONNECTOR_REQUESTED 事件，等待带外判定并返回。
        拒绝属于正常结果，助手必须绕过该项限制继续工作并如实说明；这绝不是错误。

        `request_connector` (ask the human to connect a service) and `grant_connector`
        (a lead asks the human to give one of its workers a connector) — spec §11.6.
        Both are human gates: emit CONNECTOR_REQUESTED, await the out-of-band verdict,
        hand it back. Declining is a normal outcome the coworker must work around and
        say so; it is never an error."""
        args = tool_call.arguments or {}
        request = "grant" if tool_call.name == "grant_connector" else "connect"
        connector = str(args.get("connector", "")).strip().lower()
        worker = str(args.get("worker", "")).strip()
        reason = str(args.get("reason", "")).strip()
        if not connector or (request == "grant" and not worker):
            result: dict[str, Any] = {
                "approved": False,
                "error": "name the connector" + (" and the worker" if request == "grant" else ""),
            }
        elif self.connector_requester is None:
            result = {"approved": False, "error": "connector requests aren't available here"}
        else:
            yield Event(
                EventType.CONNECTOR_REQUESTED,
                {"request": request, "connector": connector, "worker": worker, "reason": reason},
            )
            self._audit(tool_call, stage="connector_requested", reason=reason)
            result = await self._wait_tool(tool_call,
                self.connector_requester(dict(args), tool_call.id),
                interrupted={"approved": False, "error": "interrupted by user"},
            ) or {"approved": False, "error": "no response"}
            if not result.get("approved"):
                result.setdefault(
                    "guidance",
                    "The user declined. Carry on without it and say plainly what you could not do.",
                )
        status = "ok" if result.get("approved") else "denied"
        self.messages.append(self._timed_result(tool_call, result))
        self._audit(tool_call, stage="finished", status=status, result=result, result_preview=_preview(result))
        yield Event(
            EventType.TOOL_FINISHED,
            {"name": tool_call.name, "status": status, "result_preview": _preview(result)},
        )

    async def _handle_team_proposal(self, tool_call: ToolCall) -> AsyncIterator[Event]:
        """[中文] 人员配备关卡：发出拟组建的花名册，等待用户的带外决定。
        批准后会预先生成各工作节点会话（服务端内部在审批器中创建），返回的花名册中包含 actor id，以便组长分派任务；
        拒绝则返回用户的反馈以便修改方案。

        The staffing gate: emit the proposed roster, await the user's out-of-band
        decision. Approval PRE-SPAWNS the worker sessions (server-side, inside the
        approver) and the result carries the roster with actor ids so the lead can
        assign; rejection returns the user's feedback for a revised proposal."""
        from .teams.proposals import validate_team_proposal
        from .teams.model import BoardError
        args = tool_call.arguments or {}
        problem = None
        try:
            args = validate_team_proposal(args)
            validator = getattr(self.team_approver, "validate", None)
            if validator:
                validator(args)
        except (ValueError, BoardError) as error:
            problem = str(error)
        members = args.get("members", []) if isinstance(args, dict) else []
        if problem:
            result: dict[str, Any] = {
                "approved": False,
                "error": problem,
            }
        elif self.team_approver is None:
            result = {
                "approved": False,
                "error": "team staffing isn't available in this surface",
            }
        else:
            yield Event(
                EventType.TEAM_PROPOSED,
                {
                    **args,
                    "tool_call_id": tool_call.id,
                    "members": members,
                    "enable_chat": bool(args.get("enable_chat", False)),
                },
            )
            self._audit(tool_call, stage="team_proposed")
            result = await self._wait_tool(tool_call,
                self.team_approver(dict(args), tool_call.id),
                interrupted={"approved": False, "error": "interrupted by user"},
            ) or {"approved": False, "error": "no response"}

        status = "ok" if result.get("approved") else "denied"
        message = self._timed_result(tool_call, result)
        created = None
        if result.get("approved") and result.get("team_id"):
            created = {"team_id": result["team_id"], "workers": result.get("workers") or []}
            message["_display"] = {"team_created": created}
        self.messages.append(message)
        self._audit(
            tool_call,
            stage="finished",
            status=status,
            result=result,
            result_preview=_preview(result),
        )
        yield Event(
            EventType.TOOL_FINISHED,
            {
                "name": tool_call.name,
                "status": status,
                "result_preview": _preview(result),
                "display": {"team_created": created} if created else {},
                "tool_call_id": tool_call.id,
            },
        )

    async def _handle_plan_proposal(self, tool_call: ToolCall) -> AsyncIterator[Event]:
        """[中文] 发送计划供审查，等待用户的带外决定并应用：
        批准后会将运行中的 PermissionEngine 切换出 plan（计划）模式（同一个会话继续运行，保留所有探索上下文）；
        拒绝则保持 plan 模式并返回用户反馈以便智能体进行修订。

        Emit the plan for review, await the user's out-of-band decision, and apply it:
        approval flips the live PermissionEngine out of plan mode (the same session keeps
        going, with all its exploration context); rejection keeps plan mode and returns
        the user's feedback so the agent can revise."""
        args = tool_call.arguments or {}
        plan = str(args.get("plan", ""))
        if self.permissions.mode is not Mode.PLAN:
            # 该工具始终注册（模式可在会话中途切换），但只有会话确实处于 plan 模式时，
            # 提交计划才有意义。下一步因模式而异：discuss 保持只读，因此智能体应在聊天中
            # 讲清变更；可写模式则应直接执行。
            # The tool is always registered (mode can flip mid-session), but proposing a
            # plan only means something while the session is actually in plan mode. The
            # right next step differs by mode: discuss stays read-only, so the agent
            # should talk through the change; write-capable modes should just do it.
            if self.permissions.mode is Mode.DISCUSS:
                error = (
                    "not in plan mode — this is discuss mode (read-only), so describe "
                    "the proposed changes in chat instead"
                )
            else:
                error = "not in plan mode — proceed with the work directly"
            result: dict[str, Any] = {"approved": False, "error": error}
        elif self.plan_approver is None:
            result = {
                "approved": False,
                "error": "plan approval isn't available here",
            }
        else:
            yield Event(EventType.PLAN_PROPOSED, {"plan": plan})
            self._audit(tool_call, stage="plan_proposed")
            result = await self._wait_tool(tool_call,
                self.plan_approver(dict(args), tool_call.id),
                interrupted={"approved": False, "error": "interrupted by user"},
            ) or {
                "approved": False,
                "error": "no response",
            }

        if result.get("approved"):
            # 审批器可以选择计划后的模式（"interactive" 每次写入都询问，"auto" 则按已批准计划
            # 执行而不再提示）。
            # The approver may pick the post-plan mode ("interactive" asks per write,
            # "auto" executes the approved plan without further prompts).
            try:
                self.permissions.mode = Mode(str(result.get("mode", "interactive")))
            except ValueError:
                self.permissions.mode = Mode.INTERACTIVE
            result = {
                **result,
                "mode": self.permissions.mode.value,
                "note": "plan approved — implement it now",
            }

        status = "ok" if result.get("approved") else "denied"
        self.messages.append(self._timed_result(tool_call, result))
        self._audit(
            tool_call,
            stage="finished",
            status=status,
            result=result,
            result_preview=_preview(result),
        )
        yield Event(
            EventType.TOOL_FINISHED,
            {
                "name": tool_call.name,
                "status": status,
                "result_preview": _preview(result),
            },
        )

    async def _handle_tool_request(self, tool_call: ToolCall) -> AsyncIterator[Event]:
        """[中文] 发出安装提示卡片，等待用户决定并返回结果。
        拒绝属于正常结果而非错误：结果会告知智能体寻找备用方案并公开披露能力缺失，因为悄无声息漏掉检查的安全报告远比明确告知哪些检查未执行的报告更恶劣。

        Emit the install prompt, await the user's decision, hand the outcome back.

        Declining is a normal outcome, not an error: the result tells the agent to fall back
        and disclose the gap, because a security report that quietly loses a check is worse
        than one that says which checks it couldn't run.
        """
        args = tool_call.arguments or {}
        name = str(args.get("name", "")).strip()
        reason = str(args.get("reason", ""))

        if self.tool_requester is None or not name:
            result: dict[str, Any] = {
                "installed": False,
                "error": "tool requests aren't available here",
                "guidance": (
                    "Continue without it: use a fallback check if you have one, and say in "
                    "your report which checks were degraded."
                ),
            }
        elif _toolchain.describe(name) is None:
            # 不在固定目录中：完全不展示卡片（owner-hit 2026-08-20：智能体曾把普通
            # brew/pip 安装路由到安装卡片，批准后也只会失败）。智能体有 shell 及其自身审批流；
            # 引导它走那里，而不是把问题抛给用户。
            # Not in the pinned catalog: no card at all (owner-hit 2026-08-20 — agents
            # routed ordinary brew/pip installs through the install card, which could
            # only fail after approval). The agent has a shell with its own approval
            # flow; steer it there instead of at the user.
            catalog = ", ".join(sorted(_toolchain.MANAGED))
            result = {
                "installed": False,
                "error": (
                    f"'{name}' is not in the pinned tool catalog ({catalog})."
                ),
                "guidance": (
                    "Install it yourself with the shell (brew/pip/…, subject to the "
                    "normal command approval), or continue without it and say in your "
                    "report which checks were degraded."
                ),
            }
        else:
            # 提示必须预先说明“我们”是否能安装它（该平台的固定构建）。如果对一个无法获取的工具
            # 提供 Install 卡片，就会把用户批准变成必然错误。没有元数据即视为不能安装。
            # The prompt must say up front whether WE can install this (pinned build for
            # this platform) — a card that offers Install for a tool we can't fetch turns
            # the user's approval into a guaranteed error. Absence of metadata means NO.
            info = _toolchain.describe(name)
            yield Event(
                EventType.TOOL_REQUESTED,
                {
                    "name": name,
                    "reason": reason,
                    "installable": info is not None,
                    "version": (info or {}).get("version", ""),
                    "summary": (info or {}).get("summary", ""),
                    "source": (info or {}).get("source", ""),
                },
            )
            self._audit(tool_call, stage="tool_requested", reason=reason)
            result = await self._wait_tool(tool_call,
                self.tool_requester(dict(args), tool_call.id),
                interrupted={"installed": False, "error": "interrupted by user"},
            ) or {"installed": False, "error": "no response"}
            if not result.get("installed"):
                # 卡片写着“也可以自行安装后继续”——要兑现这个承诺。用户在提示期间通过 brew 等
                # 安装工具并点击 Continue，意味着他们已经提供了该工具，而不是拒绝；在视为拒绝前
                # 先寻找用户自己的副本。
                # The card says "or install it yourself and continue" — honor it. A user
                # who brewed the tool mid-prompt and clicked Continue has PROVIDED it,
                # not declined it; find their copy before treating this as a refusal.
                found = _toolchain.resolve(name)
                if found:
                    result = {
                        "installed": True,
                        "path": found,
                        "note": (
                            "the user provided their own copy instead of the managed "
                            "install — use it from this path"
                        ),
                    }
            if not result.get("installed"):
                result.setdefault(
                    "guidance",
                    "Continue without it: use a fallback check if you have one, and say in "
                    "your report which checks were degraded.",
                )

        status = "ok" if result.get("installed") else "denied"
        self.messages.append(self._timed_result(tool_call, result))
        self._audit(
            tool_call,
            stage="finished",
            status=status,
            result=result,
            result_preview=_preview(result),
        )
        yield Event(
            EventType.TOOL_FINISHED,
            {
                "name": tool_call.name,
                "status": status,
                "result_preview": _preview(result),
            },
        )

    async def _handle_directory_request(
        self, tool_call: ToolCall
    ) -> AsyncIterator[Event]:
        """[中文] 发出授权提示，等待用户的带外决定（请求者也会将其应用于当前会话的根目录集），并将结果作为工具结果返回。
        Emit the grant prompt, await the user's out-of-band decision (which the requester also
        applies to this session's roots), and return the outcome as the tool result."""
        args = tool_call.arguments or {}
        if self.directory_requester is None:
            result: dict[str, Any] = {
                "granted": False,
                "error": "directory requests aren't available here",
            }
        else:
            yield Event(
                EventType.DIRECTORY_REQUESTED,
                {
                    "reason": str(args.get("reason", "")),
                    "path": str(args.get("path", "")),
                    "writable": bool(args.get("writable", False)),
                    # Root promotion (workspace-scratch-design.md §5): the agent asks for
                    # the folder to become the session's primary workspace — the consent
                    # card must say so, it's a different grant than a plain extra root.
                    "primary": bool(args.get("primary", False)),
                },
            )
            self._audit(
                tool_call,
                stage="directory_requested",
                reason=str(args.get("reason", "")),
            )
            result = await self._wait_tool(tool_call,
                self.directory_requester(dict(args), tool_call.id),
                interrupted={"granted": False, "error": "interrupted by user"},
            ) or {
                "granted": False,
                "error": "no response",
            }

        status = "ok" if result.get("granted") else "denied"
        self.messages.append(self._timed_result(tool_call, result))
        self._audit(
            tool_call,
            stage="finished",
            status=status,
            result=result,
            result_preview=_preview(result),
        )
        yield Event(
            EventType.TOOL_FINISHED,
            {
                "name": tool_call.name,
                "status": status,
                "result_preview": _preview(result),
            },
        )

    async def _handle_ask_user(self, tool_call: ToolCall) -> AsyncIterator[Event]:
        """[中文] 发送问题，等待用户的带外答复（在有人值守的实时会话中为内联交互，无人值守时进入收件箱 Inbox），并将答复作为工具结果返回。
        Emit the question, await the user's out-of-band answer (inline in the live session or
        from the Inbox when unattended), and return it as the tool result."""
        args = tool_call.arguments or {}
        question = str(args.get("question", "")).strip()
        # 分组形式 (OPE-51)：只有 `questions` 也是合法调用；单数 `question` 字段可以为空。
        # asker 会规范化/校验条目；这里仅判断“是否问了任何问题？”。
        # Grouped form (OPE-51): `questions` alone is a valid call — the singular field may be
        # empty. The asker normalizes/validates the entries; here only "is anything asked?".
        if not question:
            for entry in args.get("questions") or []:
                if isinstance(entry, dict) and str(entry.get("question", "")).strip():
                    question = str(entry["question"]).strip()
                    break
        if self.question_asker is None or not question:
            result: dict[str, Any] = {
                "answer": "",
                "error": (
                    "no question was asked"
                    if not question
                    else "asking isn't available here"
                ),
            }
        else:
            # asker 感知模式（有人值守 -> 实时内联提示；无人值守 -> Inbox），因此由它负责展示问题。
            # 引擎只等待答案。
            # The asker is mode-aware (attended → live inline prompt; unattended → Inbox), so it
            # owns surfacing the question. The engine just awaits the answer.
            self._audit(tool_call, stage="question_requested", reason=question)
            result = await self._wait_tool(tool_call,
                self.question_asker(dict(args), tool_call.id),
                interrupted={"answer": "", "error": "interrupted by user"},
            ) or {
                "answer": "",
                "error": "no response",
            }

        status = "ok" if (result.get("answer") or result.get("answers")) else "denied"
        if status == "ok":
            self._note_ask_replies(result, question)
        self.messages.append(self._timed_result(tool_call, result))
        self._audit(
            tool_call,
            stage="finished",
            status=status,
            result=result,
            result_preview=_preview(result),
        )
        yield Event(
            EventType.TOOL_FINISHED,
            {
                "name": tool_call.name,
                "status": status,
                "result_preview": _preview(result),
            },
        )

    def _note_ask_replies(
        self, result: dict[str, Any], question: str = ""
    ) -> None:
        """[中文] 记录用户对 ask_user 的回答以供审查器历史参考 (§8.2)，连同智能体提出的问题一同记录 ——
        向裁决者展示时明确标注为智能体编写的数据（与工具参数遵循相同的 Rule-3 纪律），以便结构化回答严格作为该问题范围内的证据（所有者裁定 2026-08-24）。
        锚定于当前存在的用户消息数量，因此无论后续会话如何继续，合并始终保持时间顺序。

        新的回答还会重置 §8.4 的拒绝连续计数：用户在场且刚给出了指示 —— 审查器理应对后续操作进行重新评估。

        Record the user's ask_user answer(s) for the reviewer's history (§8.2),
        together with the agent's question — shown to the judge explicitly framed as
        agent-authored data (same Rule-3 discipline as tool arguments), so a structured
        answer counts as evidence for exactly the question's scope (owner ruling
        2026-08-24). Anchored to the number of user messages present now, so the merge
        stays chronological however the session continues.

        A fresh answer also resets the §8.4 denial streak: the user is present and just
        gave direction — the reviewer deserves a fresh look at what follows."""
        self._reviewer_denials = 0
        anchor = sum(1 for m in self.messages if m.get("role") == "user" and not m.get("source"))
        answers = result.get("answers")
        values = (
            [str(v) for v in answers.values()]
            if isinstance(answers, dict)
            else [str(result.get("answer") or "")]
        )
        q = (question or "").strip()
        for text in values:
            text = text.strip()
            if text:
                self._ask_replies.append((anchor, text, q))

    def _inject_steering(self) -> None:
        for text, source, activity in self._steering:
            message: dict[str, Any] = {
                "role": "user",
                "content": text,
                "ts": time.time(),
            }
            if source is not None:
                message["source"] = source
            if activity:
                message["_activity"] = activity
            self.messages.append(message)
        self._steering = []

    def _outbound_messages(self) -> list[dict[str, Any]]:
        """[中文] 为提供商（Provider）准备好的 `self.messages`。提供商输入的唯一来源（见 `_astream`）。

        无条件从每条消息中剥离仅供展示的附随字段（sidecars）—— `source`, `_display`, 以及 `ts`（提供商会拒绝未知键）——
        无论是否添加 `<system-context>` 块。当 context_provider 产出非空字符串时，一个临时的 `<system-context>` 块会被追加到最后一条用户消息中。
        绝不修改 `self.messages` 本身，因此剥离字段和临时上下文块都不会被持久化或在回放中出现。

        `self.messages` prepared for the provider. The SOLE provider feed (see `_astream`).

        Every message is stripped of the display-only sidecars — `source`, `_display`, and
        `ts` — (providers reject unknown keys), unconditionally — whether or not a
        `<system-context>` block is added. When a context
        provider yields a non-empty string, an ephemeral `<system-context>` block is appended to the
        last user message. Never mutates `self.messages`, so neither the strip nor the block is
        persisted/replayed.
        """
        # 剥离仅展示用的 sidecar：`source`（连接器卡片）、`_display`（例如过滤隐藏计数）、
        # `ts`（追加时间戳）、`reasoning`（思考文本）、`usage`（token 计数）、
        # `finish_reason`（回复结束方式）和 `max_output_tokens`（发送的上限）。
        # 只复制携带这些字段的消息。整条 `notice` 消息（error/interrupted/model-switch 标记）
        # 也是仅展示用：完全丢弃。
        # Strip the display-only sidecars — `source` (connector cards), `_display`
        # (e.g. filter-hidden counts), `ts` (append-time timestamps), `reasoning`
        # (thinking text), `usage` (token counts), `finish_reason` (how the reply ended)
        # and `max_output_tokens` (the ceiling sent) — copying only messages that carry
        # one. Whole `notice` messages (error/interrupted/model-switch markers) are
        # display-only too: dropped entirely.
        _SIDECARS = (
            "source",
            "_activity",
            "timing",
            "_display",
            "ts",
            "reasoning",
            "usage",
            "finish_reason",
            "max_output_tokens",
            "reasoning_effort",
            "served_by",
            "replay",
        )
        # 自动压缩 (OPE-27)：边界前的一切都由 compacted block 表示。仅 outbound 生效——
        # 规范历史保持完整——并且 block+tail 在轮次之间字节稳定，因此 prompt caching 继续有效。
        # Auto-compaction (OPE-27): everything before the boundary is represented by the
        # compacted block. Outbound-only — the canonical history stays intact — and the
        # block+tail are byte-stable between turns, so prompt caching keeps working.
        source_messages = _compaction.apply_to_outbound(
            self.messages, self.compaction_state
        )
        # 运行时回执不进入 Provider 载荷。提醒/看板上下文是较早的智能体上下文，
        # 不是归因于新进入用户的话，也绝不是 system 指令。真实用户消息保持逐字且保持最后。
        # Runtime receipts stay out of provider payloads. Reminder/board context is
        # earlier agent context, not words attributed to the incoming user and never
        # a system instruction. Keep the actual user message verbatim and last.
        expanded = []
        for msg in source_messages:
            context = (msg.get("_activity") or {}).get("text")
            if context:
                expanded.append({"role": "assistant", "content": context})
            expanded.append(msg)
        source_messages = expanded
        out = [
            (
                # OPE-171：长度截断且没有动作的回复，在重放时用存根代替。
                # OPE-171: a length-truncated, action-free reply is replayed as a stub.
                {"role": "assistant", "content": TRUNCATION_STUB}
                if msg.get("replay") == "stub"
                else {k: v for k, v in msg.items() if k not in _SIDECARS}
                if any(s in msg for s in _SIDECARS)
                else msg
            )
            for msg in source_messages
            if msg.get("role") != "notice"
        ]
        # PDF 附件（存为 `file` parts）就在这里适配当前 ACTIVE 模型——绝不写回持久历史。
        # 因此会话中途切换模型时总会重新决策：原生 PDF 模型拿到真实文档，其余模型拿到
        # 本地文本抽取/页面图片降级结果 (pdf_support.py)。
        # PDF attachments (stored as `file` parts) are adapted to the ACTIVE model right
        # here — never in the persisted history — so a mid-session model switch always
        # re-decides: native PDF models get the real document, the rest get the local
        # text-extract/page-image fallback (pdf_support.py).
        if any(
            isinstance(p, dict) and p.get("type") == "file"
            for msg in out
            if isinstance(msg.get("content"), list)
            for p in msg["content"]
        ):
            caps = self.provider.capabilities(self.model)
            if not getattr(caps, "pdf", False):
                from . import pdf_support

                out = [
                    (
                        {
                            **msg,
                            "content": pdf_support.adapt_content(msg["content"], caps),
                        }
                        if isinstance(msg.get("content"), list)
                        else msg
                    )
                    for msg in out
                ]

        # 图片使用同样的逐轮处理：无视觉能力的模型会收到可见占位符，而不是它会拒绝的载荷。
        # 和 PDF 路径一样，每次调用都重新决策，因此会话中途切入/切出视觉模型总能得到正确处理。
        # Images get the same per-turn treatment: a model without vision receives a visible
        # placeholder instead of a payload it would reject. Like the PDF path, this re-decides
        # per call, so a mid-session switch to/from a vision model always does the right thing.
        if any(
            isinstance(p, dict) and p.get("type") == "image_url"
            for msg in out
            if isinstance(msg.get("content"), list)
            for p in msg["content"]
        ):
            caps = self.provider.capabilities(self.model)
            if not getattr(caps, "vision", False):
                placeholder = {
                    "type": "text",
                    "text": "[image attachment — not viewable by this model]",
                }
                out = [
                    (
                        {
                            **msg,
                            "content": [
                                (
                                    placeholder
                                    if isinstance(p, dict)
                                    and p.get("type") == "image_url"
                                    else p
                                )
                                for p in msg["content"]
                            ],
                        }
                        if isinstance(msg.get("content"), list)
                        else msg
                    )
                    for msg in out
                ]

        context = (
            self.context_provider() if self.context_provider is not None else ""
        ) or ""
        if not context:
            return out
        # 该块挂在最后一条用户消息上——对所有 Provider 保持同一形状。在普通聊天中它就是最新消息；
        # 在工具循环中（工具结果 role 为 "tool"），它是任务提示，也就是第一条用户消息。
        # 这只有在该块不包含会自行变化的内容时才安全。OPE-192：过去它以精确到分钟的 `Now:`
        # 开头，因此每跨一分钟就重写第一条消息；而 Provider 的 prompt cache 只能复用到第一处
        # 不同字节之前，导致整段对话被重新处理（实测：一次 Fable 5.1 尝试中 105 次调用的 16 次
        # 承担了 95% cache writes；Kimi K3 运行中跨分钟命中失败率为 60%，否则为 1.5%）。
        # 现在时间是工具 (`current_time`)。剩下的内容——文件夹、技能菜单、模式提示——只在用户改变
        # 某些东西时才变，因此 outbound 历史在轮次之间保持字节一致。临时块：绝不持久化。
        # The block rides on the LAST user message — one shape for every provider. In a
        # chat that is the newest message; in a tool loop (tool results carry role "tool")
        # it is the task prompt, message one. That is only safe because the block holds
        # nothing that moves on its own. OPE-192: it used to open with a `Now:` line to the
        # minute, so every minute crossing rewrote message one, and a provider's prompt
        # cache is reusable only up to the first byte that differs — the whole conversation
        # was re-processed (measured: 16 of 105 calls on one Fable 5.1 attempt held 95% of
        # its cache writes; on the Kimi K3 run 60% of minute crossings missed vs 1.5%
        # otherwise). The time is a tool now (`current_time`). What is left — folders,
        # skill menu, mode notices — changes only when the user changes something, so the
        # outbound history stays byte-identical turn to turn. Ephemeral: never persisted.
        block = (
            f"\n\n{EPHEMERAL_CONTEXT_OPEN}\n"
            "(automatic per-turn context, not part of the user's message)\n"
            f"{context}\n</system-context>"
        )
        for i in range(len(out) - 1, -1, -1):
            if out[i].get("role") != "user":
                continue
            msg = dict(out[i])
            content = msg.get("content")
            if isinstance(content, str):
                msg["content"] = content + block
            elif isinstance(content, list):  # content-parts (text + images)
                msg["content"] = [*content, {"type": "text", "text": block}]
            else:
                msg["content"] = block
            out[i] = msg
            break
        return out


def _effort_record(turn: AssistantTurn, setting: Optional[str]) -> Optional[dict[str, Any]]:
    """为本次回复写入运行记录的 reasoning effort 信息 (OPE-176)：优先使用 Provider
    自己报告的映射；否则如果配置了某个档位，则明确记录“已请求但未报告”，避免设置静默丢失。

    What the run record says about reasoning effort for this reply (OPE-176): the
    provider's own mapping when it reported one; otherwise, when a level was configured,
    an explicit "requested but not reported" so the setting is never silently lost."""
    if turn.effort:
        return dict(turn.effort)
    if setting:
        return {
            "requested": setting,
            "effective": None,
            "note": "provider did not report an effort parameter (no knob on this path)",
        }
    return None


def _assistant_message(
    turn: AssistantTurn, model: Optional[str] = None, effort_setting: Optional[str] = None
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": turn.text or "",
        "ts": time.time(),
    }
    if turn.usage is not None:
        # 展示/聚合用 sidecar（类似 `reasoning`）：随消息持久化，Provider 调用前剥离。
        # 标记产生它的模型，使按模型汇总在会话中途切换模型后仍准确。
        # Display/aggregation sidecar (like `reasoning`): persisted with the message,
        # stripped before provider calls. Tagged with the model that produced it so
        # per-model rollups survive mid-session model switches.
        message["usage"] = {"model": model, **turn.usage.as_dict()}
    if turn.finish_reason:
        # 回复如何结束，使用引擎规范化词汇（`stop` / `tool_calls` / `length`；
        # 未知 Provider 值原样透传）。持久化后，已保存会话无需从 token 计数重新推导，
        # 就能区分“选择停止”和“触及输出上限”(OPE-173)。部分轮次及未报告 stop reason
        # 的 Provider 会省略该字段——绝不写 null。每次 Provider 调用前剥离
        # (`_outbound_messages`)。若 Provider 原始值不同，则保存在该 Provider 的 sidecar
        # 中（例如 `_anthropic.stop_reason`）。
        # How the reply ended, in the engine's normalised vocabulary (`stop` /
        # `tool_calls` / `length`; unknown provider values pass through). Persisted
        # so a saved session can tell "chose to stop" from "hit the output limit"
        # without re-deriving it from token counts (OPE-173). Omitted — never null —
        # for partial turns and providers that report no stop reason. Stripped before
        # every provider call (`_outbound_messages`). The provider's raw value, where
        # it differs, lives in that provider's sidecar (e.g. `_anthropic.stop_reason`).
        message["finish_reason"] = turn.finish_reason
    if turn.output_limit:
        # Provider 实际发送的逐回复输出上限 (OPE-177)，因此 `length` 结束能对应到产生它的限制。
        # 与 `usage` 一样是展示 sidecar：每次 Provider 调用前剥离。
        # The per-reply output ceiling the provider actually sent (OPE-177), so a
        # `length` finish can be read against the limit that produced it. Display
        # sidecar like `usage`: stripped before every provider call.
        message["max_output_tokens"] = turn.output_limit
    effort_record = _effort_record(turn, effort_setting)
    if effort_record:
        # 本次回复的 reasoning-effort 映射 (OPE-176)。与 `usage` 一样是展示 sidecar：
        # 每次 Provider 调用前剥离。
        # The reasoning-effort mapping for this reply (OPE-176). Display sidecar like
        # `usage`: stripped before every provider call.
        message["reasoning_effort"] = effort_record
    if turn.served_by:
        # 路由器为本次回复报告的上游主机（展示 sidecar）。
        # The upstream host a router reported for this reply (display sidecar).
        message["served_by"] = turn.served_by
    if turn.reasoning:
        # 仅展示用思考文本——由 GUI 渲染，每次 Provider 调用都会剥离
        # (`_outbound_messages`)；Provider 私有重放块则通过 `extras` 传递。
        # Display-only thinking text — rendered by the GUI, stripped for every provider
        # (`_outbound_messages`); provider-private replay blocks go via `extras` instead.
        message["reasoning"] = turn.reasoning
    if turn.extras:
        # Provider 私有 sidecar（例如 `_gemini` thought signatures）随消息持久化；
        # 所属 Provider 会重新附加它们，其他 Provider 会剥离它们 (base.py)。
        # Provider-private sidecars (e.g. `_gemini` thought signatures) persist with the
        # message; the owning provider reattaches them, the rest strip them (base.py).
        message.update(turn.extras)
    if turn.tool_calls:
        message["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in turn.tool_calls
        ]
    return message


_MANGLED_PREVIEW_CHARS = 200


def _is_mangled(tool_call: ToolCall) -> bool:
    """当工具调用参数不是 JSON object 时，Provider 参数解析器会回退到
    `{"_raw": <未解析文本>}`（通常是流在参数中途被截断）。

    Provider arg-parsers fall back to `{"_raw": <unparsed text>}` when a tool call's
    arguments aren't a JSON object (typically a stream truncated mid-arguments)."""
    return set(tool_call.arguments or {}) == {"_raw"}


def _sanitize_mangled_calls(turn: AssistantTurn) -> None:
    """在轮次进入历史前，把每个损坏调用保存的 raw 文本缩成短预览。
    完整文本是垃圾（半个 JSON 文档）：重放它每轮会花费成千上万 token，更糟的是还会教模型
    `_raw` 是一种它应当模仿的真实参数形状。

    Shrink each mangled call's stored raw text to a short preview BEFORE the turn
    enters history. The full text is junk (half a JSON document): replaying it costs
    thousands of tokens per turn and, worse, teaches the model that `_raw` is a real
    parameter shape it should imitate."""
    for tc in turn.tool_calls:
        if _is_mangled(tc):
            raw = str(tc.arguments.get("_raw") or "")
            if len(raw) > _MANGLED_PREVIEW_CHARS:
                tc.arguments = {
                    "_raw": raw[:_MANGLED_PREVIEW_CHARS]
                    + f"… [unparsed tool-call text, {len(raw)} chars, truncated in history]"
                }


def _tool_result_message(tool_call: ToolCall, result: Any) -> dict[str, Any]:
    content = result if isinstance(result, str) else json.dumps(result, default=str)
    return {
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": content,
        "ts": time.time(),
    }


def _tool_error_message(tool_call: ToolCall, reason: str) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": json.dumps({"error": "tool call not executed", "reason": reason}),
        "ts": time.time(),
    }


def _preview(value: Any, max_chars: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = text.replace("\n", "\\n")
    return text if len(text) <= max_chars else text[: max_chars - 3] + "..."
