"""[中文] 连接层级体系（UI-REFRESH §4）—— 按角色（Persona）和按会话（Session）的连接器配置层级。

三个层级共同决定某个连接器对特定会话是否*生效（effective）*：

1. **账户已连接（account-connected）** —— 存在包含有效凭证的连接器配置项（``connector_list[].connected``）。由 SecretStore 掌管，不存储在此处。
2. **角色默认启用（persona-default-enabled）** —— 针对每个 Persona，哪些已连接的连接器默认对其会话开启（``PersonaConnectionStore``）。由 Persona 清单的 ``recommends`` 进行初始化植入，随后支持用户编辑。
3. **会话级覆盖（session-override）** —— 针对特定会话的显式开/关设置，优先于 Persona 默认设置（``SessionConnectionStore``）。没有覆盖记录则意味着*继承 Persona 默认设置*。

``effective(connector)`` = **已连接（connected）** 且（存在 ``session_override`` 则以此为准；否则若存在 Persona 默认值则以此为准；否则继承开启）。未连接的连接器绝不会生效。对既没有 Persona 倾向又没有会话覆盖的已连接连接器，默认继承*开启* —— Persona 的 ``recommends`` 只是筛选出需要*推荐/默认植入开启*的内容，并非穷尽式的白名单，因此 Persona 从未提及的已连接连接器除非被显式关闭，否则依然可用。

两个存储器均为镜像 ``SubscriptionStore`` 模式的轻量 JSON 文件；管理器各持有一个实例并通过 :func:`effective` 解析最终生效集。

[English]
Connection hierarchy (UI-REFRESH §4) — the per-persona + per-session connector layers.

Three layers gate whether a connector is *effective* for a session:

1. **account-connected** — a connector profile with valid creds exists (``connector_list[].connected``).
   Owned by the SecretStore; not stored here.
2. **persona-default-enabled** — per persona, which connected connectors are on by default for its
   sessions (``PersonaConnectionStore``). Seeded from the persona manifest's ``recommends`` and then
   user-editable.
3. **session-override** — per session, an explicit on/off that overrides the persona default
   (``SessionConnectionStore``). Absence of an override means *inherit the persona default*.

``effective(connector)`` = **connected** AND (``session_override`` if present, else the persona
default if present, else inherit-on). A connector that is not connected is never effective. A
connector with no persona opinion and no session override inherits *on* — the persona's
``recommends`` curates what to *suggest*/seed-on, it is not an exhaustive allow-list, so a connected
connector the persona never mentions stays available unless something explicitly turns it off.

Both stores are tiny JSON files mirroring ``SubscriptionStore`` (optional path, ``_load``/``_save``,
``indent=2``); the manager owns one of each and resolves via :func:`effective`.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional


class PersonaConnectionStore:
    """[中文] ``{persona_id: {connector: bool}}`` —— 每个 Persona 对各连接器的默认开/关配置。

    [English]
    ``{persona_id: {connector: bool}}`` — the per-persona default on/off for each connector."""

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._rows: dict[str, dict[str, bool]] = {}
        self._load()

    def _load(self) -> None:
        if self.path and self.path.is_file():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._rows = {
                pid: {str(c): bool(v) for c, v in (row or {}).items()}
                for pid, row in data.get("personas", {}).items()
            }

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"personas": self._rows}, indent=2),
            encoding="utf-8",
        )

    # -- queries ----------------------------------------------------------------
    def get(self, persona_id: str) -> dict[str, bool]:
        """[中文] 获取该 Persona 存储的配置字典（副本）。若从未植入/编辑过则返回空字典 —— 本方法**不执行**植入初始化；如需从清单初始化请使用 :meth:`defaults_for`。

        [English]
        The persona's stored row (a copy). Empty dict if it was never seeded/edited — this does
        NOT seed; use :meth:`defaults_for` to seed from a manifest."""
        return dict(self._rows.get(persona_id, {}))

    def defaults_for(
        self, persona_id: str, manifest, *, connected: set[str]
    ) -> dict[str, bool]:
        """[中文] 获取 Persona 的默认连接器映射，首次读取时根据清单进行初始化植入。

        植入规则：``recommends`` 中类型为 ``connector`` 且 ``tier == "core"`` 的项默认为 **True**；其他所有推荐的连接器（可选连接器）默认为 **False**。（忽略 mcp 推荐和非连接器类型。）植入的行在首次读取时持久化，此后保持稳定 —— 后续的编辑/切换会覆盖此记录。没有清单的 Persona（如内置角色）植入空记录。

        注意：此处故意偏离了 §4.2 字面上“仅连接状态为已连接的连接器”的表述，以尊重其设计初衷。核心连接器即便尚未连接也会植入 True：:func:`effective` 本身已对 ``connected`` 进行了关卡过滤，因此在未连接时保持过滤状态，并在**之后连接时自发点亮生效** —— 而不是永远冻结为 False（陈旧植入会破坏“连接核心连接器 → 默认启用”的业务流程）。保留签名的 ``connected`` 参数以保持向后兼容，但此处不再读取，使 :func:`effective` 的已连接门禁成为判断连通性的唯一真理源。

        [English]
        The persona's default connector map, seeding it from the manifest on first read.

        Seeding rule: a ``recommends`` item of kind ``connector`` with ``tier == "core"`` defaults
        **True**; every other recommended connector (optional) defaults **False**. (mcp recommends
        and non-connector kinds are ignored.) The seeded row is persisted on first read so the seed
        is stable thereafter — a later edit/toggle persists over it. A persona with no manifest
        (e.g. a builtin) seeds an empty row.

        NOTE: this intentionally deviates from §4.2's literal "whose connector is connected" wording
        to honor its intent. A core connector seeds True even when not connected yet:
        :func:`effective` already gates on ``connected``, so it stays filtered out while
        disconnected and **self-lights when it later connects** — rather than being frozen False
        forever (a stale seed that would break the "connect a core connector → on by default"
        flow). ``connected`` is kept in the signature for back-compat but is no longer read here,
        leaving :func:`effective`'s connected-gate the single source of truth for connectedness.
        """
        with self._lock:
            if persona_id in self._rows:
                return dict(self._rows[persona_id])
            seeded: dict[str, bool] = {}
            recommends = list(getattr(manifest, "recommends", None) or [])
            for rec in recommends:
                if getattr(rec, "kind", None) != "connector":
                    continue
                # core → on by default (connectedness is enforced later by effective()).
                seeded[rec.ref] = getattr(rec, "tier", "") == "core"
            self._rows[persona_id] = seeded
            self._save()
            return dict(seeded)

    # -- mutations --------------------------------------------------------------
    # [中文] 设置指定 Persona 对特定连接器的默认启用状态
    # [English] Set default enabled state for a connector on a persona
    def set(self, persona_id: str, connector: str, enabled: bool) -> None:
        with self._lock:
            self._rows.setdefault(persona_id, {})[connector] = bool(enabled)
            self._save()


class SessionConnectionStore:
    """[中文] ``{session_id: {connector: bool}}`` —— 仅存储每会话覆盖设置；条目不存在即代表会话继承 Persona 默认设置。

    [English]
    ``{session_id: {connector: bool}}`` — per-session overrides only; an absent entry means the
    session inherits the persona default."""

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._rows: dict[str, dict[str, bool]] = {}
        self._load()

    def _load(self) -> None:
        if self.path and self.path.is_file():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._rows = {
                sid: {str(c): bool(v) for c, v in (row or {}).items()}
                for sid, row in data.get("sessions", {}).items()
            }

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"sessions": self._rows}, indent=2),
            encoding="utf-8",
        )

    # -- queries ----------------------------------------------------------------
    # [中文] 获取指定会话的连接器覆盖字典
    # [English] Get connector overrides dict for a session
    def get(self, session_id: str) -> dict[str, bool]:
        return dict(self._rows.get(session_id, {}))

    # -- mutations --------------------------------------------------------------
    # [中文] 设置指定会话对特定连接器的覆盖启用状态
    # [English] Set connector override for a session
    def set(self, session_id: str, connector: str, enabled: bool) -> None:
        with self._lock:
            self._rows.setdefault(session_id, {})[connector] = bool(enabled)
            self._save()

    def clear(self, session_id: str, connector: str) -> None:
        """[中文] 清除单个连接器的覆盖设置，使会话重新继承 Persona 默认值。

        [English]
        Drop a single override so the session inherits the persona default again."""
        with self._lock:
            row = self._rows.get(session_id)
            if row and connector in row:
                del row[connector]
                if not row:
                    del self._rows[session_id]
                self._save()

    def remove_session(self, session_id: str) -> None:
        """[中文] 删除某个会话的所有覆盖记录（在会话被删除时调用）。

        [English]
        Drop all of a session's overrides (called when the session is deleted)."""
        with self._lock:
            if session_id in self._rows:
                del self._rows[session_id]
                self._save()


def effective(
    *,
    connected: set[str],
    persona_defaults: dict[str, bool],
    session_overrides: dict[str, bool],
) -> dict[str, bool]:
    """[中文] 解析某会话最终生效启用的连接器集合 —— §4 不变量。

    对于每个**已连接（connected）**的连接器：若存在会话覆盖则以覆盖为准；否则若存在 Persona 默认值则应用该默认值；否则默认继承开启。未连接的连接器绝不生效。仅返回最终**启用（enabled）**的连接器，每个映射为 ``True``（静音/关闭的连接器被省略），以便结果直接作为会话当前的活跃连接器集合。

    [English]
    Resolve the effective-enabled connectors for a session — the §4 invariant.

    For each **connected** connector: a session override (if present) wins; otherwise the persona
    default (if present) applies; otherwise it inherits *on*. Not-connected connectors are never
    effective. Returns only the effective-**enabled** connectors, each mapped to ``True`` (muted /
    off connectors are omitted), so the result reads as the session's live connector set.
    """
    out: dict[str, bool] = {}
    for connector in connected:
        if connector in session_overrides:
            enabled = session_overrides[connector]
        elif connector in persona_defaults:
            enabled = persona_defaults[connector]
        else:
            enabled = True  # connected, no opinion → inherit on
        if enabled:
            out[connector] = True
    return out
