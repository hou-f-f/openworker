"""[中文] `ask_user` 工具 —— 智能体向用户提问并等待回复。

通用的人机协同（Human-in-the-loop）问答原语，仿照 Claude Code 的 AskUserQuestion 设计：
包含一个问题、可选的快速回复 `options` 选项，以及（默认情况下）随时可用的自由文本输入出口 ——
另有 `multi` 参数支持多选。与 `request_directory` 类似，它会被 TurnEngine 拦截：
问题会转化为一个待办收件箱（Inbox）条目（可在实时会话中内联回答，或者在会话无人值守运行时从收件箱中回答），
智能体挂起等待直至问题解决，答案作为工具结果返回。此处的调用函数仅作为 Schema 载体与安全回退实现。

OPE-51 增强功能：选项可以是富对象（{label, description, recommended, preview}）而非纯字符串，
且 `questions` 可将最多 4 个问题合并到一次调用中（前端渲染为分步向导 stepper —— 只需一次模型往返交互，
而非多次往返）。纯字符串选项和单数形式的 `question` 仍然完全有效：旧会话和简单提问的表现与以前完全一致。

The `ask_user` tool — the agent asks the user a question and waits for the answer.

The general human-in-the-loop Q&A primitive, modelled on Claude Code's own AskUserQuestion: a
question, optional quick-reply `options`, and (by default) an always-available free-text escape —
plus `multi` for choose-several. Like `request_directory`, it's intercepted by the TurnEngine: the
question becomes an Inbox item (answerable inline in the live session, or from the Inbox when the
session runs unattended), the agent suspends until it's resolved, and the answer comes back as the
tool result. The callable here is only a schema carrier + a safe fallback.

OPE-51 upgrades: options may be rich objects ({label, description, recommended, preview}) instead
of plain strings, and `questions` groups up to 4 questions into ONE call (rendered as a stepper —
one agent round-trip instead of several). Plain-string options and the singular `question` form
stay valid: old sessions and simple asks render exactly as before.
"""

from __future__ import annotations

import json

from aisuite.agents import ToolMetadata, tool

# [中文] 单次分组调用中最多可包含的问题数量（超过此数量后分步向导标签将变得难以阅读）。
# How many questions one grouped call may carry (stepper chips get unreadable past this).
MAX_GROUPED_QUESTIONS = 4

# [中文] 选项可以是纯字符串或富对象。`label` 是用户选择的内容（也是作为答案返回的内容）；
# `description` 渲染在其下方；`recommended` 添加绿色推荐标签（推荐项请排在第一位）；
# `preview` 是在侧边栏显示的等宽字体文本（代码、配置、ASCII 原型、SQL 等任意文本；
# 当至少有一个选项具有 preview 时，卡片切换为双栏布局）。
# An option is a plain string OR a rich object. `label` is what the user picks (and what comes
# back as the answer); `description` renders under it; `recommended` adds the green tag (put the
# recommended option first); `preview` is monospace text shown in the side pane (code, config,
# ASCII mockups, SQL — any text; when ≥1 option has one the card switches to two-pane layout).
_OPTION_SCHEMA = {
    "anyOf": [
        {"type": "string"},
        {
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "description": {"type": "string"},
                "recommended": {"type": "boolean"},
                "preview": {"type": "string"},
            },
            "required": ["label"],
        },
    ]
}

# [中文] 显式 Schema（模式同 todo.py）：string-or-object 选项联合类型以及嵌套的 `questions`
# 数组无法可靠地从函数签名中自动推导。
# Explicit schema (same pattern as todo.py): the string-or-object option union and the nested
# `questions` array can't be auto-generated from the signature reliably.
_ASK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": (
            "Ask the user one or more questions and wait for their answer. Use for decisions or "
            "information only the user can provide. Do not use it to ask permission for a "
            "specific action you are about to take — propose the action instead; the approval "
            "flow shows the user exactly what would run and does the asking. Group related "
            f"questions (up to {MAX_GROUPED_QUESTIONS}) into one call via `questions` instead "
            "of asking serially."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The full question, in plain language (single-question form).",
                },
                "options": {
                    "type": "array",
                    "items": _OPTION_SCHEMA,
                    "description": (
                        "Optional quick-reply choices: plain strings, or objects with `label` "
                        "(required — this is the answer value), `description` (why/when to pick "
                        "it), `recommended` (green tag; list that option first), and `preview` "
                        "(monospace text — code, config, a mockup — shown in a side pane)."
                    ),
                },
                "allow_text": {
                    "type": "boolean",
                    "description": (
                        "Keep a free-text answer available even when options exist (default true; "
                        "the \"Other / type your own\" escape). Set false only when the options "
                        "are exhaustive."
                    ),
                },
                "multi": {
                    "type": "boolean",
                    "description": "Allow the user to pick more than one option.",
                },
                "header": {
                    "type": "string",
                    "description": "Short (≤ ~12 char) chip label for the card, e.g. \"Region\".",
                },
                "questions": {
                    "type": "array",
                    "maxItems": MAX_GROUPED_QUESTIONS,
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string"},
                            "header": {
                                "type": "string",
                                "description": (
                                    "Short (≤ ~12 char) label — names this step in the stepper "
                                    "chips and keys its answer in the result."
                                ),
                            },
                            "options": {"type": "array", "items": _OPTION_SCHEMA},
                            "allow_text": {"type": "boolean"},
                            "multi": {"type": "boolean"},
                        },
                        "required": ["question"],
                    },
                    "description": (
                        f"Grouped form: up to {MAX_GROUPED_QUESTIONS} questions asked in ONE "
                        "round-trip, rendered as a stepper. When set, the singular "
                        "question/options fields are ignored."
                    ),
                },
            },
            "required": [],
        },
    },
}


def ask_user_tool() -> object:
    def ask_user(
        question: str = "",
        options: list | None = None,
        allow_text: bool = True,
        multi: bool = False,
        header: str = "",
        questions: list | None = None,
    ) -> dict:
        """[中文] 向用户提问并等待回复 —— 当你真正需要人类做出决策或提供无法自行推断的信息时使用
        （偏好设置、缺失事实、在真实备选方案之间进行抉择）。优先使用此工具，而不是随意猜测或停滞不前。

        切勿用它来为即将执行的具体操作寻求许可（“我是否应该提交 PR？”）—— 请直接提议该操作：
        批准流程会向用户展示确切的命令/参数并进行询问，这比聊天对话中的“是的”具有更强的人工确认效力。

        单问题形式返回 `{"answer": "..."}` —— 所选选项的标签或输入的文本。
        分组形式（`questions`）返回 `{"answers": {"<header or question>": "..."}}` —— 每个问题对应一个条目。
        不要询问你可以合理自行决定的事情；将此工具留给真正需要由用户决定的选择。

        Ask the user a question and wait for their answer — use when you genuinely need a human
        decision or information you can't infer (a preference, a missing fact, a choice between real
        alternatives). Prefer this over guessing or stalling.

        Never use it to ask permission for a specific action you are about to take ("shall I
        open a PR?") — propose the action instead: the approval flow shows the user the exact
        command/arguments and does the asking, which is stronger consent than a chat yes.

        Single form returns `{"answer": "..."}` — the chosen option label(s) or the typed text.
        Grouped form (`questions`) returns `{"answers": {"<header or question>": "..."}}` — one
        entry per question. Don't ask what you can reasonably decide yourself; reserve this for
        choices that are actually the user's to make.
        """
        # [中文] 真正的处理逻辑位于引擎中（需要走带外的 Inbox 异步往返）。
        # 这里的函数体仅在未挂接 question_asker 时运行（例如无头运行界面）。
        # Real handling lives in the engine (it needs the out-of-band Inbox round-trip). This body
        # only runs if no question_asker is wired (e.g. a headless surface).
        return {
            "answer": "",
            "error": "asking the user isn't available in this surface",
        }

    wrapped = tool(
        ask_user,
        metadata=ToolMetadata(
            category="interaction",
            risk_level="low",
            capabilities=["ask_user"],
            description=(
                "Ask the user a question (free-text or multiple-choice) and wait for their answer. "
                "Use for decisions or information only the user can provide — never to ask "
                "permission for a specific action; propose the action and let the approval flow ask."
            ),
        ),
    )
    wrapped.__coworker_schema__ = _ASK_SCHEMA
    return wrapped


def normalize_option(opt) -> dict:
    """[中文] 规范化单个选项为字典格式：{label, description, recommended, preview}。
    纯字符串转为 {label: str, ...其余字段为空}。label 在各处（按钮、胶囊标签、问题解析）均兼作答案值，
    因此它始终是一个非空字符串。

    One option in canonical dict form: {label, description, recommended, preview}. Plain
    strings become {label: str, ...empty}. The label doubles as the answer value everywhere
    (buttons, pills, resolutions), so it is always a non-empty-able str."""
    if isinstance(opt, dict):
        return {
            "label": str(opt.get("label", "")),
            "description": str(opt.get("description", "")),
            "recommended": bool(opt.get("recommended", False)),
            "preview": str(opt.get("preview", "")),
        }
    return {"label": str(opt), "description": "", "recommended": False, "preview": ""}


def option_label(opt) -> str:
    """[中文] 获取字符串或字典选项对应的答案值 / 按钮文本。
    The answer value / button text for a str-or-dict option."""
    return str(opt.get("label", "")) if isinstance(opt, dict) else str(opt)


def normalize_questions(raw) -> list[dict]:
    """[中文] 规范化分组 `questions` 参数为标准格式（限制数量上限，丢弃空问题）。每个条目为：
    {question, header, options: [规范化选项], allow_text, multi}。

    The grouped `questions` arg in canonical form (capped, blanks dropped). Each entry:
    {question, header, options: [canonical option], allow_text, multi}."""
    out: list[dict] = []
    for entry in list(raw or [])[:MAX_GROUPED_QUESTIONS]:
        if not isinstance(entry, dict):
            continue
        q = str(entry.get("question", "")).strip()
        if not q:
            continue
        out.append(
            {
                "question": q,
                "header": str(entry.get("header", "")),
                "options": [normalize_option(o) for o in entry.get("options") or []],
                "allow_text": bool(entry.get("allow_text", True)),
                "multi": bool(entry.get("multi", False)),
            }
        )
    return out


def question_item_fields(args: dict) -> dict | None:
    """[中文] 根据原始 ask_user 参数生成 `InboxStore.add_question` 的关键字参数；未提问时返回 None。
    分组调用也会将其“第一个”问题作为 title/options 暴露出来，因此传统界面（群聊镜像、旧版持久化条目读取器）
    可以平滑降级为合理的单问题显示。

    `InboxStore.add_question` kwargs from raw ask_user args, or None when nothing was asked.
    A grouped call surfaces its FIRST question as title/options too, so legacy surfaces (channel
    mirrors, old persisted-item readers) degrade to a sensible single question."""
    grouped = normalize_questions(args.get("questions"))
    if grouped:
        first = grouped[0]
        return {
            "title": first["question"],
            "options": first["options"],
            "allow_text": first["allow_text"],
            "multi": first["multi"],
            "header": first["header"],
            "questions": grouped,
        }
    question = str(args.get("question", "")).strip()
    if not question:
        return None
    return {
        "title": question,
        # [中文] 字符串原样传递（简单提问保持现有的胶囊标签渲染）；
        # 富对象进行规范化，确保下游代码绝不会遇到填充不全的字典。
        # Strings pass through untouched (simple asks keep rendering as today's pills);
        # rich objects are canonicalized so downstream never meets a half-filled dict.
        "options": [
            o if isinstance(o, str) else normalize_option(o)
            for o in args.get("options") or []
        ],
        "allow_text": bool(args.get("allow_text", True)),
        "multi": bool(args.get("multi", False)),
        "header": str(args.get("header", "")),
        "questions": [],
    }


def answer_result(item_questions: list, resolution: str | None) -> dict:
    """[中文] 根据 Inbox 条目的解决结果字符串构造 ask_user 工具的返回结果。
    分组条目解析为以 header-or-question 为键的 JSON 字典字符串 → `{"answers": {...}}`；
    其余情况返回简单的 `{"answer": str}` 格式。

    Shape the ask_user tool result from an Inbox item's resolution string. Grouped items
    resolve with a JSON object string keyed by header-or-question → `{"answers": {...}}`;
    everything else returns the plain `{"answer": str}` shape."""
    if item_questions:
        try:
            parsed = json.loads(resolution or "")
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            return {"answers": {str(k): str(v) for k, v in parsed.items()}}
        if resolution:
            # [中文] 来自纯文本界面（例如镜像群聊频道）的回复：将唯一的答案归因于第一个问题，避免丢失。
            # Answered from a text-only surface (e.g. a mirrored channel): attribute the lone
            # answer to the first question rather than losing it.
            first = item_questions[0] if isinstance(item_questions[0], dict) else {}
            key = str(first.get("header") or first.get("question") or "answer")
            return {"answers": {key: str(resolution)}}
        return {"answer": ""}
    return {"answer": resolution or ""}
