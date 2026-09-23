# Coworker GUI 界面开发指南 (React + Tauri)

[English](README.md) · [简体中文](README_cn.md)

Coworker 服务端的瘦客户端（基于 OpenAI 兼容 API + WebSocket 事件/审批双向流）。
同一套前端代码既可以在普通浏览器中作为开发界面运行，也可以通过 Tauri 打包为原生的 OpenWorker 桌面端应用程序。

## 首次运行：初始化 Python 后端虚拟环境

全新拉取的代码库尚未构建服务端环境——在仓库根目录运行以下脚本，完成两种启动方式所需虚拟环境的初始化：

```bash
bash packaging/setup_dev_env.sh   # → 生成 .venv (包含服务端及 aisuite 依赖)
```

## 运行方式一：浏览器模式（需两个终端）

1. **启动后端服务**（需要环境变量中提供模型 Key，例如 `OPENAI_API_KEY`——也可以稍后在界面“设置 (Settings)”中添加），在代码库根目录下运行：
   ```bash
   ./.venv/bin/openworker-server --cwd /path/to/your/project --port 8765
   ```
2. **启动前端界面：**
   ```bash
   cd surfaces/gui
   npm install      # 首次运行安装依赖
   npm run dev      # → 启动本地服务，访问 http://localhost:5173
   ```

浏览器打开 `http://localhost:5173`。前端界面默认与 `http://127.0.0.1:8765` 通信（可通过 `VITE_COWORKER_HTTP` / `VITE_COWORKER_WS` 环境变量覆盖）。请务必**在启动 Vite 之前先启动 Python 服务端**，以便前端能够读取 `<state-dir>/sidecar-8765.token` 中的启动令牌；若服务端重启，请同步重启 Vite。

## 运行方式二：从源码启动完整桌面端应用

Tauri 桌面外壳封装了同一套 React 前端界面，并自动管理 Python 服务端子进程的生命周期——无需在另一个终端中单独运行后端命令。
该模式需要本地安装 Rust 工具链（通过 `rustup` 安装）以及第一步生成的 Python 虚拟环境；在开发模式下，Tauri 会自动在 `.venv/bin/openworker-server` 找到后端入口（打包好的独立伴生二进制文件仅在运行 `packaging/` 下的发布打包脚本时生成）。

```bash
cd surfaces/gui
npm install        # 首次运行安装依赖
npm run tauri dev  # 编译 Rust 桌面宿主外壳，拉起原生窗口，并自动拉起 Python 后端服务
```

## 测试命令 (Tests)

```bash
npx tsc --noEmit && npx vitest run   # TypeScript 类型检查 + 单元测试
npx playwright test                  # 封闭隔离的 E2E 测试 (使用 Mock 的 /v1 与 WS，无需 Python 运行)
```
