"""P1 门禁测试 — 工具注册中心与权限引擎（ToolRegistry + PermissionEngine）。
P1 gate tests — tool registry + permission engine."""

from __future__ import annotations

from pathlib import Path

import pytest

import aisuite as ai
from coworker.permissions import Decision, Mode, PermissionEngine
from coworker.tools import ToolRegistry


def _registry(root: Path) -> ToolRegistry:
    """初始化测试用工具注册中心。
    Initialize test tool registry."""
    reg = ToolRegistry()
    reg.register_all(ai.toolkits.files(root=str(root), allow_write=True))
    reg.register_all(ai.toolkits.git(root=str(root)))
    return reg


# -- 工具注册中心测试 / ToolRegistry ---------------------------------------------------------------


def test_registry_exposes_schemas(tmp_path):
    """测试工具注册中心正确暴露工具的结构模式（JSON Schema）。
    Test tool registry properly exposes tool JSON schemas."""
    reg = _registry(tmp_path)
    names = set(reg.names())
    assert {"read_file", "write_file", "list_files", "git_status"} <= names

    schema = reg.get("read_file").schema
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "read_file"
    assert "parameters" in schema["function"]


def test_registry_execute_read_file(tmp_path):
    """测试工具注册中心执行 read_file 工具。
    Test tool registry executing read_file tool."""
    (tmp_path / "hello.txt").write_text("hi there", encoding="utf-8")
    reg = _registry(tmp_path)
    assert reg.execute("read_file", {"path": "hello.txt"}) == "hi there"


def test_registry_path_traversal_blocked(tmp_path):
    """测试工具注册中心拦截路径遍历逃逸（如 ../../etc/passwd）。
    Test tool registry blocks path traversal attacks."""
    reg = _registry(tmp_path)
    with pytest.raises((PermissionError, ValueError)):
        reg.execute("read_file", {"path": "../../etc/passwd"})


def test_registry_execute_unknown_tool(tmp_path):
    """测试调用未知工具时抛出 KeyError。
    Test invoking an unknown tool raises KeyError."""
    reg = _registry(tmp_path)
    with pytest.raises(KeyError):
        reg.execute("nope", {})


# -- 权限引擎测试 / PermissionEngine -----------------------------------------------------------


def _meta(reg: ToolRegistry, name: str):
    return reg.get(name).metadata


def test_read_auto_allowed(tmp_path):
    """测试只读工具默认自动放行无需人工干预。
    Test read tools are automatically allowed without user intervention."""
    reg = _registry(tmp_path)
    eng = PermissionEngine(workspace_root=tmp_path)
    d = eng.evaluate("read_file", {"path": "x"}, _meta(reg, "read_file"))
    assert d.allowed and not d.needs_user


def test_write_requires_approval(tmp_path):
    """测试写文件等修改操作需要人工审批。
    Test write operations require human approval."""
    reg = _registry(tmp_path)
    eng = PermissionEngine(workspace_root=tmp_path)
    d = eng.evaluate(
        "write_file", {"path": "x.py", "content": "x"}, _meta(reg, "write_file")
    )
    assert not d.allowed and d.needs_user


def test_write_path_escape_denied(tmp_path):
    """测试越界写入路径（逃逸工作区根目录）被彻底拒绝且不询问用户。
    Test out-of-bounds write path escape is strictly denied without asking."""
    reg = _registry(tmp_path)
    eng = PermissionEngine(workspace_root=tmp_path)
    d = eng.evaluate(
        "write_file", {"path": "../escape.py", "content": "x"}, _meta(reg, "write_file")
    )
    assert not d.allowed and not d.needs_user
    assert "escape" in d.reason


def test_plan_mode_blocks_writes(tmp_path):
    """测试规划模式（Plan Mode）下彻底禁止写操作。
    Test that Plan Mode strictly blocks all write operations."""
    reg = _registry(tmp_path)
    eng = PermissionEngine(workspace_root=tmp_path, mode=Mode.PLAN)
    d = eng.evaluate(
        "write_file", {"path": "x.py", "content": "x"}, _meta(reg, "write_file")
    )
    assert not d.allowed and not d.needs_user
    assert "read-only" in d.reason


def test_shell_allowlist(tmp_path):
    """测试 Shell 命令白名单机制（白名单命令放行，未授权危险命令需确认）。
    Test Shell command allowlist (whitelisted commands allowed, unlisted need confirmation)."""
    eng = PermissionEngine(workspace_root=tmp_path, allowed_commands=["pytest", "ls"])
    allowed = eng.evaluate("run_shell", {"command": "pytest -q"}, None)
    asked = eng.evaluate("run_shell", {"command": "rm -rf /"}, None)
    assert allowed.allowed
    assert not asked.allowed and asked.needs_user


def test_session_allow_tool_sticks(tmp_path):
    """测试会话级“总是允许该工具”授权在会话期间持续生效。
    Test session-level 'Always allow tool' grant persists across the session."""
    reg = _registry(tmp_path)
    eng = PermissionEngine(workspace_root=tmp_path)
    args = {"path": "x.py", "content": "x"}
    assert eng.evaluate("write_file", args, _meta(reg, "write_file")).needs_user
    eng.allow_tool_for_session("write_file")
    d = eng.evaluate("write_file", args, _meta(reg, "write_file"))
    assert d.allowed and not d.needs_user


def test_session_allow_command_sticks(tmp_path):
    """测试会话级“总是允许该命令”授权在会话期间持续生效。
    Test session-level 'Always allow command' grant persists across the session."""
    eng = PermissionEngine(workspace_root=tmp_path)
    assert eng.evaluate("run_shell", {"command": "make build"}, None).needs_user
    eng.allow_command_for_session("make build")
    assert eng.evaluate("run_shell", {"command": "make build"}, None).allowed


def test_custom_mode_auto_allows_configured_tools(tmp_path):
    """测试自定义模式下自动放行已配置工具，但未配置的高危工具依然需审批，且路径逃逸底线始终生效。
    Test custom mode auto-allows configured tools, still asks on non-configured, and enforces path scoping."""
    reg = _registry(tmp_path)
    eng = PermissionEngine(
        workspace_root=tmp_path, mode=Mode.CUSTOM, auto_allow_tools={"write_file"}
    )
    # 配置的工具自动放行 / configured tool auto-allowed...
    write = eng.evaluate(
        "write_file", {"path": "x.py", "content": "x"}, _meta(reg, "write_file")
    )
    assert write.allowed and not write.needs_user
    # 未配置的高风险工具仍需询问用户 / ...but a non-configured high-risk tool still asks
    shell = eng.evaluate("run_shell", {"command": "rm -rf x"}, None)
    assert not shell.allowed and shell.needs_user
    # 路径作用域在自定义模式下依然严格强制执行 / path scoping still enforced in custom mode
    escape = eng.evaluate(
        "write_file", {"path": "../x.py", "content": "x"}, _meta(reg, "write_file")
    )
    assert not escape.allowed


def test_auto_mode_allows_but_path_scopes(tmp_path):
    """测试全自动模式下自动放行正常写入，但依然坚决拦截越界路径逃逸（安全底线）。
    Test auto mode automatically allows writes, but strictly prevents path escape (security floor)."""
    reg = _registry(tmp_path)
    eng = PermissionEngine(workspace_root=tmp_path, mode=Mode.BYPASS_APPROVALS)
    ok = eng.evaluate(
        "write_file", {"path": "x.py", "content": "x"}, _meta(reg, "write_file")
    )
    escape = eng.evaluate(
        "write_file", {"path": "../x.py", "content": "x"}, _meta(reg, "write_file")
    )
    assert ok.allowed
    assert not escape.allowed
