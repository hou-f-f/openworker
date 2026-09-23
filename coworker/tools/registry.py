"""[中文] 工具注册表 —— 将可调用对象（包括 aisuite 工具套件中的工具）包装为运行时所管理的注册表：
为大模型提供 JSON schemas，并负责执行。权限检查位于 PermissionEngine 中，由轮次引擎（TurnEngine）应用，而非在此处。

Schema 生成复用了 aisuite (`Tools`)，因此我们无需重复实现 docstring/类型注解 → JSON-schema 的提取逻辑。

Tool registry — wraps callables (incl. aisuite toolkit tools) into a registry the
runtime owns: JSON schemas for the model, plus execution. Permission checks live in the
PermissionEngine and are applied by the turn engine, not here.

Schema generation is reused from aisuite (`Tools`) so we don't reimplement
docstring/type-hint → JSON-schema extraction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from aisuite.utils.tools import Tools


@dataclass
class ToolSpec:
    name: str
    schema: dict[str, Any]  # [中文] OpenAI 格式的函数工具模式定义 / OpenAI-format function tool schema
    func: Callable[..., Any]
    metadata: Any = None  # aisuite ToolMetadata or None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self,
        func: Callable[..., Any],
        *,
        metadata: Any = None,
        schema: Optional[dict[str, Any]] = None,
    ) -> ToolSpec:
        name = getattr(func, "__name__", None)
        if not name:
            raise ValueError("Tool function must have a __name__.")
        meta = metadata or getattr(func, "__aisuite_tool_metadata__", None)
        # [中文] 允许显式覆盖 schema（通过参数或 `__coworker_schema__` 属性），适用于签名无法自动转换为有效 JSON schema 的工具。
        # Allow an explicit schema override (param or a `__coworker_schema__` attribute)
        # for tools whose signature can't be auto-converted to a valid JSON schema.
        resolved_schema = (
            schema or getattr(func, "__coworker_schema__", None) or _schema_for(func)
        )
        spec = ToolSpec(name=name, schema=resolved_schema, func=func, metadata=meta)
        self._tools[name] = spec
        return spec

    def register_all(self, funcs: list[Callable[..., Any]]) -> None:
        for func in funcs:
            self.register(func)

    def names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        return [spec.schema for spec in self._tools.values()]

    def execute(self, name: str, arguments: Optional[dict[str, Any]] = None) -> Any:
        spec = self._tools.get(name)
        if spec is None:
            raise KeyError(f"Tool not registered: {name}")
        return spec.func(**(arguments or {}))


def _schema_for(func: Callable[..., Any]) -> dict[str, Any]:
    """[中文] 通过 aisuite 的模式生成器生成单个 OpenAI 格式的工具 schema。
    Generate one OpenAI-format tool schema via aisuite's schema generator."""
    return Tools([func]).tools(format="openai")[0]
