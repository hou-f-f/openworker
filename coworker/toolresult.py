"""【工具结果尺寸限制】在工具返回结果注入会话上下文之前对其进行硬性尺寸截断与外溢存储（OPE-186）。

工具结果会在后续每一次 Turn 中重新发送给大模型，因此单次超大的工具输出会持续拖累整个会话的 Token 消耗和性能。
处理机制（在 `TurnEngine._record_result` 中统一调用）：若序列化后的内容超出 `max_bytes`，全量原始内容将转储（Spill）到本地磁盘临时文件，
超长字段被替换为“头部内容 + 提示标记（标明外溢文件路径与省略字节数） + 尾部内容（Head + Tail）”。
保留头部和尾部是因为编译构建日志既需要前部的首个报错信息，又需要尾部的最终退出结论。
该标记不包含动态时间戳，因此多次重放时字节级完全一致，绝不会破坏模型的 Prompt Cache 缓存命中。
对于结构化字典结果，仅截断最大的字符串字段，保持 JSON 结构合法完整。

Bound every tool result before it enters the conversation (OPE-186, change 1).

A tool result is re-sent to the model on every later turn, so one oversized result taxes
the whole rest of the session. The shell tool used to keep the LAST 20,000 characters of
its output and drop the beginning; every other tool was unbounded (a `read_file` of a
486,000-character file was seen in one long session).

Rule, applied in one place for all tools (`TurnEngine._record_result`): if the result, as
it would be serialised into the tool message, exceeds `max_bytes`, the full text is written
to a spill file and the oversized field is replaced by its head, a marker naming the file
and the omitted byte count, and its tail. Head-plus-tail at 10,000 bytes is a common shape;
another is to keep the full output on disk and hand the model only the path.
Head AND tail because a build log needs both its first error and its final verdict.

The marker carries no timestamp, so a bounded result is byte-identical on every later
request and never disturbs the provider's prompt cache. Structured results stay valid
JSON: only the largest string field(s) are bounded, everything else is untouched.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

# 默认工具结果最大允许字节数 (10KB)
# Default max bytes for a tool result (10KB)
DEFAULT_TOOL_RESULT_MAX_BYTES = 10_000
# 计算首尾截断空间时为标记行与 JSON 转义预留的字节数
# Bytes set aside for the marker line and JSON escaping when sizing head + tail.
_MARKER_RESERVE = 400
# 无论配额多小，首尾保留的总字节数绝不低于此下限 (1000B)
# Never shrink a field below this many bytes of head + tail, whatever the budget says.
_MIN_KEEP = 1_000
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


class PagedToolResult(dict):
    """【受信任的分页工具结果】原生分页工具的返回结果，具备自身受限且可重放的分页游标。
    无需对其执行首尾截断：游标已精确描述了返回的范围。仅限原生 Python 代码构造。

    Trusted native reader result with its own bounded, replayable pagination.

    Do not head/tail its text: the cursor describes exactly the returned range.
    JSON/tool payloads cannot opt in; only native code can construct this type.
    """


def serialize_result(result: Any) -> str:
    """【序列化结果】将工具执行结果序列化为字符串，与消息内容保持一致。

    Exactly what `_tool_result_message` puts in the message content.
    """
    return result if isinstance(result, str) else json.dumps(result, default=str)


def _nbytes(text: str) -> int:
    return len(text.encode("utf-8"))


def head_tail(text: str, keep_bytes: int, *, spill_path: Optional[Path], total_bytes: int) -> str:
    """【首尾截断拼接】保留前一半字节、中间插入省略标记与外溢文件路径、保留后一半字节。

    First half of `keep_bytes`, a marker, last half. Cuts are byte-based and decoded
    with errors ignored so a multi-byte character split at the boundary is dropped, never
    corrupted."""
    keep = max(int(keep_bytes), _MIN_KEEP)
    raw = text.encode("utf-8")
    half = keep // 2
    head = raw[:half].decode("utf-8", errors="ignore")
    tail = raw[-half:].decode("utf-8", errors="ignore") if half else ""
    omitted = max(0, len(raw) - _nbytes(head) - _nbytes(tail))
    where = (
        f"full text saved to {spill_path} (read it with read_file, or "
        f"run_shell: sed -n '1,200p' \"{spill_path}\")"
        if spill_path is not None
        else "full text not saved"
    )
    marker = f"\n[... {omitted} bytes omitted here; this result was {total_bytes} bytes; {where} ...]\n"
    return head + marker + tail


def bound_tool_result(
    result: Any,
    *,
    max_bytes: Optional[int],
    spill_dir: Optional[Path],
    step: int,
    tool_name: str,
) -> Any:
    """Return `result` unchanged when it fits, else a bounded copy. `max_bytes` None or
    <= 0 disables bounding. Spill files are written only when `spill_dir` is given."""
    if isinstance(result, PagedToolResult):
        return dict(result)
    if not max_bytes or max_bytes <= 0:
        return result
    text = serialize_result(result)
    if _nbytes(text) <= max_bytes:
        return result

    safe_tool = _SAFE_NAME.sub("_", tool_name or "tool")[:40] or "tool"

    def spill(name: str, payload: str) -> Optional[Path]:
        if spill_dir is None:
            return None
        try:
            spill_dir.mkdir(parents=True, exist_ok=True)
            path = spill_dir / name
            path.write_text(payload, encoding="utf-8", errors="replace")
            return path
        except OSError:
            return None

    if isinstance(result, dict):
        out: dict[str, Any] = dict(result)
        # Bound the largest string field, re-measure, repeat for the next largest if the
        # whole message is still over budget (a result can carry two big fields).
        for _ in range(4):
            key = max(
                (k for k, v in out.items() if isinstance(v, str)),
                key=lambda k: _nbytes(out[k]),
                default=None,
            )
            if key is None or _nbytes(out[key]) < _MIN_KEEP:
                break
            others = _nbytes(serialize_result({k: v for k, v in out.items() if k != key}))
            budget = max_bytes - others - _MARKER_RESERVE
            original = out[key]
            path = spill(f"{step:04d}-{safe_tool}-{_SAFE_NAME.sub('_', key)[:30]}.txt", original)
            bounded = head_tail(original, budget, spill_path=path, total_bytes=_nbytes(original))
            # JSON escaping (newlines, quotes) grows the serialised size; tighten once.
            over = _nbytes(serialize_result({**out, key: bounded})) - max_bytes
            if over > 0:
                bounded = head_tail(original, budget - over, spill_path=path, total_bytes=_nbytes(original))
            out[key] = bounded
            if _nbytes(serialize_result(out)) <= max_bytes:
                break
        return out

    path = spill(f"{step:04d}-{safe_tool}.txt", text)
    return head_tail(text, max_bytes - _MARKER_RESERVE, spill_path=path, total_bytes=_nbytes(text))
