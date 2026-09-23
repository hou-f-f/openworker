"""[中文] Skill（技能）加载 —— 采用渐进式披露（Progressive Disclosure）的 Anthropic SKILL.md 规范。

一个技能是一个包含 `SKILL.md`（YAML frontmatter: name, description, optional allowed-tools）+ Markdown 指令正文 + 可选资源/脚本的文件夹。

渐进式披露：在会话启动时，仅将技能目录（名称 + 描述）注入到智能体的上下文 Prompt 中；完整的操作指南正文通过 `load_skill` 工具按需动态加载。

[English] Skill loading — Anthropic SKILL.md format with progressive disclosure.

A skill is a folder containing `SKILL.md` (YAML frontmatter: name, description,
optional allowed-tools) + a markdown body of instructions + optional resources/scripts.

Progressive disclosure: at session start only the catalog (name + description) is injected
into the agent's context; the full body is loaded on demand via the `load_skill` tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Union

import aisuite as ai


@dataclass
class Skill:
    """[中文] 技能数据模型。 / [English] Skill data model."""
    name: str
    description: str
    instructions: str = ""  # [中文] 完整指令正文 —— 按需加载 / [English] full body — loaded on demand
    path: Optional[str] = None
    allowed_tools: list[str] = field(default_factory=list)


class SkillLoader:
    """[中文] 技能扫描与加载器，支持在未命中时重新扫描以感知运行时新创建的技能。
    [English] Skill scanner and loader, supporting rescan on miss to detect skills created at runtime."""

    def __init__(self, dirs: list[str | Path]) -> None:
        self._dirs = [Path(d) for d in dirs]
        self._skills: dict[str, Skill] = {}
        self.rescan()

    def rescan(self) -> None:
        """[中文] 重新读取技能目录。`load_skill` 在未命中时重新扫描，以便在会话引擎构建之后创建的技能仍可被加载。
        [English] Re-read the skill dirs. load_skill rescans on a miss so a skill created AFTER
        the session's engine was built is still loadable (the catalog line stays static
        until the next session, but an explicitly requested skill must not 404)."""
        self._skills = {}
        for directory in self._dirs:
            self._discover(directory)

    def _discover(self, directory: Path) -> None:
        if not directory.is_dir():
            return
        for sub in sorted(directory.iterdir()):
            md = sub / "SKILL.md"
            if md.is_file():
                skill = _parse_skill(md)
                self._skills[skill.name] = skill

    def names(self) -> list[str]:
        return list(self._skills)

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def catalog(self) -> list[dict]:
        return [
            {"name": s.name, "description": s.description}
            for s in self._skills.values()
        ]


def _parse_skill(md: Path) -> Skill:
    """[中文] 解析 SKILL.md 文件为 Skill 实例。 / [English] Parse SKILL.md file into a Skill instance."""
    text = md.read_text(encoding="utf-8")
    name, description, allowed, body = md.parent.name, "", [], text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            frontmatter = text[3:end]
            body = text[end + 4 :].lstrip("\n")
            for line in frontmatter.splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                key, value = key.strip().lower(), value.strip()
                if key == "name" and value:
                    name = value
                elif key == "description":
                    description = value
                elif key in ("allowed-tools", "allowed_tools"):
                    allowed = [t.strip() for t in value.split(",") if t.strip()]
    return Skill(
        name=name,
        description=description,
        instructions=body.strip(),
        path=str(md.parent),
        allowed_tools=allowed,
    )


def skill_catalog_text(
    loader: SkillLoader, allowed: Optional[set[str]] = None
) -> str:
    """[中文] 生成注入到提示词中的可用技能清单摘要文本。
    [English] Generate the available skills catalog summary text to be injected into the prompt."""
    catalog = [
        c for c in loader.catalog() if allowed is None or c["name"] in allowed
    ]
    if not catalog:
        return ""
    lines = [f"- {c['name']}: {c['description']}" for c in catalog]
    return (
        "Available skills — call load_skill(name) to load one's full instructions when "
        "it's relevant to the task:\n" + "\n".join(lines)
    )


AllowedSkills = Union[set, Callable[[], set], None]


def skill_tools(loader: SkillLoader, allowed: AllowedSkills = None) -> list:
    """[中文] 构建技能工具集（`load_skill`）。`allowed` 用于限制可用技能。
    [English] Build skill tools (`load_skill`). `allowed` gates load_skill: a set is a build-time snapshot; a CALLABLE is consulted
    on every call — the manager passes one so Settings disables apply to live sessions
    immediately, and skills created after the engine was built are still loadable
    (loader rescans on a miss)."""

    def _allowed_now() -> Optional[set]:
        return allowed() if callable(allowed) else allowed

    def load_skill(name: str) -> dict:
        """[中文] 按名称加载技能的完整指令正文与资源目录路径。当目录中的某个技能与当前任务相关时调用此工具。
        [English] Load a skill's full instructions + resources path by name. Call this when a
        skill from the catalog is relevant to the current task."""
        skill = loader.get(name)
        if skill is None:
            loader.rescan()  # [中文] 会话启动后新建的技能？现在重新扫描拾取 / [English] created after this session started? pick it up now
            skill = loader.get(name)
        gate = _allowed_now()
        if skill is None or (gate is not None and name not in gate):
            available = sorted(
                n for n in loader.names() if gate is None or n in gate
            )
            return {"error": f"unknown skill: {name}", "available": available}
        return {
            "name": skill.name,
            "instructions": skill.instructions,
            "resources_path": skill.path,
        }

    return [
        ai.tool(
            load_skill,
            metadata=ai.ToolMetadata(
                category="skills", risk_level="low", capabilities=["load_skill"]
            ),
        )
    ]
