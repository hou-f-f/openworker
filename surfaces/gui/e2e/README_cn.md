# 前端端到端测试指南 (Playwright E2E)

[English](README.md) · [简体中文](README_cn.md)

前端界面的端到端回归测试套件。测试在无头 Chromium 浏览器中驱动真实的前端应用，但具备**封闭隔离性 (Hermetic)**：
所有的 `/v1` HTTP 请求和事件 WebSocket 均在浏览器网络层被 Mock 拦截，因此测试**无需启动 Python 后端**，执行结果具备完全的确定性，且绝不污染本地真实状态。

## 运行命令 (Run)

```bash
npm run e2e          # 无头模式运行全部 E2E 测试
npm run e2e:ui       # Playwright 交互式 UI 模式 (支持单步断点/观察)
npx playwright test e2e/settings.spec.ts   # 仅运行单个测试规约文件
```

## 真实后端冒烟测试 (Live smoke，不纳入 CI)

`npm run e2e:live` 会运行 `e2e-live/` 目录下的测试（基于独立的 `playwright.live.config.ts`），连接在本地 8765 端口运行的**真实**后端服务。包含两种类型，当检测到后端未启动时均会优雅跳过：

- **API 结构冒烟测试** (`api-smoke.spec.ts`)：不消耗模型 Token，不需要凭证。断言 `/v1/health` 和 `/v1/providers` 等接口返回符合前端预期的数据结构，快速发现前端 Mock 与后端真实接口之间的数据漂移。只要本地伴生后端在运行，可随时低成本执行。
- **全链路垂直验证** (`fib.spec.ts` 等)：向一个全新的 Cowork 会话发送指令要求生成 `fib.md` 并验证文件成功落盘。需要配置真实的大模型，具备非确定性，每次运行会产生少量 Token 消耗。该测试用于检验隔离测试所 Mock 的整个纵向链路：模型装配、工具调用与审批循环、文件 I/O 以及 WebSocket 流式事件推送。

测试配置 (`playwright.config.ts`) 会在专用的 **5199** 端口拉起 Vite 开发服务器（避免与在 5173 运行的正常开发服务冲突）；若服务已存在则直接复用。

## Mock 的工作原理 (How the mock works)

`e2e/fixtures.ts` 导出了定制的 `test` 对象，其 `page` 在浏览器导航前注入了 `mockApi()`：

- `page.route("**/v1/**", …)`：根据 URL 路径与 HTTP 方法，分发至与真实后端完全同构的测试桩数据（提取自真实服务端运行样本）。未知接口返回空但结构合法的响应体。
- 状态变更保存在每个测试用例的内存字典中，因此重新请求（Re-fetch）时能真实反映在 UI 上：如会话操作（归档/重命名/删除）、Persona 角色（启用/展示/删除——启用会自动关联展示，与后端行为一致）、收件箱条目与路由绑定、工作目录 Root、频道订阅等。
- 会话 WebSocket (`routeWebSocket`) 是一个**脚本化的虚拟 Agent**，严格遵循 `{type, data}` 真实事件协议：连接时推送 `ready`；接收 `user_message` → 推送 `turn_start` → 增量文本 deltas → `assistant_message "Echo: <text>"` → `turn_done`；若用户消息中包含 **"run a tool"**，则主动发送 `tool_proposed` + `permission_required`，并挂起等待客户端回传 `approval` 审批裁决。这以零模型成本完整覆盖了生产环境下的发送/流式接收/人机审批全路径代码。
- 值得了解的内置种子数据：置顶会话 "Draft the launch note" 是最新的（作为重启恢复的目标）；7 个未置顶的 "Weekly plan N" 协作会话用于测试侧边栏折叠上限；两个待办收件箱条目（cowork 上的审批、ops 上的问题）用于驱动收件箱过滤器；`acme-notes` 是用于测试启用/删除流程的禁用非内置角色。模型供应商预置了三种典型状态（OpenAI 已配置且使用中、Anthropic 已配置但未使用、Z AI 未配置但预填了 endpoint）——`POST /v1/providers` 保存后切换为 `configured`，包含 "bad" 的密钥在 `/verify` 时触发校验失败。预置了一条包含正在运行状态的自动化任务 ("Daily AI News")——`POST .../run` 追加运行记录，`PATCH`/`DELETE` 支持状态切换与删除。

- **预置历史转录回放 (Seeded transcripts)**：每个会话的 `GET /v1/sessions/{id}/messages` 默认响应 `[]`，因此重新打开时初始为空。`seedSessionMessages(page, sessionId, messages)`（从 fixtures 导出）允许为一个会话注入完整的重放历史——包含通过 `tool_call_id` 关联的 `tool_calls` 与 `role:"tool"` 结果、`_display` 伴生展示数据、`reasoning` 深度思考块、`notice` 提示标记、连接器 `source` 消息。可用于测试重新打开会话的渲染逻辑 (`itemsFromMessages`)——如重放的步骤分组、连接器卡片、尾部错误重试按钮等 live echo 无法覆盖的深层 UI 路径。详见 `seeded-history.spec.ts`。

## 添加新的测试规约 (Adding a spec)

```ts
import { test, expect } from "./fixtures";

test("…", async ({ page }) => {
  await page.goto("/");
  // 执行交互与断言
});
```

如果某个业务流程读取了新接口，请在 `fixtures.ts` 中添加对应的测试桩数据和路由分支——兜底路由返回 `{}`，这可能会导致期望接收数组的组件（如 Persona 的 `recommends` 字段）发生运行时崩溃。定位元素时优先使用 `getByRole`，但注意某些控件（如 Sources 栏、✕ 移除按钮）的可访问性名称来自于内部子内容——此时推荐使用 `getByTitle` 或 `getByLabel` 精准定位。
