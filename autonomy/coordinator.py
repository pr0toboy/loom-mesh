#!/usr/bin/env python3
"""Coordinator toolkit — the mechanical half of the tech-lead loop.

The coordinator is itself an *agent* (it runs the planning/review loop with
judgement a script can't have: how to decompose a goal, whether a review really
passes, whether a blocker needs the human). This module gives that agent the
*mechanism* so its loop stays small and deterministic:

- :func:`assign_ready`   — hand ready unowned tasks to participants by scope,
  preferring the least-loaded eligible agent.
- :func:`situation`      — one structured snapshot of the run per loop tick
  (counts, who's working on what, what needs review, what's blocked, whether
  the run has converged, the current usage level).
- :func:`next_actions`   — the to-do list that snapshot implies, so the agent
  acts instead of re-deriving it each turn.
- :func:`is_converged`   — every task done.

Judgement stays with the agent; bookkeeping lives here.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from .board import Board, dependency_faults
from .run import Run

# Default stall threshold: an owned open task with no event for this long is
# treated as a dead agent (the work-drain hook logs progress every turn, so a
# live agent always produces events — silence is a real signal). Overridable per
# run via the ``stall_after_seconds`` guardrail.
DEFAULT_STALL_AFTER_SECONDS = 1800.0


def is_converged(board: Board) -> bool:
    return board.converged()


def _age_seconds(ts: str | None, now: datetime) -> float | None:
    """Seconds between ``ts`` (ISO-8601) and ``now``; ``None`` if unparsable."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds()


def assign_ready(board: Board, agent_scopes: dict[str, list[str]], avoid=None) -> list[dict]:
    """Assign each ready unowned task to the least-loaded eligible agent.

    ``agent_scopes`` maps an agent name to the scopes it can take (e.g.
    ``{"worker-a": ["py", "infra"], "worker-b": ["frontend"]}``). A task whose
    ``scope`` matches an agent's scopes is eligible for that agent; a task with
    no ``scope`` (or no scope match) may go to any participant. Returns the list
    of assignments made, e.g. ``[{"task": "T1", "owner": "worker-a"}]``.

    ``avoid`` (optional) maps a task id → a set of agents to keep off it this
    tick: used when re-pooling a stalled task so the dead owner (now at load 0)
    isn't handed its own task straight back. The exclusion is dropped if it would
    leave the task with no eligible agent (never strand work over a preference).
    """
    if not agent_scopes:
        return []
    avoid = avoid or {}
    # current load = number of open (assigned/in_progress) tasks per agent
    tasks = board.tasks()
    load = {a: 0 for a in agent_scopes}
    for t in tasks.values():
        if t["owner"] in load and t["status"] in ("assigned", "in_progress"):
            load[t["owner"]] += 1

    made = []
    for t in board.ready_unassigned():
        scope = t.get("scope")
        eligible = [a for a, scopes in agent_scopes.items()
                    if scope is None or scope in scopes]
        if not eligible:                       # no scope match → anyone may take it
            eligible = list(agent_scopes)
        blocked = avoid.get(t["id"])
        if blocked:
            filtered = [a for a in eligible if a not in blocked]
            if filtered:                       # keep the exclusion only if work remains placeable
                eligible = filtered
        owner = min(eligible, key=lambda a: (load[a], a))
        # Guarded assign: if another coordinator/process grabbed this task between
        # our ready_unassigned() read and now, assign_if_free returns None → skip it
        # (no phantom double-assignment, no load bump for work we didn't hand out).
        if board.assign_if_free(t["id"], owner, by="coordinator") is None:
            continue
        load[owner] += 1
        made.append({"task": t["id"], "owner": owner})
    return made


@dataclass
class Situation:
    goal: str
    active: bool
    converged: bool
    counts: dict          # status -> n
    open_by_agent: dict   # agent -> [task ids]
    needs_review: list    # task ids in_review
    blocked: list         # [{id, reason}]
    ready_unassigned: list  # task ids
    usage_level: str      # ok | warn | kill | unknown
    usage_reason: str = ""
    wedged: dict = field(default_factory=dict)      # task id -> dependency fault
    stalled: list = field(default_factory=list)     # [{id, owner, age_seconds, reassignments}]
    failures: list = field(default_factory=list)    # abandoned task ids

    def as_dict(self) -> dict:
        return asdict(self)


def situation(board: Board, run: Run, usage_verdict=None,
              now: datetime | None = None, stall_after_seconds: float | None = None) -> Situation:
    tasks = board.tasks()
    counts: dict[str, int] = {}
    open_by_agent: dict[str, list[str]] = {}
    for t in tasks.values():
        counts[t["status"]] = counts.get(t["status"], 0) + 1
        if t["status"] in ("assigned", "in_progress") and t["owner"]:
            open_by_agent.setdefault(t["owner"], []).append(t["id"])

    if stall_after_seconds is None:
        stall_after_seconds = float(run.guardrails().get("stall_after_seconds",
                                                          DEFAULT_STALL_AFTER_SECONDS))
    now = now or datetime.now(timezone.utc)
    stalled = []
    for t in tasks.values():
        if t["status"] in ("assigned", "in_progress"):
            age = _age_seconds(t.get("last_event_ts"), now)
            if age is not None and age > stall_after_seconds:
                stalled.append({"id": t["id"], "owner": t["owner"],
                                "age_seconds": age, "reassignments": t.get("reassignments", 0)})
    stalled.sort(key=lambda s: s["id"])

    return Situation(
        goal=run.goal(),
        active=run.active(),
        converged=is_converged(board),
        counts=counts,
        open_by_agent=open_by_agent,
        needs_review=[t["id"] for t in board.in_review()],
        blocked=[{"id": t["id"], "reason": t["block_reason"]} for t in board.blocked()],
        ready_unassigned=[t["id"] for t in board.ready_unassigned()],
        usage_level=getattr(usage_verdict, "level", "unknown") if usage_verdict else "unknown",
        usage_reason=getattr(usage_verdict, "reason", "") if usage_verdict else "",
        wedged=dependency_faults(tasks),
        stalled=stalled,
        failures=board.failures(),
    )


@dataclass
class Actions:
    halt: bool = False            # usage kill or run inactive → stop the loop
    done: bool = False            # converged → wrap up + making-of
    stuck: bool = False           # nothing open/ready/in-review and not converged
    review: list = field(default_factory=list)    # task ids to review now
    triage: list = field(default_factory=list)    # blocked + wedged task ids needing a decision
    assign: list = field(default_factory=list)    # ready task ids to assign
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def next_actions(sit: Situation) -> Actions:
    """Turn a situation snapshot into the coordinator's to-do list for this tick."""
    if sit.usage_level == "kill":
        return Actions(halt=True, note=f"usage kill-switch: {sit.usage_reason}")
    if not sit.active:
        return Actions(halt=True, note="run is not active")
    if sit.converged:
        if sit.failures:
            return Actions(done=True, note=(
                f"converged with {len(sit.failures)} abandoned: "
                f"{', '.join(sit.failures)} — generate the making-of and end the run"))
        return Actions(done=True, note="all tasks done — generate the making-of and end the run")

    # Triage covers both explicit blocks and dependency-wedged tasks.
    triage = sorted(set([b["id"] for b in sit.blocked]) | set(sit.wedged.keys()))
    # Stuck = not converged, but nothing is open, ready, in review, or auto-fixable
    # (no stalled task to re-pool). The only remaining tasks are wedged/blocked —
    # the run cannot move without deps being fixed, a task abandoned, or the run ended.
    stuck = (not sit.open_by_agent and not sit.ready_unassigned
             and not sit.needs_review and not sit.stalled)
    if stuck:
        note = (f"run wedged: {len(sit.wedged)} unsatisfiable task(s) — fix the deps "
                f"(`loom task deps`), abandon (`loom task abandon`), or end the run")
    else:
        note = (f"{sit.counts.get('in_progress', 0)} in progress, "
                f"{len(sit.needs_review)} to review, {len(triage)} to triage, "
                f"{len(sit.stalled)} stalled")
    return Actions(
        stuck=stuck,
        review=list(sit.needs_review),
        triage=triage,
        assign=list(sit.ready_unassigned),
        note=note,
    )
