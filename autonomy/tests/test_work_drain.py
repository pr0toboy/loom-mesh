"""Tests for the work-drain Stop hook (invoked as a subprocess, like Claude Code does)."""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from autonomy.board import Board
from autonomy.run import Run

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "work_drain_stop.py"


def _run_hook(root, agent):
    """Invoke the hook with a clean env (no TMUX → MESH_AGENT decides the agent).

    ``LOOM_NIGHT_CONFIG`` points at a path that does not exist on purpose: these
    tests are about the drain itself, so they must not inherit the *host's* real
    night window (which would close them out during the day). The night gate has
    its own file, ``test_night_window.py``.
    """
    env = {
        "PATH": "/usr/bin:/bin",
        "LOOM_ROOT": str(root),
        "MESH_AGENT": agent,
        "LOOM_NIGHT_CONFIG": str(root / "no-night-window.json"),
    }
    return subprocess.run(
        [sys.executable, str(HOOK)], input="{}",
        capture_output=True, text=True, env=env, timeout=30,
    )


def _blocks(proc) -> bool:
    return '"decision": "block"' in proc.stdout


def test_noop_without_loom_root():
    env = {"PATH": "/usr/bin:/bin", "MESH_AGENT": "worker-a"}
    p = subprocess.run([sys.executable, str(HOOK)], input="{}",
                       capture_output=True, text=True, env=env, timeout=30)
    assert p.returncode == 0 and p.stdout.strip() == ""


def test_noop_when_no_active_run(tmp_path):
    Board(tmp_path).create_task("T1", "x", owner="worker-a")
    p = _run_hook(tmp_path, "worker-a")
    assert p.returncode == 0 and not _blocks(p)


def test_noop_for_non_participant(tmp_path):
    Run(tmp_path).start("r1", ["worker-a"])
    Board(tmp_path).create_task("T1", "x", owner="worker-b")
    p = _run_hook(tmp_path, "worker-b")  # worker-b not a participant
    assert not _blocks(p)


def test_blocks_when_agent_has_open_task(tmp_path):
    Run(tmp_path).start("r1", ["worker-a"])
    Board(tmp_path).create_task("T1", "build the thing", owner="worker-a")
    p = _run_hook(tmp_path, "worker-a")
    assert p.returncode == 0
    assert _blocks(p)
    assert "build the thing" in p.stdout


def test_allows_stop_when_no_open_task(tmp_path):
    Run(tmp_path).start("r1", ["worker-a"])
    b = Board(tmp_path)
    b.create_task("T1", "x", owner="worker-a")
    b.submit("T1", "worker-a", branch="feat/x")   # now in_review → not open for worker
    p = _run_hook(tmp_path, "worker-a")
    assert not _blocks(p)


def _run_hook_armed_by_pointer(root, agent, home):
    """No LOOM_ROOT: the hook must find the run through ~/mesh/loom-arm/<agent>.
    HOME is redirected so the arming directory is the test's, not the host's."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "MESH_AGENT": agent,
        "LOOM_NIGHT_CONFIG": str(root / "no-night-window.json"),
    }
    return subprocess.run([sys.executable, str(HOOK)], input="{}",
                          capture_output=True, text=True, env=env, timeout=30)


def test_pointer_file_arms_an_agent_without_a_restart(tmp_path):
    """The point of the pointer: arming must not require restarting the agent
    (which would throw away whatever it had in flight)."""
    run_root = tmp_path / "run"
    run_root.mkdir()
    Run(run_root).start("r1", ["worker-a"])
    Board(run_root).create_task("T1", "build the thing", owner="worker-a")

    home = tmp_path / "home"
    arm = home / "mesh" / "loom-arm"
    arm.mkdir(parents=True)
    (arm / "worker-a").write_text(str(run_root) + "\n")

    p = _run_hook_armed_by_pointer(run_root, "worker-a", home)
    assert _blocks(p)
    assert "build the thing" in p.stdout


def test_pointer_for_another_agent_does_not_arm_me(tmp_path):
    run_root = tmp_path / "run"
    run_root.mkdir()
    Run(run_root).start("r1", ["worker-a"])
    Board(run_root).create_task("T1", "x", owner="worker-a")

    home = tmp_path / "home"
    arm = home / "mesh" / "loom-arm"
    arm.mkdir(parents=True)
    (arm / "worker-a").write_text(str(run_root))     # only worker-a is armed

    assert not _blocks(_run_hook_armed_by_pointer(run_root, "worker-b", home))


def test_no_arming_directory_is_a_silent_noop(tmp_path):
    """The fast path taken by every unarmed agent on every turn."""
    home = tmp_path / "home"
    home.mkdir()
    p = _run_hook_armed_by_pointer(tmp_path, "worker-a", home)
    assert p.returncode == 0 and p.stdout.strip() == ""


def test_empty_pointer_file_does_not_arm(tmp_path):
    home = tmp_path / "home"
    arm = home / "mesh" / "loom-arm"
    arm.mkdir(parents=True)
    (arm / "worker-a").write_text("   \n")
    p = _run_hook_armed_by_pointer(tmp_path, "worker-a", home)
    assert p.returncode == 0 and p.stdout.strip() == ""


def test_kill_switch_stops_and_records(tmp_path):
    Run(tmp_path).start("r1", ["worker-a"], guardrails={
        "window_token_cap": 1000, "kill_fraction": 0.8,
    })
    b = Board(tmp_path)
    b.create_task("T1", "x", owner="worker-a")
    # a fresh fleet snapshot well over the kill threshold
    usage = tmp_path / ".loom" / "usage"
    usage.mkdir(parents=True)
    (usage / "host.json").write_text(json.dumps({
        "source": "host", "window_start": "W1", "window_end": "W1e",
        "remaining_minutes": 100.0, "limit_tokens": 950, "total_tokens": 950,
        "output_tokens": 0, "burn_tpm": 0.0,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }))
    p = _run_hook(tmp_path, "worker-a")
    assert not _blocks(p)                       # kill → do NOT keep going
    kinds = [e["type"] for e in b.events()]
    assert "usage_kill" in kinds                # recorded for the coordinator
    assert b.tasks()["T1"]["status"] == "blocked"


def test_stale_backstop_escalates(tmp_path):
    Run(tmp_path).start("r1", ["worker-a"])
    b = Board(tmp_path)
    b.create_task("T1", "x", owner="worker-a")
    # pre-seed the marker at the stale ceiling with the matching signature
    sig = "T1:0:assigned"
    state = tmp_path / ".loom" / "state"
    state.mkdir(parents=True)
    (state / "work-drain-worker-a.json").write_text(json.dumps({"sig": sig, "stale": 29}))
    p = _run_hook(tmp_path, "worker-a")
    assert not _blocks(p)                        # backstop fired → allow stop
    assert b.tasks()["T1"]["status"] == "blocked"


def test_reopened_to_in_progress_keeps_agent_working(tmp_path):
    # R5: a done task reopened to in_progress is open work again → the hook blocks.
    Run(tmp_path).start("r1", ["worker-a"])
    b = Board(tmp_path)
    b.create_task("T1", "finish the thing", owner="worker-a")
    b.submit("T1", "worker-a", branch="b")
    b.review("T1", "carol", passed=True)      # done
    b.reopen("T1", by="coordinator", to="in_progress")
    p = _run_hook(tmp_path, "worker-a")
    assert _blocks(p)                           # open again → keep working
    assert "finish the thing" in p.stdout
