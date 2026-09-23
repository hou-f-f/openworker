"""[中文] 待办事项 / 计划工具 —— 智能体维护且前端 UI 呈现的结构化任务列表。

这是在交互式工作中营造“条理分明的智能体”体验的关键组件。低风险，自动批准。
列表保存在前端或上层可以读取的 `TodoList` 中；`todo_write` 每次全量替换该列表。

Todo / plan tool — a structured task list the agent maintains and the UI renders.

Most of the "organized agent" feel in interactive work. Low risk, auto-approved. The list
is held in a `TodoList` the surface can read; `todo_write` replaces it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import aisuite as ai

_STATUSES = {"pending", "in_progress", "done"}

# [中文] 显式 Schema —— 对象数组结构无法可靠地自动推导生成，且各大模型服务商会拒绝裸 `list` 类型注解。
# 通过 `__coworker_schema__` 注册。
#
# 参数名为 `todos`，而不是 `items`：在至少一个托管聊天模板中（如 Together 上的 GLM-5.2，2026-07-21），
# 名为 "items" 的顶层参数键会遮蔽 minijinja 模板内置的 `.items()` 字典方法（报 "object is not callable" 错误），
# 导致每次回放该调用的请求都报 400 失败。任何不是 minijinja 字典方法的参数名都是安全的；切勿改回 items。
# Explicit schema — the array-of-objects shape can't be auto-generated reliably, and
# providers reject a bare `list` annotation. Registered via `__coworker_schema__`.
#
# The parameter is `todos`, NOT `items`: a top-level argument key named "items" shadows
# minijinja's `.items()` map method in at least one hosted chat template (Together's
# GLM-5.2, 2026-07-21 — "object is not callable"), 400-ing every request that replays
# the call. Any key name that isn't a minijinja map method is safe; never rename back.
_TODO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "todo_write",
        "description": "Replace the task list. Provide the full list of todos each call.",
        "parameters": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "done"],
                            },
                        },
                        "required": ["content", "status"],
                    },
                }
            },
            "required": ["todos"],
        },
    },
}


@dataclass
class TodoList:
    items: list[dict] = field(default_factory=list)


def todo_tools(todo: TodoList) -> list:
    """[中文] 返回绑定到指定 TodoList 实例的待办事项工具集。
    Return the todo tools bound to the specified TodoList instance.
    """
    def todo_write(todos: list = None, items: list = None) -> dict:
        """[中文] 替换任务列表。每个待办事项都是一个对象，包含 `content` 以及值为 pending、in_progress 或 done 的 `status`。

        Replace the task list. Each todo is an object with `content` and a `status`
        of pending, in_progress, or done."""
        # [中文] 继续接受 `items`（兼容某些自由发挥使用旧名称的模型，以及队列中待回放的历史调用）。
        # `items` stays accepted (models that free-style the old name; queued replays).
        normalized = []
        for entry in (todos if todos is not None else items) or []:
            if isinstance(entry, dict):
                status = entry.get("status", "pending")
                if status == "completed":  # [中文] 某些模型对 "done" 的常见别名 / common model alias for our "done"
                    status = "done"
                normalized.append(
                    {
                        "content": str(entry.get("content", "")),
                        "status": status if status in _STATUSES else "pending",
                    }
                )
            else:
                normalized.append({"content": str(entry), "status": "pending"})
        todo.items = normalized
        return {"count": len(normalized), "todos": normalized}

    wrapped = ai.tool(
        todo_write,
        metadata=ai.ToolMetadata(
            category="planning",
            risk_level="low",
            capabilities=["todo"],
        ),
    )
    wrapped.__coworker_schema__ = _TODO_SCHEMA
    return [wrapped]
