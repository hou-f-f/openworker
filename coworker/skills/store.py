"""[中文] Skill（技能）管理 —— 技能文件夹的增删改查与会话级静音 (SKILLS-SPEC §4)。

作用域 = 文件夹位置（以文件夹为事实来源）：全局技能存放在 ``state_dir()/skills``，
项目技能存放在 ``<workspace>/.coworker/skills``。没有数据库；每个操作都是文件夹 + ``SKILL.md`` 操作，
这使得项目技能可以通过 Git 免费共享。

禁用状态特意不作为标记存放在技能文件夹内：项目文件夹随代码仓库分发，一个用户的禁用绝不能提交给队友。
因此它保存在个人专用的 ``state_dir()/skills-settings.json`` 中。

上传操作是分阶段暂存的（解析 → 预览 → 确认），以便用户在任何内容落入作用域目录之前，始终准确审查将要保存的内容。
暂存内容保存在 ``state_dir()/skills-staged/<token>``，直到确认或放弃。

[English] Skill management — CRUD over skill folders + per-session mutes (SKILLS-SPEC §4).

Scope = folder location (folder-is-truth): global skills live in ``state_dir()/skills``,
project skills in ``<workspace>/.coworker/skills``. There is no database; every operation
is a folder + ``SKILL.md`` operation, which keeps project skills shareable via git for free.

Disable state is deliberately NOT a marker inside the skill folder: project folders travel
with the repo and one user's disable must not be committed to teammates. It lives in the
personal ``state_dir()/skills-settings.json`` instead.

Uploads are staged (parse → preview → confirm) so the user always reviews exactly what will
be saved before anything lands in a scope dir. Staged content sits under
``state_dir()/skills-staged/<token>`` until confirmed or discarded.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import threading
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

import aisuite as ai

from ..secrets import state_dir
from .base import Skill, _parse_skill

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_NAME = 64
GLOBAL_SCOPE = "global"
PROJECT_SCOPE = "project"


def validate_name(name: str) -> str:
    """[中文] 技能名称将成为文件夹名称 —— 拒绝任何可能逃逸出作用域目录的字符。
    [English] Skill names become folder names — reject anything that could escape the scope dir."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Skill name is required.")
    if len(name) > _MAX_NAME:
        raise ValueError(f"Skill name too long (limit {_MAX_NAME} characters).")
    if ".." in name or "/" in name or "\\" in name or not _NAME_RE.match(name):
        raise ValueError(
            "Skill name may only contain letters, digits, dots, dashes, and underscores."
        )
    return name


def _frontmatter_source(md: Path) -> str:
    """[中文] 读取可选的 ``source:`` 键（``uploaded`` 等）。若无则表示在此处创建。
    [English] Read the optional ``source:`` frontmatter key (``uploaded`` etc.). Absent → created here."""
    try:
        text = md.read_text(encoding="utf-8")
    except OSError:
        return ""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end == -1:
        return ""
    for line in text[3:end].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            if key.strip().lower() == "source":
                return value.strip()
    return ""


def _write_skill_md(
    folder: Path, *, name: str, description: str, instructions: str, source: str = ""
) -> None:
    lines = ["---", f"name: {name}", f"description: {description}"]
    if source:
        lines.append(f"source: {source}")
    lines += ["---", "", instructions.strip(), ""]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")


class SkillStore:
    """[中文] 基于文件夹的跨全局与项目作用域的技能增删改查存储器。
    [English] Folder-backed skill CRUD across the global + project scopes."""

    def __init__(self, global_dir: Optional[str | Path] = None) -> None:
        self.global_dir = Path(global_dir) if global_dir else state_dir() / "skills"
        self._settings_path = state_dir() / "skills-settings.json"
        self._staging_dir = state_dir() / "skills-staged"
        self._lock = threading.Lock()

    # -- scope dirs ---------------------------------------------------------------
    def project_dir(self, workspace: str | Path) -> Path:
        return Path(workspace).expanduser().resolve() / ".coworker" / "skills"

    def _base(self, scope: str, workspace: Optional[str | Path]) -> Path:
        if scope == GLOBAL_SCOPE:
            return self.global_dir
        if scope == PROJECT_SCOPE:
            if not workspace:
                raise ValueError("A workspace is required for a project-scoped skill.")
            ws = Path(workspace).expanduser()
            if not ws.is_dir():
                raise ValueError(f"Unknown workspace: {workspace}")
            return self.project_dir(ws)
        raise ValueError(f"Unknown scope: {scope}")

    def _folder_of(self, base: Path, name: str) -> Path:
        """[中文] 获取技能文件夹，防止逃逸其作用域目录（解析到其他位置的符号链接将被视为不存在）。
        [English] The skill's folder, guarded against escaping its scope dir (symlinked folders
        that resolve elsewhere are treated as absent rather than followed)."""
        folder = base / name
        try:
            resolved = folder.resolve()
            base_resolved = base.resolve()
        except OSError:
            raise ValueError(f"Unreadable skill folder: {name}")
        if base_resolved not in resolved.parents and resolved != base_resolved / name:
            raise ValueError(f"Skill folder escapes its scope: {name}")
        return folder

    # -- queries ------------------------------------------------------------------
    def find(
        self, name: str, workspace: Optional[str | Path] = None
    ) -> tuple[Path, str]:
        """[中文] 按名称查找技能，优先查找最本地的（项目优先于全局）—— 镜像加载器的冲突优先级。
        [English] Locate a skill by name, most-local first (project before global) — mirrors the
        loader's collision precedence so management operates on the copy the model sees."""
        name = validate_name(name)
        if workspace:
            project = self.project_dir(Path(workspace).expanduser())
            if (project / name / "SKILL.md").is_file():
                return self._folder_of(project, name), PROJECT_SCOPE
        if (self.global_dir / name / "SKILL.md").is_file():
            return self._folder_of(self.global_dir, name), GLOBAL_SCOPE
        raise ValueError(f"Unknown skill: {name}")

    def rows(self, workspace: Optional[str | Path] = None) -> list[dict[str, Any]]:
        """Enriched listing for the Settings screen: scope, source, enabled. Global first,
        then project (a project row with a colliding name is the effective copy)."""
        disabled = self.disabled_names()
        out: list[dict[str, Any]] = []
        seen: dict[str, int] = {}
        scopes: list[tuple[Path, str]] = [(self.global_dir, GLOBAL_SCOPE)]
        if workspace:
            scopes.append((self.project_dir(Path(workspace).expanduser()), PROJECT_SCOPE))
        for base, scope in scopes:
            if not base.is_dir():
                continue
            for sub in sorted(base.iterdir()):
                md = sub / "SKILL.md"
                if not md.is_file():
                    continue
                skill = _parse_skill(md)
                try:
                    # Bundled resources beyond SKILL.md (§6): a rich skill must not look
                    # identical to a one-file one in the Settings list.
                    bundled = sum(1 for p in sub.rglob("*") if p.is_file()) - 1
                except OSError:
                    bundled = 0
                row = {
                    "name": skill.name,
                    "description": skill.description,
                    "instructions": skill.instructions,  # Settings editor prefill
                    "scope": scope,
                    "source": _frontmatter_source(md) or "local",
                    "enabled": skill.name not in disabled,
                    "path": str(sub),
                    "files": max(bundled, 0),
                }
                if skill.name in seen:  # project copy shadows the global one
                    out[seen[skill.name]] = row
                else:
                    seen[skill.name] = len(out)
                    out.append(row)
        return out

    # -- mutations ----------------------------------------------------------------
    def create(
        self,
        *,
        name: str,
        description: str,
        instructions: str,
        scope: str = GLOBAL_SCOPE,
        workspace: Optional[str | Path] = None,
        source: str = "",
    ) -> dict[str, Any]:
        name = validate_name(name)
        description = (description or "").strip()
        if not (instructions or "").strip():
            raise ValueError("Skill instructions are required.")
        base = self._base(scope, workspace)
        folder = self._folder_of(base, name)
        if (folder / "SKILL.md").is_file():
            raise ValueError(f"A skill named '{name}' already exists in that scope.")
        _write_skill_md(
            folder,
            name=name,
            description=description,
            instructions=instructions,
            source=source,
        )
        return {"name": name, "scope": scope, "path": str(folder)}

    def update(
        self,
        name: str,
        *,
        description: Optional[str] = None,
        instructions: Optional[str] = None,
        workspace: Optional[str | Path] = None,
    ) -> dict[str, Any]:
        """Rewrite SKILL.md fields in place; sibling resource files are untouched."""
        folder, scope = self.find(name, workspace)
        current = _parse_skill(folder / "SKILL.md")
        if instructions is not None and not instructions.strip():
            raise ValueError("Skill instructions are required.")
        _write_skill_md(
            folder,
            name=current.name,
            description=(
                description if description is not None else current.description
            ),
            instructions=(
                instructions if instructions is not None else current.instructions
            ),
            source=_frontmatter_source(folder / "SKILL.md"),
        )
        return {"name": current.name, "scope": scope}

    def delete(self, name: str, workspace: Optional[str | Path] = None) -> None:
        folder, _scope = self.find(name, workspace)
        if folder.is_symlink():  # never follow a link out of the scope dir
            folder.unlink()
            return
        shutil.rmtree(folder)

    def move(
        self,
        name: str,
        *,
        to_scope: str,
        workspace: Optional[str | Path] = None,
    ) -> dict[str, Any]:
        folder, from_scope = self.find(name, workspace)
        if from_scope == to_scope:
            return {"name": name, "scope": to_scope}
        target_base = self._base(to_scope, workspace)
        target = self._folder_of(target_base, name)
        if (target / "SKILL.md").is_file():
            raise ValueError(
                f"A skill named '{name}' already exists in the target scope."
            )
        target_base.mkdir(parents=True, exist_ok=True)
        shutil.move(str(folder), str(target))
        return {"name": name, "scope": to_scope}

    # -- enable / disable (personal, survives restarts) -----------------------------
    def disabled_names(self) -> set[str]:
        try:
            data = json.loads(self._settings_path.read_text(encoding="utf-8"))
            return {str(n) for n in data.get("disabled", [])}
        except (OSError, ValueError):
            return set()

    def set_enabled(self, name: str, enabled: bool) -> None:
        name = validate_name(name)
        with self._lock:
            disabled = self.disabled_names()
            if enabled:
                disabled.discard(name)
            else:
                disabled.add(name)
            self._settings_path.parent.mkdir(parents=True, exist_ok=True)
            self._settings_path.write_text(
                json.dumps({"disabled": sorted(disabled)}, indent=2),
                encoding="utf-8",
            )

    # -- uploads: stage → preview → confirm -----------------------------------------
    def stage_upload(self, data: bytes, filename: str = "") -> dict[str, Any]:
        """[中文] 暂存上传并返回解析后的预览。接受 ``.zip``（文件夹技能）或带有 YAML frontmatter 的纯 ``SKILL.md``。在调用 :meth:`confirm_upload` 之前不会真正安装到系统。
        [English] Stage an upload and return the parsed preview. Accepts a ``.zip`` (folder skill)
        or a bare ``SKILL.md`` with YAML frontmatter. Nothing is installed until
        :meth:`confirm_upload`. (A ``.skill`` file is a renamed zip and still unpacks —
        just not advertised.)"""
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            return self._stage_single_md(data, filename)
        # macOS Finder's "Compress" injects __MACOSX/ shadow entries (._*) and .DS_Store —
        # metadata, not skill content. Strip them so a Mac-made zip installs clean.
        names = [
            n
            for n in archive.namelist()
            if not n.endswith("/")
            and "__MACOSX" not in Path(n).parts
            and Path(n).name != ".DS_Store"
            and not Path(n).name.startswith("._")
        ]
        for entry in names:
            p = Path(entry)
            if p.is_absolute() or ".." in p.parts or (p.parts and ":" in p.parts[0]):
                raise ValueError("Archive contains unsafe paths.")
        # SKILL.md at the root, or inside exactly one top-level folder.
        md_entries = [n for n in names if Path(n).name == "SKILL.md"]
        roots = {Path(n).parts[0] if len(Path(n).parts) > 1 else "" for n in md_entries}
        if not md_entries or len(roots) != 1:
            raise ValueError("Archive must contain exactly one skill (one SKILL.md).")
        root = roots.pop()
        token = uuid.uuid4().hex
        staged = self._staging_dir / token
        staged.mkdir(parents=True, exist_ok=True)
        for entry in names:
            parts = Path(entry).parts
            rel = Path(*parts[1:]) if root and parts[0] == root else Path(entry)
            if not str(rel):
                continue
            target = staged / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(entry))
        skill = _parse_skill(staged / "SKILL.md")
        name = skill.name if skill.name else staged.name
        try:
            validate_name(name)
        except ValueError:
            shutil.rmtree(staged, ignore_errors=True)
            raise
        extras = sorted(
            str(p.relative_to(staged))
            for p in staged.rglob("*")
            if p.is_file() and p.name != "SKILL.md"
        )
        return {
            "token": token,
            "name": name,
            "description": skill.description,
            "instructions": skill.instructions,
            "files": extras,
        }

    def _stage_single_md(self, data: bytes, filename: str) -> dict[str, Any]:
        """[中文] 单一 `.md` 文件暂存路径：仅有一个 SKILL.md，无附加资源。Frontmatter 必须包含名称。
        [English] The bare-.md path: one SKILL.md, no resources. Frontmatter must carry the name
        (there is no folder to fall back to)."""
        if filename.lower().endswith((".zip", ".skill")):
            raise ValueError("Not a valid .zip archive.")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("Not a valid skill file — upload a .zip or a SKILL.md.")
        token = uuid.uuid4().hex
        staged = self._staging_dir / token
        staged.mkdir(parents=True, exist_ok=True)
        (staged / "SKILL.md").write_text(text, encoding="utf-8")
        skill = _parse_skill(staged / "SKILL.md")
        if skill.name == token:  # no frontmatter name → parser fell back to the folder
            shutil.rmtree(staged, ignore_errors=True)
            raise ValueError(
                "The .md file needs YAML frontmatter with at least a skill name."
            )
        try:
            validate_name(skill.name)
        except ValueError:
            shutil.rmtree(staged, ignore_errors=True)
            raise
        return {
            "token": token,
            "name": skill.name,
            "description": skill.description,
            "instructions": skill.instructions,
            "files": [],
        }

    def confirm_upload(
        self,
        token: str,
        *,
        scope: str = GLOBAL_SCOPE,
        workspace: Optional[str | Path] = None,
    ) -> dict[str, Any]:
        """[中文] 确认暂存的技能上传并移动至目标作用域目录。
        [English] Confirm staged skill upload and move to target scope directory."""
        staged = self._staging_dir / str(token)
        if not (staged / "SKILL.md").is_file():
            raise ValueError("Unknown or expired upload.")
        skill = _parse_skill(staged / "SKILL.md")
        name = validate_name(skill.name)
        base = self._base(scope, workspace)
        folder = self._folder_of(base, name)
        if (folder / "SKILL.md").is_file():
            raise ValueError(f"A skill named '{name}' already exists in that scope.")
        base.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), str(folder))
        # [中文] 标记出处，以便“设置”界面可以区分上传的技能和本地创建的技能。
        # [English] Stamp provenance so the Settings screen can distinguish uploaded from local.
        if not _frontmatter_source(folder / "SKILL.md"):
            _write_skill_md(
                folder,
                name=name,
                description=skill.description,
                instructions=skill.instructions,
                source="uploaded",
            )
        return {"name": name, "scope": scope, "path": str(folder)}

    def discard_upload(self, token: str) -> None:
        """[中文] 放弃并清理暂存上传。 / [English] Discard and clean up staged upload."""
        staged = self._staging_dir / str(token)
        shutil.rmtree(staged, ignore_errors=True)


class SessionSkillStore:
    """[中文] 会话级技能禁用覆盖存储器（``{session_id: {skill: bool}}``）—— 仅记录会话级静音；缺失的条目表示继承全局/项目设置。
    [English] ``{session_id: {skill: bool}}`` — per-session mutes only; an absent entry means the
    session inherits (enabled unless disabled in Settings). Mirrors SessionConnectionStore."""

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._rows: dict[str, dict[str, bool]] = {}
        self._load()

    def _load(self) -> None:
        if self.path and self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            self._rows = {
                sid: {str(s): bool(v) for s, v in (row or {}).items()}
                for sid, row in data.get("sessions", {}).items()
            }

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"sessions": self._rows}, indent=2), encoding="utf-8"
        )

    def get(self, session_id: str) -> dict[str, bool]:
        return dict(self._rows.get(session_id, {}))

    def set(self, session_id: str, skill: str, enabled: bool) -> None:
        with self._lock:
            self._rows.setdefault(session_id, {})[skill] = bool(enabled)
            self._save()

    def clear(self, session_id: str, skill: str) -> None:
        with self._lock:
            row = self._rows.get(session_id)
            if row and skill in row:
                del row[skill]
                if not row:
                    del self._rows[session_id]
                self._save()

    def remove_session(self, session_id: str) -> None:
        with self._lock:
            if session_id in self._rows:
                del self._rows[session_id]
                self._save()


def effective_skills(
    *,
    names: set[str],
    disabled: set[str],
    session_overrides: dict[str, bool],
) -> set[str]:
    """[中文] 会话有效技能菜单的唯一真实来源 (SKILLS-SPEC §3)：任何一处禁用即生效 (any-off-wins)。
    设置中的禁用会在全局范围内移除该技能 —— 会话覆盖无法强行复活它。在无任何意见时，技能默认开启。
    [English] The single source of truth for a session's skill menu (SKILLS-SPEC §3): any-off-wins.
    A Settings disable removes the skill everywhere — a session override can NOT resurrect
    it. Absent any opinion, a skill is on."""
    out: set[str] = set()
    for name in names:
        if name in disabled:
            continue
        if not session_overrides.get(name, True):
            continue
        out.add(name)
    return out


# -- the worker-authors door (SKILLS-SPEC §5.2) -------------------------------------

_SAVE_SKILL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "save_skill",
        "description": (
            "Propose adding a finished skill to the user's skills. The user reviews the "
            "name, description, full instructions, and any bundled files on an approval "
            "card before anything is saved; once they approve, the skill is usable in "
            "every conversation. Use this after building or refining a skill in "
            "conversation, and offer it in words like: 'Want me to add <name> to your "
            "skills?' — say 'your skills', never the app name; say 'add', never "
            "'install'. If a skill with this name already exists, approving overwrites "
            "its instructions and adds the files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short folder-safe skill name (letters, digits, dots, dashes, underscores).",
                },
                "description": {
                    "type": "string",
                    "description": "One line saying when the skill applies — this is its menu entry.",
                },
                "instructions": {
                    "type": "string",
                    "description": "The full instruction body (markdown). Becomes SKILL.md.",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional paths of files in this session's folders to bundle into "
                        "the skill (scripts, examples, README). Copied in by basename."
                    ),
                },
            },
            "required": ["name", "description", "instructions"],
        },
    },
}


def save_skill_tool(
    store: Optional[SkillStore] = None,
    *,
    allowed_dirs: Optional[list[str | Path]] = None,
) -> Callable:
    """[中文] 构建 `save_skill` 工具 (SKILLS-SPEC §5.2)。`requires_approval=True` 将每个调用通过标准审批卡片进行路由 ——
    工具的参数即为审查界面，这就是为什么架构模式中携带完整指令正文与文件列表的原因。打包的文件只能从 `allowed_dirs` 读取：工作者绝不能将机器上的任意路径打包进技能。
    [English] Build the `save_skill` tool (SKILLS-SPEC §5.2). `requires_approval=True` routes every
    call through the standard approval card — the tool's ARGUMENTS are the review surface,
    which is why the schema carries the full instructions and file list. Bundled files may
    only be read from `allowed_dirs` (the session's roots): the worker must never bundle
    arbitrary machine paths into a skill."""
    store = store or SkillStore()
    dirs: list[Path] = []
    for d in allowed_dirs or []:
        try:
            dirs.append(Path(d).expanduser().resolve())
        except OSError:
            continue

    def save_skill(
        name: str,
        description: str = "",
        instructions: str = "",
        files: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        try:
            name = validate_name(name)
        except ValueError as exc:
            return {"error": str(exc)}
        if not (description or "").strip():
            return {"error": "A one-line description is required — it becomes the skill's menu entry."}
        if not (instructions or "").strip():
            return {"error": "Skill instructions are required."}

        # [中文] 在操作磁盘前解析并审查整个捆绑包，因此损坏的文件绝不会留下半写的技能。
        # [English] Resolve + vet the bundle BEFORE touching disk, so a bad file never leaves a
        # half-written skill behind.
        staged: list[tuple[Path, str]] = []
        for raw in files or []:
            p = Path(str(raw)).expanduser()
            if not p.is_absolute():
                if not dirs:
                    return {"error": f"File is outside this session's folders: {raw}"}
                p = dirs[0] / p
            try:
                rp = p.resolve()
            except OSError:
                return {"error": f"Unreadable file: {raw}"}
            if not rp.is_file():
                return {"error": f"Not a file: {raw}"}
            if not any(d == rp or d in rp.parents for d in dirs):
                return {"error": f"File is outside this session's folders: {raw}"}
            base = rp.name
            if base.lower() == "skill.md":
                # The instructions argument BECOMES SKILL.md; models routinely draft one in
                # the workspace and bundle it. Skip silently — erroring here cost the user a
                # second approval round for a self-healing retry (live drive 2026-07-27).
                continue
            if any(base == b for _, b in staged):
                return {"error": f"Duplicate bundled filename: {base}"}
            staged.append((rp, base))

        # [中文] 工作者编写的技能始终保存在全局作用域 (§3.4: 绝非临时丢弃的位置)。
        # [English] Worker-authored skills always land GLOBAL (§3.4: never a throwaway location).
        try:
            folder, _scope = store.find(name)
            action = "updated"
            store.update(name, description=description.strip(), instructions=instructions)
        except ValueError:
            action = "added"
            created = store.create(
                name=name, description=description.strip(), instructions=instructions
            )
            folder = Path(created["path"])
        for src, base in staged:
            shutil.copy2(src, folder / base)
        return {
            "ok": True,
            "name": name,
            "action": action,
            "files": [b for _, b in staged],
            "note": (
                "Saved to the user's skills — usable in every conversation from now on. "
                "Confirm in one short sentence. To browse the installed files, point the "
                "user to Settings > Skills (the file-count chip opens the folder) — do NOT "
                "link the workspace build folder as an artifact; folders don't open there."
            ),
        }

    save_skill.__name__ = "save_skill"
    save_skill.__doc__ = _SAVE_SKILL_SCHEMA["function"]["description"]
    save_skill.__aisuite_tool_metadata__ = ai.ToolMetadata(
        name="save_skill",
        category="skills",
        risk_level="medium",
        capabilities=["save_skill"],
        requires_approval=True,
    )
    save_skill.__coworker_schema__ = _SAVE_SKILL_SCHEMA
    return save_skill
