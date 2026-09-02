"""No test may drive the DEFAULT tmux server.

The guards in the bus watcher, the ticket dispatcher and night_reconcile keep
those components from typing into a server they do not own. They do not — and
cannot — stop a TEST from calling tmux directly, and a test is what runs on a
contributor's machine, right next to their own shell and their own sessions.

That is not hypothetical. An operator's shell history held 121 injected lines
out of 500, and 57 of them were a hook invocation typed and EXECUTED in his
interactive shell, carrying a `/tmp/pytest-of-.../` path: the suite reaching a
terminal that had asked for nothing. A pane does not need to be named after an
agent to receive `send-keys` — it only needs to sit on the server the caller
aimed at.

So every tmux invocation in a test names its own socket. The check is static and
AST-based on purpose: it fails on the line that would do the damage before
anyone runs it, it needs no tmux installed, and matching real call sites rather
than the word "tmux" keeps it from crying over docstrings — a check that cries
wolf is one people learn to skip, which is what the personal-data guard says
about itself two directories away.
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: Flags that give tmux a server of its own.
_SOCKET_FLAGS = {"-L", "-S"}


#: Directories that hold no test and would only slow the walk down.
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".pytest_cache"}


def _test_files() -> list[Path]:
    """Every test file on disk — deliberately NOT `git ls-files`.

    Listing tracked files only would skip the file someone is writing right now,
    which is the one this check exists to stop. Found the hard way: a probe that
    added a bare `tmux send-keys` was invisible to an earlier version of this
    check, and the check passed green while doing nothing.
    """
    out = []
    for path in REPO.rglob("*"):
        if not path.is_file() or path.suffix not in (".py", ".sh"):
            continue
        if _SKIP_DIRS & set(path.relative_to(REPO).parts):
            continue
        rel = str(path.relative_to(REPO))
        if "/tests/" in rel or path.name.startswith("test_"):
            out.append(path)
    return out


def _argv_starts_with_tmux(node: ast.AST) -> bool:
    """True for a list/tuple literal whose first element is the string "tmux"."""
    if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
        return False
    first = node.elts[0]
    return isinstance(first, ast.Constant) and first.value == "tmux"


def _names_a_socket(node: ast.List | ast.Tuple) -> bool:
    return any(isinstance(e, ast.Constant) and e.value in _SOCKET_FLAGS for e in node.elts)


def _python_offenders(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(errors="replace"))
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = getattr(func, "attr", getattr(func, "id", ""))
        if name not in {"run", "Popen", "check_call", "check_output", "call"}:
            continue
        argv = node.args[0]
        if _argv_starts_with_tmux(argv) and not _names_a_socket(argv):
            out.append(f"{path.relative_to(REPO)}:{node.lineno}: "
                       f"tmux invoked with no -L/-S")
    return out


#: A shell line that RUNS tmux: start of line or after a pipe/;/&&, not inside a
#: comment. `$TMUX_BIN`, `$tmux` and the like carry their socket in the variable.
_SH_CALL = re.compile(r'(^|[;&|]\s*|\$\(\s*)tmux\s')


def _shell_offenders(path: Path) -> list[str]:
    out = []
    for i, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        m = _SH_CALL.search(stripped)
        if m and not re.search(r'-L\b|-S\b', stripped):
            out.append(f"{path.relative_to(REPO)}:{i}: {stripped[:100]}")
    return out


def test_no_test_touches_the_default_tmux_server():
    offenders: list[str] = []
    for path in _test_files():
        if path.suffix == ".py":
            offenders += _python_offenders(path)
        elif path.suffix == ".sh":
            offenders += _shell_offenders(path)
    assert not offenders, (
        "these test call sites invoke tmux without naming a socket, so they act on "
        "the default server — the one carrying the developer's own sessions and "
        "shell:\n  " + "\n  ".join(offenders)
    )
