"""Tests for the orchestrator / coordinator loop driver."""
import json
from datetime import datetime, timezone

from autonomy.board import Board
from autonomy.orchestrator import coordinator_tick, limits_from_run
from autonomy.run import Run
from autonomy.usage_guard import UsageLimits


def test_tick_halts_when_run_inactive(tmp_path):
    rep = coordinator_tick(tmp_path, {"worker-a": ["py"]})
    assert rep.halt is True and "not active" in rep.note


def test_tick_assigns_and_lists_kicks(tmp_path):
    Run(tmp_path).start("r1", ["worker-a", "worker-b"])
    b = Board(tmp_path)
    b.create_task("T1", "api", scope="py")
    b.create_task("T2", "ui", scope="frontend")
    rep = coordinator_tick(tmp_path, {"worker-a": ["py"], "worker-b": ["frontend"]})
    assert not rep.halt and not rep.done
    owners = {a["task"]: a["owner"] for a in rep.assigned}
    assert owners == {"T1": "worker-a", "T2": "worker-b"}
    # both now own open work → both get kicked
    assert rep.kick == ["worker-a", "worker-b"]


def test_tick_surfaces_review_and_triage(tmp_path):
    Run(tmp_path).start("r1", ["worker-a"])
    b = Board(tmp_path)
    b.create_task("T1", "x", owner="worker-a")
    b.create_task("T2", "y", owner="worker-a")
    b.submit("T1", "worker-a", branch="b")   # in_review
    b.block("T2", "worker-a", "needs key")   # blocked
    rep = coordinator_tick(tmp_path, {"worker-a": []})
    assert rep.review == ["T1"]
    assert rep.triage == ["T2"]


def test_tick_done_on_convergence(tmp_path):
    Run(tmp_path).start("r1", ["worker-a"])
    b = Board(tmp_path)
    b.create_task("T1", "x", owner="worker-a")
    b.submit("T1", "worker-a", branch="b")
    # The Fable gate: convergence requires the final review to be passed by a
    # Fable agent (carol/erin), not just any reviewer.
    b.review("T1", "carol", passed=True)
    rep = coordinator_tick(tmp_path, {"worker-a": []})
    assert rep.done is True
    assert rep.gate_violations == []


def test_tick_gate_holds_done_without_fable_review(tmp_path):
    # A task marked done via a non-Fable review bypasses the gate → convergence
    # is held back and the violating task is surfaced for re-review.
    Run(tmp_path).start("r1", ["worker-a"])
    b = Board(tmp_path)
    b.create_task("T1", "x", owner="worker-a")
    b.submit("T1", "worker-a", branch="b")
    b.review("T1", "lead", passed=True)   # 'lead' is not a Fable agent
    rep = coordinator_tick(tmp_path, {"worker-a": []})
    assert rep.done is False
    assert rep.gate_violations == ["T1"]


def test_tick_halts_on_usage_kill(tmp_path):
    Run(tmp_path).start("r1", ["worker-a"], guardrails={"window_token_cap": 1000, "kill_fraction": 0.8})
    b = Board(tmp_path)
    b.create_task("T1", "x", owner="worker-a")
    usage = tmp_path / "usage"
    usage.mkdir()
    (usage / "host.json").write_text(json.dumps({
        "source": "host", "window_start": "W1", "window_end": "W1e",
        "remaining_minutes": 100.0, "limit_tokens": 950, "total_tokens": 950,
        "output_tokens": 0, "burn_tpm": 0.0,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }))
    rep = coordinator_tick(tmp_path, {"worker-a": ["py"]}, usage_dir=usage)
    assert rep.halt is True and rep.usage_level == "kill"
    # the kill is recorded for the making-of
    assert "usage_kill" in [e["type"] for e in b.events()]


def test_limits_from_run_uses_guardrails(tmp_path):
    r = Run(tmp_path)
    r.start("r1", ["worker-a"], guardrails={"window_token_cap": 6_700_000, "kill_fraction": 0.75})
    lim = limits_from_run(r)
    assert lim.window_token_cap == 6_700_000
    assert lim.kill_fraction == 0.75
    # unset field falls back to the calibrated default
    assert lim.warn_fraction == UsageLimits.warn_fraction
