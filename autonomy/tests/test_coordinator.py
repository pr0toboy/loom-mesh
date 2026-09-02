"""Tests for the coordinator toolkit (assignment, situation, next-actions)."""
from autonomy.board import Board
from autonomy.coordinator import (
    Situation, assign_ready, is_converged, next_actions, situation,
)
from autonomy.run import Run
from autonomy.usage_guard import UsageVerdict


def test_assign_ready_respects_scope(tmp_path):
    b = Board(tmp_path)
    b.create_task("T1", "api", scope="py")
    b.create_task("T2", "ui", scope="frontend")
    made = assign_ready(b, {"worker-a": ["py", "infra"], "worker-b": ["frontend"]})
    owners = {m["task"]: m["owner"] for m in made}
    assert owners == {"T1": "worker-a", "T2": "worker-b"}


def test_assign_ready_balances_load(tmp_path):
    b = Board(tmp_path)
    # worker-a already loaded with one open py task
    b.create_task("T0", "existing", scope="py", owner="worker-a")
    b.create_task("T1", "more py", scope="py")
    made = assign_ready(b, {"worker-a": ["py"], "worker-b": ["py"]})
    # the new one should go to the idle worker-b, not pile onto worker-a
    assert made == [{"task": "T1", "owner": "worker-b"}]


def test_assign_ready_unscoped_goes_to_anyone(tmp_path):
    b = Board(tmp_path)
    b.create_task("T1", "chore")  # no scope
    made = assign_ready(b, {"worker-a": ["py"]})
    assert made == [{"task": "T1", "owner": "worker-a"}]


def test_assign_ready_skips_blocked_deps(tmp_path):
    b = Board(tmp_path)
    b.create_task("T1", "first")
    b.create_task("T2", "after", deps=["T1"])
    made = assign_ready(b, {"worker-a": []})
    assert [m["task"] for m in made] == ["T1"]  # T2's dep not met yet


def test_situation_and_convergence(tmp_path):
    b = Board(tmp_path)
    r = Run(tmp_path)
    r.start("r1", ["worker-a"], goal="ship")
    b.create_task("T1", "x", owner="worker-a")
    b.create_task("T2", "y", owner="worker-a")
    b.submit("T2", "worker-a", branch="b")          # in_review
    b.block("T1", "worker-a", "needs key")          # blocked
    sit = situation(b, r)
    assert sit.goal == "ship"
    assert sit.converged is False
    assert sit.needs_review == ["T2"]
    assert sit.blocked == [{"id": "T1", "reason": "needs key"}]
    assert not is_converged(b)


def test_next_actions_halts_on_usage_kill(tmp_path):
    sit = Situation(goal="g", active=True, converged=False, counts={}, open_by_agent={},
                    needs_review=[], blocked=[], ready_unassigned=[],
                    usage_level="kill", usage_reason="window usage 85%")
    acts = next_actions(sit)
    assert acts.halt is True and "kill" in acts.note


def test_next_actions_done_on_convergence(tmp_path):
    sit = Situation(goal="g", active=True, converged=True, counts={"done": 3},
                    open_by_agent={}, needs_review=[], blocked=[], ready_unassigned=[],
                    usage_level="ok")
    assert next_actions(sit).done is True


def test_next_actions_routine_tick(tmp_path):
    sit = Situation(goal="g", active=True, converged=False, counts={"in_progress": 2},
                    open_by_agent={"worker-a": ["T1", "T2"]}, needs_review=["T3"],
                    blocked=[{"id": "T4", "reason": "x"}], ready_unassigned=["T5"],
                    usage_level="ok")
    acts = next_actions(sit)
    assert acts.review == ["T3"]
    assert acts.triage == ["T4"]
    assert acts.assign == ["T5"]
    assert not acts.halt and not acts.done


def test_situation_carries_usage_verdict(tmp_path):
    b = Board(tmp_path)
    r = Run(tmp_path)
    r.start("r1", ["worker-a"])
    b.create_task("T1", "x", owner="worker-a")
    v = UsageVerdict("warn", "window usage 65%", 0.65, 0.7, {})
    sit = situation(b, r, usage_verdict=v)
    assert sit.usage_level == "warn" and "65%" in sit.usage_reason
