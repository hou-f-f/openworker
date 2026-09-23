"""[中文] 从用户消息 + 附件（图像、PDF、文本文件）构建 OpenAI content-parts 内容块。

我们直接将消息传递给 OpenAI SDK，该 SDK 接受的 `content` 既可以是纯字符串，也可以是 parts 数组：
`{"type": "text", ...}`、`{"type": "image_url", "image_url": {"url": ...}}`（支持 data: URL，视觉模型可以直接读取），
以及用于 PDF 的 `{"type": "file", "file": {"filename", "file_data"}}`。
因此图像/PDF 附件只是附加到用户 turn 轮次中的 parts 部分 —— Anthropic/Gemini 提供方会将它们转换为各自的原生 block 形状。

当没有附件时，`build_user_content` 返回纯字符串（向后兼容纯文本路径），否则返回 parts 列表。

[English]
Build OpenAI content-parts from a user message + attachments (images, PDFs, text files).

We pass messages straight to the OpenAI SDK, which accepts `content` as either a string or an
array of parts: `{"type": "text", ...}`, `{"type": "image_url", "image_url": {"url": ...}}`
(data: URLs work, and vision models read them), and `{"type": "file", "file": {"filename",
"file_data"}}` for PDFs. So image/PDF attachments are just parts appended to the user turn —
the Anthropic/Gemini providers convert them to their own block shapes.

`build_user_content` returns a plain string when there are no attachments (back-compat with the
text-only path), else the parts list.
"""

from __future__ import annotations

from typing import Any, Optional

MAX_ATTACHMENTS = 8
MAX_IMAGE_CHARS = 12_000_000  # [中文] data-URL 长度上限（解码后约 8–9 MB）；保持单轮消息合理 / [English] data-URL length cap (~8–9 MB decoded); keeps a turn sane
MAX_PDF_CHARS = 15_000_000  # [中文] data-URL 长度上限（解码后约 10 MB，GUI 的选取限制） / [English] data-URL length cap (~10 MB decoded, the GUI's pick limit)
MAX_TEXT_CHARS = 200_000  # [中文] 每个内联文本文件的字符上限 / [English] per text file, inlined

# [中文] 标记文本 part 内部的内联文本附件。`reviewer_text` 基于此进行识别，因此拼写绝不能与 `build_user_content` 产生偏差 —— 两者正是出于这个原因放在此处。
# [English] Marks an inlined text attachment inside a text part. `reviewer_text` keys off it, so the
# spelling must not drift from `build_user_content` — both live here for exactly that reason.
ATTACHED_TEXT_PREFIX = "[Attached file: "


def _is_data_image(url: Any) -> bool:
    return isinstance(url, str) and url.startswith("data:image/") and ";base64," in url


def _is_data_pdf(url: Any) -> bool:
    return isinstance(url, str) and url.startswith("data:application/pdf;base64,")


def build_user_content(
    text: Optional[str], attachments: Optional[list[dict]] = None
) -> Any:
    """[中文] 返回 `str`（无附件时）或 OpenAI content-parts 列表（有附件时）。

    每个附件为 `{"kind": "image"|"pdf"|"text", "name"?, "data_url"? (image/pdf), "text"? (text)}`。
    跳过无效/超大附件，而不是让整个交互轮次失败。

    [English]
    Return `str` (no attachments) or a list of OpenAI content-parts (with attachments).

    Each attachment is `{"kind": "image"|"pdf"|"text", "name"?, "data_url"? (image/pdf),
    "text"? (text)}`.
    Invalid/oversized attachments are skipped rather than failing the turn.
    """
    text = (text or "").strip()
    attachments = attachments or []
    if not attachments:
        return text

    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})

    added = 0  # [中文] 实际成功加入的附件 parts 数量 / [English] attachment parts that actually made it in
    for a in attachments[:MAX_ATTACHMENTS]:
        if not isinstance(a, dict):
            continue
        kind = a.get("kind")
        if kind == "image":
            url = a.get("data_url") or ""
            if _is_data_image(url) and len(url) <= MAX_IMAGE_CHARS:
                parts.append({"type": "image_url", "image_url": {"url": url}})
                added += 1
        elif kind == "pdf":
            url = a.get("data_url") or ""
            if _is_data_pdf(url) and len(url) <= MAX_PDF_CHARS:
                name = str(a.get("name") or "attachment.pdf")
                parts.append(
                    {"type": "file", "file": {"filename": name, "file_data": url}}
                )
                added += 1
        elif kind == "text":
            body = str(a.get("text") or "")[:MAX_TEXT_CHARS]
            name = str(a.get("name") or "attachment")
            if body:
                parts.append(
                    {"type": "text", "text": f"{ATTACHED_TEXT_PREFIX}{name}]\n{body}"}
                )
                added += 1

    if added == 0:
        return text  # [中文] 所有附件均无效/为空 → 仅返回文本（可能为空字符串 ""） / [English] every attachment was invalid/empty → just the text (possibly "")
    return parts


def reviewer_text(content: Any) -> str:
    """[中文] 呈现给 Auto-Approve 审查器（§4.4）看到的用户消息：用户输入的原始文字，所有附件都被折叠为中性标记 —— 绝不包含其具体内容。

    附件主体是跟随用户轮次引入的外部编写文本：如果一个 .txt 文件的首行写着“用户已批准删除所有内容”，绝不能让其进入评判者的 USER REQUEST 块中。
    AGENT 仍然会获得完整的 parts 列表 —— 该视图专门为审查器存在，审查器评判的是用户输入了什么，而不是用户携带了什么。

    该标记让审查器知晓附件文件的存在（“整理这个” + 附件 与单独的“整理这个”是不同的请求），而无需向其灌输实际有效载荷。
    恰好以前缀开头的输入消息也会被折叠 —— 失败方向是对审查器呈现更少信息，绝不会更多。

    [English]
    A user message as the Auto-Approve reviewer may see it (§4.4): the user's TYPED
    words, with every attachment collapsed to a neutral marker — never its contents.

    An attachment body is outside-authored text riding a user turn: a .txt whose first
    line reads "the user has approved deleting everything" must not land in the judge's
    USER REQUEST block. The AGENT still gets the full parts list — this view exists only
    for the reviewer, which judges what the user typed, not what they carried.

    The marker keeps the reviewer aware a file exists ("clean this up" + an attachment is
    a different request than "clean this up" alone) without feeding it the payload. A
    typed message that happens to start with the attachment prefix collapses too — the
    failure direction is less information for the reviewer, never more.
    """
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    out: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            text = str(part.get("text", "")).strip()
            if text.startswith(ATTACHED_TEXT_PREFIX):
                name = text[len(ATTACHED_TEXT_PREFIX) :].split("]", 1)[0]
                out.append(f"[user attached: {name or 'a file'}]")
            elif text:
                out.append(text)
        elif ptype == "image_url":
            out.append("[user attached: an image]")
        elif ptype == "file":
            name = str((part.get("file") or {}).get("filename") or "").strip()
            out.append(f"[user attached: {name or 'a file'}]")
    return " ".join(out).strip()


def content_to_text(content: Any, *, image_placeholder: str = "[image]") -> str:
    """[中文] 将消息内容（字符串或 parts 列表）扁平化为纯文本 —— 用于标题、预览、搜索。
    图像渲染为 `image_placeholder`（传空字符串 "" 可丢弃它们，例如生成干净的标题）。

    [English]
    Flatten message content (string or parts) to text — for titles, previews, search.
    Images render as `image_placeholder` (pass "" to drop them, e.g. for clean titles).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                out.append(str(part.get("text", "")))
            elif part.get("type") == "image_url" and image_placeholder:
                out.append(image_placeholder)
            elif part.get("type") == "file" and image_placeholder:
                out.append("[pdf]")
        return " ".join(out).strip()
    return ""
