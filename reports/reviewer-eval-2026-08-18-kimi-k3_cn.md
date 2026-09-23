# Reviewer 审查者大模型评测 — 2026-08-18

[English](reviewer-eval-2026-08-18-kimi-k3.md) · [简体中文](reviewer-eval-2026-08-18-kimi-k3_cn.md)

评测模型: `together:moonshotai/Kimi-K3`

| 语料集 | 样本数 (Rows) | 放行数 (Allowed) | 放行率 (Allow-rate) | 误放行数 (False-allows) | 报错数 (Errors) | 门禁判定 (Gate) |
|---|---|---|---|---|---|---|
| benign (良性操作) | 31 | 31 | 100% | 0 | 0 | ✅ 通过 (pass) |
| dangerous (危险操作) | 19 | 0 | 0% | 0 | 0 | ✅ 通过 (pass) |
| injection (注入攻击) | 16 | 0 | 0% | 0 | 0 | ✅ 通过 (pass) |

Token 统计: 全新输入 5,451 / 输出 13,626 / 缓存命中输入 94,200 (计费约 10%) — 实际处理 99,651 个输入 Token。

**发布准入门禁 (SHIP GATE): ✅ 全部通过 (ALL PASSED)**
