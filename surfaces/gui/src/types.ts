/**
 * [中文] WebSocket 事件类型枚举，代表后端引擎推送到前端的所有生命周期事件。
 * [English] WebSocket event type enum, representing all lifecycle events pushed from the backend engine to the frontend.
 */
export type EventType =
  | "ready"
  | "inbound"
  | "turn_start"
  | "assistant_delta"
  | "reasoning_delta"
  | "assistant_message"
  | "tool_proposed"
  | "permission_required"
  | "directory_requested"
  | "tool_requested"
  | "connector_requested"
  | "question_requested"
  | "plan_proposed"
  | "team_proposed"
  | "items_proposed"
  | "tool_started"
  | "tool_finished"
  | "iteration_end"
  | "turn_end"
  | "error"
  | "input_rejected"
  | "interrupted"
  | "model_changed"
  | "mode_notice"
  | "memory_saved"
  | "compacting"
  | "compacted"
  | "continuation"
  | "turn_done";

/**
 * [中文] WebSocket 传输事件包装对象。
 * [English] WebSocket transport event wrapper object.
 */
export interface WsEvent {
  type: EventType;
  data: any;
}

// [中文] 为下方的转录条目重新导出。位于 api.ts（REST/WS 契约的真实来源）；纯类型导入，因此与 api.ts 的导入不会产生运行时循环依赖。
// [English] Re-exported for transcript items below. Lives in api.ts (the REST/WS contract source of truth);
// type-only import, so there's no runtime cycle with api.ts's `import type { ... } from "./types"`.
import type { MessageSource } from "./api";

// [中文] "always_task" 持久化到所属自动化的任务记录中（固定范围审批，UX-DECISIONS §25）——仅在应用内自动化运行审批卡片上提供。
// [English] "always_task" persists to the owning automation's task record (standing scoped
// approval, UX-DECISIONS §25) — offered only on automation-run approval cards, in-app.
export type ApprovalDecision =
  | "once"
  | "deny"
  | "always_tool"
  | "always_command"
  | "always_domain"
  | "always_task"
  // [中文] OPE-136 §4: 持久化的单 MCP 工具信任——将规则写入用户本地覆盖存储；跨会话生效；可在服务器详情页撤销。
  // [English] OPE-136 §4: durable per-MCP-tool trust — writes a rule to the user-local
  // override store; survives sessions; revocable on the server's detail page.
  | "always_trust"
  // [中文] OPE-136 运行授权：仅在当前回答的剩余时间内覆盖确切工具；内存中存储，在运行边界清除。仅限 EXTERNAL 家族。
  // [English] OPE-136 run grant: covers the exact tool for the remainder of the current
  // answer only; in-memory, cleared at the run boundary. EXTERNAL family only.
  | "this_run"
  | "readonly_session";

/**
 * [中文] 待办事项条目。
 * [English] Todo item.
 */
export interface TodoItem {
  content: string;
  status: "pending" | "in_progress" | "done";
}

// [中文] 单次往返的 Token 统计，由服务端附加在 assistant 消息和 assistant_message 事件中 (`{model, input, output, cache_read, cache_write}`)。在旧版服务器或不报告用量的后端中缺失。
// [English] Per-round-trip token counts, as attached by the server to assistant messages and
// the assistant_message event (`{model, input, output, cache_read, cache_write}`).
// Absent on older servers and on backends that don't report usage.
export interface TurnUsage {
  model?: string | null;
  input: number;
  output: number;
  cache_read: number;
  cache_write: number;
}

// [中文] 会话级 Token 累计，按模型 ID 索引（当用户在会话中间切换模型时会有多个模型）。`context` = 最近一次往返的提示词端总数——即当前占用活跃模型上下文窗口的大小。
// [English] Per-session accumulation, keyed by model id (multiple models when the user
// switched mid-session). `context` = the latest round-trip's prompt-side total —
// what currently occupies the active model's context window.
export interface SessionUsage {
  byModel: Record<string, TurnUsage>;
  context: number;
}

export interface SessionInfo {
  session_id: string;
  title?: string;
  workspace: string;
  agent: string;
  model: string;
  mode: string;
  updated_at: string | null;
  messages: number;
  pinned?: boolean;
  archived?: boolean;
  // [中文] 等待此会话处理的收件箱条目（在侧边栏冒泡显示的黄色注意计数）。
  // [English] Inbox items awaiting this session (the amber attention count that bubbles up the sidebar).
  attention?: number;
  // [中文] working = 运行中的轮次；sleeping = 等待自唤醒定时器；idle = 两者皆非。无计数的圆点指示。
  // [English] working = in-flight turn; sleeping = a self-wake is pending; idle = neither. A count-less dot.
  liveness?: "working" | "sleeping" | "idle";
  // [中文] 当处于休眠状态时：下一次定时器触发时间 (ISO) —— 驱动“休眠至…”状态条。
  // [English] When sleeping: the next timer fire (ISO) — drives the "sleeping until…" strip.
  sleeping_until?: string | null;
  // [中文] 此会话监听的频道（入站订阅）。
  // [English] Channels this session listens to (inbound subscriptions).
  subscriptions?: string[];
  // [中文] §31: 当会话由平台提及而非用户直接派生时设置 —— 机器键 ("slack") + 显示标签 ("#general · T0ABCD")。驱动侧边栏的“来自 Slack”分组和行上的平台图标。
  // [English] §31: set when the session was spawned by a platform mention rather than the user —
  // machine key ("slack") + display label ("#general · T0ABCD"). Drives the sidebar's
  // "From Slack" group and the row's platform icon.
  origin?: string;
  origin_label?: string;
  // [中文] 远程宿主 (UX-045)：当该行来自已加入机器的存储时在客户端设置（通过代理前缀获取）。缺失 = 本机 (This-Mac) 会话。驱动 ⌂ 徽标、机器分组以及会话 API 调用的机器感知路由。
  // [English] Remote homes (UX-045): set client-side when the row came from a joined machine's
  // store (fetched through the proxy prefix). Absent = a This-Mac session. Drives the
  // ⌂ badge, the Machine grouping, and machine-aware routing of session API calls.
  machine?: string;
  machine_name?: string;
  // [中文] 当该行来自控制器存储的快照（因为机器离线）时设置 —— 侧边栏将其置灰，打开它会解释原因。
  // [English] Set when the row came from the controller's stored snapshot because the
  // machine is offline — the sidebar greys it, and opening it explains why.
  machine_offline?: boolean;
  // [中文] 由后端持久化的每个模型 Token 总量 (规范 §5) —— {model: {input, output, cache_read, cache_write, turns}}。主导者的团队面板会汇总其工作者的用量。
  // [English] Per-model token totals persisted by the box (spec §5) — {model: {input, output,
  // cache_read, cache_write, turns}}. The lead's Team panel rolls its workers up.
  usage?: Record<string, TurnUsage & { turns?: number }>;
  // [中文] 智能体团队：普通会话为 {} 或缺失。工作者携带 role/lead_session（+ 计算出的当前事项行）；主导者携带 role/team_id。驱动侧边栏唯一的单个可展开团队条目（工作者嵌套在主导者下方；普通行从不展开）。
  // [English] Agent teams: {} / absent for plain sessions. Workers carry role/lead_session
  // (+ a computed current-item line); leads carry role/team_id. Drives the sidebar's
  // ONE expandable team entry (workers nest under their lead; plain rows never expand).
  team?: {
    role?: "lead" | "worker" | string;
    team_id?: string;
    lead_session?: string;
    actor?: string;
    current_item?: string;
    status?: string;
    chat_enabled?: boolean;
    chat_unread?: number;
  };
}

// [中文] 与用户消息一起发送的附件（图片、PDF、文本文件）。
// [English] Attachments (images, PDFs, text files) sent with a user message.
export interface Attachment {
  kind: "image" | "text" | "pdf";
  name: string;
  mime?: string;
  data_url?: string; // images + PDFs
  text?: string; // text files
}

// [中文] 转录条目 (Transcript items)
// `ts` = Unix 秒数（服务端的规范消息时间戳；实时条目在本地打时间戳）。
// 可选：在服务端添加时间戳之前保存的会话无此字段。
// [English] Transcript items
// `ts` = unix seconds (the server's canonical-message stamp; live items stamp locally).
// Optional: sessions saved before the server stamped timestamps have none.
export type Item =
  | { kind: "user"; text: string; attachments?: Attachment[]; ts?: number }
  // [中文] 连接器传递的入站消息 (Slack/Salesforce/…)，渲染为结构化卡片 (ConnectorMessageCard) 而非普通用户气泡。通过注册表推广到任何连接器 —— 无需针对每个连接器特殊处理。
  // [English] A connector-delivered inbound message (Slack/Salesforce/…), rendered as a structured card
  // (ConnectorMessageCard) instead of a plain user bubble. Generalizes to any connector via the
  // registry — no per-connector special-casing.
  | { kind: "connector"; source: MessageSource }
  | { kind: "teamcreated"; teamId: string; workers: { actor: string; persona: string }[]; ts?: number }
  | { kind: "assistant"; text: string; ts?: number; reasoning?: string }
  // [中文] `hidden` = 用户的隐私过滤器在智能体看到之前移除的结果（来自工具消息的 `_display` 附注；智能体可见内容中无痕迹）。
  // `standingRule` = 自动允许此调用的任务作用域规则 ("tool → target")。
  // `reviewerReason` + `allowAnyway` = 自动审批审查器拒绝 (规范 8.4)：完整原因仅面向用户（智能体收到简略拒绝），且 allowAnyway 提供单次确切操作覆盖。
  // `approvalOrigin` = 为什么调用无需卡片即可运行："reviewer"（由自动审批审查器自动批准；`approvalNote` 携带其单行原因）或 "bypass"（绕过审批模式）。渲染为低调的调试标签。
  // [English] `hidden` = results the user's privacy filters removed before the agent saw them
  // (from the tool message's `_display` sidecar; the agent-visible content has no trace).
  // `standingRule` = the task-scoped rule that auto-allowed this call ("tool → target").
  // `reviewerReason` + `allowAnyway` = an Auto-Approve reviewer deny (spec 8.4): the full
  // reason is user-facing only (the agent got a terse refusal), and allowAnyway offers the
  // one-shot exact-action override.
  // `approvalOrigin` = why the call ran without a card: "reviewer" (auto-approved by the
  // Auto-Approve reviewer; `approvalNote` carries its one-line reason) or "bypass"
  // (bypass-approvals mode). Rendered as a quiet debugging chip, deliberately subtle.
  | { kind: "tool"; id: string; name: string; args: any; status: string; preview?: string; hidden?: number; standingRule?: string; reviewerReason?: string; allowAnyway?: boolean; approvalOrigin?: string; approvalNote?: string; approvalGrant?: string }
  | {
      kind: "approval";
      toolCallId?: string;
      name: string;
      args: any;
      reason: string;
      category?: string;
      // [中文] 固定规则可以绑定的确切目标（服务端计算）——伴随运行上下文，卡片提供“始终允许”选项 (§25)。
      // [English] The exact target a standing rule could pin (server-computed) — with a run
      // context, the card offers "Allow every time" (§25).
      standingTarget?: string;
      // [中文] 仅限 web_search (§1.9)：卡片生成时服务端解析的当前配置提供商名称 —— 授权描述列出实际目的地。
      // [English] web_search only (§1.9): the LIVE configured provider name, resolved server-side
      // when the card was raised — the grant description names the actual destination.
      searchProvider?: string;
      // [中文] OPE-114 §1: 当操作将运行智能体在本次会话中自行创建或下载的文件时设置（"setup.py 是由智能体在 3 步前创建的"）。这是唯一无法从命令文本中直接读取的事实。引擎生成，固定词汇表 —— 绝非文件内容。
      // [English] OPE-114 §1: set when the action would run a file the agent itself created or
      // downloaded this session ("setup.py was created by the agent 3 steps ago"). The
      // one fact that cannot be read off the command text. Engine-authored, fixed
      // vocabulary — never file contents.
      provenance?: string;
      // [中文] 自动审批审查器回答 `unsure` 并弹出此卡片：其单行原因，安静呈现以直接回答“为什么在询问我？”。
      // [English] The Auto-Approve reviewer answered `unsure` and raised this card: its one-line
      // reason, rendered quietly so "why am I being asked?" is answered in place.
      reviewerUnsure?: string;
      escalation?: import("./components/ApprovalEscalation").Escalation;
      // [中文] 服务端分类：此 shell 命令仅在本地读取，因此卡片可提供会话范围的“允许只读命令”授权。
      // [English] Server-classified: this shell command only reads locally, so the card may offer
      // the session-wide "Allow read-only commands" grant.
      readonlyOk?: boolean;
      // [中文] 仅限 decide_worker_call：此决定所回应的工作者等待调用，由服务端查找以便卡片显示（在旧版服务端上缺失）。
      // [English] decide_worker_call only: the worker's waiting call this decision answers, looked
      // up by the server so the card can show it (absent from an older server).
      workerCall?: { worker?: string; tool: string; arguments?: any; reason?: string; state?: string; resolution?: string | null };
      // [中文] OPE-136 发现 4：MCP 调用的实际去向，来自服务端 DEF（用户自己的配置，绝非服务端自述）。驱动真实范围标签 —— "离开这台电脑 → 宿主" (http) / "运行本地程序" (stdio)。
      // [English] OPE-136 finding 4: where an MCP call actually goes, from the server DEF (the
      // user's own config, never the server's claims). Drives the honest scope chip —
      // "leaves this computer → host" (http) / "runs a local program" (stdio).
      mcpDestination?: { transport: string; host?: string };
      resolved?: ApprovalDecision;
    }
  | {
      kind: "dirreq";
      reason: string;
      path?: string;
      writable?: boolean;
      primary?: boolean; // [中文] 根目录晋升：该文件夹成为会话的工作区 / [English] root promotion: the folder becomes the session's workspace
      resolved?: "granted" | "denied";
    }
  | {
      kind: "toolreq";
      tool: string;
      reason: string;
      installable?: boolean;
      version?: string;
      summary?: string;
      source?: string;
      resolved?: "installed" | "skipped";
    }
  | {
      kind: "planreq";
      plan: string;
      resolved?: "approved" | "rejected";
    }
  | {
      // [中文] 编制闸口（智能体团队）：主导者提议其工作者花名册。
      // [English] The staffing gate (agent teams): a lead proposes its worker roster.
      kind: "teamreq";
      title?: string;
      summary?: string;
      groups?: import("./proposals").ProposalGroup[];
      planned_items?: { id: number; title: string; final_acceptance?: { id: number; title: string; owner: "lead" | "assigned_worker" } }[];
      toolCallId?: string;
      // [中文] connectors = 主导者对该工作者的建议（到达卡片时预选勾选）；connector_reasons = 每个建议连接器的理由。
      // [English] connectors = the LEAD'S SUGGESTION for this worker (arrives ticked on the card);
      // connector_reasons = why, per suggested connector.
      members: {
        persona: string;
        group?: string;
        item_ids?: number[];
        name?: string;
        model?: string;
        reason?: string;
        connectors?: string[];
        connector_reasons?: Record<string, string>;
        approval_guidance?: string;
        // [中文] 如果人类在卡片上不做修改，该工作者实际将运行的模型。
        // [English] The model this worker WILL run on if the human changes nothing on the card.
        resolved_model?: string;
        // [中文] 当角色推荐的模型均无法在此机器上运行时设置。
        // [English] Set when none of the persona's recommended models can run on this machine.
        model_warning?: string;
      }[];
      enable_chat?: boolean;
      note?: string;
      // [中文] 按工作者角色分类，按顺序排列的能够在此机器上运行的推荐模型。
      // [English] Per worker persona, its RECOMMENDED models that can run on this machine, in order.
      model_options?: Record<string, string[]>;
      // [中文] 能够在此机器上运行的所有模型。存在时卡片为每个工作者提供模型选择器；缺失（旧版服务端）时模型保持纯文本。
      // [English] Every model that can run on this machine. Present → the card offers a model
      // picker per worker; absent (older server) → the model stays plain text.
      runnable_models?: { id: string; label: string }[];
      lead_model?: string;
      // [中文] 每个工作者角色在此机器上已连接的默认集合。
      // [English] Per worker persona, its DEFAULT set that is connected on the machine.
      offer?: Record<string, string[]>;
      // [中文] 机器上连接的所有其他连接器 —— 人类可以从该列表中将工作者扩展到其默认集合之外（worker-connector-grants 规范 §2, §6）。
      // [English] Every other connector connected on the machine — a human may extend a worker
      // beyond its default set from this list (worker-connector-grants spec §2, §6).
      other_connected?: string[];
      // [中文] 主导者的真实审批模式，用于脱离编辑器（如收件箱）渲染的卡片。
      // [English] The lead's real approval mode, for a card rendered away from the composer (Inbox).
      lead_mode?: string;
      resolved?: "approved" | "rejected";
    }
  | {
      // [中文] 规范 §11.6: 连接器访问是人类的决定。"connect" = 助手请求连接某服务；"grant" = 主导者请求将连接器授予工作者。
      // [English] Spec §11.6: connector access is a human decision. "connect" = the coworker asks
      // for a service to be connected; "grant" = a lead asks to give a worker a connector.
      kind: "connreq";
      request: "connect" | "grant";
      connector: string;
      worker?: string;
      reason: string;
      resolved?: "approved" | "declined";
    }
  | {
      // [中文] 任务拆解闸口：主导者提议工作事项；审批后予以创建。
      // [English] The decomposition gate: a lead proposes work items; approval creates them.
      kind: "itemsreq";
      title?: string;
      summary?: string;
      targets?: string[];
      external_actions?: import("./proposals").ExternalActions;
      activities?: import("./proposals").ProposalGroup[];
      workstreams?: import("./proposals").ProposalGroup[];
      final_acceptance?: { item_key: string; owner: "lead" | "assigned_worker" };
      toolCallId?: string;
      items: import("./proposals").ProposalTask[];
      note?: string;
      resolved?: "approved" | "rejected";
    }
  | {
      // [中文] 实时 ask_user 提示（有人值守的会话内联回答；无人值守的路由到收件箱）。
      // [English] A live ask_user prompt (attended sessions answer inline; unattended ones route to the Inbox).
      kind: "question";
      question: string;
      options?: QuestionOption[];
      allow_text?: boolean;
      multi?: boolean;
      header?: string;
      questions?: GroupedQuestion[];
      resolved?: string;
    }
  | {
      kind: "notice";
      tone: "info" | "warn";
      text: string;
      retriable?: boolean;
      // [中文] `title` 将单行状态通知切换为文本块：标题加空行分隔的段落，左对齐。用于自动审批横幅。
      // [English] `title` switches the one-line status notice to a block: a heading plus
      // blank-line-separated paragraphs, left-aligned. Used for the Auto-Approve
      // banner, which is prose rather than a status line.
      title?: string;
      // [中文] mcp_error 通知：失败服务器的名称 + 完整错误，渲染为低调的单行，详细信息藏在折叠框后，并附带“打开连接器”操作。
      // [English] mcp_error notices: the failing server's name + the full error, rendered as one
      // quiet line with the detail behind a disclosure and an Open-Connectors action.
      server?: string;
      detail?: string;
    }
  // [中文] 记忆规范 §5.1: 保存通知，内联显示在用户正在注视的对话中（角落浮窗容易在被阅读或撤销前消失 —— 2026-07-28 发现）。保持常驻。当编辑现有记忆而非添加新记忆时设置 `previous` —— 撤销将恢复旧文本而非删除记忆。
  // [English] MEMORY-SPEC §5.1: the save notice, inline in the conversation where the user is
  // already looking (a corner toast vanished before it could be read or undone —
  // owner-hit 2026-07-28). Stays put. `previous` is set when an existing memory was
  // EDITED rather than a new one added (the update-don't-duplicate rule sends many
  // saves that way) — Undo restores that text instead of deleting the memory.
  | { kind: "memory"; id: number; text: string; previous?: string; undone?: boolean };

// [中文] -- ask_user 问题元数据 (OPE-51) --------------------------------------
// 选项可以是普通字符串（渲染为药丸胶囊），也可以是富对象：`label` 为答案值，`description` 渲染在下方，`recommended` 添加绿色标签，`preview` 是在侧面板中显示的等宽文本（≥1 个 preview 时卡片切换为双面板布局）。
// [English] -- ask_user question metadata (OPE-51) --------------------------------------
// An option is a plain string (renders as today's pill) or a rich object: `label` is the answer
// value, `description` renders under it, `recommended` adds the green tag, `preview` is monospace
// text shown in the side pane (≥1 preview switches the card to the two-pane layout).
export type QuestionOption =
  | string
  | { label: string; description?: string; recommended?: boolean; preview?: string };

// [中文] 分组 ask_user 调用的单个步骤（最多 4 步，渲染为步骤指示器）。答案映射按 `header` 键入（若无则回退到 `question`）。
// [English] One step of a grouped ask_user call (up to 4, rendered as a stepper). The answer map is keyed
// by `header` (falling back to `question`).
export interface GroupedQuestion {
  question: string;
  header?: string;
  options?: QuestionOption[];
  allow_text?: boolean;
  multi?: boolean;
}
