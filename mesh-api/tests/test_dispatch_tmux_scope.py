"""The dispatcher must not type a ticket into a session it does not own.

`_push_local` ends with `send-keys Enter`, so it RUNS the ticket in an agent's
pane. Two defects lived in the hardcoded "tmux" it used to call:

* a test bench could not redirect it at all, short of shadowing `tmux` on PATH —
  which is what reviewers actually did, so every bench's isolation rested on a
  trick outside this code rather than on the code itself;
* on the default server, the only thing separating a bench from a live agent was
  that no session happened to be NAMED like one. Deployments of this project name
  sessions after their agents, so there that separation does not exist.

Same rule, and same tests, as the bus watcher: the server is configurable, and a
mesh home that is not the default one may not drive the default server.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

PROBE = r'''
import json, os, sys
sys.path.insert(0, os.environ["MESH_API_SRC"])
import ticket_dispatcher as td
out = {"consistent": td._tmux_scope_is_consistent(), "tmux": td._TMUX}
out["pushed"] = td._push_local("bob", "a ticket")
print(json.dumps(out))
'''

FAKE_TMUX = '''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_TMUX_LOG"
exit 0
'''


def _run_probe(tmp_path: Path, mesh_home: Path, home: Path, mesh_tmux: str | None) -> tuple[dict, str]:
    fake_dir = tmp_path / "bin"; fake_dir.mkdir(exist_ok=True)
    fake = fake_dir / "tmux"; fake.write_text(FAKE_TMUX); fake.chmod(0o755)
    calls = tmp_path / "tmux-calls.log"; calls.write_text("")
    probe = tmp_path / "probe.py"; probe.write_text(PROBE)

    env = {**os.environ,
           "HOME": str(home),
           "MESH_HOME": str(mesh_home),
           "MESH_API_SRC": str(REPO / "mesh-api"),
           "PATH": f"{fake_dir}:{os.environ['PATH']}",
           "FAKE_TMUX_LOG": str(calls)}
    env.pop("MESH_TMUX", None)
    if mesh_tmux is not None:
        env["MESH_TMUX"] = mesh_tmux

    r = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True,
                       env=env, timeout=60)
    assert r.returncode == 0, f"probe failed:\n{r.stdout}\n{r.stderr}"
    import json as _json
    return _json.loads(r.stdout.strip().splitlines()[-1]), calls.read_text()


def test_a_bench_mesh_may_not_type_a_ticket_into_the_default_server(tmp_path):
    home = tmp_path / "home"; (home / "mesh").mkdir(parents=True)
    bench = tmp_path / "bench"; bench.mkdir()
    out, tmux_calls = _run_probe(tmp_path, bench, home, mesh_tmux=None)

    assert out["consistent"] is False
    assert out["pushed"] is False, "the dispatcher pushed onto a server this mesh does not own"
    assert tmux_calls == "", (
        "tmux was invoked at all — the refusal has to come BEFORE any send-keys, or a "
        f"session merely named like an agent receives the ticket: {tmux_calls!r}")


def test_the_default_mesh_still_dispatches(tmp_path):
    """Positive control: a guard that refuses everything would pass the test above."""
    home = tmp_path / "home"
    mesh = home / "mesh"; mesh.mkdir(parents=True)
    out, tmux_calls = _run_probe(tmp_path, mesh, home, mesh_tmux=None)

    assert out["consistent"] is True
    assert out["pushed"] is True, "delivery on the deployment's own mesh must still work"
    assert "send-keys" in tmux_calls and "Enter" in tmux_calls, (
        f"the ticket was never typed: {tmux_calls!r}")


def test_an_explicit_socket_is_used_verbatim(tmp_path):
    """MESH_TMUX is what a bench is supposed to pass — and it must be honoured."""
    home = tmp_path / "home"; (home / "mesh").mkdir(parents=True)
    bench = tmp_path / "bench"; bench.mkdir()
    out, tmux_calls = _run_probe(tmp_path, bench, home, mesh_tmux="tmux -L benchsock")

    assert out["tmux"] == ["tmux", "-L", "benchsock"], (
        f"MESH_TMUX was not honoured, so a bench cannot isolate itself: {out['tmux']}")
    assert out["pushed"] is True
    assert "-L benchsock" in tmux_calls or "benchsock" in tmux_calls, (
        f"the explicit socket never reached the command line: {tmux_calls!r}")
