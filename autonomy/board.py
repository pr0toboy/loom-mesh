#!/usr/bin/env python3
"""Shared task board — the work graph an autonomous run is built around.

Source of truth = an append-only event log at ``<root>/.loom/board.jsonl``.
The current state of every task is obtained by *folding* the events. This buys
three things at once:

1. **Nothing to corrupt.** Append-only; a crash mid-write loses at most one line.
2. **The log *is* the making-of.** Who did what, when, in order — the narrative
   generator just reads this file (see ``making_of.py``).
3. **Trivially serializable.** A read-only API or dashboard is a thin view over
   the same fold.

Stdlib only (portable across a Pi, a proot Termux distro, WSL). Concurrency is
handled with an advisory ``flock`` so several agents on the same host can append
safely. Cross-host runs serialize through git (each host commits its branch; the
coordinator integrates), not through this file.

Task lifecycle::

    todo ──assign──▶ assigned ──claim──▶ in_progress ──submit──▶ in_review ──▶ done
                                              ▲   │
                                       unblock│   │block
                                              │   ▼
                                            blocked

An agent keeps working while it owns a task in ``{assigned, in_progress}`` whose
dependencies are met (see :meth:`Board.open_for`). ``in_review`` and ``blocked``
hand control back to the coordinator, not the worker.
"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# Statuses a worker is expected to actively push on.
OPEN_STATUSES = {"assigned", "in_progress"}
# Terminal statuses — a run converges when every task is in one of these.
TERMINAL = {"done", "abandoned"}
# A *dependency* is only satisfied by a real `done`. An `abandoned` dependency
# does NOT satisfy anything — it makes its dependents unsatisfiable (wedged).
DEP_SATISFIED = {"done"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Board:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.dir = self.root / ".loom"
        self.events_path = self.dir / "board.jsonl"
        self.lock_path = self.dir / ".board.lock"
        self._lock_fd = None
        self._lock_depth = 0

    # ---- low-level ---------------------------------------------------------
    def _ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        if not self.events_path.exists():
            self.events_path.touch()

    @contextmanager
    def locked(self):
        """Hold the board's exclusive ``flock`` across a read-modify-write.

        Every mutation that *decides in function of a read* (check-then-append)
        must run inside this so the fold it sees and the append it emits cannot
        interleave with another writer — the same discipline as the mesh ack fix
        (flush *before* releasing the lock, never an implicit post-unlock flush).

        **Reentrant intra-process**: the OS ``flock`` is only taken at depth 0 and
        released at depth 0. Without this, an ``append()`` called from inside a
        ``locked()`` block would ``flock`` a *second* fd of the same process and
        deadlock (flock on two distinct fds of one process blocks)."""
        self._ensure()
        if self._lock_depth == 0:
            self._lock_fd = open(self.lock_path, "w")
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
        self._lock_depth += 1
        try:
            yield
        finally:
            self._lock_depth -= 1
            if self._lock_depth == 0:
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                finally:
                    self._lock_fd.close()
                    self._lock_fd = None

    def _append_unlocked(self, event: dict) -> dict:
        """Write one event line and fsync it. Caller must already hold ``locked()``."""
        self._ensure()
        event.setdefault("ts", now_iso())
        line = json.dumps(event, ensure_ascii=False)
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())  # on disk before the lock is released
        return event

    def append(self, event: dict) -> dict:
        """Append one event under an exclusive lock (atomic, multi-writer safe)."""
        with self.locked():
            return self._append_unlocked(event)

    def events(self) -> list[dict]:
        self._ensure()
        out: list[dict] = []
        with self.events_path.open(encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    out.append(json.loads(ln))
                except Exception:
                    continue  # a torn final line never breaks a read
        return out

    # ---- fold → current state ---------------------------------------------
    def tasks(self) -> dict[str, dict]:
        """Replay the event log into the current state of every task."""
        tasks: dict[str, dict] = {}
        for e in self.events():
            etype = e.get("type")
            tid = e.get("task")
            if etype == "task_created":
                if not tid or tid in tasks:
                    continue  # replaying a setup must not resurrect/overwrite an existing task
                tasks[tid] = {
                    "id": tid,
                    "title": e.get("title", ""),
                    "description": e.get("description", ""),
                    "scope": e.get("scope"),
                    "owner": e.get("owner"),
                    "deps": e.get("deps", []) or [],
                    "acceptance": e.get("acceptance", []) or [],
                    "status": "assigned" if e.get("owner") else "todo",
                    "branch": e.get("branch"),
                    "attempts": 0,
                    "reassignments": 0,
                    "artifacts": [],
                    "block_reason": None,
                    "review_notes": None,
                    "last_event_ts": e.get("ts"),
                    "history": [],
                }
                continue
            t = tasks.get(tid)
            if t is None:
                continue  # event about an unknown task → ignore
            t["history"].append(e)
            t["last_event_ts"] = e.get("ts") or t.get("last_event_ts")
            if etype == "task_assigned":
                t["owner"] = e.get("owner") or t["owner"]
                if t["status"] == "todo":
                    t["status"] = "assigned"
            elif etype == "task_claimed":
                t["owner"] = e.get("owner") or t["owner"]
                t["status"] = "in_progress"
            elif etype == "task_started":
                t["status"] = "in_progress"
            elif etype == "task_progress":
                t["attempts"] += 1
            elif etype == "task_blocked":
                t["status"] = "blocked"
                t["block_reason"] = e.get("reason")
            elif etype == "task_unblocked":
                t["status"] = "in_progress"
                t["block_reason"] = None
            elif etype == "task_submitted":
                t["status"] = "in_review"
                t["branch"] = e.get("branch") or t["branch"]
                if e.get("artifacts"):
                    t["artifacts"] += e["artifacts"]
            elif etype in ("review_passed", "task_done"):
                t["status"] = "done"
            elif etype == "review_failed":
                t["status"] = "in_progress"
                t["review_notes"] = e.get("notes")
            elif etype == "task_reopened":
                # Only a *done* task can be reopened; a reopen on anything else is
                # a stale/late event and is ignored (owner and branch are kept).
                # `to` must be a real reopen target — an out-of-range value would
                # fabricate an unknown status, so it falls back to in_review.
                if t["status"] == "done":
                    to = e.get("to")
                    t["status"] = to if to in ("in_review", "in_progress") else "in_review"
            elif etype == "task_unassigned":
                if t["status"] in ("assigned", "in_progress"):
                    t["status"] = "todo"
                    t["owner"] = None
                    t["reassignments"] += 1
            elif etype == "task_abandoned":
                # Terminal, but never overrides a genuine done.
                if t["status"] != "done":
                    t["status"] = "abandoned"
            elif etype == "task_deps_changed":
                t["deps"] = e.get("deps", []) or []  # status unchanged
        return tasks

    # ---- queries -----------------------------------------------------------
    def deps_met(self, task: dict, tasks: dict[str, dict]) -> bool:
        # Only a real `done` satisfies a dependency; a missing or abandoned dep
        # leaves the dependent unsatisfiable (surfaced as `wedged`, not "ready").
        return all(tasks.get(d, {}).get("status") in DEP_SATISFIED for d in task.get("deps", []))

    def open_for(self, agent: str) -> list[dict]:
        """Tasks ``agent`` should be (re)working on right now."""
        tasks = self.tasks()
        return [
            t for t in tasks.values()
            if t.get("owner") == agent
            and t["status"] in OPEN_STATUSES
            and self.deps_met(t, tasks)
        ]

    def ready_unassigned(self) -> list[dict]:
        """Unowned ``todo`` tasks whose dependencies are already satisfied."""
        tasks = self.tasks()
        return [
            t for t in tasks.values()
            if t.get("owner") is None and t["status"] == "todo"
            and self.deps_met(t, tasks)
        ]

    def in_review(self) -> list[dict]:
        return [t for t in self.tasks().values() if t["status"] == "in_review"]

    def blocked(self) -> list[dict]:
        return [t for t in self.tasks().values() if t["status"] == "blocked"]

    def converged(self) -> bool:
        """True when every task has reached a terminal state (``done`` or
        ``abandoned``). All ``done`` = success; any ``abandoned`` = converged with
        failures (see :meth:`failures`)."""
        tasks = self.tasks()
        return bool(tasks) and all(t["status"] in TERMINAL for t in tasks.values())

    def failures(self) -> list[str]:
        """Ids of tasks that ended ``abandoned`` (sorted, deterministic)."""
        return sorted(t["id"] for t in self.tasks().values() if t["status"] == "abandoned")

    def all_done(self) -> bool:
        """Deprecated alias for :meth:`converged` — kept for existing callers."""
        return self.converged()

    # ---- mutations (thin wrappers over append) -----------------------------
    def create_task(self, tid, title, description="", scope=None, owner=None,
                    deps=None, acceptance=None, branch=None, by=None) -> dict:
        # The existence check and the append share one lock hold so two concurrent
        # create_task on the same id can't both pass the check and both write.
        with self.locked():
            if tid in self.tasks():
                raise ValueError(f"task {tid!r} already exists — replaying a setup must not "
                                  f"re-create it (would reset status/history)")
            return self._append_unlocked({
                "type": "task_created", "task": tid, "title": title,
                "description": description, "scope": scope, "owner": owner,
                "deps": deps or [], "acceptance": acceptance or [],
                "branch": branch, "by": by,
            })

    def assign(self, tid, owner, by=None):
        return self.append({"type": "task_assigned", "task": tid, "owner": owner, "by": by})

    def assign_if_free(self, tid, owner, by=None) -> dict | None:
        """Assign only if the task is still ``todo`` and unowned. Returns the event,
        or ``None`` if another writer already took it (checked under the lock)."""
        with self.locked():
            t = self.tasks().get(tid)
            if t is None or t["status"] != "todo" or t["owner"] is not None:
                return None
            return self._append_unlocked({"type": "task_assigned", "task": tid, "owner": owner, "by": by})

    def claim(self, tid, agent):
        return self.append({"type": "task_claimed", "task": tid, "owner": agent, "by": agent})

    def claim_if_available(self, tid, agent) -> dict | None:
        """Claim only if the task is still claimable (``todo``/``assigned`` and
        unowned or already ours). Returns the event, or ``None`` if it moved on."""
        with self.locked():
            t = self.tasks().get(tid)
            if t is None or t["status"] not in ("todo", "assigned") or t["owner"] not in (None, agent):
                return None
            return self._append_unlocked({"type": "task_claimed", "task": tid, "owner": agent, "by": agent})

    def start(self, tid, agent):
        return self.append({"type": "task_started", "task": tid, "by": agent})

    def progress(self, tid, agent, note=""):
        return self.append({"type": "task_progress", "task": tid, "by": agent, "note": note})

    def block(self, tid, agent, reason):
        return self.append({"type": "task_blocked", "task": tid, "by": agent, "reason": reason})

    def unblock(self, tid, by=None):
        return self.append({"type": "task_unblocked", "task": tid, "by": by})

    def submit(self, tid, agent, branch=None, artifacts=None, note=""):
        return self.append({"type": "task_submitted", "task": tid, "by": agent,
                            "branch": branch, "artifacts": artifacts or [], "note": note})

    def review(self, tid, reviewer, passed, notes=""):
        return self.append({
            "type": "review_passed" if passed else "review_failed",
            "task": tid, "by": reviewer, "notes": notes,
        })

    def done(self, tid, by=None):
        return self.append({"type": "task_done", "task": tid, "by": by})

    def reopen(self, tid, by=None, reason="", to="in_review") -> dict | None:
        """Reopen a ``done`` task (coordinator remediation of a gate violation).
        No-op returning ``None`` if the task is no longer ``done`` (pre-state moved)."""
        with self.locked():
            t = self.tasks().get(tid)
            if t is None or t["status"] != "done":
                return None
            return self._append_unlocked({"type": "task_reopened", "task": tid,
                                          "to": to, "reason": reason, "by": by})

    def abandon(self, tid, by=None, reason="") -> dict | None:
        """Mark a non-``done`` task terminal (``abandoned``). No-op on a task that
        is already ``done`` or ``abandoned``."""
        with self.locked():
            t = self.tasks().get(tid)
            if t is None or t["status"] in ("done", "abandoned"):
                return None
            return self._append_unlocked({"type": "task_abandoned", "task": tid,
                                          "reason": reason, "by": by})

    def unassign(self, tid, by=None, reason="") -> dict | None:
        """Return an owned open task to the pool (stall remediation). No-op unless
        the task is ``assigned``/``in_progress``."""
        with self.locked():
            t = self.tasks().get(tid)
            if t is None or t["status"] not in ("assigned", "in_progress"):
                return None
            return self._append_unlocked({"type": "task_unassigned", "task": tid,
                                          "reason": reason, "by": by})

    def set_deps(self, tid, deps, by=None):
        """Replace a task's dependency list (status unchanged)."""
        return self.append({"type": "task_deps_changed", "task": tid,
                            "deps": list(deps or []), "by": by})

    def log(self, kind, by=None, **fields):
        """Free-form event for the making-of: ``commit``, ``skill_created``,
        ``hook_created``, ``mcp_created``, ``note``, ``discussion``, ..."""
        ev = {"type": kind, "by": by}
        ev.update(fields)
        return self.append(ev)


# ---- dependency-fault detection (pure, deterministic) ----------------------

def dependency_faults(tasks: dict[str, dict]) -> dict[str, str]:
    """Non-terminal tasks whose dependencies can never be satisfied.

    Returns ``{task_id: fault}`` where fault is one of:
      - ``missing:<dep>``    — the dependency id is not on the board;
      - ``abandoned:<dep>``  — the dependency ended ``abandoned``;
      - ``cycle:t1→t2→…→t1`` — the task sits on a dependency cycle.

    Pure and deterministic (ids sorted), so the coordinator can surface a wedged
    run mechanically without ever consulting anything outside the fold."""
    faults: dict[str, str] = {}
    non_terminal = {tid: t for tid, t in tasks.items() if t["status"] not in TERMINAL}

    # missing / abandoned deps (first offending dep per task, deps in order)
    for tid, t in non_terminal.items():
        for d in t.get("deps", []):
            dep = tasks.get(d)
            if dep is None:
                faults[tid] = f"missing:{d}"
                break
            if dep["status"] == "abandoned":
                faults[tid] = f"abandoned:{d}"
                break

    # cycles among the non-terminal sub-graph (deps that exist and are non-terminal)
    graph = {
        tid: sorted(d for d in t.get("deps", []) if d in non_terminal)
        for tid, t in non_terminal.items()
    }
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}
    stack: list[str] = []

    def _dfs(n: str) -> None:
        color[n] = GRAY
        stack.append(n)
        for m in graph.get(n, []):
            if color.get(m) == GRAY:                      # back-edge → a cycle
                cyc = stack[stack.index(m):] + [m]
                label = "cycle:" + "→".join(cyc)
                for node in cyc[:-1]:
                    faults.setdefault(node, label)       # missing/abandoned wins
            elif color.get(m) == WHITE:
                _dfs(m)
        stack.pop()
        color[n] = BLACK

    for n in sorted(graph):
        if color[n] == WHITE:
            _dfs(n)
    return faults
