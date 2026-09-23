---
name: dependency-audit
description: 扫描 Lockfile 锁文件排查易受攻击的第三方依赖，并依据真实代码可达性进行分诊
---
[English](SKILL.md) · [简体中文](SKILL_cn.md)

审计项目的第三方依赖库，将切实可被黑客利用的漏洞与理论误报噪音清晰解耦。

1. **识别当前代码库的技术生态**（`package-lock.json` / `pnpm-lock.yaml` / `yarn.lock`、`requirements*.txt` / `uv.lock` / `poetry.lock`、`go.sum`、`Cargo.lock`、`pyproject.toml`）。
2. **挑选本地已有的扫描工具（先检查本地环境；安装新工具前先取得许可）**：
   - `osv-scanner --lockfile <各lockfile路径> --format json`（跨技术生态最佳工具）
   - `npm audit --json` / `pip-audit -f json` / `trivy fs --scanners vuln . -f json`
3. **跨扫描器对安全通告去重**（基于安全通告 ID + 依赖包名唯一键），随后结合业务代码进行逐项分诊：
   - 是直接依赖还是间接传递依赖？（运行 `npm ls <pkg>`、`pipdeptree -r -p <pkg>` 或 grep 全局 import 语句）
   - 存在漏洞的函数/组件在当前项目中是否被实际调用？Grep 检索受影响的具体 API；在仅用于开发阶段的工具中不可达的漏洞，无论其 CVSS 评分有多高，在此均属 LOW 低危。
   - 针对每项通告给出裁决结论：`fix-now`（立即修复）/ `fix-soon`（近期修复）/ `accept-with-note`（带书面说明接受风险），附带一行核心判定依据。
4. **将每个 `fix-now` 漏洞映射到解决该问题的最小升级跨度**（依据通告元数据中的 fixed-in 修复版本）；特别注明需要跨大版本（Major）才能解决的场景及涉及的破坏性迁移改动。
5. **交付成果**：按实际优先级排序的依赖审计表格（通告 ID · 依赖包 · 是否直接依赖 · 是否路径可达 · 研判结论 · 最小修复版本）——随后移交给 `safe-upgrade-pr` 技能执行具体的依赖升级操作。提议补充 CI 流水线门禁（如增加 osv-scanner 检查步骤），确保新引入的漏洞直接在 PR 阶段被拦截，而不是等到下一次集中审计。
