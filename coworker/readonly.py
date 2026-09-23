"""[中文] 用于会话作用域授权的保守只读 Shell 命令分类器。

“允许本会话执行只读命令”（2026-08-11 负责人需求，源于安全扫描会话中的审批疲劳：每次运行需手动审批约 15 次）
仅当本分类器接受该命令时才自动允许。该契约规定：

- **仅限本地文件系统读取。** 网络客户端（curl/wget/ssh/nc）被特意排除，即使是 GET 请求也不允许 —
  在 Prompt 注入攻击下，自动允许的网络命令就是数据外泄通道。解释器（python/ruby/sh -c）以及任何可写入、
  执行或修改的操作均被排除。
- **允许管道操作**（`nl … | sed -n … | grep …`）— 其中的每个阶段都必须通过分类。
  所有其他 Shell 操作符（;, &&, ||, &, 重定向, 命令替换）均被直接拒绝。
- **故障闭合（Fail closed）。** 未知命令、无法解析的输入、通过路径调用的二进制文件以及任何存疑的标志都会被拒绝。
  假阴性（误拒）的代价是一次手动审批；假阳性（误放）的代价是一次未受审查的副作用 — 这里的每个边缘情况都由这种不对称性决定。

这是建立在审批流程之上的用户可选便利功能，而非沙箱：会话仍在自身的权限模式下运行，且用户显式授予了该作用域。

[English]
Conservative read-only shell-command classifier for the session-scoped grant.

"Allow read-only commands for this session" (owner ask 2026-08-11, born of approval
fatigue in security-scan sessions: ~15 hand-approvals per run) auto-allows a command only
when THIS classifier accepts it. The contract:

- **Local filesystem reads only.** Network clients (curl/wget/ssh/nc) are deliberately
  excluded even for GET — an auto-allowed network command is an exfiltration channel
  under prompt injection. Interpreters (python/ruby/sh -c) and anything that can write,
  execute, or mutate are excluded.
- **Pipelines are allowed** (`nl … | sed -n … | grep …`) — every stage must classify.
  All other shell operators (;, &&, ||, &, redirections, substitutions) are rejected
  outright.
- **Fail closed.** Unknown commands, unparseable input, path-invoked binaries, and any
  doubtful flag reject. False negatives cost one manual approval; false positives cost
  an unreviewed side effect — the asymmetry decides every edge case here.

This is a user-elected convenience on top of the approval flow, not a sandbox: the
session still runs under its permission mode, and the user granted the scope explicitly.
"""

from __future__ import annotations

import re
import shlex

# [中文] 仅读取本地状态且无需防范写入标志的安全命令。
# [English] Commands that only read local state, with no writing flags to police.
_SIMPLE_SAFE = {
    "ls", "cat", "head", "tail", "wc", "nl", "sort", "uniq", "cut", "tr",
    "grep", "egrep", "fgrep", "rg", "ugrep", "file", "stat", "du", "df",
    "pwd", "echo", "printf", "which", "whoami", "id", "date", "uname",
    "basename", "dirname", "realpath", "readlink", "jq", "column", "diff",
    "comm", "strings", "md5sum", "shasum", "sha1sum", "sha256sum",
    "hexdump", "xxd", "od", "true", "false", "yamllint", "actionlint",
}

# [中文] 仅读取的 Git 子命令。注意下方的每子命令防范 — 若干 git “读取”命令会通过特定标志演变为写入/执行行为。
# [English]
# Git subcommands that only read. Note the per-subcommand guards below — several git
# "read" commands grow write/exec behavior through specific flags.
_GIT_SAFE = {
    "status", "log", "show", "diff", "blame", "shortlog", "describe",
    "rev-parse", "rev-list", "ls-files", "ls-tree", "grep", "cat-file",
    "name-rev", "merge-base", "count-objects", "var", "check-ignore",
}

_GIT_BRANCH_FLAG_OK = {
    "--show-current", "--list", "-a", "-r", "-v", "-vv", "--contains",
    "--merged", "--no-merged", "--all",
}

_FIND_BAD = ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fls", "-fprintf")

_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=[^;&|<>`]*$")

# [中文] 调用 `w`/`W`（写文件）命令的 sed 脚本标记：在起始处、分隔符后或地址后。保守处理 — 误伤仅仅意味着一次手动审批。
# [English]
# A sed script token that invokes the `w`/`W` (write-file) command: at the start, after a
# separator, or after an address. Conservative — a false hit just means one manual approval.
_SED_WRITE = re.compile(r"(^|[;{])\s*[0-9,$/ ]*[wW]\s")


def _has_unquoted_shell_variable(command: str) -> bool:
    quote: str | None = None
    escaped = False
    for char in command:
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif char == "'" and quote != '"':
            quote = None if quote == "'" else "'"
        elif char == '"' and quote != "'":
            quote = None if quote == '"' else '"'
        elif char == "$" and quote != "'":
            return True
    return False


def _stages(command: str) -> list[list[str]] | None:
    """[中文] 保留操作符进行分词；拆分为管道各个阶段。返回 None 表示拒绝。
    [English] Tokenize with operators surfaced; split into pipeline stages. None = reject."""
    if not command or not command.strip():
        return None
    # Shell variables are expanded after this check; single-quoted '$' is literal.
    if _has_unquoted_shell_variable(command) or "$(" in command or "`" in command or "<(" in command or ">(" in command:
        return None
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        tokens = list(lex)
    except ValueError:
        return None  # unbalanced quotes etc.
    stages: list[list[str]] = [[]]
    for tok in tokens:
        if tok == "|":
            stages.append([])
        elif tok in {";", "&", "&&", "||", "|&"} or (tok and set(tok) <= {">", "<", "&", "0", "1", "2"} and any(c in tok for c in "<>&")):
            return None  # every operator except a plain pipe rejects (incl. 2>, &>, <<)
        else:
            stages[-1].append(tok)
    if any(not s for s in stages):
        return None  # empty stage ("| cmd", "cmd |")
    return stages


def _git_ok(args: list[str]) -> bool:
    # [中文] 全局标志：仅 `-C <dir>` 和 `--no-pager` 放行；`-c`/`--config-env` 可以设置 core.pager 及类似执行钩子 — 予以拒绝。
    # [English]
    # Global flags: only `-C <dir>` and `--no-pager` pass; `-c`/`--config-env` can set
    # core.pager and similar exec hooks — rejected.
    i = 0
    while i < len(args):
        if args[i] == "-C" and i + 1 < len(args):
            i += 2
            continue
        if args[i] == "--no-pager":
            i += 1
            continue
        break
    if i >= len(args):
        return False
    sub, rest = args[i], args[i + 1 :]
    if any(t.startswith("--output") for t in rest):
        return False  # [中文] git log/diff --output=<file> 会写入文件 / [English] git log/diff --output=<file> writes
    if sub in _GIT_SAFE:
        return True
    if sub == "branch":
        return all(t in _GIT_BRANCH_FLAG_OK or t.startswith(("--format=", "--sort=")) for t in rest)
    if sub == "tag":
        return bool(rest) and all(
            t in {"-l", "--list", "-n", "--contains", "--merged"} or t.startswith("-n") for t in rest
        )
    if sub == "stash":
        return bool(rest) and rest[0] in {"list", "show"}
    if sub == "remote":
        return not rest or rest[0] in {"-v", "show", "get-url"}
    if sub == "config":
        return any(t in {"--get", "--get-all", "--get-regexp", "--list", "-l"} for t in rest)
    if sub == "reflog":
        return not rest or rest[0] == "show"
    return False


def _stage_ok(argv: list[str]) -> bool:
    # [中文] 前导 VAR=value 赋值（例如 LC_ALL=C grep …）是惰性的 — 跳过它们。
    # [English]
    # Leading VAR=value assignments (LC_ALL=C grep …) are inert — skip them.
    i = 0
    while i < len(argv) and _ENV_ASSIGN.match(argv[i]):
        i += 1
    argv = argv[i:]
    if not argv:
        return False
    head = argv[0]
    if "/" in head:
        return False  # [中文] 通过路径调用的二进制文件可能是任何程序；仅限纯命令名 / [English] path-invoked binaries can be anything; bare names only
    args = argv[1:]
    if head in _SIMPLE_SAFE:
        return True
    if head == "env":
        return not args  # [中文] 单独的 `env` 仅打印；`env CMD` 会执行命令 / [English] bare `env` prints; `env CMD` executes
    if head == "command":
        return bool(args) and args[0] in {"-v", "-V"}
    if head == "git":
        return _git_ok(args)
    if head == "sed":
        if any(t.startswith(("-i", "--in-place", "-f", "--file")) for t in args):
            return False
        return not any(_SED_WRITE.search(t) for t in args if not t.startswith("-"))
    if head in {"awk", "gawk", "mawk", "nawk"}:
        return not any(">" in t or "system" in t for t in args)
    if head == "find":
        return not any(t.startswith(_FIND_BAD) for t in args)
    return False


def is_readonly_command(command: str) -> bool:
    """[中文] 当且仅当 `command` 是单个命令或纯粹由本地只读阶段组成的管道时为 True。
    [English] True iff `command` is a single command or pure pipeline of local read-only stages."""
    stages = _stages(str(command or ""))
    if stages is None:
        return False
    return all(_stage_ok(s) for s in stages)


# -- [中文] 读取目标（OPE-130） / [English] read targets (OPE-130) ------------------------------------------------------------
# [中文] 上述分类器决定命令可以“做什么”。它对命令可以“读取什么”只字未提，因此旨在“不要再询问我的项目文件”的会话授权
# 同时也覆盖了 ~/.aws/credentials、~/.ssh/id_rsa 以及 OpenWorker 自身的 secrets 文件。这些辅助函数解析出文件操作数，
# 以便调用方将其限制在会话的根目录下 — 与 OPE-122 中针对浏览器上传的修复形式相同。
#
# 从任意 Shell 中提取读取目标通常是不可能的；在此处之所以可行，仅因为分类器已将输入缩小为上述动词。
# [English]
# The classifier above decides what a command may DO. It says nothing about what the
# command may READ, so a session grant meant for "stop asking about my project files" also
# covered ~/.aws/credentials, ~/.ssh/id_rsa and OpenWorker's own secrets file. These
# helpers name the file operands so the caller can hold them to the session's roots — the
# same shape as the fix for browser uploads in OPE-122.
#
# Extracting read targets from arbitrary shell is not possible in general; it is tractable
# here only because the classifier has already narrowed the input to the verbs above.

# Operands are not paths: arguments are strings, charsets, or command names.
_NO_PATH_OPERANDS = {
    "echo", "printf", "pwd", "whoami", "id", "date", "uname", "true", "false",
    "which", "basename", "dirname", "tr", "command", "env",
}
# The FIRST non-flag operand is a pattern/program, not a path; the rest are files.
_PATTERN_FIRST = {"grep", "egrep", "fgrep", "rg", "ugrep", "jq", "awk", "gawk", "mawk", "nawk", "sed"}
# Flags whose VALUE is a path, for the commands that accept them.
_PATH_VALUE_FLAGS = {"-f", "--file", "--exclude-from", "--include-from"}
# `head -n 5`, `cut -f 1`, `sed -n 2p`: a bare number is some flag's count, never a file
# worth scoping. Dropping them keeps the target list honest without a per-flag table.
_NUMERIC = re.compile(r"^[0-9]+([,:.-][0-9]+)*[a-zA-Z]?$")


def _stage_targets(argv: list[str]) -> list[str]:
    """[中文] 单个已被接受的管道阶段的文件操作数。
    [English] File operands of one accepted pipeline stage."""
    i = 0
    while i < len(argv) and _ENV_ASSIGN.match(argv[i]):
        i += 1
    argv = argv[i:]
    if not argv:
        return []
    head, args = argv[0], argv[1:]
    if head in _NO_PATH_OPERANDS:
        return []

    if head == "git":
        # Only `-C <dir>` escapes the working directory; everything else the classifier
        # accepts reads the repo already in scope. Operands after `--` are pathspecs.
        out: list[str] = []
        for j, tok in enumerate(args):
            if tok == "-C" and j + 1 < len(args):
                out.append(args[j + 1])
            elif tok == "--":
                out.extend(t for t in args[j + 1 :] if not t.startswith("-"))
                break
        return out

    out = []
    skip_next = False
    seen_operand = False
    for tok in args:
        if skip_next:
            # `-f` is a pattern FILE for grep but a field NUMBER for cut; the numeric test
            # separates them without needing a per-command flag table.
            if not _NUMERIC.match(tok):
                out.append(tok)
            # `grep -f patterns.txt build.log`: the pattern came from the flag, so the
            # first positional is already a FILE and must not be skipped as the pattern.
            seen_operand = True
            skip_next = False
            continue
        if tok.startswith("-"):
            if tok in _PATH_VALUE_FLAGS:
                skip_next = True
            elif head == "find":
                break  # find's predicates start here; paths precede them
            continue
        if head in _PATTERN_FIRST and not seen_operand:
            seen_operand = True  # the pattern/script/filter, not a file
            continue
        seen_operand = True
        if not _NUMERIC.match(tok):
            out.append(tok)
    return out


def read_targets(command: str) -> list[str]:
    """[中文] `command` 将要读取的每个文件操作数，用于对照会话根目录进行范围限定。

    仅对 `is_readonly_command` 所接受的命令有意义 — 它假定已经过审查。
    宁可多列出操作数：多一个的代价是一次手动审批，少一个则是一次无范围限定的读取，
    该不对称性决定了此处的边缘情况，正如上文所述。

    已知限制：通过此表未列出的标志所触及的路径不会返回。携带真实风险的位置操作数均已覆盖。

    [English] Every file operand `command` would read, for scoping against the session's roots.

    Only meaningful for commands `is_readonly_command` accepts — it assumes that vetting.
    Errs toward naming MORE operands: an extra one costs a manual approval, a missed one
    is an unscoped read, and that asymmetry decides the edge cases here as it does above.

    Known limit: a path reached through a flag this table does not list is not returned.
    The positional operands that carry the real exposure are covered.
    """
    stages = _stages(str(command or ""))
    if stages is None:
        return []
    return [t for stage in stages for t in _stage_targets(stage)]
