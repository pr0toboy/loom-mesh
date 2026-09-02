#!/usr/bin/env python3
"""Run lifecycle — the switch that decides when the autonomy engine is *active*.

A "run" is one autonomous project execution. Outside a run, the work-drain Stop
hook does nothing: the mesh's normal interactive life is untouched. This is also
the sandbox boundary — wiring the hook into an agent has zero effect until a run
is started and that agent is listed as a participant.

State lives in ``<root>/.loom/run.json``, deliberately separate from the board
so the hook can read it fast and fail-open. The run also carries the operator
guardrails (token cap, max iterations) so they travel with the run, not the code.
"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path

from .board import now_iso


class Run:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.path = self.root / ".loom" / "run.json"

    def status(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"active": False}

    def active(self) -> bool:
        return bool(self.status().get("active"))

    def participants(self) -> list[str]:
        return list(self.status().get("participants", []))

    def goal(self) -> str:
        return self.status().get("goal", "")

    def guardrails(self) -> dict:
        """Operator limits for this run (token cap, max iterations, ...)."""
        return dict(self.status().get("guardrails", {}))

    def start(self, run_id: str, participants: list[str], goal: str = "",
              guardrails: dict | None = None) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "active": True,
            "id": run_id,
            "participants": list(participants),
            "goal": goal,
            "guardrails": guardrails or {},
            "iterations": 0,
            "started": now_iso(),
        }
        self._write(state)
        return state

    @contextmanager
    def _locked(self):
        """Hold ``.run.lock`` around a read-modify-write of run.json, so a
        ``bump_iteration`` and an ``end`` can't lose each other's write."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.parent / ".run.lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)

    def bump_iteration(self) -> int:
        """Increment and return the coordinator-tick counter under ``.run.lock``
        (one coordinator by design, but the lock keeps the read-modify-write atomic
        if that ever changes). Same discipline as the board lock."""
        with self._locked():
            state = self.status()
            state["iterations"] = int(state.get("iterations", 0)) + 1
            self._write(state)
            return state["iterations"]

    def end(self, reason: str = "") -> dict:
        # Under the same lock as bump_iteration: end() reads-then-writes run.json,
        # so a concurrent bump between its read and write would otherwise be lost
        # (e.g. the iterations increment vanishing when the run is ended).
        with self._locked():
            state = self.status()
            state["active"] = False
            state["ended"] = now_iso()
            if reason:
                state["end_reason"] = reason
            self._write(state)
            return state

    def resume(self, reason: str = "") -> dict:
        """Re-activate a paused run **without resetting its counters**.

        Calling ``start()`` again would zero ``iterations``, which is the
        max-iterations runaway guard: a run paused and restarted every night
        would get a fresh budget each night and the guard would never bite. So
        the nightly reopen goes through here, not through ``start()``.
        """
        with self._locked():
            state = self.status()
            state["active"] = True
            state["resumed"] = now_iso()
            state.pop("ended", None)
            state.pop("end_reason", None)
            if reason:
                state["resume_reason"] = reason
            self._write(state)
            return state

    def _write(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(state, ensure_ascii=False, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
