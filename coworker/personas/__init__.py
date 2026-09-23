"""[中文] 角色（Personas）—— 声明式、具备技能属性的专业协作智能体。

Persona 是一个清单文件（YAML frontmatter 元数据 + 作为系统 Prompt 的 Markdown 正文），它组合了经过审核的 Catalog 能力、家族/工作区形态以及生命周期元数据。内置的各种工作形态（Code、Cowork、Chat、Ops）本身就是清单 —— 与第三方使用的格式完全相同。详见 `platform/docs/PERSONAS.md`。

[English]
Personas — specialized coworkers as declarative, skill-shaped bundles.

A persona is a manifest (YAML frontmatter + a markdown body that is the system prompt) that
composes vetted catalog capabilities, a family/workspace shape, and lifecycle metadata. The
built-in surfaces (Code, Cowork, Chat, Ops) are themselves manifests — the same format third
parties use. See `platform/docs/PERSONAS.md`.
"""

from __future__ import annotations

from .manifest import PersonaManifest, ManifestError, parse_manifest, load_manifest_file
from .registry import PersonaRegistry, PersonaState, DEFAULT_PERSONA_ID

__all__ = [
    "PersonaManifest",
    "ManifestError",
    "parse_manifest",
    "load_manifest_file",
    "PersonaRegistry",
    "PersonaState",
    "DEFAULT_PERSONA_ID",
]
