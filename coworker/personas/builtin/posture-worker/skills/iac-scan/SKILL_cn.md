---
name: iac-scan
description: 使用 trivy config 扫描 Terraform/IaC 基础设施代码，并在代码中实施关键修复
---
[English](SKILL.md) · [简体中文](SKILL_cn.md)

扫描代码库中的基础设施即代码（IaC），并将扫描漏洞转化为最小化、安全的 Terraform 代码变更。

1. **选择扫描器（按以下优先级——若均未安装切勿直接跳过）**：
   - `trivy config . --format json -o /tmp/iac.json`（同时能够覆盖 Dockerfile 与 Kubernetes 编排文件）
   - `checkov -d . -o json > /tmp/iac.json`（若仓库原本即采用 Checkov）
   - 两者皆未安装：调用 `request_tool("trivy", …)` 申请安装 Trivy。若用户拒绝，对照步骤 2 中的安全清单进行手动代码审计，并在报告中明确注明本次扫描为手动执行。
   切勿推荐过时的 `tfsec`——该工具已被官方废弃，`trivy config` 是其官方替代者。
2. **依据真实暴露面进行分诊，结合周围上下文 Terraform 代码研判**：
   - 优先排查**公网可达面**（入站规则开放 `0.0.0.0/0`、公开 S3 存储桶、公开 ALB）。
   - 其次排查**身份权限爆炸半径**（包含通配符的 IAM Policy、宽泛的 AssumeRole 信任关系）。
   - 随后排查**加密与日志审计基线**。
   对于疑似有意配置的业务项（如公开静态站存储桶、堡垒机专用安全组），标注为“是否符合业务意图？”并向用户请示确认，切勿私自擅自“修复”。
3. **在定义资源的具体 Module 模块中修复**（跟随 Module 引用链定位），契合该代码库原有的 Terraform 编码风格——变量定义、Locals 以及 Tags 规范。
4. **验证每次改动**：在修改的文件上运行 `terraform fmt`，并在环境允许时运行 `terraform init -backend=false && terraform validate`。若环境允许执行，将 `terraform plan` 的对比输出附在 PR 中——**绝对严禁运行 `terraform apply`**。
5. **交付成果**：按暴露面风险排序的漏洞清单表格（资源标识 · 缺陷描述 · 研判结论 · 处置动作）、修复分支/PR、以及等待人类决断的“业务意图确认”项。仅针对团队明确同意接受的残留项，提供附带合理解释的 `.trivyignore` 忽略配置。
