"""【事件模型】TurnEngine 与前端交互层（TUI/GUI/IDE）之间的通信协议规范。
事件粒度细化到消息级与工具级，支持流式输出、推理思考过程、权限审批等各类系统事件。

Event model — the contract between the turn engine and any surface (TUI/GUI/IDE).

No token streaming in v1, so granularity is per-message/per-tool. Streaming later adds
`assistant_delta` / `tool_output_delta` without changing the rest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    # 轮次启动
    # Turn starts
    TURN_START = "turn_start"
    # 助手增量文本输出（流式文本块）
    # Assistant text delta (streaming chunk)
    ASSISTANT_DELTA = "assistant_delta"
    # 模型深度思考增量文本（仅供前端展示，历史重放时不注入模型上下文）
    # model thinking text (display-only, never replayed)
    REASONING_DELTA = "reasoning_delta"
    # 助手完整回复消息
    # Full assistant message
    ASSISTANT_MESSAGE = "assistant_message"
    # Agent 提议调用工具
    # Agent proposes a tool call
    TOOL_PROPOSED = "tool_proposed"
    # 触发安全门禁，需要审批许可
    # Permission required before tool execution
    PERMISSION_REQUIRED = "permission_required"
    # Agent 请求用户授予指定目录的访问权限
    # agent asks the user to grant a folder
    DIRECTORY_REQUESTED = "directory_requested"
    # Agent 请求安装或提供缺失的命令行工具（如扫描器等）
    # agent asks for a missing CLI tool (scanner, etc.)
    TOOL_REQUESTED = "tool_requested"
    # Agent 请求连接器授权（request_connector / grant_connector 门禁）
    # request_connector / grant_connector gate (§11.6)
    CONNECTOR_REQUESTED = "connector_requested"
    # Agent 向用户发起自由文本或多选问答
    # agent asks the user a free-text/multiple-choice question
    QUESTION_REQUESTED = (
        "question_requested"
    )
    # Agent 提交行动计划供用户批准（计划模式退出门禁）
    # agent presents a plan for approval (plan mode exit)
    PLAN_PROPOSED = (
        "plan_proposed"
    )
    # Team Lead 提议协作者团队花名册（人员配置门禁）
    # a lead proposes a worker roster (the staffing gate)
    TEAM_PROPOSED = (
        "team_proposed"
    )
    # Team Lead 提议工作任务条目（任务拆解门禁；与 propose_plan 不同，该操作与模式无关——批准后直接在看板上创建条目）
    # a lead proposes work items (the decomposition gate);
    # unlike propose_plan this is mode-independent — approval creates the items
    ITEMS_PROPOSED = (
        "items_proposed"
    )
    # 工具开始执行
    # Tool execution begins
    TOOL_STARTED = "tool_started"
    # 工具执行完毕
    # Tool execution finished
    TOOL_FINISHED = "tool_finished"
    # 单次模型与工具交互迭代结束
    # Single model↔tool iteration ends
    ITERATION_END = "iteration_end"
    # 整个 Turn 执行轮次结束
    # Full turn ends
    TURN_END = "turn_end"
    # 运行时错误
    # Runtime error
    ERROR = "error"
    # 执行被用户或系统中断
    # Execution interrupted
    INTERRUPTED = "interrupted"
    # 上下文压缩启动（前端展示过渡提示）
    # compaction started — surfaces show a transient signal
    COMPACTING = "compacting"
    # 出站历史记录已完成压缩（通过摘要提炼或尾部裁剪）
    # outbound history was compacted (summary or trim)
    COMPACTED = "compacted"
    # 回复因达到输出 Token 上限被截断且未产生工具调用；引擎提示模型采取行动并重新发起循环
    # OPE-171: the reply was cut off at the output-token limit with no tool call; the
    # engine nudged the model to act and is going round the loop again.
    CONTINUATION = "continuation"


@dataclass
class Event:
    """【执行事件】携带事件类型与具体负载数据的标准事件包。

    Standard event packet carrying event type and payload data.
    """
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
