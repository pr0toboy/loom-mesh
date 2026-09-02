"""F5 — max_iterations as a hard, run-ending cap."""
import multiprocessing as mp
import subprocess
import sys
from pathlib import Path

from autonomy.board import Board
from autonomy.orchestrator import coordinator_tick
from autonomy.run import Run

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "work_drain_stop.py"


def test_cap_ends_run_on_tick_over_limit(tmp_path):
    Run(tmp_path).start("r1", ["w"], guardrails={"max_iterations": 3})
    b = Board(tmp_path)
    b.create_task("T1", "x", owner="w")  # keeps the run non-stuck/non-converged
    # ticks 1..3 run normally
    for i in range(3):
        rep = coordinator_tick(tmp_path, {"w": []})
        assert rep.halt is False, f"tick {i+1} should not halt"
    # tick 4 exceeds the cap → halt + run ended
    rep = coordinator_tick(tmp_path, {"w": []})
    assert rep.halt is True
    assert "max_iterations" in rep.note
    r = Run(tmp_path)
    assert r.active() is False
    assert "max_iterations" in r.status()["end_reason"]
    assert "max_iterations_reached" in [e["type"] for e in b.events()]


def test_ended_run_lets_work_drain_stop(tmp_path):
    # After the cap ends the run, a participant with an open task may stop.
    Run(tmp_path).start("r1", ["w"], guardrails={"max_iterations": 1})
    b = Board(tmp_path)
    b.create_task("T1", "x", owner="w")
    coordinator_tick(tmp_path, {"w": []})   # tick 1 ok
    coordinator_tick(tmp_path, {"w": []})   # tick 2 > cap → run ended
    assert Run(tmp_path).active() is False
    env = {"PATH": "/usr/bin:/bin", "LOOM_ROOT": str(tmp_path), "MESH_AGENT": "w"}
    p = subprocess.run([sys.executable, str(HOOK)], input="{}",
                       capture_output=True, text=True, env=env, timeout=30)
    assert '"decision": "block"' not in p.stdout   # inactive run → allow stop


def test_no_cap_increments_iterations(tmp_path):
    Run(tmp_path).start("r1", ["w"])   # no max_iterations
    b = Board(tmp_path)
    b.create_task("T1", "x", owner="w")
    for _ in range(5):
        rep = coordinator_tick(tmp_path, {"w": []})
        assert rep.halt is False
    assert Run(tmp_path).status()["iterations"] == 5   # visible in run status


def _bump_many(root, n):
    r = Run(root)
    for _ in range(n):
        r.bump_iteration()
    return n


def test_bump_iteration_concurrent_no_lost_update(tmp_path):
    Run(tmp_path).start("r1", ["w"])   # iterations init 0
    with mp.Pool(2) as pool:
        pool.starmap(_bump_many, [(str(tmp_path), 50), (str(tmp_path), 50)])
    assert Run(tmp_path).status()["iterations"] == 100  # 2 × 50, no lost update


def test_cli_rejects_non_positive_max_iter(tmp_path, monkeypatch):
    # R4: 0 would silently disable the cap, a negative value ends on tick 1.
    import pytest
    from autonomy.cli import main
    monkeypatch.setenv("LOOM_ROOT", str(tmp_path))
    monkeypatch.delenv("TMUX", raising=False)
    for bad in ("0", "-3"):
        with pytest.raises(SystemExit):
            main(["run", "start", "r1", "--participants", "w", "--max-iter", bad])
    assert Run(tmp_path).active() is False   # never started
