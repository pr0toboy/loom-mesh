#!/usr/bin/env python3
"""PreToolUse hook: keep an agent inside its declared scope.

Every agent on the mesh runs as the same OS user, so nothing at the operating
system level stops one from writing into another's files. This hook is the
boundary instead: it reads the tool call the agent is about to make and refuses
the ones its scope forbids.

Two properties matter more than the checks themselves.

**It fails open.** A malformed payload, a missing scope file, a broken regex —
anything unexpected — allows the call. A guard that blocks work when it breaks
gets disabled by whoever is trying to get something done, and then protects
nothing. What it must never do is fail *silently*, so every refusal and every
internal error is written to the log.

**It starts in log-only mode.** With ``MESH_SCOPE_ENFORCE`` unset, a violation
is recorded as ``WOULD_DENY`` and the call proceeds. Run it that way first and
read the log: a scope table written from imagination denies legitimate work on
its first day. Flip enforcement on once the log is quiet.

Scopes live in ``$MESH_HOME/scopes.json``, not in this file — they describe one
deployment's agents, and hardcoding them here would mean editing the shipped
code to install it:

    {
      "curator": {
        "blocked_paths": ["^{home}/builder/.*"],
        "blocked_bash_patterns": ["systemctl\\\\s+.*\\\\bdatabase\\\\b"]
      }
    }

``{home}`` and ``{mesh_home}`` are substituted (regex-escaped) before matching.

Environment:
    MESH_AGENT           which agent is running (no scope applies without it)
    MESH_HOME            bus directory (default ~/mesh)
    MESH_SCOPE_ENFORCE   set to 1 to actually deny; unset = log only
    MESH_SCOPES_FILE     override the scope table location
"""
from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

MESH_HOME = Path(os.environ.get("MESH_HOME", os.path.expanduser("~/mesh")))
SCOPES_FILE = Path(os.environ.get("MESH_SCOPES_FILE", MESH_HOME / "scopes.json"))
LOG_FILE = MESH_HOME / "logs" / "scope-check.log"
AGENT = os.environ.get("MESH_AGENT", "")
ENFORCE = os.environ.get("MESH_SCOPE_ENFORCE", "") == "1"


def _log(level: str, msg: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().isoformat(timespec="seconds")
        with LOG_FILE.open("a") as fh:
            fh.write(f"{ts} [{AGENT or 'unknown'}] {level}: {msg}\n")
    except Exception:
        pass


def load_scope(agent: str) -> dict:
    """The scope table for one agent, with placeholders resolved."""
    if not agent:
        return {}
    try:
        with SCOPES_FILE.open(encoding="utf-8") as fh:
            table = json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        _log("CONFIG_ERROR", f"{SCOPES_FILE} unreadable ({exc}) — no scope applied")
        return {}

    scope = table.get(agent)
    if not isinstance(scope, dict):
        return {}

    subs = {
        "home": re.escape(os.path.expanduser("~")),
        "mesh_home": re.escape(str(MESH_HOME)),
    }

    def expand(patterns) -> list[str]:
        out = []
        for p in patterns or []:
            if not isinstance(p, str):
                continue
            try:
                out.append(p.format(**subs))
            except Exception as exc:
                # A regex quantifier like `.{0,3}` makes str.format raise, and
                # the pattern was then kept with `{home}` unexpanded - matching
                # nothing, silently, in a file that promises never to fail
                # silently. Keep the rule, but say that it is now inert.
                _log("CONFIG_ERROR",
                     f"pattern {p!r} could not be expanded ({exc.__class__.__name__}: "
                     f"{exc}); kept as-is and will not match a real path - escape "
                     f"regex braces as {{{{ }}}} or avoid them")
                out.append(p)
        return out

    return {
        "blocked_paths": expand(scope.get("blocked_paths")),
        "blocked_bash_patterns": expand(scope.get("blocked_bash_patterns")),
    }


def _matches(pattern: str, candidate: str) -> bool:
    """Does this pattern cover this path?

    The candidate is also tried with a trailing slash. Scope patterns are
    written as `^/some/dir/.*`, which covers everything *inside* the directory
    and not the directory itself - so an operation on the directory as a whole
    (removing it, entering it, handing it to a tool with `-C`) matched nothing.
    That is the wrong way round: acting on the container is at least as
    consequential as acting on one file in it.
    """
    return bool(re.match(pattern, candidate) or re.match(pattern, candidate + "/"))


def normalize_path_variants(raw: str, cwd: str) -> set[str]:
    """Every plausible absolute spelling of a path.

    The patterns are anchored regexes on absolute paths, so a rule is bypassed
    by writing the same file differently: ``../``, a leading ``~``, a symlinked
    directory, or NFD Unicode that looks identical on screen. A match on any
    spelling counts as a match.
    """
    if not raw:
        return set()
    p = raw.strip().strip('"').strip("'")
    if not p:
        return set()
    p = unicodedata.normalize("NFC", p)
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        p = os.path.join(cwd, p)
    variants = set()
    for fn in (os.path.normpath, os.path.realpath):
        try:
            variants.add(fn(p))
        except Exception:
            pass
    return variants


# Shell write vectors. Path scope applied only to Edit/Write is cosmetic the
# moment a shell is available: `echo > file`, `tee file`, `sed -i file` and
# `dd of=file` all write without ever naming a file tool. This is heuristic —
# there is no shell parser here — and deliberately over-approximates, which is
# safe while the hook is in log-only mode and visible in the log afterwards.
_REDIR_RE = re.compile(r'(?:^|[\s;&|`(])\d*>>?\s*([^\s;|&<>()`"\']+)')
_TEE_RE = re.compile(r'\btee\b((?:\s+-\S+)*)\s+([^\s;|&<>()`"\']+)')
_DD_OF_RE = re.compile(r'\bof=([^\s;|&<>()`"\']+)')
# Every command below either creates, moves, deletes or overwrites a path given
# as an argument. The first version listed only the copy-and-write family, so
# deletion, directory creation, `git checkout`, `curl -o` and a one-line Python
# open() all walked straight through a path scope - deletion being the one that
# matters most. `cd` is here because `cd <blocked> && echo > x` writes into the
# blocked directory through a relative path no check would resolve.
_WRITE_CMD_RE = re.compile(
    r'\b(?:cp|mv|install|sed\s+-i\S*|truncate|ln|chmod|chown|touch|dd|rsync'
    r'|rm|rmdir|mkdir|git|tar|unzip|curl|wget|python3?|cd)\b'
)
_TOKEN_RE = re.compile(r'[^\s;|&<>()`"\']+')


def extract_bash_write_targets(cmd: str) -> set[str]:
    targets: set[str] = set()
    for m in _REDIR_RE.finditer(cmd):
        targets.add(m.group(1))
    for m in _TEE_RE.finditer(cmd):
        targets.add(m.group(2))
    for m in _DD_OF_RE.finditer(cmd):
        targets.add(m.group(1))
    if _WRITE_CMD_RE.search(cmd):
        for tok in _TOKEN_RE.findall(cmd):
            if tok.startswith("-"):
                continue
            if "/" in tok or tok.startswith("~"):
                targets.add(tok)
    return targets


def _emit(decision: str, reason: str = "") -> None:
    payload = {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
    }}
    if reason:
        payload["hookSpecificOutput"]["permissionDecisionReason"] = reason
    print(json.dumps(payload))
    sys.exit(0)


def refuse(reason: str) -> None:
    if ENFORCE:
        _log("DENY", reason)
        _emit("deny", reason)
    _log("WOULD_DENY", reason)
    _emit("allow")


def decide(data: dict) -> None:
    tool_name = data.get("tool_name") or ""
    tool_input = data.get("tool_input") or {}
    scope = load_scope(AGENT)
    if not scope:
        _emit("allow")

    blocked_paths = scope["blocked_paths"]
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    if tool_name in ("Edit", "Write", "NotebookEdit"):
        # NotebookEdit names its target `notebook_path`, not `file_path`: reading
        # only the latter let every notebook edit through a path scope.
        raw_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        for cand in {raw_path} | normalize_path_variants(raw_path, cwd):
            for pattern in blocked_paths:
                if cand and _matches(pattern, cand):
                    refuse(f"tool={tool_name} path={raw_path} (→{cand}) is outside "
                           f"the scope of agent {AGENT} (pattern {pattern!r})")

    elif tool_name == "Bash":
        cmd = tool_input.get("command") or ""
        for raw_target in extract_bash_write_targets(cmd):
            for cand in {raw_target} | normalize_path_variants(raw_target, cwd):
                for pattern in blocked_paths:
                    if _matches(pattern, cand):
                        refuse(f"tool=Bash write-target={raw_target} (→{cand}) is outside "
                               f"the scope of agent {AGENT} (pattern {pattern!r}; "
                               f"command: {cmd[:160]!r})")
        for pattern in scope["blocked_bash_patterns"]:
            if re.search(pattern, cmd):
                refuse(f"tool=Bash command={cmd!r} matches {pattern!r}, "
                       f"outside the scope of agent {AGENT}")

    _emit("allow")


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception as exc:
        _log("PARSE_ERROR", f"unreadable hook payload ({exc}) — allowed")
        _emit("allow")
        return
    try:
        decide(data)
    except SystemExit:
        raise
    except Exception as exc:   # a bug here must not stop the agent working
        _log("HOOK_ERROR", f"{exc.__class__.__name__}: {exc} — allowed")
        _emit("allow")


if __name__ == "__main__":
    main()
