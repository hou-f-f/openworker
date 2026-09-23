"""[中文] `request_directory` 工具 —— 智能体请求用户授予对某个文件夹的访问权限。

与普通工具不同，该工具会被 TurnEngine 拦截：它会触发 DIRECTORY_REQUESTED 事件，
并等待用户在带外选择/批准文件夹（GUI 界面会弹出提示），
随后当前运行的会话将获得该根目录，工具结果则向智能体告知授权结果。
此处的函数仅作为 Schema 载体，以及针对未连接请求器的运行界面的安全回退实现。

The `request_directory` tool — the agent asks the user to grant access to a folder.

Unlike ordinary tools, this one is intercepted by the TurnEngine: it emits a DIRECTORY_REQUESTED
event and waits for the user to pick/approve a folder out-of-band (the GUI surfaces a prompt),
then the live session gains that root and the tool result tells the agent the outcome. The
callable here is only a schema carrier + a safe fallback for surfaces without a requester.
"""

from __future__ import annotations

from aisuite.agents import ToolMetadata, tool


def request_directory_tool() -> object:
    def request_directory(
        reason: str, path: str = "", writable: bool = False, primary: bool = False
    ) -> dict:
        """[中文] 当任务需要当前目录之外的文件时（例如读取用户提到的某个项目，或将交付物保存到特定位置），
        请求用户授予对目录的访问权限。在 `reason` 中说明原因；可选择性建议一个 `path` 并指定是否需要
        `writable` 写入权限。仅当被授予的文件夹应当成为会话的主工作区时（即整场对话的核心项目），
        才将 `primary=true` 设置为真 —— 该操作仅允许执行一次，且仅限会话仍在临时草稿目录中运行时。
        用户负责选择/批准文件夹；返回结果会说明是否已授予权限。切勿使用此工具逃逸沙箱 —— 仅用于响应用户请求。

        Ask the user for access to a directory when the task needs files outside the current
        ones (e.g. to read a project the user mentioned, or to save a deliverable somewhere
        specific). Explain why in `reason`; optionally suggest a `path` and whether you need
        `writable` access. Set `primary=true` only when the granted folder should become the
        session's main workspace (the project the whole conversation is about) — allowed once,
        and only while the session is still running on its scratch directory. The user
        picks/approves the folder; the result says whether it was granted. Do not use this to
        escape sandboxing — only to serve the user's request.
        """
        # [中文] 实际处理逻辑位于引擎中（需要走 GUI 的带外异步往返）。这里的函数体仅在未挂接请求器时运行（如无头运行界面）。
        # Real handling lives in the engine (it needs the out-of-band GUI round-trip). This body
        # only runs if no requester is wired (e.g. a headless surface).
        return {
            "granted": False,
            "error": "directory requests aren't available in this surface",
        }

    return tool(
        request_directory,
        metadata=ToolMetadata(
            category="filesystem",
            risk_level="low",
            capabilities=["request_directory"],
            description=(
                "Ask the user to grant access to a directory (read-only or read-write) when the "
                "task needs files outside the directories you already have."
            ),
        ),
    )
