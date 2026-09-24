<h1 align="center">OpenWorker（开源桌面 AI 协作助手）</h1>

<p align="center"><strong><a href="https://openworker.com">openworker.com</a></strong> · <a href="#下载">下载客户端</a> · <a href="https://github.com/andrewyng/openworker/issues">问题反馈 (Issues)</a> · <a href="README.md">English</a></p>

<p align="center"><a href="https://trendshift.io/repositories/91434?utm_source=trendshift-badge&amp;utm_medium=badge&amp;utm_campaign=badge-trendshift-91434" target="_blank" rel="noopener noreferrer"><img src="https://trendshift.io/api/badge/trendshift/repositories/91434/daily" alt="andrewyng%2Fopenworker | Trendshift" width="250" height="55"/></a></p>

> **公测阶段 (Beta)** - OpenWorker 当前处于公开测试阶段：功能完全可用，支持自动更新，我们正在持续打磨细节。非常欢迎在 [Issues](https://github.com/andrewyng/openworker/issues) 中提出反馈。

**帮你搞定日常工作的实用 AI。** OpenWorker 是一个运行在你桌面上的开源 AI 协作助手（AI Coworker），它交付的是**最终工作成果**，而不仅仅是聊天对话：包含漏洞扫描与就绪修复代码的 Review 审查、排版完善的文档、带有真实数据的 Slack 回复、自动分类整理的收件箱等。它首先提供了**专业的安全协作专家 (Security Coworkers)**——攻击者早已开始利用 AI，防守方理应在完善治理的前提下获得同等强大的生产力武器。

它完全运行在你的本地机器上，且不绑定任何特定模型：你可以自带（BYOK）OpenAI、Anthropic、Google 或开源权重模型的 API Key，也可以通过 Ollama 完全离线本地运行。你的数据只有在通过*你自行选择*的模型和连接器时才会离开本地。Agent 所采取的每一个操作都受到严格管控与审计——详见[设计即受控 (Governed by design)](#设计即受控-governed-by-design)。

[![OpenWorker 工作原理](docs/assets/how-it-works.png)](https://openworker.com)

## 下载

[**⬇ macOS (Apple Silicon 芯片)**](https://download.openworker.com/mac)
<sub>支持 macOS 12+ · 已签名及公证 · 支持自动静默更新</sub>

[**⬇ Windows 10/11 (x64)**](https://download.openworker.com/windows)
<sub>当前构建版本尚未完成代码签名，SmartScreen 会提示警告；签名正在进行中</sub>

打开应用程序，填入你的模型 API Key（或直接连接 Ollama），即可开始处理真实工作任务。

## 适用场景 (Use cases)

挑选一位数字同事，向它分配真实任务，直接获取交付成果：

- **安全代码审查 (Security review)** - 扫描代码库及其依赖以发现真实安全风险。扫描结果结合了确定性扫描器（如 Semgrep）以及大模型的逻辑推理能力；生成的修复方案会在你最终批准前重新扫描并进行 Diff 审查——修复代码的执行者绝不能是唯一的检查者。
- **云安全态势审计 (Cloud posture)** - 依据常见配置缺陷类别审计云基础设施配置，并起草修复建议方案。
- **安全事件分诊 (Incident triage)** - 应对安全或运维事件：跨越多种工具汇总上下文背景、起草时间线事件并生成处置报告。
- **日常生产力工作 (Everyday work)** - 结合 CRM 与收件箱准备客户拜访沟通提纲；将零散的笔记转化为可执行的项目计划；生成专业文档与电子表格；自动处理日程和 Slack 讨论。
- **常驻自动化任务 (Standing automations)** - 每日晨会简报、每周总结报告、持续监控指定频道——按定时任务调度，提供完整的执行日志与转录。

专业协作角色（Specialist Coworkers）出厂时即已配齐对应工作所需的工具集、工作风格与确认打卡机制。目前安全类协作角色优先发布。

## 它是如何工作的 (How it works)

1. 告诉 OpenWorker 你希望达成的最终目标——例如“准备一份客户简报”、“整理本周日程”、“起草项目报告”、“核对跨 Jira 和 GitHub 的版本发布进展”。
2. 它会将任务拆解为步骤，并联动你的桌面文件系统、终端和已连接的外部应用执行。
3. 在执行任何具有实质性后果的操作之前——如发送消息、修改日历日程、执行终端命令——它会暂停并向你发起确认，由你来决定批准、拒绝或纠正方向。
4. 你最终得到的是交付完毕的成果文件，而不是一张未完成的待办清单。

底层架构分层：

```text
┌────────────────────────────────────────────────┐
│              OpenWorker 桌面端应用              │  原生 Shell 宿主 + React GUI 界面
├────────────────────────────────────────────────┤
│           本地 Agent 服务端 (Python)            │  引擎 · 工具系统 · 连接器 - 基于 aisuite 构建
├───────────────┬────────────────┬───────────────┤
│   你的本地文件 │   你的外部工具  │   你的大模型   │  一切均使用你自己的密钥与环境，
│   与系统终端   │ 25+ 款连接器   │  任意模型厂商  │  完整保留在你的本地机器上
└───────────────┴────────────────┴───────────────┘
```

## 设计即受控 (Governed by design)

治理管控是底层架构的基石，而不是外挂的插件——Agent 永远无法自行提升权限，任何 Prompt 注入也无法越过底层门禁。系统拥有三层安全保障，均在源码中公开：

1. **绝对安全底线 (Hard floors)**：一系列极具破坏性或不可逆的操作被严格限定为“必须由人工决定”。没有任何模式（包括完全自动审批模式）能够跨越该底线；遇到此类操作必然上报用户。
2. **渐进式授权阶梯 (A ladder of earned autonomy)**：默认情况下，任何操作都需要逐次审批。经过实践验证的高频操作可以逐步升级为“长期规则 (standing rules)”，进而升级为配置文件白名单——每一步都是显式、透明且随时可撤回的。在自动审批（auto-approve）模式下，独立的审查者模型（Reviewer model）会放行常规操作，而对任何存疑操作升级上报给人类；若连续发生多次拒绝，将触发熔断机制（circuit breaker），暂停 Reviewer 并将控制权交还人类。审查者的判断是智能推理而非百分之百保证——底线规则和审计日志是其兜底保障。
3. **“谁做了什么、为什么做”的完整审计链路**：每一次工具调用均会记录其审批来源与证明链（provenance）——是自动审批、人工批准还是被拒绝，并附带 Reviewer 审查者的推理依据——全部随会话持久化存储。

无人值守（Unattended）运行模式永远不会自我批准：所有需要确认的请求都会停留在待办收件箱中，直到人类做出答复。如果您发现了安全隐患或漏洞，请参阅 [SECURITY_cn.md](SECURITY_cn.md)。

## 核心功能清单

- **产出可直接交付的成果**：文档、表格、分析报告与网页最终以本地文件的形式产出，可直接打开与分享。
- **与 Slack 无缝协作**：在 Slack 频道中 `@OpenWorker`；你的桌面端会自动打开一个会话，利用你本地的工具执行任务，并将处理结果作为 Thread 回复发送。
- **打通日常生产力工具**：支持 25+ 种流行生态集成，包括 GitHub、Slack、Jira、Notion、Linear、HubSpot、Outlook、monday.com、Gmail 和 Google Calendar，以及你本地的**终端与文件系统**。任何支持 [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) 协议的工具也均可即插即用，并支持精细到单个工具的权限控制。
- **支持定时自动化**：处理重复性工作的定时任务：早间简报、周度汇总、关键频道持续盯盘。运行记录会完整沉淀在应用中，包含完整交互转录。
- **谋定而后动，行前必确认**：文件写入、外部消息发送和 Shell 命令执行均受到审批门禁保护，可选的自动审批模式遇到不确定行为依然会升级人工干预——详见[设计即受控](#设计即受控-governed-by-design)。

## 自带模型 (Bring your own model)

模型接入完全由你主导：挑选供应商、填入 API Key、随时切换。开箱即用的支持包括：

**OpenAI · Anthropic · Google Gemini · 火山引擎 BytePlus Ark · 火山引擎 Ark Agent Plan · Inkling (Thinking Machines) · 智谱清言 GLM (Z.ai) · DeepSeek (深度求索) · Kimi (月之暗面 Moonshot) · 通义千问 Qwen · MiniMax · Mistral · Grok (xAI)**——此外还支持通过 **Together** 与 **Fireworks** 托管的开源权重模型，以及通过 **Ollama** 完全本地离线运行的模型。

官方提供精选模型列表，标明经过充分工具调用验证的模型。手动填入任意兼容模型字符串亦可运行，风险自担。

## 隐私与数据安全 (Privacy)

OpenWorker 严格遵循“本地优先 (Local-first)”原则。所有数据均保存在你的本地机器上：包括 Agent 执行循环、会话历史、连接器凭证与模型 API 密钥——全部保存在应用本地的加密安全密钥库中。唯一的云端组件是一个极小型的服务，仅用于协助部分连接器的 OAuth 授权回调握手。你完全可以在不登录账户的情况下使用本应用——通过手动创建的凭证/API-Key 配置连接器即可。

## 从源码运行 (Run from source)

前置依赖环境：Python 3.10+、Node 20+，以及（若构建桌面宿主）通过 [rustup](https://rustup.rs/) 安装的 Rust 工具链。

```shell
git clone https://github.com/andrewyng/openworker
cd openworker

# 1. 一次性环境初始化 - 在 .venv 目录下创建 Python 虚拟环境
#    (Windows 用户请在 Git Bash 或 WSL 下运行)
bash packaging/setup_dev_env.sh

# 2. 启动本地 Agent 服务端
.venv/bin/openworker-server --cwd ~/some/project --port 8765
#    (Windows: .venv\Scripts\openworker-server.exe)

# 3. 在第二个终端窗口中启动前端界面
cd surfaces/gui
npm install
npm run dev        # 浏览器界面运行在 Vite 开发端口
```

独立服务端启动时会在 `<state-dir>/sidecar-8765.token` 生成每次启动专属的安全 Token；Vite 启动时会读取该仅限当前用户权限访问的文件。若直接调用服务端 API，请在请求头中携带 `X-OpenWorker-Token`。桌面应用程序则在内存中保存启动 Token，绝不落地到磁盘。

若要直接启动完整的桌面应用程序而非纯网页界面，将步骤 3 替换为 `npm run tauri dev`（在 `surfaces/gui/` 目录下运行）——Tauri 桌面外壳会拉起原生窗口并自动管理 Python 伴生服务的生命周期。

运行测试：
- 服务端测试：`.venv/bin/pytest`
- 前端测试：在 `surfaces/gui` 下运行 `npm test`（前端单元测试）与 `npm run e2e`（隔离端到端测试）。
- 桌面安装包打包脚本：`packaging/build_dmg.sh` / `packaging/build_windows.ps1`。

## 目录结构地图 (Repository layout)

初次阅读本仓库？请参阅 [中文项目学习手册](docs/项目学习手册.md)，了解详尽的目录结构、模块依赖关系、核心执行流程与推荐精读路径。

| 目录 | 包含内容 |
|---|---|
| `coworker/` | Python 后端——Agent 引擎、模型适配层、连接器、MCP 客户端、长期记忆、自动化调度 |
| `surfaces/gui/` | 桌面客户端——React 界面 + 负责管理服务子进程的 Tauri 桌面外壳 |
| `stt/` | 语音转文字 (Speech-to-Text) Rust 伴生程序，用于本地语音输入 |
| `packaging/` | 安装包打包脚本（macOS DMG、Windows）、自动更新配置清单、开发环境初始化引导 |
| `docs/` | 核心设计规范与决策备忘录 |
| `tests/` | 后端自动化测试套件 |

## 基于 aisuite 构建 (Built on aisuite)

OpenWorker 的核心引擎构建在 [**aisuite**](https://github.com/andrewyng/aisuite) 之上。aisuite 是一个轻量级 Python 库，提供了统一各大主流大模型 Chat Completions 调用的标准接口，以及附带工具、工具箱与 MCP 支持的 Agent 基础层。如果你希望亲手搭建自己的轻量 Agent 底座而非直接使用本套系统，建议先从 aisuite 开始；本仓库则是 aisuite 在生产级桌面复杂应用场景下的工程参考实现。

OpenWorker 最初诞生于 aisuite 仓库内部，随后迁移至独立仓库发展；在此由衷感谢 aisuite 开源贡献者们打下的坚实底座。

## 参与贡献 (Contributing)

非常欢迎提交代码贡献与缺陷反馈——欢迎提交 [Issue](https://github.com/andrewyng/openworker/issues) 或 Pull Request。本应用支持自动更新，因此修复能迅速推送至安装实例。
提交 PR 时，请附上修复前缺陷现象与修复后效果的截图。我们很快会公布社区可直接认领的功能列表。
请注意：核心团队目前正紧密围绕内部路线图与目标进行迭代，因此若 PR 涉及已在规划开发中的特性，或偏离整体产品方向，可能暂不合并。

## 开源协议 (License)

遵循 MIT 开源协议——详见 [LICENSE](LICENSE)。
