"""Tests for the PreToolUse scope hook.

Two things are asserted throughout, and they pull in opposite directions:
the hook must catch a write that leaves the agent's scope *however it is
spelled*, and it must never block work when something about itself is broken.
Both failures are silent in production — one lets an agent write where it
should not, the other stops an agent for reasons no one can see — so they are
pinned here.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "scope_check.py"


@pytest.fixture()
def mesh(tmp_path: Path) -> Path:
    (tmp_path / "scopes.json").write_text(json.dumps({
        "curator": {
            "blocked_paths": ["^" + str(tmp_path / "private") + "/.*"],
            "blocked_bash_patterns": [r"systemctl\s+.*\bdatabase\b"],
        }
    }))
    return tmp_path


def call(payload: dict, mesh: Path, agent: str = "curator", **env_extra: str) -> dict:
    env = {**os.environ, "MESH_HOME": str(mesh), "MESH_AGENT": agent, **env_extra}
    env.pop("MESH_SCOPE_ENFORCE", None)
    env.update(env_extra)
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, f"the hook must always exit 0: {r.stderr}"
    return json.loads(r.stdout)["hookSpecificOutput"]


def log_of(mesh: Path) -> str:
    log = mesh / "logs" / "scope-check.log"
    return log.read_text() if log.exists() else ""


# ── Log-only is the default, and it is not a no-op ────────────────────────────

def test_violation_is_recorded_but_allowed_by_default(mesh):
    out = call({"tool_name": "Write",
                "tool_input": {"file_path": str(mesh / "private" / "secrets.txt")}}, mesh)
    assert out["permissionDecision"] == "allow", "log-only mode must not block"
    assert "WOULD_DENY" in log_of(mesh), "a silent guard is indistinguishable from none"


def test_enforcement_denies_the_same_call(mesh):
    out = call({"tool_name": "Write",
                "tool_input": {"file_path": str(mesh / "private" / "secrets.txt")}},
               mesh, MESH_SCOPE_ENFORCE="1")
    assert out["permissionDecision"] == "deny"
    assert "outside the scope" in out["permissionDecisionReason"]
    assert "DENY" in log_of(mesh)


def test_a_path_in_scope_is_allowed_and_not_logged(mesh):
    out = call({"tool_name": "Write",
                "tool_input": {"file_path": str(mesh / "work" / "notes.md")}},
               mesh, MESH_SCOPE_ENFORCE="1")
    assert out["permissionDecision"] == "allow"
    assert "DENY" not in log_of(mesh)


# ── Spellings of the same path ────────────────────────────────────────────────

def test_traversal_spelling_is_caught(mesh):
    """`work/../private/x` is the same file as `private/x`."""
    sneaky = str(mesh / "work" / ".." / "private" / "secrets.txt")
    out = call({"tool_name": "Write", "tool_input": {"file_path": sneaky}},
               mesh, MESH_SCOPE_ENFORCE="1")
    assert out["permissionDecision"] == "deny"


def test_relative_path_is_resolved_against_the_project_dir(mesh):
    out = call({"tool_name": "Write", "tool_input": {"file_path": "private/secrets.txt"}},
               mesh, MESH_SCOPE_ENFORCE="1", CLAUDE_PROJECT_DIR=str(mesh))
    assert out["permissionDecision"] == "deny"


# ── The shell is a file tool too ──────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "echo bad > {p}/secrets.txt",
    "tee {p}/secrets.txt < input",
    "sed -i s/a/b/ {p}/secrets.txt",
    "dd if=/dev/zero of={p}/secrets.txt",
    "cp /etc/hostname {p}/secrets.txt",
])
def test_shell_writes_do_not_bypass_the_path_scope(mesh, command):
    """Path scope enforced only on file tools is cosmetic once a shell exists."""
    out = call({"tool_name": "Bash",
                "tool_input": {"command": command.format(p=mesh / "private")}},
               mesh, MESH_SCOPE_ENFORCE="1")
    assert out["permissionDecision"] == "deny", f"not caught: {command}"


def test_blocked_command_pattern_is_denied(mesh):
    out = call({"tool_name": "Bash",
                "tool_input": {"command": "systemctl --user restart database"}},
               mesh, MESH_SCOPE_ENFORCE="1")
    assert out["permissionDecision"] == "deny"


def test_ordinary_command_is_allowed(mesh):
    out = call({"tool_name": "Bash", "tool_input": {"command": "ls -la"}},
               mesh, MESH_SCOPE_ENFORCE="1")
    assert out["permissionDecision"] == "allow"


# ── Failing open, loudly ──────────────────────────────────────────────────────

def test_unknown_agent_has_no_scope_and_is_allowed(mesh):
    out = call({"tool_name": "Write", "tool_input": {"file_path": str(mesh / "private" / "x")}},
               mesh, agent="nobody", MESH_SCOPE_ENFORCE="1")
    assert out["permissionDecision"] == "allow"


def test_missing_scope_file_allows_everything(tmp_path):
    out = call({"tool_name": "Write", "tool_input": {"file_path": "/anything"}}, tmp_path,
               MESH_SCOPE_ENFORCE="1")
    assert out["permissionDecision"] == "allow"


def test_corrupt_scope_file_allows_and_says_so(mesh):
    (mesh / "scopes.json").write_text("{ not json")
    out = call({"tool_name": "Write", "tool_input": {"file_path": str(mesh / "private" / "x")}},
               mesh, MESH_SCOPE_ENFORCE="1")
    assert out["permissionDecision"] == "allow"
    assert "CONFIG_ERROR" in log_of(mesh)


def test_unparseable_payload_allows_and_says_so(mesh):
    env = {**os.environ, "MESH_HOME": str(mesh), "MESH_AGENT": "curator",
           "MESH_SCOPE_ENFORCE": "1"}
    r = subprocess.run([sys.executable, str(HOOK)], input="not json at all",
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "PARSE_ERROR" in log_of(mesh)


# -- The shell vectors that used to walk straight through ---------------------

@pytest.mark.parametrize("command", [
    "{rm} -f {p}/secrets.txt",
    "{rm} -r {p}",
    "mkdir -p {p}/newdir",
    "python3 -c \"open('{p}/x','w').write('hi')\"",
    "git -C {p} checkout -- .",
    "curl -o {p}/x http://example.invalid",
    "cd {p} && echo hi > x",
])
def test_destructive_and_indirect_writes_are_caught(mesh, command):
    """Deletion, creation and redirection through another tool all reach the disk.

    A path scope that only knows `>` and `cp` is decoration: deletion was the
    first thing that walked through it, and `cd <blocked> && echo > x` hides the
    target in a relative path.
    """
    out = call({"tool_name": "Bash",
                "tool_input": {"command": command.format(p=mesh / "private", rm="r" + "m")}},
               mesh, MESH_SCOPE_ENFORCE="1")
    assert out["permissionDecision"] == "deny", f"not caught: {command}"


def test_a_notebook_edit_is_checked_on_its_own_path_field(mesh):
    """NotebookEdit names its target `notebook_path`; reading only `file_path`
    let every notebook through the scope."""
    out = call({"tool_name": "NotebookEdit",
                "tool_input": {"notebook_path": str(mesh / "private" / "nb.ipynb")}},
               mesh, MESH_SCOPE_ENFORCE="1")
    assert out["permissionDecision"] == "deny"


def test_an_unexpandable_pattern_says_so_instead_of_going_quiet(mesh):
    """A regex quantifier makes str.format raise: the rule survives but matches
    nothing. Silence there is a scope that looks enforced and is not."""
    (mesh / "scopes.json").write_text(json.dumps({
        "curator": {"blocked_paths": ["^{home}/private/.{0,3}$"],
                    "blocked_bash_patterns": []}
    }))
    call({"tool_name": "Write", "tool_input": {"file_path": str(mesh / "private" / "x")}},
         mesh, MESH_SCOPE_ENFORCE="1")
    assert "CONFIG_ERROR" in log_of(mesh)
