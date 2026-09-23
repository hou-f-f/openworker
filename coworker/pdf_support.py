"""[中文] 为缺乏原生 PDF 支持的模型提供本地 PDF 处理能力。

规范的会话历史始终将 PDF 附件存储为 OpenAI `file` 内容部分（参见 attachments.py）。
在发送请求时，引擎检查当前激活模型的具体能力（`ModelCapabilities.pdf`），当模型无法原生接收 PDF 时，
会在调用 provider 提供方前就地替换掉该 file 部分 —— 存储的历史记录绝不会被修改，因此在中途切换到支持 PDF 的模型时，
会再次发送真实的 PDF 文档。

两种降级回退模式（用户设置，Settings → Token savings）：
  - "text"   — 在本地提取嵌入的文本（pypdf；纯 Python 实现）。
  - "images" — 将每页渲染为 PNG 图像（pypdfium2）并作为 image parts 发送；仅在模型具备视觉（vision）能力时有用，否则无论如何都会降级为文本。

所有操作均在本地运行 —— 文档绝不会被发送到任何第三方供应商的“文件提取”端点。
结果按内容哈希缓存，因为历史记录会在每一轮交互中重放。

[English]
Local PDF handling for models without native PDF support.

The canonical history always stores a PDF attachment as an OpenAI `file` content part
(attachments.py). At send time the engine checks the ACTIVE model's capabilities
(`ModelCapabilities.pdf`) and, when the model can't take PDFs natively, replaces the
file part right before the provider call — the stored history is never mutated, so
switching to a PDF-capable model mid-session sends the real document again.

Two fallback modes (user setting, Settings → Token savings):
  - "text"   — extract embedded text locally (pypdf; pure Python).
  - "images" — render each page to a PNG (pypdfium2) and send as image parts; only
               useful when the model has vision, else it degrades to text anyway.

Everything runs locally — the document never goes to any vendor "file extract"
endpoint. Results are cached by content hash because the history is replayed on every
turn.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

MAX_EXTRACT_CHARS = 200_000  # [中文] 与 attachments.MAX_TEXT_CHARS 匹配 / [English] match attachments.MAX_TEXT_CHARS
RASTER_SCALE = 2.0  # [中文] 约 144 dpi；保证文本清晰且不产生巨大有效载荷 / [English] ~144 dpi; readable text without giant payloads
RASTER_MAX_PAGES = 100  # [中文] 硬性上限；用户页面阈值在附加时进行门控 / [English] hard ceiling; the user's page threshold gates at attach time

FALLBACK_MODES = ("text", "images")

# [中文] 全局用户首选项，在启动时以及设置变更时由 server manager 从首选项中配置。CLI/库模式使用保留 "text" 默认值。
# [English] Global user preference, set by the server manager from prefs at startup and on
# settings change. CLI/library use keeps the "text" default.
_fallback_mode = "text"


def set_fallback_mode(mode: Any) -> str:
    global _fallback_mode
    _fallback_mode = mode if mode in FALLBACK_MODES else "text"
    return _fallback_mode


def fallback_mode() -> str:
    return _fallback_mode


# [中文] (data URL 的 sha256, 操作类型) → 结果。小型类 LRU 缓存：历史记录每轮都会重放，而提取/光栅化 10MB PDF 是昂贵开销。
# [English] (sha256 of data URL, operation) → result. Tiny LRU-ish cache: history replays every
# turn, and extraction/rasterization of a 10MB PDF is the expensive part.
_cache: dict[tuple[str, str], Any] = {}
_CACHE_MAX = 8


def _cached(key: tuple[str, str], compute):
    if key in _cache:
        return _cache[key]
    value = compute()
    if len(_cache) >= _CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[key] = value
    return value


def _digest(file_data: str) -> str:
    return hashlib.sha256(file_data.encode("ascii", "ignore")).hexdigest()


def _pdf_bytes(file_data: str) -> Optional[bytes]:
    prefix = "data:application/pdf;base64,"
    if not isinstance(file_data, str) or not file_data.startswith(prefix):
        return None
    try:
        return base64.b64decode(file_data[len(prefix) :], validate=False)
    except Exception:
        return None


def inspect(file_data: str) -> dict[str, Any]:
    """[中文] 获取 PDF data URL 的页数与大小 —— 附加时阈值检查。

    绝不抛出异常：对于任何无法读取的内容返回 `{"ok": False, "error": ...}`。

    [English]
    Page count + size for a PDF data URL — the attach-time threshold check.

    Never raises: `{"ok": False, "error": ...}` for anything unreadable.
    """
    raw = _pdf_bytes(file_data)
    if raw is None:
        return {"ok": False, "error": "not a PDF data URL"}
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw), strict=False)
        if reader.is_encrypted:
            try:
                reader.decrypt("")  # [中文] 带所有者密码但未加密的 PDF 可通过此方式打开 / [English] unencrypted-with-owner-password PDFs open this way
            except Exception:
                return {"ok": False, "error": "PDF is password-protected"}
        return {"ok": True, "pages": len(reader.pages), "bytes": len(raw)}
    except Exception as exc:
        return {"ok": False, "error": f"could not read PDF: {exc.__class__.__name__}"}


def extract_text(file_data: str) -> Optional[str]:
    """[中文] 整个文档的嵌入文本（受上限约束），如果无法读取则返回 None。
    扫描版 PDF 合法返回 "" —— 调用方对此进行明确区分。

    [English]
    Embedded text of the whole document (capped), or None if unreadable.
    Scanned PDFs legitimately return "" — callers surface that distinctly.
    """

    def compute() -> Optional[str]:
        raw = _pdf_bytes(file_data)
        if raw is None:
            return None
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(raw), strict=False)
            chunks: list[str] = []
            total = 0
            for page in reader.pages:
                text = page.extract_text() or ""
                if text:
                    chunks.append(text)
                    total += len(text)
                    if total >= MAX_EXTRACT_CHARS:
                        break
            return "\n\n".join(chunks)[:MAX_EXTRACT_CHARS]
        except Exception:
            logger.warning("pdf text extraction failed", exc_info=True)
            return None

    return _cached((_digest(file_data), "text"), compute)


def _encode_png(
    width: int, height: int, pixels: bytes, stride: int, channels: int
) -> bytes:
    """[中文] 极简 PNG 编码器（RGB/RGBA，8 位），避免为此引入 Pillow 依赖 ——
    打包的 sidecar 特意排除了 PIL（减小 bundle 体积、减少签名面）。

    [English]
    Minimal PNG writer (RGB/RGBA, 8-bit) so we don't ship Pillow just for this —
    the packaged sidecar deliberately excludes PIL (bundle size, signing surface).
    """
    import struct
    import zlib

    color_type = 6 if channels == 4 else 2
    row_bytes = width * channels
    scanlines = bytearray()
    for y in range(height):
        scanlines.append(0)  # filter: None
        start = y * stride
        scanlines.extend(pixels[start : start + row_bytes])

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(scanlines), 6))
        + chunk(b"IEND", b"")
    )


def rasterize(file_data: str, max_pages: int = RASTER_MAX_PAGES) -> Optional[list[str]]:
    """[中文] 将每一页转换为 PNG data URL，若无法渲染（缺少 pypdfium2 或文档损坏）则返回 None —— 调用方将降级为文本。

    [English]
    Each page as a PNG data URL, or None when rendering isn't possible
    (pypdfium2 missing or the document is broken) — callers fall back to text.
    """

    def compute() -> Optional[list[str]]:
        raw = _pdf_bytes(file_data)
        if raw is None:
            return None
        try:
            import pypdfium2

            doc = pypdfium2.PdfDocument(raw)
            pages: list[str] = []
            try:
                for index in range(min(len(doc), max_pages)):
                    # [中文] rev_byteorder 将 pdfium 原生的 BGR(A) 翻转为 PNG 所需的 RGB(A)。
                    # [English] rev_byteorder flips pdfium's native BGR(A) to the RGB(A) PNG wants.
                    bitmap = doc[index].render(scale=RASTER_SCALE, rev_byteorder=True)
                    png = _encode_png(
                        bitmap.width,
                        bitmap.height,
                        bytes(bitmap.buffer),
                        bitmap.stride,
                        bitmap.n_channels,
                    )
                    encoded = base64.b64encode(png).decode("ascii")
                    pages.append(f"data:image/png;base64,{encoded}")
            finally:
                doc.close()
            return pages or None
        except Exception:
            logger.warning("pdf rasterization failed", exc_info=True)
            return None

    return _cached((_digest(file_data), f"images:{max_pages}"), compute)


def adapt_content(content: list[dict[str, Any]], caps: Any) -> list[dict[str, Any]]:
    """[中文] 为缺乏原生 PDF 支持的模型替换 `file` parts 内容部分。

    vision + "images" 模式 → 页面图像 parts；否则使用提取出的文本。
    当没有任何可用内容产出时，两条路径都会以可见的文本附注结尾 —— PDF 绝不能在交互轮次中悄无声息地消失。

    [English]
    Replace `file` parts for a model without native PDF support.

    vision + "images" mode → page-image parts; otherwise extracted text. Both paths end
    in a VISIBLE text note when nothing usable comes out — a PDF must never silently
    vanish from the turn.
    """
    out: list[dict[str, Any]] = []
    for part in content:
        if not (isinstance(part, dict) and part.get("type") == "file"):
            out.append(part)
            continue
        file = part.get("file") or {}
        name = str(file.get("filename") or "attachment.pdf")
        file_data = file.get("file_data") or ""

        if fallback_mode() == "images" and getattr(caps, "vision", False):
            images = rasterize(file_data)
            if images:
                out.append(
                    {
                        "type": "text",
                        "text": f"[Attached PDF: {name} — {len(images)} page image(s), rendered locally]",
                    }
                )
                out.extend(
                    {"type": "image_url", "image_url": {"url": url}} for url in images
                )
                continue

        text = extract_text(file_data)
        if text:
            out.append(
                {
                    "type": "text",
                    "text": (
                        f"[Attached PDF: {name} — text extracted locally; "
                        f"this model has no native PDF support]\n{text}"
                    ),
                }
            )
        else:
            out.append(
                {
                    "type": "text",
                    "text": (
                        f"[Attached PDF: {name} — no extractable text (likely scanned). "
                        "A model with native PDF support (Claude, GPT, Gemini) can read it.]"
                    ),
                }
            )
    return out
