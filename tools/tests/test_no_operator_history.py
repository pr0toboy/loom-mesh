"""No test may write to the operator's shell history.

`bash --norc -i` skips their `.bashrc`; it does **not** touch `HISTFILE`, which
stays `~/.bash_history` — theirs. An interactive bash appends its history when it
exits, so every line a test typed into its pane ended up in the operator's own
history file, and surfaced days later when they pressed the up arrow.

It took two days to find because the channel was a **file**, not a terminal:
every tmux measurement came back clean, and rightly so — the send-keys went to a
private socket, no pane sat on their tty, an open witness pty received nothing.
Nothing was ever written to their terminal. Twenty-nine lines were written to
their history.

So the rule is about the *shell*, not the socket, and it is the companion of
`test_no_default_socket.py`: that one keeps a test from reaching the wrong tmux
server, this one keeps it from reaching the wrong file.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".pytest_cache"}

#: An interactive shell: `bash -i`, `bash --norc -i`, `sh -i`, flags in any order.
_INTERACTIVE = re.compile(r"\b(?:ba|z|k)?sh\b[^\n]*(?:\s-i\b|\s-[a-zA-Z]*i\b)")
#: What makes it harmless: the history file is sent somewhere that is not theirs.
_NEUTRALISED = re.compile(r"HISTFILE\s*=")


def _sources() -> list[Path]:
    """Every Python file a test run executes, not only ``test_*.py``.

    Not `git ls-files`: the file being written right now is the one this check
    exists to stop. And not `test_*.py` alone — the 2026-09-09 gate pointed out
    that `conftest.py` and the shared helper `_systemd.py` were invisible, which
    is exactly where a harness shell would naturally be factored out to.
    """
    out = []
    for p in REPO.rglob("*.py"):
        if any(part in _SKIP_DIRS or part.startswith(".") for part in p.parts):
            continue
        if p.name.startswith("test_") or p.name == "conftest.py" or "tests" in p.parts:
            out.append(p)
    return sorted(set(out))


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Ids of the string constants that are docstrings — prose, not commands.

    Reading raw lines instead flagged this very file for the sentence that
    explains the defect. A check that cries wolf is one people learn to skip.
    """
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def _offenders(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstring_nodes(tree)
    bad = []
    for node in ast.walk(tree):
        # A command split across list items — ["bash", "--norc", "-i"] — is the
        # same command; joining the parts is what makes the check see it. The
        # gate found this form invisible to the single-string version.
        if isinstance(node, (ast.List, ast.Tuple)):
            parts = [e.value for e in node.elts
                     if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if parts:
                joined = " ".join(parts)
                if _INTERACTIVE.search(joined) and not _NEUTRALISED.search(joined):
                    bad.append((node.lineno, joined.strip()))
            continue
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in skip:
            continue
        text = node.value
        if _INTERACTIVE.search(text) and not _NEUTRALISED.search(text):
            bad.append((node.lineno, text.strip()))
    return bad


def test_the_scan_sees_something():
    """A regex that matched nothing would make this file pass forever."""
    assert _sources(), "no test file found — this guard is scanning nothing"


@pytest.mark.parametrize("path", _sources(), ids=lambda p: str(p.relative_to(REPO)))
def test_no_interactive_shell_writes_the_operator_history(path: Path):
    bad = _offenders(path)
    assert not bad, (
        f"{path.relative_to(REPO)} starts an interactive shell without neutralising "
        f"HISTFILE, so whatever is typed into it is appended to the operator's own "
        f"~/.bash_history when the shell exits:\n"
        + "\n".join(f"  line {n}: {text}" for n, text in bad)
        + "\nUse the module's HARNESS_SHELL constant instead."
    )
