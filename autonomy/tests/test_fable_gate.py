"""Tests for the Fable review gate (policy validated 2026-06-09)."""
from autonomy.board import Board
from autonomy.fable_gate import (
    DEFAULT_FABLE_AGENTS,
    gate_violations,
    is_fable,
    review_routing,
    reviewed_by_fable,
    route_reviewer,
)


def test_is_fable_default_roster():
    assert is_fable("carol")
    assert is_fable("erin")
    assert not is_fable("alice")
    assert not is_fable("dave")


def test_route_reviewer_scope_routing():
    # game/PMD scope -> Erin, everything else -> Carol
    assert route_reviewer({"id": "T1", "scope": "pmd"}) == "erin"
    assert route_reviewer({"id": "T2", "scope": "game"}) == "erin"
    assert route_reviewer({"id": "T3", "scope": "infra"}) == "carol"
    assert route_reviewer({"id": "T4", "scope": None}) == "carol"


def test_route_reviewer_prefers_idle():
    # general task would prefer carol, but if only erin is idle, balance to it
    assert route_reviewer({"id": "T", "scope": "infra"}, idle={"erin"}) == "erin"
    # scope preference kept when the preferred reviewer is among the idle ones
    assert route_reviewer({"id": "T", "scope": "pmd"}, idle={"carol", "erin"}) == "erin"
    # nobody idle -> falls back to the scope preference (queues)
    assert route_reviewer({"id": "T", "scope": "infra"}, idle=set()) == "carol"


def test_route_reviewer_no_fable_available():
    assert route_reviewer({"id": "T", "scope": "infra"}, fable_agents=()) is None


def test_reviewed_by_fable_scans_history():
    b = Board.__new__(Board)  # not needed; build a task dict directly
    task_ok = {"history": [{"type": "review_passed", "by": "carol"}]}
    task_nonfable = {"history": [{"type": "review_passed", "by": "lead"}]}
    task_selfdone = {"history": [{"type": "task_done", "by": "worker-a"}]}
    assert reviewed_by_fable(task_ok)
    assert not reviewed_by_fable(task_nonfable)
    assert not reviewed_by_fable(task_selfdone)


def test_review_routing_over_board(tmp_path):
    b = Board(tmp_path)
    b.create_task("G1", "office zone", scope="pmd", owner="w")
    b.create_task("I1", "infra fix", scope="infra", owner="w")
    b.submit("G1", "w", branch="b1")
    b.submit("I1", "w", branch="b2")
    routing = review_routing(b)
    assert routing == {"G1": "erin", "I1": "carol"}


def test_gate_violations_flags_non_fable_done(tmp_path):
    b = Board(tmp_path)
    # T1 done properly via a Fable review
    b.create_task("T1", "a", owner="w")
    b.submit("T1", "w", branch="b")
    b.review("T1", "erin", passed=True)
    # T2 marked done directly by the worker (no Fable review) -> violation
    b.create_task("T2", "b", owner="w")
    b.submit("T2", "w", branch="b")
    b.done("T2", by="w")
    # T3 reviewed by a non-Fable -> violation
    b.create_task("T3", "c", owner="w")
    b.submit("T3", "w", branch="b")
    b.review("T3", "alice", passed=True)
    assert gate_violations(b) == ["T2", "T3"]


def test_gate_violations_empty_when_clean(tmp_path):
    b = Board(tmp_path)
    b.create_task("T1", "a", owner="w")
    b.submit("T1", "w", branch="b")
    b.review("T1", "carol", passed=True)
    assert gate_violations(b) == []


def test_default_roster_is_the_two_fable_agents():
    assert set(DEFAULT_FABLE_AGENTS) == {"carol", "erin"}
