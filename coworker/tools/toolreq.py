"""[中文] `request_tool` 工具 —— 智能体请求用户安装其需要但未找到的 CLI 命令行工具。

与 `request_directory` 类似：TurnEngine 会拦截它，触发 TOOL_REQUESTED 事件，
并由用户在带外做出决定（安装经过校验的固定版本构建，或跳过并允许运行降级继续）。
此处的函数仅作为 Schema 载体，以及针对未连接请求器的运行界面的回退实现。

此机制因特定失败模式而引入（OPE-85）：在缺少 gitleaks 的情况下，安全审查会静默丢弃其 git 历史敏感信息扫描 ——
该检查并非报错失败，而是直接从报告中消失了。工具缺失必须成为用户可见的明确决策，绝不能成为隐蔽的漏洞盲区。

The `request_tool` tool — the agent asks the user for a CLI it needs but can't find.

Sibling of `request_directory`: the TurnEngine intercepts it, emits TOOL_REQUESTED, and the
user decides out-of-band (install the pinned build, or skip and let the run continue
degraded). The callable here is only a schema carrier + the fallback for surfaces with no
requester wired.

This exists because of a specific failure mode (OPE-85): with gitleaks absent, a security
review silently dropped its git-history secret scan — the check didn't fail, it vanished
from the report. A missing tool must become a visible decision, never an invisible gap.
"""

from __future__ import annotations

from aisuite.agents import ToolMetadata, tool


def request_tool_tool() -> object:
    def request_tool(name: str, reason: str) -> dict:
        """[中文] 当本机器上缺少你所需要但在固定工具目录中的 CLI 工具时，请求用户进行安装。
        该工具目录是一个小型的封闭集合 —— 目前包括 `gitleaks`、`trivy`、`osv-scanner` ——
        安装具有固定版本和校验和校验的构建。

        对于任何其他缺失的 CLI 工具（semgrep、jq、kubectl 等），切勿使用此工具：
        请使用 Shell（通过 brew/pip 等）自行安装（走正常命令批准流程），或者在不安装的情况下继续。

        `reason` 请保持为一句话：说明哪项检查需要此工具。
        用户看到的提示界面已经解释了安装内容（固定版本、发布者、校验和）以及如果拒绝会发生什么 ——
        切勿在 `reason` 中重复赘述这些内容。

        使用此工具来替代悄无声息地跳过检查。如果用户拒绝，请使用回退方案继续进行
        （例如自行阅读 git 历史而不是运行 gitleaks），并在报告中直白说明哪些检查被降级及其原因。

        Ask the user to install one of the PINNED catalog tools you need but can't find
        on this machine. The catalog is a small closed set — currently `gitleaks`,
        `trivy`, `osv-scanner` — installed at a pinned, checksum-verified version.

        For ANY other missing CLI (semgrep, jq, kubectl, …) do NOT use this tool: install
        it yourself with the shell (brew/pip/…), which goes through the normal command
        approval, or proceed without it.

        Keep `reason` to ONE sentence: which check needs the tool. The prompt the user
        sees already explains what the install is (pinned version, publisher, checksum)
        and what happens if they decline — don't restate any of that in `reason`.

        Use this INSTEAD of quietly skipping a check. If the user declines, carry on with a
        fallback (e.g. reading git history yourself instead of running gitleaks) and state
        plainly in your report which checks were degraded and why.
        """
        return {
            "installed": False,
            "error": "tool requests aren't available in this surface",
        }

    return tool(
        request_tool,
        metadata=ToolMetadata(
            category="system",
            risk_level="low",
            capabilities=["request_tool"],
            description=(
                "Ask the user to install a missing command-line tool, rather than silently "
                "skipping the check that needs it."
            ),
        ),
    )
