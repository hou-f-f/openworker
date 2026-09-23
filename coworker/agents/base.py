"""[中文] Agent（智能体）—— 顶层交互界面与角色形态（Code / Chat / Cowork）。

智能体拥有自己的系统提示词 + 基础工具集 + 是否需要工作区。这与 Skill（技能）不同：
技能是遵循 Anthropic 规范、可插拔加载的能力，任何智能体都可以引入它（参见 coworker.skills）。

[English] Agent — a top-level surface (Code / Chat / Cowork).

An agent owns its system prompt + base toolset + whether it needs a workspace. Distinct
from a Skill: skills are Anthropic-format, loadable capabilities that ANY agent can pull
in (see coworker.skills).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from ..tools.todo import TodoList


@dataclass
class AgentContext:
    workspace: Optional[Path] = None
    executor: Optional[Any] = None
    todo: Optional[TodoList] = None
    # [中文] 会话可触及的共享可变 RootDir 列表（主暂存区 + 用户添加的文件夹）。若为 None，工具回退到单一 `workspace` 根目录。以引用持有，因此运行时增删文件夹能够被其构建的文件工具实时感知。
    # [English] Shared, mutable list of RootDir the session may touch (primary scratch + added folders).
    # When None, tools fall back to the single `workspace` root. Held by reference so runtime
    # add/remove of folders is seen by the file tools built from it.
    roots: Optional[list] = None


@dataclass
class Agent:
    name: str
    title: str
    system_prompt: str
    tool_factory: Optional[Callable[[AgentContext], list]] = None
    # [中文] 替代旧版根据智能体名称分支判断的特性标志 (Traits)：
    # requires_folder: 会话必须在用户选取主文件夹后才能启动（输入框 + 引擎门禁；其他会话在暂存目录上启动）。
    # subagents: 允许派生只读的探索子智能体。
    # scheduling: 允许调度任务 + 自唤醒定时器。
    # messaging: 已退役（规范 §11, 2026-09-05）—— 聊天工具与所有其他连接器工具一样遵循 `connectors` 规则；保留一个版本以兼容旧调用方。
    # connectors: 加载集成工具集 —— True = 每个已连接的连接器（仅限通用内置），元组 = 白名单（会话获得声明 ∩ 已连接的交集；OPE-93），False = 无。
    # [English] Traits that replace the old per-agent-name branching in build_engine / manager.
    # requires_folder: the session cannot start without a user-picked primary folder
    # (composer + engine gate; everything else starts on a scratch dir). subagents:
    # read-only explorer fan-out. scheduling: scheduled tasks + self-wake. messaging:
    # RETIRED (spec §11, 2026-09-05) — chat tools follow `connectors` like every other
    # connector tool; the field is kept one release so old callers still construct.
    # connectors: loads the integration toolset — True = every
    # connected connector (general builtins only), a tuple = allowlist (session gets
    # declared ∩ connected; OPE-93), False = none. Defaults keep non-persona callers
    # behaving as before. (The old family/needs_workspace/workspace trio collapsed into
    # these — see ocw-context/docs/workspace-scratch-design.md.)
    requires_folder: bool = False
    subagents: bool = False
    scheduling: bool = False
    messaging: bool = False
    connectors: bool | tuple[str, ...] = False
    # [中文] 团队身份："lead"（主管）| "worker"（工作者）| None（仅单机独立运行）。限制看板/案例工具集和团队编制资格 —— 独立角色绝不能被团队编制。
    # [English] Team identity: "lead" | "worker" | None (solo-only). Gates the board/journal
    # toolsets and staffing eligibility — solo personas are never team-staffable.
    team: Optional[str] = None
    # [中文] 设计师编写的自动审批上下文，并非直接的授权授予。
    # [English] Designer-authored context for Auto-approve, not an access grant.
    approval_guidance: str = ""

    def build_tools(self, context: AgentContext) -> list:
        return list(self.tool_factory(context)) if self.tool_factory else []
