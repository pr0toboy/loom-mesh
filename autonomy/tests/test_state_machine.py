"""F1 — state machine: reopen / abandon / unassign / deps folding + gate ownership."""
import pytest

from autonomy.board import Board
from autonomy.fable_gate import gate_violations, reviewed_by_fable
from autonomy.orchestrator import coordinator_tick
from autonomy.run import Run


# --- fold transitions -------------------------------------------------------

def test_reopen_done_to_in_review(tmp_path):
    b = Board(tmp_path)
    b.create_task("T1", "x", owner="w")
    b.submit("T1", "w", branch="b")
    b.review("T1", "carol", passed=True)
    assert b.tasks()["T1"]["status"] == "done"
    ev = b.reopen("T1", by="coordinator", reason="gate")
    assert ev is not None
    t = b.tasks()["T1"]
    assert t["status"] == "in_review"
    assert t["owner"] == "w"        # owner kept
    assert t["branch"] == "b"       # branch kept


def test_reopen_to_in_progress(tmp_path):
    b = Board(tmp_path)
    b.create_task("T1", "x", owner="w")
    b.submit("T1", "w", branch="b")
    b.review("T1", "carol", passed=True)
    b.reopen("T1", by="c", to="in_progress")
    t = b.tasks()["T1"]
    assert t["status"] == "in_progress"
    # a reopened-to-in_progress task is once again open work for its owner (so the
    # work-drain hook will keep that agent going on it).
    assert [x["id"] for x in b.open_for("w")] == ["T1"]


def test_reopen_to_out_of_range_falls_back_to_in_review(tmp_path):
    # R3: a raw task_reopened with a bogus `to` must not fabricate an unknown
    # status — the fold clamps it to in_review.
    b = Board(tmp_path)
    b.create_task("T1", "x", owner="w")
    b.submit("T1", "w", branch="b")
    b.review("T1", "carol", passed=True)
    b.append({"type": "task_reopened", "task": "T1", "to": "garbage"})
    assert b.tasks()["T1"]["status"] == "in_review"


def test_reopen_on_non_done_is_ignored(tmp_path):
    b = Board(tmp_path)
    b.create_task("T1", "x", owner="w")  # assigned, not done
    assert b.reopen("T1", by="c") is None            # guarded mutation no-ops
    # even a raw event bypassing the guard is ignored by the fold
    b.append({"type": "task_reopened", "task": "T1", "to": "in_review"})
    assert b.tasks()["T1"]["status"] == "assigned"


def test_unassign_returns_to_pool_and_counts(tmp_path):
    b = Board(tmp_path)
    b.create_task("T1", "x", owner="w")
    b.claim("T1", "w")
    assert b.tasks()["T1"]["status"] == "in_progress"
    b.unassign("T1", by="coordinator", reason="stall")
    t = b.tasks()["T1"]
    assert t["status"] == "todo" and t["owner"] is None
    assert t["reassignments"] == 1
    # unassign on a todo (no owner) is a no-op
    assert b.unassign("T1", by="c") is None
    assert b.tasks()["T1"]["reassignments"] == 1


def test_abandon_is_terminal(tmp_path):
    b = Board(tmp_path)
    b.create_task("T1", "x", owner="w")
    b.abandon("T1", by="c", reason="dead")
    assert b.tasks()["T1"]["status"] == "abandoned"
    # abandon never overrides a done
    b.create_task("T2", "y", owner="w")
    b.submit("T2", "w", branch="b")
    b.review("T2", "carol", passed=True)
    assert b.abandon("T2", by="c") is None
    assert b.tasks()["T2"]["status"] == "done"


def test_deps_changed_replaces_deps(tmp_path):
    b = Board(tmp_path)
    b.create_task("T1", "x", deps=["A", "B"])
    b.set_deps("T1", ["C"], by="c")
    assert b.tasks()["T1"]["deps"] == ["C"]
    b.set_deps("T1", [], by="c")
    assert b.tasks()["T1"]["deps"] == []


# --- fold independence from the Fable roster + real env override ------------

def test_fold_is_independent_of_fable_roster(tmp_path):
    b = Board(tmp_path)
    b.create_task("T1", "x", owner="w")
    b.submit("T1", "w", branch="b")
    b.review("T1", "carol", passed=True)
    b.reopen("T1", by="c")
    b.review("T1", "erin", passed=True)
    # The fold is a pure replay: two reads yield the identical state, and tasks()
    # takes no roster argument — it cannot depend on one.
    s1 = {tid: (t["status"], t["owner"]) for tid, t in Board(tmp_path).tasks().items()}
    s2 = {tid: (t["status"], t["owner"]) for tid, t in Board(tmp_path).tasks().items()}
    assert s1 == s2
    # The roster only drives POLICY: same board, different fable_agents ARGUMENT →
    # different gate verdict. That is what proves the roster lives outside the fold.
    assert gate_violations(b, fable_agents=("carol", "erin")) == []   # erin passed
    assert gate_violations(b, fable_agents=("nobody",)) == ["T1"]         # no Fable pass counts


def test_fable_roster_honours_env_override(monkeypatch):
    # Spec F1: LOOM_FABLE_AGENTS overrides the default roster. It is read at import,
    # so exercise it with a real module reload rather than a no-op setenv.
    import importlib
    import autonomy.fable_gate as fg
    monkeypatch.setenv("LOOM_FABLE_AGENTS", "frank, zzz")
    fg2 = importlib.reload(fg)
    try:
        assert fg2.DEFAULT_FABLE_AGENTS == ("frank", "zzz")
        assert fg2.is_fable("frank") and not fg2.is_fable("carol")
    finally:
        monkeypatch.delenv("LOOM_FABLE_AGENTS", raising=False)
        importlib.reload(fg)   # restore the module global for the rest of the suite


# --- gate ownership: a reopen invalidates a prior Fable pass ----------------

def test_reviewed_by_fable_reset_by_reopen(tmp_path):
    b = Board(tmp_path)
    b.create_task("T1", "x", owner="w")
    b.submit("T1", "w", branch="b")
    b.review("T1", "carol", passed=True)     # fable pass
    b.reopen("T1", by="c")                     # invalidates it
    b.review("T1", "lead", passed=True)        # re-passed by a non-Fable → done
    t = b.tasks()["T1"]
    assert t["status"] == "done"
    assert not reviewed_by_fable(t)            # stale fable pass does not count
    assert gate_violations(b) == ["T1"]


# --- orchestrator: mechanical reopen + convergence release ------------------

def test_gate_reopens_and_routes_then_converges(tmp_path):
    Run(tmp_path).start("r1", ["w"])
    b = Board(tmp_path)
    b.create_task("T1", "x", owner="w")
    b.submit("T1", "w", branch="b")
    b.review("T1", "lead", passed=True)        # non-Fable → gate violation

    # tick 1: convergence held, task reopened to in_review and routed to a Fable
    rep = coordinator_tick(tmp_path, {"w": []})
    assert rep.done is False
    assert rep.gate_violations == ["T1"]
    assert b.tasks()["T1"]["status"] == "in_review"
    assert rep.review == ["T1"]
    assert rep.review_routing.get("T1") in ("carol", "erin")

    # tick 2 on the same still-violating history emits only ONE reopen total
    coordinator_tick(tmp_path, {"w": []})
    reopens = [e for e in b.events() if e.get("type") == "task_reopened" and e.get("task") == "T1"]
    assert len(reopens) == 1

    # a Fable now passes the re-review → done → next tick converges
    b.review("T1", "carol", passed=True)
    rep = coordinator_tick(tmp_path, {"w": []})
    assert rep.done is True
    assert rep.gate_violations == []
