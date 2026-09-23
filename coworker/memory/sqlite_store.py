"""基于 SQLite 的记忆存储库（默认适配器）。
SQLite-backed memory store (the default adapter)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Optional

from .base import MemoryItem, MemoryStore, Scope


class SQLiteMemoryStore(MemoryStore):
    """基于 SQLite 本地数据库实现的持久化记忆存储库。
    Persistent memory store implemented on a local SQLite database."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: 服务器在与创建存储库不同的线程上运行 WebSocket 处理程序；
        # 通过线程锁实现串行化安全访问。
        # check_same_thread=False: the server runs the WS handler on a different thread
        # than the store was created on; a lock serializes access.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                key TEXT,
                content TEXT NOT NULL,
                summary TEXT,
                workspace TEXT,
                session_id TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """)
        # 在引入 summary 列之前创建的历史数据库：缺少摘要的行在渲染时自动回退至截断的内容首行（无需复杂数据迁移）。
        # Databases created before the summary column existed: rows without one fall
        # back to a truncated first line of content at render time (no data migration).
        cols = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(memories)").fetchall()
        }
        if "summary" not in cols:
            self._conn.execute("ALTER TABLE memories ADD COLUMN summary TEXT")
        self._conn.commit()

    def add(
        self,
        content: str,
        *,
        scope: Scope = Scope.WORKSPACE,
        key: Optional[str] = None,
        summary: Optional[str] = None,
        workspace: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> MemoryItem:
        """添加一条新记忆。
        Add a new memory item."""
        scope = Scope(scope)
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO memories (scope, key, content, summary, workspace, session_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (scope.value, key, content, summary, workspace, session_id),
            )
            self._conn.commit()
            item = self.get(cursor.lastrowid)
        assert item is not None
        return item

    def get(self, item_id: int) -> Optional[MemoryItem]:
        """按 ID 获取记忆项。
        Get a memory item by ID."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memories WHERE id = ?", (item_id,)
            ).fetchone()
        return _row_to_item(row) if row else None

    def list(
        self,
        *,
        scope: Optional[Scope] = None,
        workspace: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> list[MemoryItem]:
        """列出符合作用域/工作区/会话过滤条件的记忆项。
        List memory items matching scope/workspace/session filter criteria."""
        query = "SELECT * FROM memories WHERE 1 = 1"
        params: list[object] = []
        if scope is not None:
            query += " AND scope = ?"
            params.append(Scope(scope).value)
        if workspace is not None:
            query += " AND workspace = ?"
            params.append(workspace)
        if session_id is not None:
            query += " AND session_id = ?"
            params.append(session_id)
        query += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_row_to_item(row) for row in rows]

    def update(
        self, item_id: int, content: str, *, summary: Optional[str] = None
    ) -> Optional[MemoryItem]:
        """更新记忆项的正文与摘要。
        Update content and summary of a memory item."""
        with self._lock:
            if summary is not None:
                self._conn.execute(
                    "UPDATE memories SET content = ?, summary = ? WHERE id = ?",
                    (content, summary, item_id),
                )
            else:
                self._conn.execute(
                    "UPDATE memories SET content = ? WHERE id = ?", (content, item_id)
                )
            self._conn.commit()
        return self.get(item_id)

    def delete(self, item_id: int) -> bool:
        """删除指定 ID 的记忆项。
        Delete a memory item by ID."""
        with self._lock:
            cursor = self._conn.execute("DELETE FROM memories WHERE id = ?", (item_id,))
            self._conn.commit()
        return cursor.rowcount > 0

    def delete_all(self, *, scope: Optional[Scope] = None) -> int:
        """删除所有记忆（可选择仅删除特定作用域）。返回删除的条目数。
        Delete every memory (optionally one scope). Returns the number removed."""
        with self._lock:
            if scope is not None:
                cursor = self._conn.execute(
                    "DELETE FROM memories WHERE scope = ?", (Scope(scope).value,)
                )
            else:
                cursor = self._conn.execute("DELETE FROM memories")
            self._conn.commit()
        return cursor.rowcount

    def rekey_workspace(self, old: str, new: str) -> int:
        """将工作区作用域的记忆从一个项目键重新映射到另一个项目键 — 路径到 Git 仓库键的一次性迁移。
        各行相互独立，因此与 `new` 下现有行的碰撞只是取并集。返回移动的行数。

        Re-key workspace-scoped memories from one project key to another — the
        twentieth-pass one-time path→git migration. Rows are independent, so a
        collision with existing rows under `new` is just a union. Returns the
        number of rows moved."""
        if old == new:
            return 0
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE memories SET workspace = ? WHERE workspace = ? AND scope = ?",
                (new, old, Scope.WORKSPACE.value),
            )
            self._conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        self._conn.close()


def _row_to_item(row: sqlite3.Row) -> MemoryItem:
    return MemoryItem(
        id=row["id"],
        scope=Scope(row["scope"]),
        content=row["content"],
        key=row["key"],
        summary=row["summary"],
        workspace=row["workspace"],
        session_id=row["session_id"],
        created_at=row["created_at"],
    )
