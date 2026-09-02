"""`_interrupt_pane` is the third writer, and it had no test.

The bus watcher and the ticket dispatcher both refuse to drive the default tmux
server unless the deployment declares one — a copy of this repository would
otherwise reach live agent panes, since sessions are named after their agents.
`night_reconcile._interrupt_pane` sends Escape the same way, and a review found
that turning its guard into `return True` left the whole suite green: the rule
was enforced in two places out of three, and nothing said so.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

PROBE = r'''
import json, os, sys
sys.path.insert(0, os.environ["REPO"])
from autonomy import night_reconcile as nr
lines = []
nr._interrupt_pane("bob", lines.append)
print(json.dumps({"consistent": nr._tmux_scope_is_consistent(),
                  "tmux": nr._TMUX, "log": lines}))
'''

FAKE_TMUX = '''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_TMUX_LOG"
exit 0
'''


def _probe(tmp_path: Path, mesh_home: Path, home: Path, mesh_tmux: str | None):
    fake_dir = tmp_path / "bin"; fake_dir.mkdir(exist_ok=True)
    fake = fake_dir / "tmux"; fake.write_text(FAKE_TMUX); fake.chmod(0o755)
    calls = tmp_path / "tmux-calls.log"; calls.write_text("")
    probe = tmp_path / "probe.py"; probe.write_text(PROBE)

    env = {**os.environ, "HOME": str(home), "MESH_HOME": str(mesh_home),
           "REPO": str(REPO), "PATH": f"{fake_dir}:{os.environ['PATH']}",
           "FAKE_TMUX_LOG": str(calls)}
    env.pop("MESH_TMUX", None)
    if mesh_tmux is not None:
        env["MESH_TMUX"] = mesh_tmux
    r = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True,
                       env=env, timeout=60)
    assert r.returncode == 0, f"probe failed:\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1]), calls.read_text()


def test_an_undeclared_mesh_may_not_interrupt_a_pane(tmp_path):
    home = tmp_path / "home"; (home / "mesh").mkdir(parents=True)
    bench = tmp_path / "bench"; bench.mkdir()
    out, tmux_calls = _probe(tmp_path, bench, home, mesh_tmux=None)

    assert out["consistent"] is False
    assert tmux_calls == "", (
        f"tmux was invoked from a mesh that declared no server: {tmux_calls!r}")
    assert any("REFUSED" in line for line in out["log"]), (
        f"the refusal must be logged, not silent: {out['log']!r}")


def test_a_declared_server_is_used(tmp_path):
    """Positive control: a guard that refuses everything would pass the test above."""
    home = tmp_path / "home"; (home / "mesh").mkdir(parents=True)
    bench = tmp_path / "bench"; bench.mkdir()
    out, tmux_calls = _probe(tmp_path, bench, home, mesh_tmux="tmux -L benchsock")

    assert out["consistent"] is True
    assert out["tmux"] == ["tmux", "-L", "benchsock"], out["tmux"]
    assert "has-session" in tmux_calls, (
        f"the declared server was never reached: {tmux_calls!r}")
