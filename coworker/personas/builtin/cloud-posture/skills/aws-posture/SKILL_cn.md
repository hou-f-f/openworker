---
name: aws-posture
description: AWS 云安全态势只读核验——公网暴露面、IAM 权限爆炸半径与安全卫生基线
---
[English](SKILL.md) · [简体中文](SKILL_cn.md)

使用严格只读的 CLI 命令核验线上真实 AWS 账号的安全态势，随后在 IaC 基础设施代码中从源头修复根本原因。

硬性红线规则：**只读意味着绝对只读**——仅允许调用 describe / list / get / simulate 类命令。任何时候都**绝不允许**执行 create / put / update / delete / attach 等写入命令，且**绝不执行 `terraform apply`**。若需要修复，编写进入 Terraform 代码由人类团队统一实施。

1. **确认访问权限与检查范围**：运行 `aws sts get-caller-identity`（在任何输出记录中，务必对账号 ID 进行脱敏，仅保留后 4 位）。询问重点关注哪些 AWS Region 区域；默认覆盖 Terraform State 中涉及的区域。
2. **排查高敏感度暴露面，优先检查最容易暴露的资产**：
   - **公网入口点**：S3 存储桶（`get-public-access-block`、Bucket Policies 策略）、在敏感端口对 `0.0.0.0/0` 全放通的安全组规则、公网可达的 RDS/ES 数据库端点、未启用 TLS 的 ALB 负载均衡监听器。
   - **IAM 权限爆炸半径**：直接附加管理员权限策略的用户、在自定义策略中使用通配符 `*` 授予 `Action`/`Resource` 的策略、陈旧长久未轮转的 Access Key 访问密钥（通过 `iam get-credential-report` 审计）、带有过于宽泛 Trust Policy 信任关系的 IAM Role 角色。
   - **安全卫生基线 (Hygiene)**：CloudTrail 审计日志是否开启且覆盖多区域、EBS 卷与 S3 存储桶是否开启默认加密、Root 根账号是否启用了 MFA 多因素认证（从凭证报告中核对）。
3. **将每项发现与代码库中的 Terraform 进行交叉比对**：该高危配置是在代码中声明的（在代码中直接修复）、线上发生了配置漂移（标明漂移 Diff）、还是尚未被 IaC 代码纳管的孤立资源（提议通过 Terraform Import 导入）？
4. **交付成果**：按暴露面风险排序的云态势报告（问题 · 资源标识 · 验证命令 · 代码定义位置 · 处置建议）、代码纳管资源的 IaC 修复分支、以及需要人类决断的待办清单。每一项结论必须附带可复现验证的只读 CLI 命令，以便团队随时核验。
