"""[中文] 看板事项的内容寻址附件 —— 以屏幕截图为首要应用场景。

审查工件不属于代码仓库（它们不是源代码，且会在代码签出时丢失），
也不属于看板事件日志（事件只携带引用，绝不内嵌大对象 —— 哈希链下不能承载数兆字节数据）。
它们保存在此处：在 state 状态目录中按 sha256 命名的文件，并通过携带
`attachment://<hash>.<ext>#<name>` 引用的常规评论事件接入看板。

内容寻址（Content addressing）带来三大优势：天然免费的重复数据删除（同一张截图附加两次只存储一份）、
构造即不可变（引用绝不可能悬空指向已变更的字节），以及独立于智能体工作区。
看板及其附件存储保留在本机上；暂未实现跨机器同步。

图像和惰性报告格式受限于 10MB。发布过程绝不执行或渲染活动文档内容。

[English]
Content-addressed attachments for board items — screenshots first.

Review artifacts don't belong in the repo (they aren't source, and they die with
checkouts) and don't belong in the board log (events carry refs, never blobs — no
megabytes under the hash chain). They live here: files named by their sha256 in the
state dir, bridged into the board as a normal comment event carrying an
`attachment://<hash>.<ext>#<name>` ref.

Content addressing buys three things: dedupe for free (the same screenshot attached
twice stores once), immutability by construction (the ref can never dangle onto
changed bytes), and independence from agent workspaces. Boards and their attachment
store stay on this machine; cross-machine sync is not implemented.

Images and inert report formats are bounded to 10MB. Publication never executes
or renders active document content.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Optional

from .model import BoardError, BoardNotFoundError

ATTACHMENT_SCHEME = "attachment://"
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

# [中文] 我们接受的扩展名 → MIME 类型映射。嗅探到的文件魔数必须与声明的扩展名一致 —— 不是 PNG 的 .png 会被拒绝，而不是被重命名。
# [English] Extension → mime for the types we accept. Sniffed magic must agree with the
# claimed extension — a .png that isn't a PNG is refused, not renamed.
_IMAGE_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}
_TEXT_TYPES = {"txt": "text/plain", "md": "text/plain", "log": "text/plain",
               "csv": "text/plain", "json": "text/plain"}
_FILE_TYPES = {**_IMAGE_TYPES, **_TEXT_TYPES, "pdf": "application/pdf"}

_MAGIC = {
    "png": b"\x89PNG\r\n\x1a\n",
    "jpg": b"\xff\xd8\xff",
    "jpeg": b"\xff\xd8\xff",
    "gif": b"GIF8",
    "webp": b"RIFF",  # [中文] RIFF….WEBP — 通过下方的 fourcc 进行校验 / [English] RIFF….WEBP — checked with the fourcc below
}

_STORED_NAME = re.compile(r"[0-9a-f]{64}\.[a-z0-9]{1,5}")


class AttachmentStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()

    def put(self, data: bytes, filename: str) -> str:
        """[中文] 存储一个附件；返回其 `attachment://` 引用。幂等操作 —— 相同的字节数据对应同一个文件。

        [English]
        Store one attachment; returns its `attachment://` ref. Idempotent —
        identical bytes land on the same file."""
        ext = _validate(data, filename)
        stored = f"{hashlib.sha256(data).hexdigest()}.{ext}"
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / stored
        if target.exists():
            if target.is_symlink() or target.stat().st_size != len(data) or target.read_bytes() != data:
                raise BoardError("stored attachment failed integrity verification")
        else:
            # [中文] 唯一的临时暂存文件：并发的相同捕获不得共享 .tmp 文件名。只有完整刷入磁盘的字节对读取者可见。
            # [English] Unique staging files: concurrent identical captures must not share
            # a .tmp name. Only complete, flushed bytes become visible to readers.
            tmp = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.root, prefix=".attachment-", delete=False) as file:
                    tmp = Path(file.name)
                    file.write(data)
                    file.flush()
                    os.fsync(file.fileno())
                tmp.replace(target)
                if target.read_bytes() != data:
                    raise BoardError("stored attachment failed integrity verification")
            finally:
                if tmp is not None:
                    tmp.unlink(missing_ok=True)
        safe_name = Path(filename).name.replace("#", "_")
        return f"{ATTACHMENT_SCHEME}{stored}#{safe_name}"

    def path_for(self, stored: str) -> Path:
        """[中文] 将存储文件名（`<sha256>.<ext>`）解析为其物理文件。严格的文件名校验即是目录遍历防护 —— 不允许其他输入触及文件系统。

        [English]
        Resolve a stored name (`<sha256>.<ext>`) to its file. The strict name
        check is the traversal guard — nothing else reaches the filesystem."""
        stored = validate_stored_name(stored)
        path = self.root / stored
        if path.is_symlink() or not path.is_file():
            raise BoardNotFoundError("attachment not found")
        return path

    def mime_for(self, stored: str) -> str:
        return _FILE_TYPES.get(stored.rsplit(".", 1)[-1], "application/octet-stream")


def read_image_file(path: str | Path, *, roots=None) -> tuple[bytes, str]:
    data, name = read_attachment_file(path, roots=roots)
    if Path(name).suffix.lower().lstrip(".") not in _IMAGE_TYPES:
        raise BoardError("attach_image requires an image; use attach_file for reports")
    return data, name


def read_attachment_file(path: str | Path, *, roots=None) -> tuple[bytes, str]:
    """[中文] 读取有大小上限的常规文件。智能体调用方必须提供当前已授予的根目录列表。

    None 仅保留给拥有自身文件系统访问权限的操作员 CLI/MCP 调用方；
    智能体的根目录列表为空时将自动快速失败。智能体相对路径使用主根目录解析。

    [English]
    Read a bounded regular file. Agent callers MUST supply current granted roots.

    None is reserved for operator CLI/MCP callers with their own filesystem access;
    an empty agent root list fails closed. Relative agent paths use the primary root.
    """
    from ..roots import normalize_roots

    allowed = normalize_roots(roots) if roots is not None else None
    source = Path(path).expanduser()
    if allowed is not None:
        if not allowed:
            raise BoardError("no session directory is available for attachments")
        if not source.is_absolute():
            source = allowed[0].path / source
    source = source.resolve()
    if allowed is not None and not any(source.is_relative_to(r.path) for r in allowed):
        raise BoardError("attachment is outside the session's directories")
    # [中文] O_NONBLOCK 防止 FIFO 等特殊文件挂起工具；O_NOFOLLOW 拒绝在路径解析后叶子节点被替换为符号链接。
    # [English] O_NONBLOCK prevents special files such as FIFOs from hanging the tool;
    # O_NOFOLLOW rejects a leaf swapped to a symlink after resolution.
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(source, flags)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise BoardError("attachment must be a regular file")
        if info.st_size > MAX_ATTACHMENT_BYTES:
            raise BoardError("attachment exceeds 10MB")
        data = file.read(MAX_ATTACHMENT_BYTES + 1)
    _validate(data, source.name)
    return data, source.name


def stored_name(ref: str) -> Optional[str]:
    """[中文] `attachment://<hash>.<ext>#<name>` → `<hash>.<ext>`；其他引用返回 None。 / [English] `attachment://<hash>.<ext>#<name>` → `<hash>.<ext>`; None for other refs."""
    if not ref.startswith(ATTACHMENT_SCHEME):
        return None
    return ref[len(ATTACHMENT_SCHEME):].split("#", 1)[0]


def validate_stored_name(stored: str) -> str:
    """[中文] 返回规范化的已存储文件名，拒绝格式错误的输入。 / [English] Return one normalized stored name, rejecting malformed input."""
    stored = stored.strip()
    if not _STORED_NAME.fullmatch(stored):
        raise BoardError(f"not an attachment name: {stored!r}")
    return stored


def _validate(data: bytes, filename: str) -> str:
    if not data:
        raise BoardError("attachment is empty")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise BoardError(
            f"attachment exceeds {MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB"
        )
    ext = Path(filename).suffix.lstrip(".").lower()
    if ext not in _FILE_TYPES:
        raise BoardError(
            f"unsupported attachment type .{ext or '?'}"
            f" ({', '.join(sorted(_FILE_TYPES))})"
        )
    if ext in _TEXT_TYPES:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise BoardError("text attachments must be UTF-8") from None
        if any(ord(c) < 32 and c not in "\n\r\t" for c in text):
            raise BoardError("text attachment contains binary/control characters")
        return ext
    if ext == "pdf":
        if not data.startswith(b"%PDF-"):
            raise BoardError("file content does not look like .pdf")
        return ext
    if not data.startswith(_MAGIC[ext]) or (
        ext == "webp" and data[8:12] != b"WEBP"
    ):
        raise BoardError(f"file content does not look like .{ext}")
    return "jpg" if ext == "jpeg" else ext
