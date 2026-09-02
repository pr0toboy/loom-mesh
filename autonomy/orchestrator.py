#!/usr/bin/env python3
"""Orchestrator — the coordinator's loop driver.

The coordinator is an agent (it decomposes the goal, judges reviews, decides
escalations). But the *mechanical* part of its loop — assign ready work, gate on
usage, notice what needs review or is blocked, detect convergence — should be one
deterministic call so the agent's loop stays a thin "call ``coordinator_tick``,
then act on the report" cycle.

That is this module. One function, :func:`coordinator_tick`, runs the bookkeeping
for a single iteration and returns a :class:`TickReport` telling the agent exactly
what to do next:

- ``halt``   → the run is inactive or the usage kill-switch tripped; stop the loop.
- ``done``   → every task is done; generate the making-of and end the run.
- ``assigned`` → tasks just handed out this tick (newly-owned).
- ``kick``   → participants who currently own open work and should be nudged to
  look at the board (``loom mine``). The *how* of nudging (send-keys, a mesh
  message) is the caller's binding — this only says *who*.
- ``review`` → tasks in ``in_review`` waiting for the coordinator's judgement.
- ``triage`` → blocked tasks needing a decision.

Pure orchestration over board/run/coordinator/usage_guard. No I/O beyond the
board files and an optional usage directory.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .board import Board
from .coordinator import assign_ready, next_actions, situation
from .fable_gate import DEFAULT_FABLE_AGENTS, gate_violations, review_routing
from .run import Run
from .usage_guard import UsageLimits, evaluate, fleet_snapshot

# Guardrail defaults (overridable per run via run.json guardrails).
DEFAULT_MAX_REASSIGNMENTS = 2   # unassign a stalled task at most this many times, then block
DEFAULT_STUCK_TICKS_HALT = 3    # consecutive fully-stuck ticks before halting the loop


def _coord_state_path(root) -> Path:
    return Path(root) / ".loom" / "state" / "coordinator.json"


def _read_coord_state(root) -> dict:
    try:
        return json.loads(_coord_state_path(root).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_coord_state(root, state: dict) -> None:
    p = _coord_state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, p)


def _bump_ticks(root, key: str) -> int:
    st = _read_coord_state(root)
    st[key] = int(st.get(key, 0)) + 1
    _write_coord_state(root, st)
    return st[key]


def _reset_ticks(root, key: str) -> None:
    st = _read_coord_state(root)
    if st.get(key):
        st[key] = 0
        _write_coord_state(root, st)


def limits_from_run(run: Run) -> UsageLimits:
    """Build usage limits from the guardrails the run was started with, falling
    back to the calibrated defaults for anything unset."""
    g = run.guardrails()
    return UsageLimits(
        window_token_cap=int(g.get("window_token_cap", UsageLimits.window_token_cap)),
        warn_fraction=float(g.get("warn_fraction", UsageLimits.warn_fraction)),
        kill_fraction=float(g.get("kill_fraction", UsageLimits.kill_fraction)),
    )


@dataclass
class TickReport:
    halt: bool = False
    done: bool = False
    assigned: list = field(default_factory=list)   # [{task, owner}]
    kick: list = field(default_factory=list)        # agent names with open work
    review: list = field(default_factory=list)      # task ids in_review
    triage: list = field(default_factory=list)      # blocked task ids
    review_routing: dict = field(default_factory=dict)   # task id -> Fable reviewer
    gate_violations: list = field(default_factory=list)  # done tasks w/o Fable review
    usage_level: str = "unknown"
    note: str = ""
    situation: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


DEFAULT_UNKNOWN_GRACE_TICKS = 3   # consecutive unknown-usage ticks before a soft pause halt


def coordinator_tick(root, agent_scopes: dict[str, list[str]],
                     usage_dir=None, limits: UsageLimits | None = None,
                     fable_agents=None, idle=None, usage_sources=None) -> TickReport:
    """Run one mechanical coordination step over the run at ``root``.

    ``agent_scopes`` maps participant → scopes it can take (drives assignment).
    ``usage_dir`` (optional) is the fleet snapshot directory; when given, the
    usage kill-switch is evaluated and can ``halt`` the loop. ``limits`` overrides
    the run's guardrails if supplied.

    ``fable_agents`` (optional) is the Fable review roster (defaults to the mesh
    roster). The report carries ``review_routing`` (which Fable should review each
    ``in_review`` task) and ``gate_violations`` (tasks marked ``done`` without a
    Fable review). The convergence ``done`` is held back while any gate violation
    stands — a task that skipped the Fable review is not really finished. ``idle``
    (optional) is the set of currently-idle agents, used to balance review load.
    """
    board = Board(root)
    run = Run(root)
    fa = tuple(fable_agents) if fable_agents is not None else DEFAULT_FABLE_AGENTS

    if not run.active():
        return TickReport(halt=True, note="run is not active")

    # Hard iteration cap (F5): count this tick first (halt/kill ticks count too —
    # a tick is a tick), and if it exceeds max_iterations, END the run. Ending it
    # (not just messaging) mechanically disarms every participant's Stop hook,
    # which all test run.active(). This is the real terminal backstop.
    it = run.bump_iteration()
    mi = run.guardrails().get("max_iterations")
    if mi and it > int(mi):
        board.log("max_iterations_reached", by="coordinator", iterations=it)
        run.end(reason=f"max_iterations ({mi}) reached")
        return TickReport(halt=True, note=f"max_iterations ({mi}) reached, run ended")

    # Usage gate first — never assign more work into a window that's about to wall.
    verdict = None
    usage_unknown = False
    if usage_dir is not None:
        expected = usage_sources if usage_sources is not None else run.guardrails().get("usage_sources")
        snap, coverage = fleet_snapshot(usage_dir, expected=expected)
        verdict = evaluate(snap, limits or limits_from_run(run), coverage)
        if verdict.level == "kill":
            board.log("usage_kill", by="coordinator", reason=verdict.reason)
            return TickReport(halt=True, usage_level="kill",
                              note=f"usage kill-switch: {verdict.reason}",
                              situation=situation(board, run, verdict).as_dict())
        # unknown (fail-closed): incomplete coverage / no data → soft pause below.
        usage_unknown = verdict.level == "unknown"

    # Stall remediation (F4): a task owned but silent past the stall threshold is
    # treated as a dead agent. Re-pool it (up to max_reassignments), then escalate
    # to blocked. Done BEFORE assignment so a freed task can be redistributed this
    # same tick (potentially to another agent).
    g = run.guardrails()
    max_reassign = int(g.get("max_reassignments", DEFAULT_MAX_REASSIGNMENTS))
    pre = situation(board, run, verdict)
    avoid: dict[str, set] = {}
    for s in pre.stalled:
        age = int(s["age_seconds"])
        if s["reassignments"] < max_reassign:
            board.unassign(s["id"], by="coordinator", reason=f"stall: {age}s with no event")
            if s["owner"]:
                avoid[s["id"]] = {s["owner"]}   # don't hand the dead owner its own task back
        else:
            board.block(s["id"], "coordinator", "stalled repeatedly - needs a human or the coordinator")

    # Mechanical assignment of ready work — skipped entirely on unknown usage
    # (fail-closed: no new work assigned while we can't see the budget).
    assigned = [] if usage_unknown else assign_ready(board, agent_scopes, avoid=avoid)

    sit = situation(board, run, verdict)
    acts = next_actions(sit)
    routing = review_routing(board, fa, idle=idle)
    violations = gate_violations(board, fa)

    if acts.done:
        # The Fable gate converges *mechanically*: if convergence is reached but a
        # task hit `done` without a Fable review_passed, the coordinator re-opens
        # it (done → in_review) and routes it to a Fable reviewer here and now,
        # instead of the old passive "held" report. Auto-limiting: once reopened
        # the task is no longer `done`, so the next tick sees no violation and
        # emits no second reopen. In the normal flow (tasks pass through a Fable
        # review) `violations` is empty and this whole branch is a no-op.
        if violations:
            for tid in violations:
                board.reopen(tid, by="coordinator",
                             reason="fable gate: done without a Fable review", to="in_review")
            # recompute from the new log: the reopened tasks are now in_review
            sit = situation(board, run, verdict)
            routing = review_routing(board, fa, idle=idle)
            return TickReport(
                assigned=assigned,
                kick=sorted(sit.open_by_agent.keys()),
                review=sit.needs_review,
                triage=[b["id"] for b in sit.blocked],
                review_routing=routing,
                gate_violations=violations,
                usage_level=sit.usage_level,
                note=(f"fable gate: {len(violations)} task(s) done without a Fable "
                      f"review — reopened to in_review and routed to a Fable agent"),
                situation=sit.as_dict(),
            )
        return TickReport(done=True, usage_level=sit.usage_level,
                          note=acts.note, situation=sit.as_dict())

    # Soft pause on unknown usage (F2): no new work was assigned; keep reporting
    # review/triage so judgement work still flows. After unknown_grace_ticks
    # consecutive unknown ticks, log usage_pause and halt the loop — but DON'T end
    # the run (a later tick with fresh data resumes normally). Fresh data → reset.
    if usage_unknown:
        grace = int(g.get("unknown_grace_ticks", DEFAULT_UNKNOWN_GRACE_TICKS))
        ut = _bump_ticks(root, "unknown_ticks")
        if ut >= grace:
            if ut == grace:   # log only once, at the crossing (anti board-spam)
                board.log("usage_pause", by="coordinator", reason=verdict.reason, ticks=ut)
            return TickReport(halt=True, usage_level="unknown",
                              review=acts.review, triage=acts.triage,
                              review_routing=routing, gate_violations=violations,
                              note=f"usage pause (soft): {verdict.reason}",
                              situation=sit.as_dict())
        return TickReport(
            assigned=[], kick=[],   # soft pause: no forced relaunch of agents either
            review=acts.review, triage=acts.triage, review_routing=routing,
            gate_violations=violations, usage_level="unknown",
            note=(f"usage unknown ({ut}/{grace}): soft pause, no new assignment — {verdict.reason}"),
            situation=sit.as_dict(),
        )
    _reset_ticks(root, "unknown_ticks")

    # Stuck run (F4): nothing open/ready/in-review and nothing auto-fixable, yet
    # not converged — the only tasks left are wedged/blocked. Count consecutive
    # stuck ticks; after the cap, log `run_wedged` and halt the loop (the run
    # stays active — it's for the coordinator-agent / human to break the deadlock;
    # the max_iterations cap is the hard backstop). A tick that unblocks resets it.
    if acts.stuck:
        stuck_halt = int(g.get("stuck_ticks_halt", DEFAULT_STUCK_TICKS_HALT))
        st = _bump_ticks(root, "stuck_ticks")
        if st >= stuck_halt:
            if st == stuck_halt:   # log only once, at the crossing (anti board-spam)
                board.log("run_wedged", by="coordinator", ticks=st,
                          wedged=sorted(sit.wedged.keys()), reason=acts.note)
            return TickReport(halt=True, triage=acts.triage,
                              usage_level=sit.usage_level, note=acts.note,
                              situation=sit.as_dict())
    else:
        _reset_ticks(root, "stuck_ticks")

    return TickReport(
        assigned=assigned,
        kick=sorted(sit.open_by_agent.keys()),
        review=acts.review,
        triage=acts.triage,
        review_routing=routing,
        gate_violations=violations,
        usage_level=sit.usage_level,
        note=acts.note,
        situation=sit.as_dict(),
    )


def _main(argv=None) -> int:
    import argparse
    import json
    import os
    p = argparse.ArgumentParser(description="Run one coordinator tick and print the report")
    p.add_argument("--root", default=os.environ.get("LOOM_ROOT", ""))
    p.add_argument("--scopes", required=True,
                   help="agent:scope,scope;agent:scope  (e.g. 'worker-a:py,infra;worker-b:frontend')")
    p.add_argument("--usage-dir", help="fleet usage snapshot directory")
    args = p.parse_args(argv)
    if not args.root:
        raise SystemExit("no project root: pass --root or set LOOM_ROOT")
    scopes: dict[str, list[str]] = {}
    for part in args.scopes.split(";"):
        if not part.strip():
            continue
        name, _, rest = part.partition(":")
        scopes[name.strip()] = [s.strip() for s in rest.split(",") if s.strip()]
    report = coordinator_tick(args.root, scopes, usage_dir=args.usage_dir)
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    return 2 if report.halt else 0


if __name__ == "__main__":
    raise SystemExit(_main())
