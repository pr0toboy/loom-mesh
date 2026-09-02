"""F4 — wedged detection: dep faults, cycles, stalls, terminal/convergence."""
from datetime import datetime, timezone

from autonomy.board import Board, dependency_faults
from autonomy.coordinator import next_actions, situation
from autonomy.orchestrator import coordinator_tick
from autonomy.run import Run


# --- dependency faults ------------------------------------------------------

def test_missing_dep_is_wedged(tmp_path):
    b = Board(tmp_path)
    b.create_task("T1", "x", deps=["GHOST"])
    faults = dependency_faults(b.tasks())
    assert faults == {"T1": "missing:GHOST"}


def test_abandoned_dep_makes_dependent_wedged(tmp_path):
    b = Board(tmp_path)
    b.create_task("DEP", "d", owner="w")
    b.create_task("T1", "x", deps=["DEP"])
    b.abandon("DEP", by="c")
    faults = dependency_faults(b.tasks())
    assert faults["T1"] == "abandoned:DEP"
    # and it is never "ready"
    assert [t["id"] for t in b.ready_unassigned()] == []


def test_cycle_marks_both_nodes(tmp_path):
    b = Board(tmp_path)
    b.create_task("T1", "x", deps=["T2"])
    b.create_task("T2", "y", deps=["T1"])
    faults = dependency_faults(b.tasks())
    assert set(faults) == {"T1", "T2"}
    assert all(v.startswith("cycle:") for v in faults.values())
    # breaking the cycle makes T1 assignable
    b.set_deps("T1", [], by="c")
    assert "T1" not in dependency_faults(b.tasks())
    assert "T1" in {t["id"] for t in b.ready_unassigned()}


def test_terminal_tasks_are_not_wedged(tmp_path):
    b = Board(tmp_path)
    b.create_task("DEP", "d", owner="w")
    b.create_task("T1", "x", deps=["DEP"])
    b.abandon("T1", by="c")  # T1 terminal → not evaluated even though its dep is gone
    assert "T1" not in dependency_faults(b.tasks())


# --- convergence with failures ----------------------------------------------

def test_converged_with_abandoned(tmp_path):
    Run(tmp_path).start("r1", ["w"])
    b = Board(tmp_path)
    b.create_task("T1", "x", owner="w")
    b.create_task("T2", "y", owner="w")
    b.submit("T1", "w", branch="b")
    b.review("T1", "carol", passed=True)   # done + Fable-reviewed
    b.abandon("T2", by="c", reason="dead end")
    assert b.converged() and b.failures() == ["T2"]
    rep = coordinator_tick(tmp_path, {"w": []})
    assert rep.done is True
    assert "abandoned" in rep.note and "T2" in rep.note
    # the gate never demands a review on an abandoned task
    assert rep.gate_violations == []


# --- stall remediation ------------------------------------------------------

def _force_stall(guardrails=None):
    g = {"stall_after_seconds": -1}  # any positive age counts as a stall
    if guardrails:
        g.update(guardrails)
    return g


def test_stalled_task_is_reassigned_to_other_agent(tmp_path):
    # R2: the dead owner (now at load 0) must NOT be handed its own task back —
    # the re-pool excludes it, so an idle peer picks it up.
    Run(tmp_path).start("r1", ["worker-a", "worker-b"], guardrails=_force_stall())
    b = Board(tmp_path)
    b.create_task("T1", "x", owner="worker-a")  # owned, silent → stalled
    rep = coordinator_tick(tmp_path, {"worker-a": [], "worker-b": []})
    assert "task_unassigned" in [e["type"] for e in b.events()]
    t = b.tasks()["T1"]
    assert t["reassignments"] == 1
    assert t["owner"] == "worker-b"          # reassigned away from the dead owner
    assert t["status"] == "assigned"


def test_stall_reassign_falls_back_to_only_agent(tmp_path):
    # With a single participant, the exclusion is dropped rather than stranding work.
    Run(tmp_path).start("r1", ["worker-a"], guardrails=_force_stall())
    b = Board(tmp_path)
    b.create_task("T1", "x", owner="worker-a")
    coordinator_tick(tmp_path, {"worker-a": []})
    assert b.tasks()["T1"]["owner"] == "worker-a"   # only option → keeps it


def test_stall_blocks_after_max_reassignments(tmp_path):
    Run(tmp_path).start("r1", ["worker-a"],
                        guardrails=_force_stall({"max_reassignments": 2}))
    b = Board(tmp_path)
    # drive reassignments up to the cap by hand, then leave it owned+stalled
    b.create_task("T1", "x", owner="worker-a")
    b.unassign("T1", by="c"); b.assign("T1", "worker-a")   # reassignments -> 1
    b.unassign("T1", by="c"); b.assign("T1", "worker-a")   # reassignments -> 2
    assert b.tasks()["T1"]["reassignments"] == 2
    rep = coordinator_tick(tmp_path, {"worker-a": []})
    # at the cap → block instead of a 3rd reassignment
    assert b.tasks()["T1"]["status"] == "blocked"
    assert b.tasks()["T1"]["reassignments"] == 2


# --- stuck run --------------------------------------------------------------

def test_stuck_run_halts_after_cap(tmp_path):
    Run(tmp_path).start("r1", ["w"], guardrails={"stuck_ticks_halt": 3})
    b = Board(tmp_path)
    b.create_task("T1", "x", deps=["GHOST"])  # unsatisfiable → wedged, nothing to do
    # ticks 1 and 2: stuck flagged but not halted yet
    for _ in range(2):
        rep = coordinator_tick(tmp_path, {"w": []})
        assert rep.halt is False
        assert "T1" in rep.triage
    # tick 3: halt + run_wedged, but the run stays active (human/coordinator call)
    rep = coordinator_tick(tmp_path, {"w": []})
    assert rep.halt is True
    assert "run_wedged" in [e["type"] for e in b.events()]
    assert Run(tmp_path).active() is True
    # tick 4: still stuck, still halts — but run_wedged is NOT re-logged (R1)
    coordinator_tick(tmp_path, {"w": []})
    assert sum(1 for e in b.events() if e["type"] == "run_wedged") == 1


def test_stuck_counter_resets_when_unblocked(tmp_path):
    # Discriminating: a non-stuck tick between stuck runs must RESET the counter,
    # so a fresh stuck run needs the full stuck_ticks_halt again. With a no-op
    # reset this test halts early and fails.
    Run(tmp_path).start("r1", ["w"], guardrails={"stuck_ticks_halt": 3})
    b = Board(tmp_path)
    b.create_task("T1", "x", deps=["GHOST"])    # wedged
    b.create_task("T2", "y", deps=["GHOST2"])   # wedged
    # two stuck ticks (count 1, 2) — no halt yet
    for _ in range(2):
        assert coordinator_tick(tmp_path, {"w": []}).halt is False
    # unblock T1 → assignable → this tick is NOT stuck → counter resets to 0
    b.set_deps("T1", [], by="c")
    rep = coordinator_tick(tmp_path, {"w": []})
    assert rep.halt is False and any(a["task"] == "T1" for a in rep.assigned)
    # push T1 out of the open set (done) so the run is stuck again on T2 alone
    b.submit("T1", "w", branch="b")
    b.review("T1", "carol", passed=True)
    # two FRESH stuck ticks must not halt — they would (count 3) if the reset were a no-op
    for _ in range(2):
        assert coordinator_tick(tmp_path, {"w": []}).halt is False
    # the third fresh stuck tick halts
    rep = coordinator_tick(tmp_path, {"w": []})
    assert rep.halt is True
    assert "run_wedged" in [e["type"] for e in b.events()]


# --- determinism ------------------------------------------------------------

def test_fold_deterministic_with_unassign_and_abandon(tmp_path):
    b = Board(tmp_path)
    b.create_task("T1", "x", owner="w")
    b.unassign("T1", by="c")
    b.assign("T1", "w")
    b.unassign("T1", by="c")
    b.create_task("T2", "y", owner="w")
    b.abandon("T2", by="c")
    snap1 = {tid: (t["status"], t["owner"], t["reassignments"]) for tid, t in Board(tmp_path).tasks().items()}
    snap2 = {tid: (t["status"], t["owner"], t["reassignments"]) for tid, t in Board(tmp_path).tasks().items()}
    assert snap1 == snap2
    assert snap1["T1"] == ("todo", None, 2)
    assert snap1["T2"][0] == "abandoned"


def test_situation_reports_wedged_and_stalled(tmp_path):
    r = Run(tmp_path)
    r.start("r1", ["w"])
    b = Board(tmp_path)
    b.create_task("T1", "x", deps=["GHOST"])
    b.create_task("T2", "y", owner="w")
    # force T2 stale via an explicit past clock through the stall threshold
    sit = situation(b, r, stall_after_seconds=-1,
                    now=datetime.now(timezone.utc))
    assert sit.wedged.get("T1") == "missing:GHOST"
    assert any(s["id"] == "T2" for s in sit.stalled)
    acts = next_actions(sit)
    assert "T1" in acts.triage
