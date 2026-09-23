"""[中文] `team-board` —— 在 stdio 上作为 MCP 服务器运行的看板与日志。

外部编码智能体加入团队的标准途径：其 MCP 配置运行
`ocw board mcp --url … --token … --space …`（或无头模式 `--db …`），
即可看到具有角色作用域的看板工具，用户进而指示其认领事项并开展工作。
身份凭据与权限控制绝不在本文件中实现：方言（dialect）已预先绑定到特定 actor（通过 token 或本地命令行参数），
且每次写入都由 store/server 底层进行权威裁决 —— 本文件仅是一个轻量级适配器，可以安全提供给任何测试框架或智能体宿主。

工具执行结果均为 JSON 格式 —— 为智能体提供结构化原始数据，而非格式化散文。

[English]
`team-board` — the board and journal as an MCP server on stdio.

The way an external coding agent joins a team: its MCP config runs
`ocw board mcp --url … --token … --space …` (or `--db …` headless), it sees the
role-scoped board tools, and the user asks it to claim an item and work. Identity
and authority never live here: the dialect is already bound to one actor (token or
local flags), and every write is judged by the store/server — this file is a thin
adapter, safe to hand to any harness.

Tool results are JSON — raw data for the agent, not prose.
"""

from __future__ import annotations

from typing import Any, Optional

from .model import BoardError


def build(dialect, *, space: str):
    """[中文] 为指定的方言+空间组装 FastMCP 服务器。与 serve() 分离，以便测试用例可以在无需传输层的情况下检查已注册的工具集。

    [English]
    Assemble the FastMCP server for one dialect+space. Split from serve() so
    tests can inspect the registered tool set without a transport."""
    from mcp.server.fastmcp import FastMCP

    who = dialect.whoami()
    role = who.get("role", "worker")
    mcp = FastMCP(
        "team-board",
        instructions=(
            f"A shared team work board (you are '{who.get('actor')}', role"
            f" {role}) plus the team journal. Items carry acceptance criteria —"
            " what gets verified before they can be done. Typical worker loop:"
            " board_list → board_claim an open item → board_move to in_progress →"
            " work, journal_append findings as you go → board_move to review with"
            " a hand-off comment and refs. Never mark items done — done is the"
            " verdict after review."
        ),
    )

    def _safe(func, *args, **kwargs) -> Any:
        try:
            result = func(*args, **kwargs)
            if func.__name__ in ("transition", "assign", "claim", "set_status", "comment", "link"):
                from .tools import mutation_receipt
                return mutation_receipt(result)
            return result
        except (BoardError, ValueError) as error:
            return {"error": str(error)}

    @mcp.tool()
    def board_list(state: str = "", assignee: str = "", after_item: int = 0, limit: int = 50) -> Any:
        """[中文] 列出看板上的工作事项，可按状态（open/in_progress/blocked/review/done/canceled）或分配对象过滤。

        [English]
        List work items on the board, optionally filtered by state
        (open/in_progress/blocked/review/done/canceled) or assignee."""
        result = _safe(
            dialect.list_items, space, state=state or None, assignee=assignee or None
        )
        if not isinstance(result, list):
            return result
        if isinstance(after_item, bool) or not isinstance(after_item, int) or after_item < 0:
            return {"error": "after_item must be a non-negative integer"}
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            return {"error": "limit must be between 1 and 100"}
        from .tools import item_snapshot
        items = [i for i in result if i["id"] > after_item]
        page = [item_snapshot(i, brief=True) for i in items[:limit]]
        for i in page:
            if i.get("proposal_ref"):
                i["proposal_ref"]["read_tool"] = "board_proposal"
        return {"items": page, "has_more": len(items) > limit,
                "next_after_item": page[-1]["id"] if page else after_item}

    @mcp.tool()
    def board_show(item: int) -> Any:
        """[中文] 获取当前任务详情，而非历史评论。使用 board_comments 获取增量交接信息，使用 board_proposal 获取共享的已批准意图。

        [English]
        Current task details, not historical comments. Use board_comments for
        incremental handoffs and board_proposal for shared approved intent."""
        from .tools import item_snapshot
        result = _safe(dialect.get_item, space, item)
        snapshot = item_snapshot(result)
        if snapshot.get("proposal_ref"):
            snapshot["proposal_ref"]["read_tool"] = "board_proposal"
        return snapshot

    @mcp.tool()
    def board_comments(item: int, after_seq: int = 0, limit: int = 20) -> Any:
        """[中文] 获取指定序号后的完整新评论。当 has_more 为 True 时跟进 next_after_seq。序号为 0 表示重放；超大评论使用 board_comment_text。

        [English]
        Complete new comments after a sequence. Follow next_after_seq while
        has_more. Zero replays; oversized comments use board_comment_text."""
        result = _safe(dialect.comment_page, space, item, after_seq=after_seq, limit=limit)
        for entry in result.get("comments", []):
            if entry.get("read_tool"):
                entry["read_tool"] = "board_comment_text"
        return result

    @mcp.tool()
    def board_comment_text(item: int, seq: int, offset: int = 0, max_chars: int = 12000) -> Any:
        """[中文] 分页受限读取单条评论的确切内容；当 has_more 为 True 时跟进 next_offset。 / [English] Read an exact comment in bounded pages; follow next_offset while has_more."""
        return _safe(dialect.comment_text, space, item, seq=seq, offset=offset, max_chars=max_chars)

    @mcp.tool()
    def board_proposal(item: int) -> Any:
        """[中文] 读取共享的已批准提案意图与外部操作声明；这些并不是权限授予。仅在首次读取或缺少上下文时读取。

        [English]
        Read shared approved proposal intent and external-action declarations;
        these are not permission grants. Read once or when context is missing."""
        result = _safe(dialect.get_item, space, item)
        return result if "error" in result else {"proposal": result.get("proposal"), "authority": "intent_not_access_grants"}

    @mcp.tool()
    def board_create(
        title: str,
        criteria: str,
        description: str = "",
        parent: Optional[int] = None,
        case: str = "",
    ) -> Any:
        """[中文] 提交新的工作事项（处于 open 状态，未分配 —— 只有在被分配或认领后工作才正式开始）。
        `criteria` 为验收标准 —— 事项完成前必须被验证的内容；必填项。

        [English]
        File a new work item (open, unassigned — work starts when it is
        assigned or claimed). `criteria` is the acceptance criteria — what gets
        verified before the item can be done; required."""
        return _safe(
            dialect.create_item,
            space,
            title=title,
            criteria=criteria,
            description=description,
            parent=parent,
            case=case or None,
        )

    @mcp.tool()
    def board_claim(item: int) -> Any:
        """[中文] 为自己认领一个处于 open 状态且未分配的事项。先认领者胜出；该事项将成为你的分配任务。仅认领你当前可以立即开始的工作。

        [English]
        Claim an open, unassigned item for yourself. First claim wins; the
        item becomes your assignment. Only claim work you can start on now."""
        return _safe(dialect.claim, space, item)

    @mcp.tool()
    def board_move(item: int, to: str, comment: str = "", refs: list[str] = []) -> Any:
        """[中文] 移动工作事项状态：开始工作时置为 in_progress；受阻时置为 blocked 并将阻碍原因写入 `comment`；完成工作时置为 review 并附带交接说明及制品引用（分支、PR、file:line）。

        [English]
        Move a work item: in_progress when you start, blocked with the blocker
        as `comment`, review with a hand-off comment and artifact refs (branch,
        PR, file:line) when finished."""
        return _safe(
            dialect.transition, space, item, to, comment=comment, refs=list(refs or [])
        )

    @mcp.tool()
    def board_comment(item: int, body: str, refs: list[str] = [], needs_attention: bool = False) -> Any:
        """[中文] 在工作事项上发表评论 —— 持久化并归属到发表者；重要的答复应当写在此处。常规备忘是静默的；needs_attention=True 会唤醒 lead 以获取明确的疑问/决策支持。Review 状态流转即是工作移交。`refs` 用于附加制品指针。

        [English]
        Comment on a work item — durable and attributed; answers that matter
        belong here. Routine notes are quiet; needs_attention=True wakes the lead
        for an explicit question/decision. Review transitions are the handoff.
        `refs` attach artifact pointers."""
        return _safe(dialect.comment, space, item, body, refs=list(refs or []), needs_attention=needs_attention)

    @mcp.tool()
    def board_attach(item: int, path: str, caption: str = "") -> Any:
        """[中文] 从本地文件向工作事项附加屏幕截图或图片（png/jpg/gif/webp，≤10MB）—— 以便 lead/reviewer 能够直观看到你的成果。请附带说明该图片展示内容的标题（caption）。与 review 移交配合使用效果极佳。

        [English]
        Attach a screenshot or image (png/jpg/gif/webp, ≤10MB) from a local
        file to a work item — so the lead/reviewer can SEE what you did. Give it
        a caption saying what the image shows. Great with review hand-offs."""
        from .attachments import read_image_file

        try:
            data, name = read_image_file(path)
        except (BoardError, ValueError, OSError) as error:
            return {"error": str(error)}
        return _safe(
            dialect.attach,
            space,
            item,
            data,
            name,
            caption=caption,
        )

    @mcp.tool()
    def board_pending() -> Any:
        """[中文] 你的未消费事件流：包含分配给你或由你归档的事项上的所有事件 —— 任务指派、附带反馈的退回重修、来自 lead 或用户的评论、取消操作等。在开始工作会话时以及结束前均应检查；通过 board_consume 进行确认。

        [English]
        Your unconsumed feed: every event on items assigned to you or filed
        by you — assignments, send-backs with feedback, comments from the lead
        or user, cancellations. Check at the start of a work session and before
        finishing; acknowledge with board_consume."""
        return _safe(dialect.pending, space)

    @mcp.tool()
    def board_consume(upto_seq: int) -> Any:
        """[中文] 确认截止到某个序列号（来自 board_pending）的事件流，使其不再被重复推送。

        [English]
        Acknowledge feed events up to a sequence number (from board_pending),
        so they are not re-delivered."""
        return _safe(lambda: (dialect.consume(space, upto_seq), {"ok": True})[1])

    if role == "worker":

        @mcp.tool()
        def board_set_status(item: int, text: str) -> Any:
            """[中文] 在当前分配给你的事项上设置单行只读展示进度（最多 80 个字符）。这不会更改状态，也不会唤醒 lead。

            [English]
            Set one display-only progress line (at most 80 characters) on an item
            currently assigned to you. Does not change state or wake the lead."""
            return _safe(dialect.set_status, space, item, text)

    if role in ("lead", "user"):

        @mcp.tool()
        def board_assign(item: int, assignee: str) -> Any:
            """[中文] 将工作事项分配给 worker（或分配给你自己以预留该任务）。

            [English]
            Assign a work item to a worker (or to yourself to reserve it)."""
            return _safe(dialect.assign, space, item, assignee)

        @mcp.tool()
        def board_link(src: int, kind: str, dst: int) -> Any:
            """[中文] 关联两个事项：`parent`（dst 成为 src 的父级）或 `blocks`（src 阻碍 dst）。

            [English]
            Link two items: `parent` (dst becomes src's parent) or `blocks`
            (src blocks dst)."""
            return _safe(dialect.link, space, src, kind, dst)

        @mcp.tool()
        def board_policy(claims: str = "") -> Any:
            """[中文] 查看看板的认领策略，或进行设置：`open`（允许 worker 自主认领 open 事项）或 `lead-only`（仅限 lead 分配）。

            [English]
            Show the board's claim policy, or set it: `open` (workers may
            self-claim open items) or `lead-only`."""
            if claims:
                return _safe(dialect.set_policy, space, claims=claims)
            return _safe(dialect.policy, space)

    @mcp.tool()
    def journal_append(
        case: str,
        body: str,
        kind: str = "note",
        item: Optional[int] = None,
        entities: list[str] = [],
        refs: list[str] = [],
    ) -> Any:
        """[中文] 在工作进行中向日志案例追加记录：kind 可以是 finding（发现）、evidence（证据）、decision（决策）、note（笔记）或 raw（引用文件的原始抓取摘录）。`entities` 是该记录涉及的具体事物（路径、资源、ID）。

        [English]
        Append to a journal case as you work: kind is finding, evidence,
        decision, note, or raw (a capture excerpt referencing a file).
        `entities` are the concrete things it is about (paths, resources, ids)."""
        return _safe(
            dialect.journal_append,
            case,
            body,
            kind=kind,
            space=space,
            item=item,
            entities=list(entities or []),
            refs=list(refs or []),
        )

    @mcp.tool()
    def journal_read(
        case: str,
        item: Optional[int] = None,
        author: str = "",
        kind: str = "",
        entity: str = "",
        include_raw: bool = False,
        limit: int = 50,
    ) -> Any:
        """[中文] 读取日志案例，可按事项、作者、条目类型或实体进行过滤。建议尽量使用精准窄范围读取；除非明确要求，否则跳过 raw 原始抓取记录。

        [English]
        Read a journal case, filtered by item, author, entry kind, or entity.
        Prefer narrow reads; raw captures are skipped unless asked."""
        return _safe(
            dialect.journal_read,
            case,
            item=item,
            author=author or None,
            kind=kind or None,
            entity=entity or None,
            include_raw=include_raw,
            limit=limit,
        )

    @mcp.tool()
    def journal_cases() -> Any:
        """[中文] 你有权读取的日志案例列表，包含条目计数。

        [English]
        The journal cases you can read, with entry counts."""
        return _safe(dialect.journal_overview)

    return mcp


# [中文] 启动并运行团队 MCP stdio 服务
# [English] Run team MCP stdio server
def serve(dialect, *, space: str) -> None:
    build(dialect, space=space).run("stdio")
