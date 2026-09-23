"""[中文] 多 Inbox 路由 — 命名 inboxes + 投递绑定。

Inbox 是一个带有可选投递绑定的命名队列：应用内始终是权威记录存储；绑定也可以将条目镜像到 Slack 频道或 Telegram 聊天。
会话通过会话级覆盖项路由到某个 Inbox，其次使用 Persona 的默认值，再次使用 ``"default"``。
绑定是双向的：条目在嵌入其 id 后投递到绑定的频道，入站回复（通过该 id 关联）解析该条目 —
因此连接器/移动端只是相同条目的传输渠道。网关接线通过注入（``sender`` 可调用对象）实现，
因此该模块在不触及 Slack/Telegram 的情况下即可进行测试。

[English]
Multi-inbox routing — named inboxes + delivery bindings.

An inbox is a named queue with optional delivery binding(s): in-app is always the store of
record; a binding can also mirror items to a Slack channel or Telegram chat. Sessions route to
an inbox by a per-session override, else the persona's default, else ``"default"``. Bindings
are bidirectional: an item is delivered to the bound channel with its id embedded, and an
inbound reply (correlated by that id) resolves the item — so the connectors/mobile are just
transports of the same items. The gateway wiring is injected (a ``sender`` callable) so this
module stays testable without touching Slack/Telegram.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

DEFAULT_INBOX = "default"
# [中文] 在投递的消息中嵌入条目 id。自 Bot 更名为 OpenWorker（2026-07-22）起以 [ow:…] 发出；
# 传统的 [ocw:…] 拼写仍保持可解析，以便对重命名之前发送的消息的回复仍能正常解析。
# [English]
# Embeds the item id in a delivered message. Emitted as [ow:…] since the bot's rebrand
# to OpenWorker (2026-07-22); the legacy [ocw:…] spelling stays parseable so replies to
# messages sent before the rename still resolve.
_ID_TOKEN = re.compile(r"\[o(?:c)?w:([0-9a-f]{6,})\]")


@dataclass
class InboxBinding:
    name: str
    channel: Optional[str] = None  # [中文] None（仅应用内） | "slack" | "telegram" / [English] None (in-app only) | "slack" | "telegram"
    target: str = ""  # [中文] 绑定的频道 id / 聊天 id / [English] channel id / chat id for the binding


class InboxRouting:
    def __init__(self, path: Optional[str | Path] = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._bindings: dict[str, InboxBinding] = {
            DEFAULT_INBOX: InboxBinding(DEFAULT_INBOX)
        }
        self._persona_default: dict[str, str] = {}
        self._session_override: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if self.path and self.path.is_file():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for raw in data.get("bindings", []):
                b = InboxBinding(**raw)
                self._bindings[b.name] = b
            self._persona_default = dict(data.get("persona_default", {}))
            self._session_override = dict(data.get("session_override", {}))

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "bindings": [asdict(b) for b in self._bindings.values()],
                    "persona_default": self._persona_default,
                    "session_override": self._session_override,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    # -- config -----------------------------------------------------------------
    def set_binding(
        self, name: str, *, channel: Optional[str] = None, target: str = ""
    ) -> None:
        with self._lock:
            self._bindings[name] = InboxBinding(name, channel, target)
            self._save()

    def binding_for(self, name: str) -> InboxBinding:
        return self._bindings.get(name) or InboxBinding(name)

    def set_persona_default(self, persona_id: str, inbox_name: str) -> None:
        with self._lock:
            self._persona_default[persona_id] = inbox_name
            self._save()

    def set_session_override(self, session_id: str, inbox_name: str) -> None:
        with self._lock:
            self._session_override[session_id] = inbox_name
            self._save()

    # -- resolution -------------------------------------------------------------
    def route_for(self, session_id: str, persona_id: Optional[str] = None) -> str:
        """[中文] 每会话覆盖项 > Persona 默认值 > 全局默认 inbox。
        [English] Per-session override > persona default > the global default inbox."""
        if session_id in self._session_override:
            return self._session_override[session_id]
        if persona_id and persona_id in self._persona_default:
            return self._persona_default[persona_id]
        return DEFAULT_INBOX

    def bindings(self) -> list[dict]:
        return [asdict(b) for b in self._bindings.values()]


# -- delivery + inbound correlation ---------------------------------------------
Sender = Callable[[str, str, str], None]  # (channel, target, text) -> None


def deliver(item, binding: InboxBinding, sender: Optional[Sender]) -> bool:
    """[中文] 将 inbox 条目镜像到其绑定的频道（如果有）。嵌入条目 id 以便将入站回复关联回来。
    仅应用内的绑定在此处不投递任何内容。如果发送了频道消息则返回 True。

    [English] Mirror an inbox item to its bound channel (if any). The item id is embedded so an inbound
    reply can be correlated back. In-app-only bindings deliver nothing here. Returns True if a
    channel message was sent."""
    if not binding.channel or sender is None:
        return False
    text = f"{item.title}\n{item.body}\n[ow:{item.id}]".strip()
    sender(binding.channel, binding.target, text)
    return True


# [中文] 针对频道回复的决策关键词。仅与回复的起始单词/表情符号进行匹配（参见 _reply_intent）。
# 子字符串匹配曾将 "disallow" 变成 allow，将 "note" 变成 deny；任何位置的全词匹配（临时修复）
# 仍会导致否定回复被倒置 — 例如 "I cannot approve this yet" 匹配了 \bapprove\b，且由于优先检查 allow，
# 结果执行了被拒绝的操作。起始词意图识别使得 "Yes, go ahead" / "No." / "👍" 正常工作；
# 其他一切均被视为自由文本回答，审批路径已将其映射为拒绝 — 这是审批门控的安全默认值。
# [English]
# Decision keywords for a channel reply. Matched against the reply's LEADING word/emoji
# only (see _reply_intent). Substring matching turned "disallow" into allow and "note"
# into deny; whole-word matching anywhere (the interim fix) still inverted negated
# replies — "I cannot approve this yet" matched \bapprove\b and, with allow checked
# first, executed the declined action. Leading-word intent keeps "Yes, go ahead" /
# "No." / "👍" working; everything else is a free-text answer, which the approval path
# already maps to deny — the safe default for an approval gate.
_ALLOW_WORDS = frozenset({"approve", "approved", "allow", "allowed", "yes"})
_DENY_WORDS = frozenset({"deny", "denied", "reject", "rejected", "no"})
_ALLOW_EMOJI = ("👍", "✅")
_DENY_EMOJI = ("👎", "❌")
_TOKEN_TRIM = ".,!?:;'\"()"


def _reply_intent(text: str) -> Optional[str]:
    """[中文] 从回复的第一个单词（或表情符号）判断 allow/deny 意图，否则返回 None。
    [English] Allow/deny intent from the first word (or emoji) of a reply, else None."""
    first = text.split()[0] if text.split() else ""
    if first.startswith(_ALLOW_EMOJI):  # [中文] startswith：容忍肤色修饰符 / [English] startswith: tolerate skin-tone modifiers
        return "allow"
    if first.startswith(_DENY_EMOJI):
        return "deny"
    word = first.strip(_TOKEN_TRIM).lower()
    if word in _ALLOW_WORDS:
        return "allow"
    if word in _DENY_WORDS:
        return "deny"
    return None


def resolve_from_reply(
    reply: str, resolve: Callable[[str, str], bool]
) -> Optional[bool]:
    """[中文] 将入站频道回复与对应条目关联（通过嵌入的 id）并解析它。

    查找回复中的 ``[ow:<id>]`` 标记（或旧的 ``[ocw:…]``）以及起始词中的 allow/deny 意图；
    若无法识别则降级为将整条消息作为自由文本回答处理。
    ``resolve(item_id, resolution)`` 即为 InboxStore.resolve。
    返回 resolve() 的结果，如果未找到条目 id 则返回 None。

    [English] Correlate an inbound channel reply to its item (by the embedded id) and resolve it.

    Looks for the ``[ow:<id>]`` token (or legacy ``[ocw:…]``) and an allow/deny intent in the
    reply's leading word; falls back to treating the whole message as a free-text answer.
    ``resolve(item_id, resolution)`` is the InboxStore.resolve.
    Returns the resolve() result, or None if no item id was found."""
    m = _ID_TOKEN.search(reply or "")
    if not m:
        return None
    item_id = m.group(1)
    text = _ID_TOKEN.sub("", reply).strip()
    resolution = _reply_intent(text) or text  # [中文] 对问题的自由文本回答 / [English] free-text answer to a question
    return resolve(item_id, resolution)
