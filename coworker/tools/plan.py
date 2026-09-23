"""[中文] `propose_plan` 工具 —— 智能体展示其实施计划并请求开始执行。

仅在会话以 plan 模式启动时注册。与 `request_directory` 类似，它会被 TurnEngine 拦截：
它会触发 PLAN_PROPOSED 事件并等待用户在带外做出决定。
若获批准，当前运行的 PermissionEngine 将退出 plan 模式（保持同一会话，完整保留之前的探索上下文）；
若遭拒绝，则返回用户的反馈意见，以便智能体修订计划。
此处的函数仅作为 Schema 载体，以及针对未连接审批器的界面的安全回退实现。

The `propose_plan` tool — the agent presents its plan and asks to start executing.

Registered only when the session starts in plan mode. Like `request_directory`, it is
intercepted by the TurnEngine: it emits a PLAN_PROPOSED event and waits for the user's
out-of-band decision. Approval flips the live PermissionEngine out of plan mode (same
session, full exploration context kept); rejection returns the user's feedback so the
agent can revise the plan. The callable here is only a schema carrier + a safe fallback
for surfaces without an approver.
"""

from __future__ import annotations

from aisuite.agents import ToolMetadata, tool


def propose_plan_tool() -> object:
    def propose_plan(plan: str) -> dict:
        """[中文] 向用户展示你的实施计划以待批准。一旦你完成了充分的探索并确定了实施方案，请使用此工具：
        总结你将做出哪些更改、涉及哪些文件、以及你将如何验证。
        如果获得批准，会话将退出只读的 plan 模式，由你开始实施计划；
        如果被拒绝，请根据结果中的反馈意见进行修订。
        切勿直接开始描述实施步骤就像你已经在做一样 —— 请先提交提议。

        Present your implementation plan to the user for approval. Use this once you
        have explored enough to commit to an approach: summarize what you'll change, in
        which files, and how you'll verify it. If approved, the session switches out of
        read-only plan mode and you implement the plan; if rejected, revise it using the
        feedback in the result. Don't start describing implementation steps as if you
        were doing them — propose first.
        """
        # [中文] 实际处理逻辑位于引擎中（需要走带外的批准往返流程）。
        # 这里的函数体仅在未挂接审批器时运行（如无头运行界面）。
        # Real handling lives in the engine (it needs the out-of-band approval round-trip).
        # This body only runs if no approver is wired (e.g. a headless surface).
        return {
            "approved": False,
            "error": "plan approval isn't available in this surface",
        }

    return tool(
        propose_plan,
        metadata=ToolMetadata(
            category="planning",
            risk_level="low",
            capabilities=["plan"],
            description=(
                "Present the implementation plan for user approval; approval exits "
                "read-only plan mode and starts execution."
            ),
        ),
    )
