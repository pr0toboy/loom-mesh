"""End to end: `bootstrap.sh`, then the API — with no roster written by hand.

Every other test in this suite gets its roster from `conftest.py`, which writes
one. That is convenient and it hid a real defect for months: `bootstrap.sh`
writes `$MESH_HOME/peers.py`, the API only ever read `$MESH_HOME/mesh_roster.py`,
and so a freshly installed mesh had an API that knew no agents. `GET /status`
was empty, `POST /send` answered 422 "unknown peer", and the installation
guide's ticket example could not work. The suite stayed green throughout,
because the tests supplied the file the installer does not.

So this test installs a mesh the way the documentation says to, and then asks
the API the questions a new user asks first. It is deliberately slower and
heavier than a unit test: it is the only one that exercises the seam between
the installer and the service, which is exactly where that class of bug lives.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO / "bootstrap.sh"


def _bootstrap(tmp_path: Path, mesh_home: Path | None = None,
               systemd: bool = False) -> tuple[Path, str]:
    """Install a two-agent mesh under tmp_path. Returns (mesh_home, token).

    ``mesh_home`` moves the install off the default location, which is what a
    deployment that sets `home` in its config does. ``systemd`` renders the unit
    files (still with --no-ssh/--skip-health): the units are written into the
    isolated HOME, and nothing is enabled there, so this touches no live service.
    """
    home = tmp_path / "home"
    mesh_home = mesh_home or (home / "mesh")
    home.mkdir(exist_ok=True)

    config = tmp_path / "mesh.toml"
    config.write_text(textwrap.dedent(f"""
        [mesh]
        home = "{mesh_home}"
        api_port = 8765
        api_bind = "127.0.0.1"
        log_dir = "{mesh_home}/logs"

        [hosts.primary]
        user = "tester"
        host = "localhost"

        [[agents]]
        name = "builder"
        host = "primary"
        workdir = "{home}/builder"
        charter = "worker"
        model = "claude-sonnet-4-6"

        [[agents]]
        name = "ops"
        host = "primary"
        workdir = "{home}/ops"
        charter = "supervisor"
        model = "claude-haiku-4-5-20251001"
    """).strip() + "\n")

    # HOME is isolated so the installer cannot touch the real one — it creates
    # agent workdirs under it. But isolating HOME also hides packages installed
    # in ~/.local, so the prerequisite check fails on a machine where pydantic
    # is a user install. Keep the real user site visible; it is a property of
    # the test environment, not of the thing under test.
    env = {**os.environ, "HOME": str(home), "USER": "tester",
           "PYTHONUSERBASE": os.environ.get(
               "PYTHONUSERBASE", str(Path(os.path.expanduser("~")) / ".local"))}
    argv = ["bash", str(BOOTSTRAP), "--no-ssh", "--skip-health"]
    if not systemd:
        argv.insert(2, "--no-systemd")
    result = subprocess.run(
        [*argv, str(config)], capture_output=True, text=True, env=env, timeout=180,
    )
    assert result.returncode == 0, f"bootstrap failed:\n{result.stdout}\n{result.stderr}"

    tokens = json.loads((mesh_home / "api-tokens.json").read_text())
    return mesh_home, tokens[0]["token"]


PROBE = r'''
import json, os, sys
from fastapi.testclient import TestClient
sys.path.insert(0, os.environ["MESH_API_SRC"])
from mesh_api.main import app

token = os.environ["MESH_TOKEN"]
auth = {"Authorization": f"Bearer {token}"}
out = {}
with TestClient(app) as c:
    out["status_agents"] = sorted((c.get("/status", headers=auth).json().get("agents") or {}).keys())
    out["send"] = c.post("/send", headers=auth, json={
        "from": "ops", "to": "builder", "priority": "normal", "body": "first ticket"}).status_code
    # No "from" at all — how the web UI posts. This is the seam the explicit
    # "from" above hides: the model's default sender has to be a peer the
    # installer wrote, not a name that only the test roster knows.
    r = c.post("/send", headers=auth, json={
        "to": "builder", "priority": "normal", "body": "posted without a from"})
    out["send_no_from"] = r.status_code
    out["send_no_from_detail"] = r.text[:400]
    # Exactly the payload docs/getting-started.md step 7 tells the reader to send.
    r = c.post("/tickets", headers=auth, json={
        "to": "builder",
        "prompt": "Write hello_world.py that prints the current time.",
        "dispatch_mode": "armed"})
    out["ticket"] = r.status_code
    out["ticket_detail"] = r.text[:400]

# The dispatcher derives the agents it watches from the same installed roster.
# Its unit tests always pass an explicit ``agents`` list, so the derivation
# itself is only ever exercised here — and a dispatcher that watches nobody
# starts cleanly, logs nothing, and never dispatches.
import ticket_dispatcher as td
out["dispatch_agents"] = td._dispatch_agents()
out["dispatch_config_agents"] = td.DEFAULT_CONFIG["agents"]
print(json.dumps(out))
'''


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_units_declare_the_mesh_home_and_the_tmux_server(tmp_path):
    """An install whose home is not ~/mesh must still produce working services.

    The writers refuse to drive the DEFAULT tmux server unless the deployment
    declares one — otherwise a copy of this repository types into live agent
    panes. That guard read "mesh home is not the default" as "this is a bench",
    which is wrong: `home` is a free config key, and the documentation tells
    operators to move it. A perfectly ordinary install then had a watcher that
    exited 2 on every start (with Restart=always, a loop) and a dispatcher that
    refused every ticket.

    So the signal is a DECLARATION, not an inference: bootstrap.sh writes
    Environment=MESH_TMUX into the units it renders. A clone run by hand
    declares nothing and is still refused, which is the case the guard is for.
    """
    home = tmp_path / "home"
    elsewhere = home / "somewhere" / "else" / "mesh"      # deliberately not ~/mesh
    _bootstrap(tmp_path, mesh_home=elsewhere, systemd=True)

    unit = home / ".config" / "systemd" / "user" / "mesh-watcher.service"
    assert unit.exists(), "bootstrap wrote no watcher unit"
    text = unit.read_text()
    assert f"Environment=MESH_HOME={elsewhere}" in text, (
        "the unit does not carry the mesh home, so the service would read ~/mesh "
        f"instead of the installed one:\n{text}")
    assert "Environment=MESH_TMUX=" in text, (
        f"the unit declares no tmux server, so the watcher refuses to start:\n{text}")
    assert "{{" not in text, f"unrendered placeholder in an installed unit:\n{text}"

    # And the guard must actually accept it: run the watcher's own predicate with
    # what the unit declares.
    declared = [l.split("=", 2)[2] for l in text.splitlines()
                if l.startswith("Environment=MESH_TMUX=")][0]
    r = subprocess.run(
        ["bash", "-c",
         f'source "{REPO}/bus/watcher.sh" >/dev/null 2>&1; _tmux_scope_is_consistent'],
        capture_output=True, text=True,
        env={**os.environ, "MESH_HOME": str(elsewhere), "MESH_TMUX": declared})
    assert r.returncode == 0, (
        "the watcher still refuses the server its own unit declares — the install "
        "would loop on Restart=always")

    # The control: the same mesh home with nothing declared stays refused.
    r2 = subprocess.run(
        ["bash", "-c",
         f'source "{REPO}/bus/watcher.sh" >/dev/null 2>&1; _tmux_scope_is_consistent'],
        capture_output=True, text=True,
        env={k: v for k, v in {**os.environ, "MESH_HOME": str(elsewhere)}.items()
             if k != "MESH_TMUX"})
    assert r2.returncode != 0, (
        "a mesh that declares no server must still be refused, or the guard is gone")


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_a_bootstrapped_mesh_has_an_api_that_knows_its_agents(tmp_path):
    mesh_home, token = _bootstrap(tmp_path)

    # The installer must have produced a roster the API can read. Which file it
    # is does not matter here; that it exists at all does.
    assert (mesh_home / "peers.py").exists()

    probe = tmp_path / "probe.py"
    probe.write_text(PROBE)
    env = {**os.environ,
           "HOME": str(tmp_path / "home"),
           "PYTHONUSERBASE": os.environ.get(
               "PYTHONUSERBASE", str(Path(os.path.expanduser("~")) / ".local")),
           "MESH_HOME": str(mesh_home),
           "MESH_TOKENS_PATH": str(mesh_home / "api-tokens.json"),
           "MESH_API_SRC": str(REPO / "mesh-api"),
           "MESH_TOKEN": token}
    env.pop("MESH_API_NO_AUTH", None)
    # Set on this machine for the live dispatcher; it would override the
    # derivation this test is here to check.
    env.pop("MESH_DISPATCH_AGENTS", None)

    run = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True,
                         env=env, timeout=180)
    assert run.returncode == 0, f"probe failed:\n{run.stdout}\n{run.stderr}"
    result = json.loads(run.stdout.strip().splitlines()[-1])

    assert result["status_agents"] == ["builder", "ops"], (
        "the API must list the agents the installer just created; an empty roster "
        "here is the bug this test exists for"
    )
    assert result["send"] == 200, "POST /send rejected an agent that bootstrap.sh created"
    assert result["send_no_from"] == 200, (
        "POST /send with no 'from' must work on a freshly installed mesh — the "
        "default sender in models.py has to be one of the peers bootstrap.sh "
        f"writes: {result.get('send_no_from_detail')}"
    )
    assert result["ticket"] in (200, 201), (
        f"POST /tickets rejected a freshly installed agent: {result.get('ticket_detail')}")
    assert result["dispatch_agents"] == ["builder", "ops"], (
        "the dispatcher must derive its agents from the roster bootstrap.sh wrote; "
        f"it watches {result['dispatch_agents']}"
    )
    assert result["dispatch_config_agents"] == ["builder", "ops"], (
        "DEFAULT_CONFIG['agents'] is evaluated at import — it is the value the "
        f"daemon actually runs with: {result['dispatch_config_agents']}"
    )
