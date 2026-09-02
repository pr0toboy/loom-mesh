"""Tests for the SessionStart hook.

What is pinned here is mostly what the hook must NOT do: inject another agent's
inbox, replay work that was already acknowledged, or fall silent on the one
turn where silence reads as "I have decided to stop".
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

#: The shell these tests drive with send-keys. `--norc` skips the operator's
#: .bashrc — it does NOT touch HISTFILE, which stays ~/.bash_history, *theirs*.
#: An interactive bash appends its history on exit, so every line typed into a
#: test pane landed in the operator's own history file, on a private socket, with
#: nothing ever written to their terminal. Measured 2026-09-09, three times over
#: two days, 29 lines; found from their `history` output, not from ours. The
#: channel was a FILE, which is why every tmux measurement came back clean.
HARNESS_SHELL = "env HISTFILE=/dev/null bash --norc -i"

HOOK = Path(__file__).resolve().parents[1] / "session_start.py"


@pytest.fixture()
def mesh(tmp_path: Path) -> Path:
    (tmp_path / "mesh_roster.py").write_text(
        'INBOX_PEERS = ("alice", "bob")\nFACADE_PEERS = ()\n'
    )
    return tmp_path


def write_inbox(mesh: Path, agent: str, messages: list[dict]) -> None:
    (mesh / f"inbox-{agent}.jsonl").write_text(
        "".join(json.dumps(m) + "\n" for m in messages), encoding="utf-8")


def msg(mid: str, ts: str, body: str = "do the thing", acked: bool = False) -> dict:
    return {"id": mid, "ts": ts, "from": "alice", "to": "bob",
            "priority": "normal", "body": body, "acked": acked}


def run(mesh: Path, source: str = "startup", agent: str = "bob", **env_extra):
    env = {**os.environ, "MESH_HOME": str(mesh), "MESH_AGENT": agent}
    env.pop("TMUX", None)
    env.update(env_extra)
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps({"source": source}),
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    if not r.stdout.strip():
        return None
    return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


def test_unread_messages_are_injected(mesh):
    write_inbox(mesh, "bob", [msg("aaa11111", "2026-09-02T10:00:00+02:00")])
    out = run(mesh)
    assert "1 unread message" in out
    assert "do the thing" in out and "aaa11111" in out


def test_an_acked_message_is_never_replayed(mesh):
    """It was handled live, mid-session; the cursor has not moved past it yet.

    Replaying it makes the agent do the same work twice, which on a mesh means
    sending the same instruction to someone else twice.
    """
    write_inbox(mesh, "bob", [msg("aaa11111", "2026-09-02T10:00:00+02:00", acked=True)])
    assert run(mesh) is None


def test_the_cursor_advances_so_the_next_session_is_quiet(mesh):
    write_inbox(mesh, "bob", [msg("aaa11111", "2026-09-02T10:00:00+02:00")])
    assert run(mesh) is not None
    assert run(mesh) is None
    state = json.loads((mesh / "state-bob.json").read_text())
    assert state["last_read"]["alice"]["id"] == "aaa11111"


def test_same_second_siblings_are_both_delivered(mesh):
    """Two messages in one second: advancing on the timestamp alone loses one."""
    ts = "2026-09-02T10:00:00+02:00"
    write_inbox(mesh, "bob", [msg("aaa11111", ts, "first"), msg("bbb22222", ts, "second")])
    out = run(mesh)
    assert "first" in out and "second" in out
    state = json.loads((mesh / "state-bob.json").read_text())
    assert set(state["last_read"]["alice"]["ids_at_ts"]) == {"aaa11111", "bbb22222"}


def test_a_reset_always_gets_a_resume_directive_even_with_an_empty_inbox(mesh):
    """The failure this prevents: an agent that acknowledges and stops mid-task.

    After a compact or clear the harness gives a fresh turn with no instruction.
    A silent hook there ends the work with no error and no explanation.
    """
    for source in ("compact", "clear"):
        out = run(mesh, source=source)
        assert out is not None, f"{source} must not be silent"
        assert "not a signal to stop" in out


def test_a_plain_startup_with_nothing_to_say_stays_silent(mesh):
    assert run(mesh, source="startup") is None


def test_an_unknown_agent_gets_nothing(mesh):
    write_inbox(mesh, "bob", [msg("aaa11111", "2026-09-02T10:00:00+02:00")])
    assert run(mesh, agent="stranger") is None


def test_a_corrupt_line_does_not_hide_the_rest(mesh):
    inbox = mesh / "inbox-bob.jsonl"
    inbox.write_text("{ broken\n" + json.dumps(msg("aaa11111", "2026-09-02T10:00:00+02:00")) + "\n")
    out = run(mesh)
    assert "do the thing" in out


def test_the_pane_wins_over_a_wrongly_inherited_agent_name(mesh):
    """A shared tmux server exports the name of whoever started it.

    Every pane it later creates inherits that MESH_AGENT, so trusting the
    variable inside a real pane makes one agent read — and advance the cursor of
    — another's inbox. Inside a pane, the pane's own session name decides.
    """
    if subprocess.run(["which", "tmux"], capture_output=True).returncode != 0:
        pytest.skip("tmux not installed")

    socket = f"loomhook-{os.getpid()}"
    tmux = ["tmux", "-L", socket]
    write_inbox(mesh, "bob", [msg("aaa11111", "2026-09-02T10:00:00+02:00", "for bob")])
    write_inbox(mesh, "alice", [{**msg("ccc33333", "2026-09-02T10:00:00+02:00", "for alice"),
                                 "from": "bob", "to": "alice"}])
    # A shell, not a long-running command: send-keys types into whatever the
    # pane is running, and `sleep` reads no input.
    subprocess.run([*tmux, "new-session", "-d", "-s", "bob", HARNESS_SHELL],
                   capture_output=True, check=True)
    subprocess.run(["sleep", "1"])
    try:
        # The pane is bob's; the environment lies and says alice.
        r = subprocess.run(
            [*tmux, "send-keys", "-t", "bob",
             f"MESH_HOME={mesh} MESH_AGENT=alice {sys.executable} {HOOK} "
             f"< /dev/null > {mesh}/out.json 2>{mesh}/err.txt", "Enter"],
            capture_output=True)
        assert r.returncode == 0
        subprocess.run(["sleep", "2"])
        out = (mesh / "out.json").read_text()
        assert out.strip(), "the hook produced nothing inside the pane"
        context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "for bob" in context, "the pane's own inbox must be the one injected"
        assert "for alice" not in context
        assert "identity conflict" in (mesh / "err.txt").read_text()
    finally:
        subprocess.run([*tmux, "kill-server"], capture_output=True)
