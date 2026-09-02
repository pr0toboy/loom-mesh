#!/usr/bin/env python3
"""Fable review gate — the policy half of the final-phase review.

Policy: **every task's final phase must be
reviewed and audited by a *Fable* agent before it counts as done.** Opus agents
are the chiefs/workers that own tasks end-to-end; Fable agents (the premium tier)
cost ~2× the usage, so they are a *premium review tier* pulled in on the gate,
never run in free-wheel. See the global charter section "Tiers Opus / Fable".

This module is the deterministic bookkeeping for that policy, and nothing more:

- :func:`review_routing` — for the tasks currently ``in_review``, decide *which*
  Fable agent should review each (the specialist for its scope, the generalist otherwise;
  an ``idle`` set, when given, is preferred to balance load).
- :func:`gate_violations` — audit the event log for tasks that reached ``done``
  *without* a Fable ``review_passed`` — i.e. the gate was bypassed. The
  coordinator acts on this (re-open / send for review).

It is strictly **additive** over :mod:`board`/:mod:`coordinator`: it reads the
same event log and changes no existing transition, so the rest of the flow is
untouched. Judgement (does the review actually pass?) stays with the Fable
agent; this only routes and audits.
"""
from __future__ import annotations

import os

from .board import Board

# Default Fable roster on the Loom mesh, overridable via LOOM_FABLE_AGENTS
# (comma-separated) so a fleet change doesn't require a code edit. The general reviewer is
# the premium-by-default agent; the specialist runs premium only
# on explicit call but stays the game/PMD specialist when it is.
_ENV_FABLE_AGENTS = os.environ.get("LOOM_FABLE_AGENTS", "").strip()
DEFAULT_FABLE_AGENTS = (
    tuple(a.strip() for a in _ENV_FABLE_AGENTS.split(",") if a.strip())
    if _ENV_FABLE_AGENTS else ()
)

# Scopes whose review is best routed to the the specialist reviewer.
GAME_SCOPES = frozenset({"game", "pmd", "pmd-infinite", "donjon"})

# The reviewer the game-specialist scopes prefer, and the generalist fallback.
GAME_REVIEWER = os.environ.get("LOOM_SPECIALIST_REVIEWER", "")
GENERAL_REVIEWER = os.environ.get("LOOM_GENERAL_REVIEWER", "")

# Empty defaults: this module knows no agent of its own. But a gate that routes
# to nobody does not "degrade" — it CANCELS the review, and does so quietly,
# which is the worst of both: work passes the gate precisely because no reviewer
# was ever assigned. So it says so once, at import, rather than leaving a green
# suite on top of a pipeline that reviews nothing.
if not DEFAULT_FABLE_AGENTS or not GENERAL_REVIEWER:
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "review gate NOT CONFIGURED (LOOM_FABLE_AGENTS=%r, LOOM_GENERAL_REVIEWER=%r): "
        "no review will be routed. Set these variables, or the gate lets everything "
        "through.", _ENV_FABLE_AGENTS, GENERAL_REVIEWER)


def is_fable(agent, fable_agents=DEFAULT_FABLE_AGENTS) -> bool:
    return agent in set(fable_agents)


def reviewed_by_fable(task: dict, fable_agents=DEFAULT_FABLE_AGENTS) -> bool:
    """True iff this task holds a Fable ``review_passed`` *after its last reopen*.

    A ``task_done`` written directly by a worker, or a ``review_passed`` by a
    non-Fable, does NOT satisfy the gate. A ``task_reopened`` **invalidates** any
    prior Fable pass: otherwise a stale Fable sign-off from before a
    reopen→re-pass-by-a-non-Fable cycle would silently whitewash the new work.
    """
    fa = set(fable_agents)
    passed = False
    for e in task.get("history", []):
        et = e.get("type")
        if et == "task_reopened":
            passed = False
        elif et == "review_passed" and e.get("by") in fa:
            passed = True
    return passed


def route_reviewer(task: dict, fable_agents=DEFAULT_FABLE_AGENTS, idle=None) -> str | None:
    """Pick the Fable agent that should review ``task``.

    Routing: a game/PMD-scoped task prefers the specialist reviewer; every
    other task prefers the generalist. When an ``idle`` set of agents is
    supplied, an idle Fable is preferred over a busy one (load balancing) while
    keeping the scope preference as the tie-breaker. Returns ``None`` if no Fable
    agent is available.
    """
    fa = [a for a in fable_agents]
    if not fa:
        return None
    scope = task.get("scope")
    preferred = GAME_REVIEWER if scope in GAME_SCOPES else GENERAL_REVIEWER
    if preferred not in fa:
        preferred = fa[0]

    if idle is not None:
        idle_fa = [a for a in fa if a in set(idle)]
        if idle_fa:
            # honour scope preference among the idle ones, else any idle Fable
            return preferred if preferred in idle_fa else idle_fa[0]
        # nobody idle → fall through to the scope preference (will queue)
    return preferred


def review_routing(board: Board, fable_agents=DEFAULT_FABLE_AGENTS, idle=None) -> dict[str, str]:
    """Map each ``in_review`` task id → the Fable agent that should review it."""
    out: dict[str, str] = {}
    for t in board.in_review():
        reviewer = route_reviewer(t, fable_agents, idle=idle)
        if reviewer is not None:
            out[t["id"]] = reviewer
    return out


def gate_violations(board: Board, fable_agents=DEFAULT_FABLE_AGENTS) -> list[str]:
    """Task ids that are ``done`` but never passed a Fable review — gate bypassed.

    These should be re-opened (or sent for a Fable review) by the coordinator:
    the policy says no final phase is really done without a Fable sign-off.
    """
    bad: list[str] = []
    for t in board.tasks().values():
        if t.get("status") == "done" and not reviewed_by_fable(t, fable_agents):
            bad.append(t["id"])
    return sorted(bad)
