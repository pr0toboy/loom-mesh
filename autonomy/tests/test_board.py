"""Tests for the event-sourced task board."""
from autonomy.board import Board


def test_create_folds_to_todo_or_assigned(tmp_path):
    b = Board(tmp_path)
    b.create_task("T1", "build X")
    b.create_task("T2", "build Y", owner="worker-a")
    tasks = b.tasks()
    assert tasks["T1"]["status"] == "todo"
    assert tasks["T1"]["owner"] is None
    assert tasks["T2"]["status"] == "assigned"
    assert tasks["T2"]["owner"] == "worker-a"


def test_full_lifecycle_to_done(tmp_path):
    b = Board(tmp_path)
    b.create_task("T1", "build X")
    b.assign("T1", "worker-a", by="lead")
    b.claim("T1", "worker-a")
    b.progress("T1", "worker-a", note="scaffolded")
    b.submit("T1", "worker-a", branch="feat/x", artifacts=["src/x.py"])
    t = b.tasks()["T1"]
    assert t["status"] == "in_review"
    assert t["branch"] == "feat/x"
    assert t["artifacts"] == ["src/x.py"]
    assert t["attempts"] == 1
    b.review("T1", "lead", passed=True)
    assert b.tasks()["T1"]["status"] == "done"
    assert b.all_done()


def test_review_failed_returns_to_in_progress(tmp_path):
    b = Board(tmp_path)
    b.create_task("T1", "build X", owner="worker-a")
    b.submit("T1", "worker-a", branch="feat/x")
    b.review("T1", "lead", passed=False, notes="tests red")
    t = b.tasks()["T1"]
    assert t["status"] == "in_progress"
    assert t["review_notes"] == "tests red"
    assert not b.all_done()


def test_block_and_unblock(tmp_path):
    b = Board(tmp_path)
    b.create_task("T1", "build X", owner="worker-a")
    b.block("T1", "worker-a", reason="needs API key")
    assert b.tasks()["T1"]["status"] == "blocked"
    assert b.blocked()[0]["id"] == "T1"
    b.unblock("T1", by="lead")
    assert b.tasks()["T1"]["status"] == "in_progress"


def test_open_for_respects_deps(tmp_path):
    b = Board(tmp_path)
    b.create_task("T1", "schema")
    b.create_task("T2", "api on schema", owner="worker-b", deps=["T1"])
    # T2 is owned but blocked by an undone dependency → not open yet
    assert b.open_for("worker-b") == []
    b.assign("T1", "worker-a")
    b.submit("T1", "worker-a", branch="feat/schema")
    b.review("T1", "lead", passed=True)
    # now T1 is done → T2 becomes workable
    opn = b.open_for("worker-b")
    assert [t["id"] for t in opn] == ["T2"]


def test_ready_unassigned_filters_owned_and_blocked_deps(tmp_path):
    b = Board(tmp_path)
    b.create_task("T1", "free")
    b.create_task("T2", "owned", owner="worker-a")
    b.create_task("T3", "needs T1", deps=["T1"])
    ready = {t["id"] for t in b.ready_unassigned()}
    assert ready == {"T1"}  # T2 owned, T3 dep not met


def test_persistence_across_instances(tmp_path):
    Board(tmp_path).create_task("T1", "x", owner="worker-a")
    # a fresh Board over the same root sees the same folded state
    assert Board(tmp_path).tasks()["T1"]["owner"] == "worker-a"


def test_free_log_events_are_recorded(tmp_path):
    b = Board(tmp_path)
    b.log("skill_created", by="worker-a", name="/deploy")
    b.log("commit", by="worker-a", sha="abc123", message="init")
    kinds = [e["type"] for e in b.events()]
    assert kinds == ["skill_created", "commit"]


def test_create_task_rejects_replay_on_existing_id(tmp_path):
    # Replaying a setup script must not resurrect a done task or wipe its history.
    b = Board(tmp_path)
    b.create_task("T1", "build X", owner="worker-a")
    b.assign("T1", "worker-a", by="lead")
    b.claim("T1", "worker-a")
    b.submit("T1", "worker-a", branch="feat/x")
    b.review("T1", "carol", passed=True)
    assert b.tasks()["T1"]["status"] == "done"

    import pytest
    with pytest.raises(ValueError):
        b.create_task("T1", "build X again", owner="worker-a")

    # state is untouched: still done, history intact
    assert b.tasks()["T1"]["status"] == "done"
    assert len(b.tasks()["T1"]["history"]) > 0


def test_tasks_fold_ignores_duplicate_task_created_event(tmp_path):
    # Even a raw duplicate event appended directly to the log (bypassing
    # create_task's guard) must not reset the folded state.
    b = Board(tmp_path)
    b.create_task("T1", "build X", owner="worker-a")
    b.submit("T1", "worker-a", branch="feat/x")
    b.append({"type": "task_created", "task": "T1", "title": "resurrected", "owner": None})
    assert b.tasks()["T1"]["status"] == "in_review"
    assert b.tasks()["T1"]["title"] == "build X"
