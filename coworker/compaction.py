"""[中文] 长会话历史自动压缩（OPE-27）。

当发送给模型的外发历史记录接近模型的上下文上限时，外发视图中的较早部分会被替换为：
(a) 由大语言模型生成的结构化摘要，以及 (b) 机械化提取的状态 —— 最近的若干轮次以及所有的用户消息均得以保留。
磁盘上持久化的对话记录绝不会被修改；仅改变发送给模型的内容。完整设计参见 ocw-context
docs/auto-compaction-spec.md（2026-07-28 批准）。

本模块由纯函数 + 一个数据类组成；由引擎负责决定“何时”（其运行循环）以及“用什么”（其提供商/模型），
两者均在此处注入。这种解耦使得 engine.py 中的相关代码仅占几行，并且使得每项压缩策略都可以在没有真实提供商的情况下进行单元测试。

Auto-compaction of long session histories (OPE-27).

When the outbound history approaches the model's context limit, the older portion of the
*outbound* view is replaced with (a) an LLM-written structured summary and (b) mechanically
extracted state — the recent turns and all user messages survive. The persisted transcript
is never modified; only what is sent to the model. Full design: ocw-context
docs/auto-compaction-spec.md (approved 2026-07-28).

This module is pure functions + one dataclass; the engine owns *when* (its run loop) and
*with what* (its provider/model), both injected here. That split keeps the engine.py
footprint to a few lines and makes every policy testable without a provider.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

# [中文] 触发阈值：min(threshold_pct × context_window, cap_tokens)。设置上限是为了让 1M 超长上下文模型提早压缩 ——
# 因为在达到名义上的极限之前，模型的生成质量和延迟就已经严重劣化。
# Trigger: min(threshold_pct × context_window, cap_tokens). The cap exists so 1M-context
# models compact early — quality and latency degrade well before the nominal limit.
DEFAULT_THRESHOLD_PCT = 0.8
DEFAULT_CAP_TOKENS = 250_000
# [中文] 矩阵中缺少经校验的 context_window 条目时的默认模型上下文窗口。
# Models without a verified context_window entry in the matrix.
DEFAULT_CONTEXT_WINDOW = 128_000
# [中文] 逐字完整保留的最新切片，占触发阈值的比例（按 Token 预算而非轮次数计算 —— 避免单次庞大的工具循环挤占工作集）。
# The newest slice kept verbatim, as a fraction of the trigger (a token budget, not a
# turn count — one huge tool loop shouldn't starve the working set).
KEEP_RECENT_FRACTION = 0.25
# [中文] 摘要调用本身：关闭工具调用。OPE-189：对于推理模型来说，3,000 Token 太低了 ——
# 该预算与模型的思考过程共享，在 15 个任务的试验中，Kimi K3 在 103 次摘要调用中有 60 次因为思考而耗尽了预算，
# 导致摘要本身被截断或根本未开始输出。可在每次运行中覆盖（压缩设置中的 "summary_max_tokens"）。
# The summarizer call itself: tools off. OPE-189: 3,000 was too low for a reasoning model
# — the budget is shared with the model's thinking, and in a 15-task trial Kimi K3
# spent it thinking on 60 of 103 summary calls, so the summary itself was cut off or never
# started. Overridable per run ("summary_max_tokens" in the compaction settings).
SUMMARY_MAX_TOKENS = 16_000
# [中文] 摘要是同事对该时间段的“唯一”记忆，因此仅用“非空”作为验收标准门槛太低了。
# 低于此字符数或缺少必须的章节，摘要将被拒绝并重试（OPE-189）。
# A summary is the coworker's ONLY memory of the span, so "not empty" is too low a bar to
# accept one. Below this, or missing the sections, it is refused and retried (OPE-189).
SUMMARY_MIN_CHARS = 400
# [中文] 8 个章节标题的小写片段，用于对摘要进行健全性校验。第一个是锚点：误把任务当成继续会话的回复绝不会包含它。
# Lowercase fragments of the eight section headings, used to sanity-check a summary. The
# first is the anchor: a reply that slipped into continuing the session never has it.
SUMMARY_SECTION_MARKERS = (
    "primary request",
    "key concepts",
    "artifacts and files",
    "errors and fixes",
    "user messages",
    "pending tasks",
    "current work",
    "next step",
)
# [中文] 为摘要器渲染时间段跨度时对单条消息的裁剪长度；工具结果是最先被裁剪的
# （因为体积巨大且大多已过时 —— 40 轮前读取的文件不如重新读取）。
# Per-message clip when rendering the span for the summarizer; tool results are the
# first casualty (huge and mostly stale — a file read 40 turns ago is better re-read).
_SPAN_TOOL_RESULT_CLIP = 400
_SPAN_BUDGET_CHARS = 400_000
# [中文] 在压缩块中机械保留的用户消息。我们不能简单地全部保留 —— 否则列表会无限追加，
# 压缩块会逐渐蚕食刚刚释放的上下文窗口 —— 但 OPE-189 显示旧规则的两半（每条截断至 600 字符，
# 保留最新的 40 条）都容易丢失最关键的那条消息。智能体会话的“第一条”用户消息是其根本任务委派指令，
# 且永不过期，而任务陈述经常超过 600 字符：在 15 个任务的试验中，15 个任务提示词中有 8 个在半句话处被截断，
# 丢失了任务所检查的输出路径和必需字段名称。因此：完整钉住第一条消息，然后从最新消息开始按 Token 预算向后填充。
# 被丢弃的消息（现在位于中间部分）继续统计计数，且压缩块会指明缺失区间所在的位置。
# User messages preserved mechanically in the compacted block. They cannot simply all be
# kept — otherwise the list appends forever and the block slowly reclaims the window it
# freed — but OPE-189 showed both halves of the old rule (clip each to 600 chars, keep the
# newest 40) losing the one message that matters. An agent session's FIRST user message is
# its mandate and never goes stale, and a task statement routinely runs past 600 chars: on
# a 15-task trial 8 of the 15 task prompts were cut mid-sentence, dropping the output
# path and the required field names the task checks. So: pin the first message whole,
# then fill backwards from the newest with a token budget. Dropped ones (now from the
# MIDDLE) stay counted, and the block says where the gap is.
_USER_PIN_MAX_TOKENS = 4_000  # [中文] 即使粘贴大段开篇消息也不会撑破压缩块 / a pasted opening message still can't blow the block
_USER_BUDGET_FRACTION = 0.08  # [中文] 占压缩触发阈值的比例，因此降低触发阈值时也会等比缩放 / of the compaction trigger, so a lowered trigger scales too
_USER_BUDGET_MIN = 2_000
_USER_BUDGET_MAX = 20_000  # [中文] 默认触发阈值下 8% 对应的大小 / what 8% comes to at the default trigger
_TRIM_FRACTION = 0.10


# -- token math ---------------------------------------------------------------


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """chars/4 over the serialized messages — the fallback signal for providers that
    never report usage (documented in the metering code)."""
    total = 0
    for msg in messages:
        try:
            total += len(json.dumps(msg, default=str))
        except (TypeError, ValueError):
            total += len(str(msg))
    return total // 4


def trigger_tokens(
    context_window: Optional[int],
    *,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    cap_tokens: int = DEFAULT_CAP_TOKENS,
) -> int:
    window = context_window or DEFAULT_CONTEXT_WINDOW
    return min(int(threshold_pct * window), int(cap_tokens))


def should_compact(
    signal: int,
    context_window: Optional[int],
    *,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    cap_tokens: int = DEFAULT_CAP_TOKENS,
) -> bool:
    return signal >= trigger_tokens(
        context_window, threshold_pct=threshold_pct, cap_tokens=cap_tokens
    )


# -- state --------------------------------------------------------------------


@dataclass
class CompactionState:
    """[中文] 单个压缩点。`boundary_index` 是标准消息列表中的索引：
    其之前的消息在外发视图中由压缩块代表；从其开始的消息则逐字完整发送。
    随会话持久化存储，以便重新加载时保留压缩视图。

    One compaction point. `boundary_index` is an index into the CANONICAL message list:
    messages before it are represented by the compacted block in the outbound view; messages
    from it on are sent verbatim. Persisted with the session so reloads keep the view."""

    boundary_index: int
    summary_text: str
    working_state: str
    user_messages: list[str] = field(default_factory=list)
    # [中文] 在本会话的所有压缩过程中，因 _USER_MESSAGES_MAX 上限而丢弃的历史较早用户消息数 ——
    # 保证压缩块中“省略了较早的 N 条”数字准确诚实。
    # How many older user messages were dropped by the _USER_MESSAGES_MAX cap, across
    # all compactions of this session — keeps the block's "N earlier omitted" honest.
    user_messages_dropped: int = 0
    created_at: float = 0.0
    model_used: str = ""
    trimmed: bool = False  # [中文] 当该状态来自无摘要的裁剪回退时为 True / True when this state came from the no-summary trim fallback
    # [中文] OPE-186 改动 3：压缩轮次的逐字完整转录记录所写入的文件路径
    # （当引擎无处写入时为空）。在压缩块中注明该路径，以便模型可以回读摘要所丢弃的任何细节。
    # OPE-186 change 3: where the verbatim transcript of the compacted turns was written
    # (empty when the engine had nowhere to write it). Named in the compacted block so the
    # model can read back anything the summary dropped.
    transcript_path: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "boundary_index": self.boundary_index,
            "summary_text": self.summary_text,
            "working_state": self.working_state,
            "user_messages": list(self.user_messages),
            "user_messages_dropped": self.user_messages_dropped,
            "created_at": self.created_at,
            "model_used": self.model_used,
            "trimmed": self.trimmed,
            "transcript_path": self.transcript_path,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["CompactionState"]:
        if not isinstance(raw, dict) or "boundary_index" not in raw:
            return None
        return cls(
            boundary_index=int(raw.get("boundary_index", 0)),
            summary_text=str(raw.get("summary_text", "")),
            working_state=str(raw.get("working_state", "")),
            user_messages=[str(u) for u in raw.get("user_messages") or []],
            user_messages_dropped=int(raw.get("user_messages_dropped", 0)),
            created_at=float(raw.get("created_at", 0.0)),
            model_used=str(raw.get("model_used", "")),
            trimmed=bool(raw.get("trimmed", False)),
            transcript_path=str(raw.get("transcript_path", "") or ""),
        )


# -- boundary -----------------------------------------------------------------


def _turn_starts(messages: list[dict[str, Any]], *, start: int) -> tuple[list[int], list[int]]:
    """[中文] `start` 之后的候选边界索引：用户消息索引（轮次起始点，优先）以及助手索引
    （迭代起始点 —— 合法的后缀头部；`tool` 消息绝不能作为外发视图的开头）。

    Candidate boundary indexes past `start`: user-message indexes (turn starts,
    preferred) and assistant indexes (iteration starts — legal suffix heads; a `tool`
    message must never head the outbound view)."""
    users, assistants = [], []
    for i in range(start, len(messages)):
        role = messages[i].get("role")
        if role == "user":
            users.append(i)
        elif role == "assistant":
            assistants.append(i)
    return users, assistants


def pick_boundary(messages: list[dict[str, Any]], *, keep_tokens: int) -> Optional[int]:
    """[中文] 逐字保留尾部的标准起始索引：其后缀能够容纳在保留预算内的最早轮次起始点。
    优先选择用户消息边界；当最新单轮本身就超过预算时（巨大的工具调用循环），回退到迭代（助手）边界。
    当没有具有实质意义的内容可供总结时返回 None。

    The canonical index where the verbatim tail begins: the earliest turn start whose
    suffix fits the keep budget. Prefers user-message boundaries; falls back to iteration
    (assistant) boundaries when the newest turn alone exceeds the budget (a giant tool
    loop). None when there is nothing meaningful to summarize."""
    start = 1 if messages and messages[0].get("role") == "system" else 0
    users, assistants = _turn_starts(messages, start=start)

    def _fit(candidates: list[int]) -> Optional[int]:
        for i in candidates:  # earliest-first: keep as much verbatim as fits
            if estimate_tokens(messages[i:]) <= keep_tokens:
                return i
        return None

    boundary = _fit(users)
    if boundary is None and users:
        # The newest user turn alone blows the budget — cut inside it at an iteration
        # boundary, keeping at least the most recent assistant step.
        inside = [i for i in assistants if i > users[-1]]
        boundary = _fit(inside)
        if boundary is None:
            boundary = inside[-1] if inside else users[-1]
    if boundary is None:
        boundary = _fit(assistants) or (assistants[-1] if assistants else None)
    # A boundary at (or before) the first real message summarizes nothing — skip.
    if boundary is None or boundary <= start:
        return None
    return boundary


# -- mechanical extraction (no LLM — zero hallucination risk) -----------------

_WRITE_HINTS = ("write", "edit", "append", "save", "create", "patch")
_ARTIFACT_HINTS = ("artifact", "publish", "deploy")


def _iter_tool_calls(span: list[dict[str, Any]]):
    """(name, args, result_content) for every tool call in the span, in order."""
    results = {
        m.get("tool_call_id"): m.get("content")
        for m in span
        if m.get("role") == "tool"
    }
    for msg in span:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (ValueError, TypeError):
                args = {}
            yield str(fn.get("name") or ""), args, results.get(tc.get("id"))


def _result_status(result: Any) -> str:
    if not isinstance(result, str):
        return ""
    try:
        parsed = json.loads(result)
    except (ValueError, TypeError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    if parsed.get("error"):
        return "error"
    if "exit_code" in parsed:
        code = parsed.get("exit_code")
        return "ok" if code in (0, "0") else f"exit {code}"
    return ""


def extract_working_state(span: list[dict[str, Any]]) -> str:
    """[中文] 由代码通过时间段内的工具调用记录机械提取并附加到摘要末尾的状态块：
    写入的文件、最近的命令（+ 退出状态）、生成的工件、使用的工具。

    The mechanical block appended to the summary by CODE, from the span's tool-call
    records: files written, recent commands (+ exit status), artifacts, tools used."""
    files: list[str] = []
    commands: list[str] = []
    artifacts: list[str] = []
    tools: list[str] = []
    for name, args, result in _iter_tool_calls(span):
        if name and name not in tools:
            tools.append(name)
        lowered = name.lower()
        path = args.get("path") or args.get("file_path")
        if path and any(h in lowered for h in _WRITE_HINTS):
            files.append(str(path))
        if lowered == "run_shell" and args.get("command"):
            status = _result_status(result)
            line = " ".join(str(args["command"]).split())[:160]
            commands.append(f"{line}" + (f"  [{status}]" if status else ""))
        if any(h in lowered for h in _ARTIFACT_HINTS):
            location = args.get("url") or args.get("path") or args.get("title")
            if location:
                artifacts.append(str(location))

    def _dedupe_recent_first(items: list[str], limit: int) -> list[str]:
        seen: list[str] = []
        for item in reversed(items):  # most recent first
            if item not in seen:
                seen.append(item)
            if len(seen) >= limit:
                break
        return seen

    lines = ["## Working state (extracted mechanically from tool records)"]
    written = _dedupe_recent_first(files, 20)
    if written:
        lines.append("Files written/edited (most recent first):")
        lines += [f"- {p}" for p in written]
    recent_cmds = commands[-10:]
    if recent_cmds:
        lines.append("Recent shell commands:")
        lines += [f"- {c}" for c in recent_cmds]
    made = _dedupe_recent_first(artifacts, 10)
    if made:
        lines.append("Artifacts produced:")
        lines += [f"- {a}" for a in made]
    if tools:
        lines.append("Tools used in the summarized span: " + ", ".join(sorted(tools)))
    return "\n".join(lines) if len(lines) > 1 else ""


def _text_of(content: Any) -> str:
    """A message's text, whether plain or content-parts (images become a placeholder)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(str(p.get("text", "")))
            elif isinstance(p, dict) and p.get("type") == "image_url":
                parts.append("[image]")
        return "\n".join(parts)
    return "" if content is None else str(content)


def extract_user_messages(span: list[dict[str, Any]]) -> list[str]:
    """[中文] 时间跨度内的所有用户消息，按时间顺序逐字保留（空白字符已规范化）。
    通过机械提取保留 —— 尽管提示词也要求摘要生成器列出它们，但用户的原话是真实意图的事实来源，
    绝不能依赖大模型是否记得包含它们。
    按预算裁剪是 `fit_user_messages` 的职责而非本函数：此处不进行任何截断，因此调用方始终拥有真实文本。

    Every user message in the span, chronological, VERBATIM (whitespace normalized).
    Preserved mechanically — the summarizer is also asked to list them, but user words are
    the ground truth of intent and must not depend on an LLM remembering to include them.
    Fitting them to a budget is `fit_user_messages`'s job, not this one's: nothing is
    clipped here, so the caller always has the real text to work from."""
    out: list[str] = []
    for msg in span:
        if msg.get("role") != "user":
            continue
        text = " ".join(_text_of(msg.get("content")).split())
        if text:
            out.append(text)
    return out


def user_message_budget(trigger: int) -> int:
    """[中文] 保留用户消息的 Token 预算：占压缩触发阈值的比例，并限制上下界。
    固定的固定预算在两端都是错误的 —— 针对 60,000 的触发阈值分配 20,000 Token 将耗掉压缩刚释放空间的三分之一。

    Token budget for the preserved user messages: a fraction of the compaction trigger,
    bounded. A FIXED budget would be wrong at both ends — 20,000 tokens against a 60,000
    trigger spends a third of the space compaction just freed."""
    return max(_USER_BUDGET_MIN, min(_USER_BUDGET_MAX, int(_USER_BUDGET_FRACTION * trigger)))


def _clip_to_tokens(text: str, tokens: int) -> str:
    limit = max(1, tokens) * 4  # the chars/4 estimate used everywhere in this module
    return text if len(text) <= limit else text[: limit - 1] + "…"


def fit_user_messages(
    messages: list[str],
    *,
    prior_dropped: int,
    budget_tokens: int,
    pin_tokens: int = _USER_PIN_MAX_TOKENS,
) -> tuple[list[str], int]:
    """[中文] 拟合保留列表：第一条消息完整钉住保留，然后由最新的消息向后填充至 `budget_tokens` 上限。
    返回 (保留的消息列表, 历史被丢弃的消息累计总数)。

    钉住第一条消息是核心关键所在。第一条用户消息阐明了会话为何存在 —— 任务目标、规范、固定约束 ——
    不同于普通的聊天闲聊（意图会漂移且最新一轮才是实时需求），智能体会话的开篇消息直到最后一轮都是核心支柱。
    纯粹以最新优先的预算最终会将其挤出，而这正是旧版“保留最新40条”规则悄悄做出的错误举动。

    The preserved list: the FIRST message pinned whole, then the newest filling
    `budget_tokens` backwards. Returns (kept, running total ever dropped).

    Pinning is the point. The first user message states why the session exists — the task,
    the spec, the standing constraints — and unlike a chat, where intent drifts and the
    newest turns are the live ask, an agent session's opening message stays load-bearing to
    the last turn. A pure newest-first budget would eventually push it out,
    which is exactly what the old newest-40 rule did silently.
    """
    if not messages:
        return [], prior_dropped
    kept_first = _clip_to_tokens(messages[0], pin_tokens)
    rest = messages[1:]
    if not rest:
        return [kept_first], prior_dropped
    tail: list[str] = []
    spent = 0
    for text in reversed(rest):
        cost = max(1, len(text) // 4)
        if tail and spent + cost > budget_tokens:
            break
        # [中文] 最新的消息即使本身超出预算也必须保留 —— 裁剪以适应大小，但绝不丢弃：
        # 因为这是用户实际所说的最新一句话。
        # The newest message is kept even if it alone exceeds the budget — clipped to fit,
        # never dropped: it is the most recent thing the user actually said.
        tail.append(_clip_to_tokens(text, budget_tokens))
        spent += cost
    tail.reverse()
    return [kept_first] + tail, prior_dropped + (len(rest) - len(tail))


# -- summarizer ---------------------------------------------------------------

SUMMARY_SYSTEM_PROMPT = """You are compacting an AI coworker's session history so the coworker can continue working in a smaller context. Write a structured summary of the conversation below. It is the coworker's ONLY memory of these turns, so preserve everything load-bearing.

Produce ALL of the following sections, in this order, each as a markdown heading:

1. **Primary request and intent** — what the user is trying to get done, in their terms, including standing constraints stated at any point (e.g. "never send without my approval"). Constraints outlive the turns they were stated in.
2. **Key concepts and decisions** — domain facts, technical choices, and rationale established so far. Include the WHY, not just the what — a decision without its reason gets relitigated.
3. **Artifacts and files** — every file/deliverable created, modified, or read that still matters: path, its role, and a short excerpt of load-bearing content only.
4. **Errors and fixes** — problems hit and how they were resolved, including user corrections ("no, do it this way") — those are feedback with lasting force.
5. **All user messages** — a chronological list of every user message (trimmed of pasted bulk). This is the intent audit-trail.
6. **Pending tasks** — explicitly incomplete items, promised follow-ups, things the user said "later" about.
7. **Current work** — precisely what was in progress at this point: which step, which file, what state.
8. **Next step** — the immediate next action, justified by the user's request.

Rules:
- Do NOT carry full file contents as truth. Note THAT a file was read/edited; the coworker re-reads if it needs the content again. Stale memory of a file is worse than no memory.
- Be concrete: paths, names, commands, ids — not vague references.
- Output only the summary sections, no preamble."""

# [中文] OPE-189：上述指令约 1,800 字符；随后的对话记录有数万字符，且往往停留在会话发生时的任意状态 ——
# 通常是一个原始的工具调用结果。因此模型在生成之前最后阅读的内容是任务中途的同事发言，
# 从而模型顺着那个口吻继续对话而非进行总结：在该试验的 16 次简短摘要中，有 13 次生成了下一步操作的句子
# （“输出被截断了。让我重新运行剩余的检查。”），每次都在仍有数千 Token 预算未用的情况下主动停下。
# 因此，指令在对话记录之后以加粗形式再次重申，并给出了明确的起始首行。
# 此处利用了模型的“近因效应”，这也是为什么重复提示而非直接替换系统提示词的原因。
# OPE-189: the instruction above is ~1,800 chars; the transcript that follows it is tens of
# thousands, and it ends wherever the session happened to be — usually on a raw tool result.
# The last thing the model read before generating was therefore the coworker mid-task, and
# it continued that voice instead of summarizing: 13 of 16 short summaries in that trial were a
# next-action sentence ("The output got truncated. Let me re-run the remaining checks."),
# each stopping voluntarily with thousands of tokens of budget unused. So the instruction is
# restated AFTER the transcript, in bold, with an explicit first line to start from. Recency
# is doing the work here, which is why this repeats rather than replaces the system prompt.
SUMMARY_TAIL_INSTRUCTION = """**--- END OF TRANSCRIPT ---**

**The transcript above is a session you are summarizing. It is NOT a conversation you are part of. Do not continue it, do not answer it, do not take its next action.**

**Write the structured summary now. Produce all eight sections, in this order, each as a markdown heading:**

**1. Primary request and intent — 2. Key concepts and decisions — 3. Artifacts and files — 4. Errors and fixes — 5. All user messages — 6. Pending tasks — 7. Current work — 8. Next step**

**Begin your reply with the line `## 1. Primary request and intent` and write nothing before it.**"""

CONTINUATION_CONTRACT = (
    "Continue where you left off: pick up the current work and next step exactly as "
    "described. Do not re-ask answered questions, do not recap, do not mention that the "
    "context was compacted. If you need the contents of a file noted above, re-read it."
)


def _render_span(span: list[dict[str, Any]], *, budget_chars: int = _SPAN_BUDGET_CHARS) -> str:
    """[中文] 将待总结时间段渲染为提供给摘要器的紧凑文本。工具结果会被大力截断（首要裁剪对象）；
    若整体渲染依然超出预算，则丢弃最早的行 —— 最新的上下文最具支撑价值。

    The summarized span as compact text for the summarizer. Tool results are clipped
    hard (first casualty); if the whole render still exceeds the budget, oldest lines are
    dropped — the newest context is the most load-bearing."""
    lines: list[str] = []
    for msg in span:
        role = msg.get("role")
        if role == "system":
            continue
        if role == "notice":
            continue
        if role == "tool":
            text = _text_of(msg.get("content"))
            text = " ".join(text.split())
            if len(text) > _SPAN_TOOL_RESULT_CLIP:
                text = text[: _SPAN_TOOL_RESULT_CLIP - 1] + "…"
            lines.append(f"[tool result] {text}")
            continue
        text = _text_of(msg.get("content"))
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                args = " ".join(str(fn.get("arguments", "")).split())
                if len(args) > 200:
                    args = args[:199] + "…"
                lines.append(f"[assistant → {fn.get('name')}] {args}")
            if text:
                lines.append(f"[assistant] {text}")
        elif role == "user":
            lines.append(f"[user] {text}")
    rendered = "\n".join(lines)
    if len(rendered) > budget_chars:
        rendered = "(…oldest turns elided…)\n" + rendered[-budget_chars:]
    return rendered


def summarizer_messages(
    span: list[dict[str, Any]], *, prior_summary: str = ""
) -> list[dict[str, Any]]:
    """[中文] 为摘要调用准备的符合提供商格式的消息列表。在连续多次压缩时，
    上一次的摘要作为新时间跨度的第 0 条消息 —— 与之后的轮次一并进行总结。

    The provider-ready messages for the summarizer call. On repeated compaction the
    previous summary is message zero of the new span — summarized along with the turns
    since."""
    body = _render_span(span)
    if prior_summary:
        body = (
            "[previous compaction summary — fold its still-relevant content into the new "
            "summary]\n" + prior_summary + "\n\n[conversation since]\n" + body
        )
    # Delimited, then the instruction restated last — see SUMMARY_TAIL_INSTRUCTION.
    user = (
        "**--- BEGIN TRANSCRIPT TO SUMMARIZE ---**\n\n"
        + body
        + "\n\n"
        + SUMMARY_TAIL_INSTRUCTION
    )
    return [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def summarize_span(
    provider: Any,
    model: str,
    span: list[dict[str, Any]],
    *,
    prior_summary: str = "",
    max_tokens: int = SUMMARY_MAX_TOKENS,
) -> str:
    """[中文] 单次摘要器模型往返（阻塞式 —— 引擎在事件循环外运行它）。工具已被禁用；
    “设置”中的模型覆盖项只是传入不同的 `model` ID。在提供商调用失败或生成不可用摘要时抛出异常 ——
    由调用方决定重试或裁剪策略。

    One summarizer round-trip (blocking — the engine runs it off-loop). Tools are
    disabled; the Settings model override is just a different `model` id. Raises on
    provider failure or an UNUSABLE summary — the caller owns the retry/trim policy."""
    turn = provider.complete(
        model=model,
        messages=summarizer_messages(span, prior_summary=prior_summary),
        tools=None,
        max_tokens=max_tokens,
    )
    text = (getattr(turn, "text", None) or "").strip()
    problem = summary_quality_problem(text)
    if problem:
        raise RuntimeError(f"summarizer returned an unusable summary: {problem}")
    return text


def summary_quality_problem(text: Optional[str]) -> Optional[str]:
    """[中文] 检查为何该摘要无法代表该时间跨度；若合格则返回 None。

    OPE-189：旧的检验标准仅要求“非空”，导致一个 61 字符的短句被当作同事对过去 75 轮对话的全部记忆。
    现在的校验故意保持粗粒度 —— 一个仅仅是内容单薄的摘要依然好过回退到无摘要裁剪 ——
    重点针对实际观察到的两种异常形态：完全为空，以及用同事口吻生成的下一步行动单句（这种回答绝不会带章节标题）。

    Why this summary can't stand in for the span, or None if it can.

    OPE-189: the old bar was "not empty", which accepted a 61-character sentence as the
    coworker's whole memory of 75 turns. The checks are deliberately coarse — a summary
    that is merely thin still beats falling back to the no-summary trim — and aimed at the
    two shapes actually observed: nothing at all, and a next-action sentence in the
    coworker's voice (which never carries the section headings).
    """
    stripped = (text or "").strip()
    if not stripped:
        return "empty"
    if len(stripped) < SUMMARY_MIN_CHARS:
        return f"too short ({len(stripped)} chars, minimum {SUMMARY_MIN_CHARS})"
    low = stripped.lower()
    if SUMMARY_SECTION_MARKERS[0] not in low:
        return f"does not open the sections (no '{SUMMARY_SECTION_MARKERS[0]}' heading)"
    found = sum(1 for marker in SUMMARY_SECTION_MARKERS if marker in low)
    if found * 2 < len(SUMMARY_SECTION_MARKERS):
        return f"only {found} of {len(SUMMARY_SECTION_MARKERS)} sections present"
    return None


# -- building + applying a compaction -----------------------------------------


def build_state(
    messages: list[dict[str, Any]],
    *,
    provider: Any,
    model: str,
    keep_tokens: int,
    prior: Optional[CompactionState] = None,
    summary_max_tokens: int = SUMMARY_MAX_TOKENS,
    user_budget_tokens: int = _USER_BUDGET_MAX,
) -> Optional[CompactionState]:
    """[中文] 将选定边界之前的所有旧内容总结为一个新的 CompactionState。
    在多次连续压缩时，先前的摘要将作为新时间跨度的开头。没有内容需要压缩时返回 None；
    摘要器失败时抛出异常（由调用方执行重试/回退策略）。

    Summarize everything older than the picked boundary into a new CompactionState.
    On repeated compaction the prior summary heads the new span. Returns None when there
    is nothing to compact; raises when the summarizer fails (caller applies policy)."""
    boundary = pick_boundary(messages, keep_tokens=keep_tokens)
    if boundary is None or (prior is not None and boundary <= prior.boundary_index):
        return None
    span_start = prior.boundary_index if prior is not None else 0
    span = messages[span_start:boundary]
    prior_users = list(prior.user_messages) if prior is not None else []
    summary = summarize_span(
        provider,
        model,
        span,
        prior_summary=prior.summary_text if prior is not None else "",
        max_tokens=summary_max_tokens,
    )
    users, dropped = fit_user_messages(
        prior_users + extract_user_messages(span),
        prior_dropped=prior.user_messages_dropped if prior is not None else 0,
        budget_tokens=user_budget_tokens,
    )
    return CompactionState(
        boundary_index=boundary,
        summary_text=summary,
        working_state=extract_working_state(span),
        user_messages=users,
        user_messages_dropped=dropped,
        created_at=time.time(),
        model_used=model,
    )


def trim_state(
    messages: list[dict[str, Any]],
    *,
    prior: Optional[CompactionState] = None,
    fraction: float = _TRIM_FRACTION,
    user_budget_tokens: int = _USER_BUDGET_MAX,
) -> Optional[CompactionState]:
    """[中文] 无需大模型的回退方案：将边界向前推进约 `fraction` 比例的外发消息。
    不生成摘要 —— 但机械状态块和用户消息列表（按照规范，绝不会被裁剪丢弃）是零成本提取的，
    因此模型依然能够获得确定性的状态。

    The no-LLM fallback: advance the boundary past ~`fraction` of the outbound
    messages. No summary — but the mechanical block and the user-message list (never
    trimmed away, per spec) are free, so the model still gets deterministic state."""
    start = prior.boundary_index if prior is not None else 0
    remaining = len(messages) - start
    if remaining <= 2:
        return None
    step = max(1, int(remaining * fraction))
    target = start + step
    # [中文] 落在目标位置或之后的合法后缀头部（绝不能是 tool 消息）/ Land on a legal suffix head at or after the target (never a tool message).
    boundary = None
    for i in range(target, len(messages)):
        if messages[i].get("role") in ("user", "assistant"):
            boundary = i
            break
    if boundary is None or boundary <= start or boundary >= len(messages):
        return None
    span = messages[start:boundary]
    prior_users = list(prior.user_messages) if prior is not None else []
    summary = (
        (prior.summary_text + "\n\n" if prior is not None and prior.summary_text else "")
        + "(Older turns were trimmed to fit the context window; no summary is available "
        "for them. Re-read files and re-run commands if earlier results are needed.)"
    )
    users, dropped = fit_user_messages(
        prior_users + extract_user_messages(span),
        prior_dropped=prior.user_messages_dropped if prior is not None else 0,
        budget_tokens=user_budget_tokens,
    )
    return CompactionState(
        boundary_index=boundary,
        summary_text=summary,
        working_state=extract_working_state(span),
        user_messages=users,
        user_messages_dropped=dropped,
        created_at=time.time(),
        model_used="",
        trimmed=True,
    )


def compacted_block(state: CompactionState) -> str:
    """[中文] 代表边界前所有内容的单个外发消息块。
    The single outbound message standing in for everything before the boundary."""
    parts = [
        "<compacted-history>",
        "Earlier turns of this session were compacted. The summary below is your memory "
        "of them.",
        "",
        state.summary_text,
    ]
    if state.working_state:
        parts += ["", state.working_state]
    if state.user_messages:
        parts += ["", "## User messages in the compacted span (verbatim, chronological)"]
        # [中文] 第一条消息已钉住，因此任何缺失都是中间的空缺 —— 在此处说明，否则提示看起来像是开篇请求丢失了一样。
        # The first is pinned, so any omission is a gap in the MIDDLE — say so there, or
        # the note reads as if the opening request were the thing that went missing.
        parts += [f"- {state.user_messages[0]}"]
        if state.user_messages_dropped:
            parts += [
                f"- ({state.user_messages_dropped} older user messages omitted here — "
                "their intent is covered by the summary above)"
            ]
        parts += [f"- {u}" for u in state.user_messages[1:]]
    if state.transcript_path:
        parts += [
            "",
            "The verbatim transcript of the compacted turns (every message, tool call and "
            f"tool result, without your private reasoning) is saved at {state.transcript_path}. "
            "If a detail you need is missing from the summary, read that file (read_file, "
            "or run_shell with grep / sed -n) instead of guessing.",
        ]
    parts += ["", CONTINUATION_CONTRACT, "</compacted-history>"]
    return "\n".join(parts)


def render_transcript(messages: list[dict[str, Any]], upto: int) -> str:
    """[中文] 将索引 `upto` 之前的标准消息渲染为可读的 Markdown 格式用于转录文件：
    角色、助手的可见文本及工具调用（名称 + 参数）、每个工具调用的完整结果、通知。
    私有思考侧车数据已被排除：它们是模型自己的思考而非会话事实，且体积最为庞大。

    The canonical messages before index `upto`, rendered as readable Markdown for the
    transcript file: role, the assistant's visible text and tool calls (name + arguments),
    every tool result in full, notices. Private reasoning sidecars are left out: they are
    the model's own thinking, not session facts, and they are the bulkiest part."""
    lines = [
        "# Compacted transcript",
        "",
        f"Messages 0 to {max(0, upto - 1)} of this session, verbatim, written when they were "
        "summarised into the compacted history block.",
        "",
    ]
    for i, m in enumerate(messages[:upto]):
        role = str(m.get("role") or "")
        if role == "system":
            continue
        if role == "notice":
            lines += [f"## [{i}] notice: {m.get('kind', '')}", "", str(m.get("content") or ""), ""]
            continue
        lines.append(f"## [{i}] {role}")
        content = m.get("content")
        if isinstance(content, str) and content:
            lines += ["", content]
        elif content:
            lines += ["", json.dumps(content, default=str, ensure_ascii=False)]
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") if isinstance(tc, dict) else None
            name = (fn or {}).get("name") if fn else (tc.get("name") if isinstance(tc, dict) else "")
            args = (fn or {}).get("arguments") if fn else (tc.get("arguments") if isinstance(tc, dict) else "")
            if not isinstance(args, str):
                args = json.dumps(args, default=str, ensure_ascii=False)
            lines += ["", f"tool call: {name}", "```", str(args), "```"]
        lines.append("")
    return "\n".join(lines)


def apply_to_outbound(
    messages: list[dict[str, Any]], state: Optional[CompactionState]
) -> list[dict[str, Any]]:
    """[中文] 外发视图：[系统提示词?] + 压缩块（作为一条 user 消息） + 逐字完整保留的尾部。
    标准历史记录保持不变；被总结时间段内的提供商私有侧车数据随之消失（压缩点之后合法重新开始回放链）。
    当状态为空或过期时为空操作。

    The outbound view: [system?] + the compacted block (as a user message) + the
    verbatim tail. Canonical history is untouched; provider-private sidecars in the
    summarized span vanish with it (replay chains legally restart after a compaction
    point). No-op when state is absent or stale."""
    if state is None:
        return messages
    boundary = state.boundary_index
    if boundary <= 0 or boundary >= len(messages):
        return messages
    head: list[dict[str, Any]] = []
    if messages and messages[0].get("role") == "system":
        head.append(messages[0])
    head.append({"role": "user", "content": compacted_block(state)})
    return head + messages[boundary:]


# -- overflow detection -------------------------------------------------------

_OVERFLOW_MARKERS = (
    "context_length_exceeded",
    "maximum context length",
    "context window",
    "prompt is too long",
    "input is too long",
    "too many tokens",
    "input length and `max_tokens` exceed",
    "exceeds the maximum number of tokens",
)


def is_context_overflow(exc: BaseException) -> bool:
    """[中文] 识别主模型返回的原始上下文溢出 400 错误（例如估算路径导致的压缩预测失误）——
    路由到压缩策略处理，而不是直接抛出给用户。

    A raw context-overflow 400 from the main model (compaction mispredicted, e.g. the
    estimate path) — routed into the compaction policy instead of surfacing."""
    text = str(exc).lower()
    return any(marker in text for marker in _OVERFLOW_MARKERS)
