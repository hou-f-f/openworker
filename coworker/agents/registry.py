"""[中文] Agent（智能体）注册中心 —— 将角色 ID 解析为其运行时 Agent 实例。

委托给角色注册表（``coworker.personas``），使内置交互界面和 Markdown/第三方角色通过统一步径进行解析。
MyHelper 是直接解析的遗留个人助手角色（为仍然引用它的会话保留）。
角色注册表的导入为延迟导入，以避免循环导入（personas → agents builders）。

[English] Agent registry — resolves a persona id to its runtime Agent.

Delegates to the persona registry (``coworker.personas``) so built-in surfaces and
markdown/third-party personas resolve through one path. MyHelper is a legacy personal-helper
persona resolved directly (kept for sessions that still reference it).
Imports of the persona registry are lazy to avoid an import cycle (personas → agents builders).
"""

from __future__ import annotations

from .base import Agent
from .myhelper import myhelper_agent


def get_agent(name: str) -> Agent:
    """[中文] 根据名称获取 Agent 运行时对象（默认为 "code"）。
    [English] Get Agent runtime object by name (defaults to "code")."""
    name = name or "code"
    if name == "myhelper":
        return myhelper_agent()
    from ..personas.registry import get_registry

    return get_registry().agent(name)


def list_agents() -> list[dict]:
    """[中文] 列出在新会话选择器中展示的会话角色界面（已启用且向侧边栏暴露的角色）。
    [English] Session surfaces shown in the new-session picker (enabled + surfaced personas)."""
    from ..personas.registry import get_registry

    return get_registry().sidebar()
