"""Tests for the night window: the module's arithmetic, and the gate in the hook.

The gate is exercised through the real hook subprocess (like Claude Code invokes
it), and each test builds a window *relative to the current clock* so it is open
or closed by construction — no frozen time, no test-only backdoor in the module.
"""
import json
import subprocess
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

from autonomy.board import Board
from autonomy.night_window import decide
from autonomy.run import Run

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "work_drain_stop.py"


def _hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _window_around_now(before_minutes: int, after_minutes: int) -> dict:
    """A window that certainly contains right now."""
    now = datetime.now()
    return {"start": _hhmm(now - timedelta(minutes=before_minutes)),
            "stop": _hhmm(now + timedelta(minutes=after_minutes))}


def _window_excluding_now() -> dict:
    """A window that certainly does not contain right now (a 20 min slot, 2 h away)."""
    start = datetime.now() + timedelta(hours=2)
    return {"start": _hhmm(start), "stop": _hhmm(start + timedelta(minutes=20))}


def _write(tmp_path, cfg) -> Path:
    p = tmp_path / "night.json"
    p.write_text(cfg if isinstance(cfg, str) else json.dumps(cfg), encoding="utf-8")
    return p


# --- the module ------------------------------------------------------------

def test_unconfigured_is_not_a_refusal(tmp_path):
    d = decide(path=tmp_path / "does-not-exist.json")
    assert d.open is None and not d.closed


def test_wrapping_window_covers_midnight(tmp_path):
    p = _write(tmp_path, {"start": "23:00", "stop": "02:30"})
    inside = [time(23, 0), time(23, 59), time(0, 0), time(2, 29)]
    outside = [time(2, 30), time(3, 0), time(8, 0), time(22, 59)]
    day = datetime.now().date()
    for t in inside:
        assert decide(datetime.combine(day, t), p).open is True, t
    for t in outside:
        assert decide(datetime.combine(day, t), p).open is False, t


def test_the_0300_boundary_is_the_point_of_the_gate(tmp_path):
    """02:30 must be closed: a window opened at 03:00 would run to 08:00."""
    p = _write(tmp_path, {"start": "23:00", "stop": "02:30"})
    day = datetime.now().date()
    assert decide(datetime.combine(day, time(2, 30)), p).open is False


def test_corrupt_config_fails_closed(tmp_path):
    for bad in ["{ not json", json.dumps({"start": "23:00"}),
                json.dumps({"start": "25:00", "stop": "02:30"}),
                json.dumps({"start": "23h", "stop": "02:30"})]:
        p = _write(tmp_path, bad)
        d = decide(path=p)
        assert d.closed, bad
        assert "closed" in d.reason


def test_empty_window_is_closed_not_open_all_day(tmp_path):
    p = _write(tmp_path, {"start": "23:00", "stop": "23:00"})
    assert decide(path=p).closed


# --- the gate, through the hook -------------------------------------------

def _run_hook(root, agent, night_cfg: Path | None):
    env = {"PATH": "/usr/bin:/bin", "LOOM_ROOT": str(root), "MESH_AGENT": agent}
    if night_cfg is not None:
        env["LOOM_NIGHT_CONFIG"] = str(night_cfg)
    return subprocess.run([sys.executable, str(HOOK)], input="{}",
                          capture_output=True, text=True, env=env, timeout=30)


def _blocks(proc) -> bool:
    return '"decision": "block"' in proc.stdout


def _seed(tmp_path):
    Run(tmp_path).start("r1", ["worker-a"])
    b = Board(tmp_path)
    b.create_task("T1", "build the thing", owner="worker-a")
    return b


def test_hook_blocks_inside_the_window(tmp_path):
    """The control: same setup as the refusal below, only the window differs."""
    _seed(tmp_path)
    cfg = _write(tmp_path, _window_around_now(60, 60))
    assert _blocks(_run_hook(tmp_path, "worker-a", cfg))


def test_hook_lets_agent_stop_outside_the_window(tmp_path):
    b = _seed(tmp_path)
    cfg = _write(tmp_path, _window_excluding_now())
    p = _run_hook(tmp_path, "worker-a", cfg)
    assert not _blocks(p)
    # and the work is preserved for the next night, not blamed on the agent
    assert b.tasks()["T1"]["status"] == "assigned"
    kinds = [e["type"] for e in b.events()]
    assert "night_window_closed" in kinds


def test_closing_time_pauses_the_stale_count_it_does_not_reset_it(tmp_path):
    """A wedged agent must still reach the backstop across several nights: if
    closing time cleared the marker, the 30-turn count would restart nightly and
    the escalation would never fire."""
    _seed(tmp_path)
    marker = tmp_path / ".loom" / "state" / "work-drain-worker-a.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"sig": "T1:0:assigned", "stale": 5}))

    cfg = _write(tmp_path, _window_excluding_now())
    assert not _blocks(_run_hook(tmp_path, "worker-a", cfg))
    assert marker.exists(), "closing time wiped the stale count"
    assert json.loads(marker.read_text())["stale"] == 5


def test_hook_unaffected_when_no_config(tmp_path):
    """Backward compatibility: no window file → the engine behaves as before."""
    _seed(tmp_path)
    assert _blocks(_run_hook(tmp_path, "worker-a", tmp_path / "absent.json"))
