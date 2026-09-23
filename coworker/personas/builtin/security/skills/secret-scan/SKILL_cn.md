---
name: secret-scan
description: 使用 gitleaks 扫描代码库与提交历史中的泄露凭证，并引导实施安全轮转
---
[English](SKILL.md) · [简体中文](SKILL_cn.md)

排查已提交到代码库中的敏感凭证，并推进其轮转吊销与代码清理——在此过程中绝对不要自行导致凭据发生二次暴露。

绝对红线规则：**严禁在任何地方明文输出敏感密钥的真实数值**——无论在命令输出、笔记、待办条目、Git Commit 还是 PR 中。对每个命中的凭据，统一使用 “`<kind> in <file>:<line> (commit <short-sha>)`” 格式指代。

1. **检查扫描工具**：运行 `gitleaks version`。若缺失，**绝不要跳过该项检查**，也不要中断审查流程——调用 `request_tool("gitleaks", …)` 申请安装。若用户拒绝或该操作系统平台暂无预编译构建，降级执行步骤 2b 中的手动排查，并在最终报告中明确说明本次排查为手动执行。
2. **同时扫描当前工作树与全量 Git 历史**——历史记录最为关键：在当前 HEAD 中删除了的密钥，在代码库的每一次 Clone 副本中依然存活，这也是用户往往最容易遗漏的盲区。
   a. **使用 Gitleaks 自动化扫描**：
      `gitleaks detect --source . --report-format json --report-path /tmp/gitleaks.json`
   b. **无 Gitleaks 时手动等效排查（并在报告中如实说明）**：
      - 扫描当前工作树：`git grep -nIE '(api[_-]?key|secret|token|password|BEGIN [A-Z ]*PRIVATE KEY|AKIA[0-9A-Z]{16}|sk_(live|test)_[0-9a-zA-Z]{16,}|xox[baprs]-)'`
      - 扫描历史提交，包括已被删除的文件：`git log -p --all -S 'AKIA' --pickaxe-all` 以及 `git log --diff-filter=D --name-only --pretty=format:%h -- '*.env*' '*credential*' '*secret*'`，随后通过 `git show <sha>^:<path>` 检视被删除文件的历史内容。
      - 读取内容时务必通过脱敏工具（如 `sed -E "s/[A-Za-z0-9_\\-]{16,}/[REDACTED]/g"`）进行过滤管道重定向，切勿直接打印明文到你的对话转录中——严禁明文打印的红线依然生效。
3. **结合上下文对每个命中项进行分诊**：
   - 是真实生产凭据、测试桩 Fixture、还是示例占位符？明确给出结论并说明原因。
   - 针对真实凭据：它赋予了什么系统权限？目前是否大概率依然处于有效存活状态？
4. **针对每个真实泄露的密钥，按严格顺序执行**：
   a. **首要任务：立即轮转吊销 (ROTATE first)**——明确告知用户去哪里吊销/轮换该凭据（提供对应服务商的控制台链接或 CLI 命令）。轮转吊销的重要性绝对高于删除代码：未轮转吊销前单纯重写 Git 历史只是自欺欺人的虚假安全感。
   b. **从代码中移除**：迁移至环境变量或项目既有的 Secret Manager，契合该代码库原有的配置管理范式。
   c. **防止再次泄露**：在 `.gitignore` 中补充本地密钥文件规则，并提供 `.gitleaks.toml` 基线配置以及 Pre-commit Git 钩子。
   d. **彻底清洗 Git 历史 (git filter-repo / BFG)**：该操作具有**破坏性 (DESTRUCTIVE)** 并会重写共享提交历史——向用户讲清利弊风险，仅当用户显式提出要求时方可执行。
5. **交付成果**：一份凭证命中清单（类别 · 所在位置 · 研判结论 · 轮转状态）、代码清理分支/PR、以及已配置或建议部署的防泄漏预防机制。
