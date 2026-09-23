"""[中文] 机密信息存储 — 用于连接器/MCP凭据的单一规范、文件支持的存储库。

设计理念（源自 OpenClaw）：机密信息**绝不进入模型的上下文、Prompt 或跟踪记录**。
存储库保存按 `connector[:account]` 索引的 Profile；其值可以是字面量，
或者在读取时从进程环境变量 / `~/.config/coworker/.env` 解析的 `${ENV_VAR}` 引用。

v1 是在此接口背后的一个 `0600` 权限 JSON 文件；接口是调用方所依赖的契约，
因此后续可以替换为 Keychain / age 加密后端，而无需改动调用方。

[English]
Secret store — one canonical, file-backed store for connector/MCP credentials.

Design (from OpenClaw): secrets **never enter the model's context, prompts, or traces**.
The store holds profiles keyed by `connector[:account]`; values may be literals OR
`${ENV_VAR}` references resolved at read time from the process env / `~/.config/coworker/.env`.

v1 is a `0600` JSON file behind this interface; the interface is what callers depend on, so
a Keychain / age-encrypted backend can swap in later without touching them.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_IS_WINDOWS = sys.platform == "win32"


def state_dir() -> Path:
    """[中文] coworker 保存其状态的位置 — 跨平台的唯一事实来源。

    解析顺序：
    1. `$COWORKER_STATE_DIR` — 任何操作系统上的显式覆盖项（由测试/sidecar 使用）。
    2. Windows：`%APPDATA%\\coworker`（例如 `C:\\Users\\You\\AppData\\Roaming\\coworker`），
       平台原生的每用户应用程序数据目录。
    3. macOS / Linux：`~/.config/coworker`（XDG 风格，与旧有行为一致）。

    [English] Where coworker keeps its state — the one cross-platform source of truth.

    Resolution order:
    1. `$COWORKER_STATE_DIR` — explicit override on any OS (used by tests/sidecars).
    2. Windows: `%APPDATA%\\coworker` (e.g. `C:\\Users\\You\\AppData\\Roaming\\coworker`),
       the native per-user app-data location.
    3. macOS / Linux: `~/.config/coworker` (XDG-style, unchanged from prior behavior).
    """
    base = os.environ.get("COWORKER_STATE_DIR")
    if base:
        return Path(base).expanduser()
    # [中文] 受限于 OPENWORKER_BASE_DIR 的容器/沙箱也将其状态保存在那里（coworker/basedir.py）— 该机器上的任何东西都不会存在于其外部。
    # [English]
    # A box confined to OPENWORKER_BASE_DIR keeps its state there too
    # (coworker/basedir.py) — nothing of the machine's lives outside it.
    confined = os.environ.get("OPENWORKER_BASE_DIR", "").strip()
    if confined:
        return Path(confined).expanduser() / "state"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "coworker"
    return Path.home() / ".config" / "coworker"


def _load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _restrict_to_user(path: Path, *, is_dir: bool) -> None:
    """[中文] 限制路径访问权限，使其仅当前用户可以访问。

    POSIX 通过权限位（0700 目录 / 0600 文件）表示。Windows 没有此类权限位 —
    那里的 `os.chmod` 仅切换只读标志，因此 0600 的 chmod 是静默的空操作，
    文件会继承宽松的 ACL（SYSTEM、Administrators 等）。改用 ACL：剥离继承的条目，
    仅授予当前用户。在 Windows 上尽力而为，使临时的 icacls 失败绝不会阻止保存密钥。

    [English] Restrict a path so only the current user can access it.

    POSIX expresses this with mode bits (0700 dir / 0600 file). Windows has no such bits —
    `os.chmod` there only toggles the read-only flag, so a 0600 chmod is a silent no-op and
    the file inherits broad ACLs (SYSTEM, Administrators, …). Use an ACL instead: strip
    inherited entries and grant the current user alone. Best-effort on Windows so a transient
    icacls failure never blocks saving a key."""
    if _IS_WINDOWS:
        user = os.environ.get("USERNAME")
        if not user:
            return
        domain = os.environ.get("USERDOMAIN")
        account = f"{domain}\\{user}" if domain else user
        # [中文] 目录授权必须是可继承的 — 文件的对象继承 (OI)，子目录的容器继承 (CI) —
        # 以便内部创建的所有内容（SQLite 存储、对话等）都能继承用户的访问权限。
        # 如果缺少这些标志，/inheritance:r 会导致目录带有不可继承的 ACE，任何子文件最终都会具有空的 DACL
        # → sqlite3 报错“unable to open database file”，在启动时导致服务器崩溃。
        # [English]
        # A directory grant MUST be inheritable — (OI) object-inherit for files, (CI)
        # container-inherit for subdirs — so everything created inside (the SQLite stores,
        # conversations, …) inherits the user's access. Without these flags, /inheritance:r
        # leaves the directory with a non-inheritable ACE and any child file ends up with an
        # empty DACL → sqlite3 "unable to open database file", crashing the server on launch.
        grant = f"{account}:(OI)(CI)F" if is_dir else f"{account}:F"
        try:
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
                capture_output=True,
                check=False,
            )
        except OSError:
            pass
        return
    os.chmod(path, 0o700 if is_dir else 0o600)


def _atomic_private_write(target: Path, content: str) -> Path:
    """[中文] 原子性地将 `content` 写入 `target`，绝不通过可读的临时文件暴露内容。

    临时文件过去由 `Path.write_text` 创建，随后才调用 chmod，因此明文在写入期间按 umask 默认值
    （普通机器上为 0644）停留在磁盘上 — 可被所有本地进程以及备份该目录的任何工具读取。
    这就是 Issue #143；过去这里的两个写入器中都存在相同的模式。

    `tempfile.mkstemp` 在写入任何字节之前使用 0600 和 O_EXCL 创建文件，这也消除了固定的 `<name>.tmp` 文件名。
    那个名称是可预测的，因此本地攻击者可以预先将其创建为符号链接，从而使写入内容落入链接所指向的任意位置。

    Windows 上 mkstemp 不提供模式位，因此在写入内容之前，先将 ACL 应用于仍为空的文件。

    [English] Write `content` to `target` atomically, never exposing it through a readable temp.

    The temp file used to be created by `Path.write_text` and only chmod-ed afterwards, so
    the plaintext sat on disk at the umask default (0644 on a normal box) for the length of
    the write — readable by every local process and by anything backing the directory up.
    That is issue #143; the same pattern was in both writers here.

    `tempfile.mkstemp` creates with 0600 and O_EXCL before a byte is written, which also
    removes the fixed `<name>.tmp` filename. That name was predictable, so a local attacker
    could pre-create it as a symlink and have the write land wherever the link pointed.

    Windows gets no mode bits from mkstemp, so the ACL is applied to the still-empty file
    before the content goes in.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        _restrict_to_user(target.parent, is_dir=True)
    except OSError:
        pass

    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        _restrict_to_user(tmp, is_dir=False)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return target


def write_private_text(path: str | Path, content: str) -> Path:
    """[中文] 使用 SecretStore 的操作系统安全保护原子性写入仅限当前用户的文本文件。
    [English] Atomically write a user-only text file using the SecretStore's OS protections."""
    return _atomic_private_write(Path(path).expanduser(), content)


class SecretStore:
    """[中文] 基于文件支持的机密信息存储库。读取时解析 `${VAR}` 引用；状态查询绝不泄漏实际机密值。
    [English] File-backed secret store. Reads resolve `${VAR}` refs; status never leaks values."""

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self.path = Path(path).expanduser() if path else state_dir() / "secrets.json"
        self._dotenv_path = self.path.parent / ".env"
        self._lock = threading.Lock()

    # -- [中文] 读取操作 / [English] reads ------------------------------------------------------------------
    def get(self, profile: str) -> Optional[dict[str, Any]]:
        """[中文] 返回已解析 `${VAR}` 引用的 Profile，若不存在则返回 None。
        [English] Return a profile with `${VAR}` refs resolved, or None if absent."""
        data = self._read().get(profile)
        if data is None:
            return None
        return self.resolve(data)

    def resolve(self, value: Any) -> Any:
        """[中文] 从环境变量 + 本地 `.env` 中递归解析值中的 `${VAR}` 引用。
        [English] Resolve `${VAR}` refs in a value (recursively) from env + the local `.env`."""
        env = _load_dotenv(self._dotenv_path)

        def _walk(v: Any) -> Any:
            if isinstance(v, str):
                return _REF.sub(
                    lambda m: os.environ.get(m.group(1))
                    or env.get(m.group(1))
                    or m.group(0),
                    v,
                )
            if isinstance(v, dict):
                return {k: _walk(x) for k, x in v.items()}
            if isinstance(v, list):
                return [_walk(x) for x in v]
            return v

        return _walk(value)

    def status(self) -> list[dict[str, Any]]:
        """[中文] 仅包含 Profile 元数据 — **绝不包含**机密值本身。
        [English] Profile metadata only — **never** the secret values themselves."""
        out: list[dict[str, Any]] = []
        for profile, data in self._read().items():
            data = data if isinstance(data, dict) else {}
            expires = data.get("expires")
            expired = isinstance(expires, (int, float)) and expires < time.time()
            out.append(
                {
                    "profile": profile,
                    "type": data.get("type"),
                    "account": data.get("account_id"),
                    "expired": bool(expired),
                }
            )
        return out

    # -- [中文] 写入操作 / [English] writes -----------------------------------------------------------------
    def put(self, profile: str, data: dict[str, Any]) -> None:
        with self._lock:
            store = self._read()
            store[profile] = data
            self._write(store)

    def delete(self, profile: str) -> bool:
        with self._lock:
            store = self._read()
            if profile not in store:
                return False
            del store[profile]
            self._write(store)
            return True

    # -- [中文] 内部方法 / [English] internals --------------------------------------------------------------
    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, store: dict[str, Any]) -> None:
        _atomic_private_write(self.path, json.dumps(store, indent=2))


class EphemeralSecretStore(SecretStore):
    """[中文] 绝不触碰磁盘的临时机密信息存储库（SecretStore）。

    用于暂存发往另一台机器的连接器授权（机器规范 §Remote OAuth）：
    常规存储层针对此存储库运行，精确产生全新连接将写入的 Profile 键，
    随后对结果进行封印与传输 — 授权信息绝不会写入本地 secrets 文件。

    [English] A SecretStore that never touches disk.

    Used to stage a connector grant that is destined for ANOTHER machine
    (machines spec §Remote OAuth): the normal storage layers run against this
    store, producing exactly the profile keys a fresh connect would write, and
    the result is sealed and shipped — the grant never lands in the local
    secrets file.
    """

    def __init__(self) -> None:
        super().__init__(path="/dev/null/ephemeral")  # never read or written
        self._data: dict[str, Any] = {}

    def _read(self) -> dict[str, Any]:
        return dict(self._data)

    def _write(self, store: dict[str, Any]) -> None:
        self._data = dict(store)

    def profiles(self) -> dict[str, Any]:
        """[中文] 所有暂存的 Profile（按键索引）— 封装部署传输的有效负载。
        [English] Every staged profile, by key — the payload a sealed deploy ships."""
        return dict(self._data)
