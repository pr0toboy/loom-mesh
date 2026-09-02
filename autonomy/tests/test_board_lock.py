"""F3 — the board lock: concurrent check-then-append must not race.

These spawn real OS processes (not threads) so the ``flock`` is actually
contended across process boundaries, the way two coordinators or two agents on
one host would contend.
"""
import multiprocessing as mp

import pytest

from autonomy.board import Board
from autonomy.coordinator import assign_ready


def _try_create(root, tid):
    try:
        Board(root).create_task(tid, "x")
        return "ok"
    except ValueError:
        return "dup"
    except Exception as e:  # pragma: no cover - surfaced only on a real bug
        return f"err:{e!r}"


def _try_assign_free(root, tid, owner):
    ev = Board(root).assign_if_free(tid, owner, by="coordinator")
    return owner if ev is not None else None


def _try_claim(root, tid, agent):
    ev = Board(root).claim_if_available(tid, agent)
    return agent if ev is not None else None


_DRIVE_SCOPES = {"worker-a": [], "worker-b": []}


def _drive_assign_ready(root):
    return assign_ready(Board(root), _DRIVE_SCOPES)


def test_concurrent_create_one_wins_one_valueerror(tmp_path):
    Board(tmp_path)._ensure()
    with mp.Pool(2) as pool:
        results = pool.starmap(_try_create, [(str(tmp_path), "T1"), (str(tmp_path), "T1")])
    assert sorted(results) == ["dup", "ok"]  # exactly one success, one rejected
    # and exactly one task_created landed in the log
    events = Board(tmp_path).events()
    assert sum(1 for e in events if e.get("type") == "task_created" and e.get("task") == "T1") == 1


def test_concurrent_assign_free_single_owner(tmp_path):
    b = Board(tmp_path)
    b.create_task("T1", "x")
    with mp.Pool(2) as pool:
        results = pool.starmap(_try_assign_free,
                               [(str(tmp_path), "T1", "worker-a"), (str(tmp_path), "T1", "worker-b")])
    winners = [r for r in results if r is not None]
    assert len(winners) == 1  # exactly one process assigned it
    assigns = [e for e in b.events() if e.get("type") == "task_assigned" and e.get("task") == "T1"]
    assert len(assigns) == 1


def test_concurrent_claim_single_owner(tmp_path):
    b = Board(tmp_path)
    b.create_task("T1", "x")
    with mp.Pool(2) as pool:
        results = pool.starmap(_try_claim,
                               [(str(tmp_path), "T1", "worker-a"), (str(tmp_path), "T1", "worker-b")])
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert b.tasks()["T1"]["owner"] == winners[0]
    assert b.tasks()["T1"]["status"] == "in_progress"


def test_assign_ready_concurrent_no_double_assignment(tmp_path):
    """Two coordinators fire assign_ready at once over the same ready pool → each
    task gets exactly one effective task_assigned event."""
    b = Board(tmp_path)
    for i in range(6):
        b.create_task(f"T{i}", "x")  # unscoped, all ready

    with mp.Pool(2) as pool:
        pool.map(_drive_assign_ready, [str(tmp_path), str(tmp_path)])

    assigns = {}
    for e in b.events():
        if e.get("type") == "task_assigned":
            assigns[e["task"]] = assigns.get(e["task"], 0) + 1
    assert set(assigns) == {f"T{i}" for i in range(6)}
    assert all(n == 1 for n in assigns.values())  # never assigned twice


def test_assign_if_free_noop_when_taken(tmp_path):
    b = Board(tmp_path)
    b.create_task("T1", "x")
    assert b.assign_if_free("T1", "worker-a") is not None
    assert b.assign_if_free("T1", "worker-b") is None  # already owned → no-op
    assert b.tasks()["T1"]["owner"] == "worker-a"


def test_reentrant_lock_no_deadlock(tmp_path):
    """append() taken from inside a locked() block must not self-deadlock, and
    create_task (fold-under-lock then append) must complete."""
    b = Board(tmp_path)
    with b.locked():
        b.append({"type": "note", "text": "nested"})  # would deadlock without reentrancy
    b.create_task("T1", "x")  # folds under lock, then appends under the same lock
    kinds = [e["type"] for e in b.events()]
    assert "note" in kinds and "task_created" in kinds


def test_simple_appends_unchanged(tmp_path):
    b = Board(tmp_path)
    b.log("commit", by="w", sha="abc")
    b.log("note", by="w", text="hi")
    assert [e["type"] for e in b.events()] == ["commit", "note"]
