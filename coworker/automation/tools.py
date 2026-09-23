"""[中文] 面向智能体的调度工具（Cowork + MyHelper）。

`create_scheduled_task` 设有门禁保护（`requires_approval`），因此在创建常驻自动化之前会先弹出确认卡片（在创建时审批）。智能体自行将自然语言（“每天晚上7:10”）转换为 cron 字符串。工具与发起源绑定：创建的任务记录发起会话并在其工作区中运行，以便原始对话可以读取结果（产物是该文件夹中的实际文件）。

[English]
Agent-facing scheduling tools (Cowork + MyHelper).

`create_scheduled_task` is gated (`requires_approval`) so it surfaces a confirm card before a
standing automation is created (approve-at-creation). The agent converts natural language
("7:10pm everyday") into a cron string itself. Tools are origin-bound: a created task records
the launching session and runs in its workspace, so the origin conversation can read the
results (the artifacts are real files in that folder).
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import aisuite as ai

from .models import Schedule, ScheduledTask, grant_entries
from .store import TaskStore

_CREATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_scheduled_task",
        "description": (
            "Create a scheduled automation that re-runs `instructions` on a schedule. Convert "
            "the user's natural-language timing into a cron expression yourself (e.g. "
            "'every day at 7:10pm' → '10 19 * * *'), or pass a one-time `fire_at` ISO datetime. "
            "The user confirms before it is created."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short label, e.g. 'Daily news briefing'.",
                },
                "instructions": {
                    "type": "string",
                    "description": (
                        "What to do on each run, written as a direct command to execute "
                        "immediately (e.g. 'Prepare a market analysis report covering …'). Do "
                        "NOT restate the schedule or timing here — timing belongs in cron/"
                        "fire_at; this text is handed verbatim to the agent every run."
                    ),
                },
                "cron": {
                    "type": "string",
                    "description": "5-field cron, e.g. '10 19 * * *'. Omit for one-time.",
                },
                "fire_at": {
                    "type": "string",
                    "description": "ISO datetime for a one-time run. Omit for recurring.",
                },
                "timezone": {
                    "type": "string",
                    "description": "IANA tz, e.g. 'America/New_York'. Defaults to the machine's local time — pass it only to override.",
                },
                "permissions": {
                    "type": "array",
                    "description": (
                        "What this automation will touch, surfaced on the creation consent "
                        "card. List every external read and write the instructions imply. "
                        "Reads (access:'read') are disclosure only. Writes (access:'write') "
                        "become standing grants IF the user approves: the automation may then "
                        "call that exact tool against that exact target without asking each "
                        "run. Targets must be exact (a channel address like 'slack:T…/C…', a "
                        "recipient) — no wildcards. Omit writes whose target you don't know "
                        "yet; the run will ask instead."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {
                                "type": "string",
                                "description": "Exact tool name, e.g. 'send_message'.",
                            },
                            "target": {
                                "type": "string",
                                "description": "The exact target argument value the rule binds to.",
                            },
                            "access": {
                                "type": "string",
                                "enum": ["read", "write"],
                                "description": "'write' proposes a standing grant; 'read' is disclosure.",
                            },
                        },
                        "required": ["tool", "target", "access"],
                    },
                },
            },
            "required": ["title", "instructions"],
        },
    },
}

_UPDATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_scheduled_task",
        "description": "Enable/disable or edit a scheduled task (its instructions, cron, or title).",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "enabled": {"type": "boolean"},
                "instructions": {"type": "string"},
                "cron": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["id"],
        },
    },
}

_ID_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delete_scheduled_task",
        "description": "Delete a scheduled task and its run history.",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
}

_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_scheduled_tasks",
        "description": "List the user's scheduled tasks (title, schedule, next run, status).",
        "parameters": {"type": "object", "properties": {}},
    },
}


# [中文] 为工具函数绑定 Schema 元数据与审批关卡标记
# [English] Bind schema metadata and approval gate flags to tool function
def _gated(func: Callable, schema: dict, *, approval: bool) -> Callable:
    func.__name__ = schema["function"]["name"]
    func.__doc__ = schema["function"]["description"]
    func.__aisuite_tool_metadata__ = ai.ToolMetadata(
        name=schema["function"]["name"],
        category="automation",
        risk_level="medium" if approval else "low",
        capabilities=["scheduling"],
        requires_approval=approval,
    )
    func.__coworker_schema__ = schema
    return func


# [中文] 构建并返回面向智能体的调度工具集（创建、列举、更新、删除任务）
# [English] Construct and return agent-facing scheduling tools (create, list, update, delete tasks)
def scheduling_tools(
    store: TaskStore,
    *,
    origin: dict[str, Any],
    default_workspace: str,
) -> list[Callable[..., Any]]:
    # [中文] 创建定时自动化任务（需人工确认批准）
    # [English] Create a scheduled automation task (requires human approval)
    def create_scheduled_task(
        title, instructions, cron=None, fire_at=None, timezone="local", permissions=None
    ):
        from croniter import croniter

        if not cron and not fire_at:
            return {
                "error": "provide a cron (recurring) or a fire_at ISO datetime (one-time)"
            }
        if cron and not croniter.is_valid(cron):
            return {"error": f"invalid cron expression: {cron}"}
        schedule = Schedule(
            kind="once" if (fire_at and not cron) else "cron",
            cron=cron,
            fire_at=fire_at,
            timezone=timezone or "local",
        )
        workspace = origin.get("workspace") or default_workspace
        # [中文] 智能体提议权限；用户通过批准此受控调用进行授权（许可卡片展示该提案）。
        # 仅已验证的写权限生效：工具必须声明目标参数（绝非执行/破坏性工具），且目标非空。
        # [English]
        # The agent PROPOSES permissions; the human granted them by approving this gated
        # call (the consent card rendered the proposal). Only validated write grants stick:
        # tool must declare a target argument (never exec/destructive), target non-empty.
        grants = grant_entries(permissions)
        task = ScheduledTask(
            title=title,
            instructions=instructions,
            schedule=schedule,
            workspace=workspace,
            origin_surface=origin.get("surface", "cowork"),
            origin_session_id=origin.get("session_id", ""),
            agent=origin.get("agent", "cowork"),
            always_allowed_tools=grants,
        )
        store.save(task)
        return {
            "ok": True,
            "id": task.id,
            "title": title,
            "schedule": schedule.human(),
            "next_run": task.next_run,
            "workspace": workspace,
            "always_allowed": grants,
        }

    # [中文] 列举所有定时任务（公开元数据）
    # [English] List all scheduled tasks (public metadata)
    def list_scheduled_tasks():
        return {"tasks": [t.public() for t in store.list()]}

    # [中文] 更新定时任务状态或属性（需人工确认批准）
    # [English] Update scheduled task status or properties (requires human approval)
    def update_scheduled_task(
        id, enabled=None, instructions=None, cron=None, title=None
    ):
        from croniter import croniter

        task = store.get(id)
        if task is None:
            return {"error": f"no such task: {id}"}
        if cron is not None:
            if not croniter.is_valid(cron):
                return {"error": f"invalid cron expression: {cron}"}
            task.schedule.cron = cron
            task.schedule.kind = "cron"
        if enabled is not None:
            task.enabled = bool(enabled)
        if instructions is not None:
            task.instructions = instructions
        if title is not None:
            task.title = title
        store.save(task)
        return {"ok": True, "task": task.public()}

    # [中文] 删除定时任务及其运行历史（需人工确认批准）
    # [English] Delete scheduled task and its run history (requires human approval)
    def delete_scheduled_task(id):
        return {"ok": store.delete(id), "id": id}

    return [
        _gated(create_scheduled_task, _CREATE_SCHEMA, approval=True),
        _gated(list_scheduled_tasks, _LIST_SCHEMA, approval=False),
        _gated(update_scheduled_task, _UPDATE_SCHEMA, approval=True),
        _gated(delete_scheduled_task, _ID_SCHEMA, approval=True),
    ]
