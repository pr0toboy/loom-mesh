#!/usr/bin/env python3
"""Stop hook: don't go idle while you still own open work (during a run).

This is the heart of the autonomy engine. It is modelled on the mesh
``mesh-inbox-drain-stop.py`` anti-skip hook: same protocol — print
``{"decision": "block", "reason": ...}`` to keep the turn going, exit 0 bare to
let the agent stop — and fail-open everywhere.

The key difference from the mesh anti-skip net: a message is acked in a turn or
two (low block cap), but a *task* spans many turns. So this blocks for as long
as the agent has open work, with an anti-thrash backstop: if the work signature
(which tasks + how many progress events) hasn't moved for ``MAX_STALE`` turns,
it escalates (lets the agent stop, marks the tasks blocked) and lets the
coordinator take over.

This backstop covers a *live* agent spinning in place (it still runs turns, so
its signature is checked here). The coordinator's stall detector (F4) covers the
complementary case — a *dead* agent that produces no turns at all, so no signature
ever updates: after ``stall_after_seconds`` of board silence the coordinator
re-pools the task. The two nets are complementary, not redundant. Real convergence comes from the agent itself calling
``loom-task submit``/``done`` → the task leaves ``open_for`` → the hook allows
the stop.

Boundary guards (= the sandbox):
  - no LOOM_ROOT and no arming
    pointer for this agent       → no-op.
  - no active run                → no-op (normal mesh life is untouched).
  - agent not a run participant  → no-op.
  - outside the night window     → let the agent stop, tasks stay open
                                   (``autonomy/night_window.py``).

Kill-switch (shared subscription protection): before asking the agent to keep
going, it consults the fleet usage guard. On a KILL verdict it does NOT continue
— it records the kill on the board (so the coordinator sees the run must pause)
and lets the agent stop. This is the cable between ``usage_guard`` and the loop.

stdin (Claude Code Stop hook): ``{"session_id": ..., "transcript_path": ...}``.
Env: ``LOOM_ROOT`` (project root — or, without it, the arming pointer
``~/mesh/loom-arm/<agent>``), ``MESH_AGENT`` (agent name; tmux #S wins when
inside a real pane, matching the mesh hook).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Make the autonomy package importable however the hook is invoked.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

MAX_STALE = 30  # turns without progress on the same signature before escalating

# Second way to arm an agent, besides ``LOOM_ROOT`` in its environment: a pointer
# file ``~/mesh/loom-arm/<agent>`` holding the run root. Environment variables are
# read when the agent's service starts, so arming through ``LOOM_ROOT`` alone means
# **restarting the agent** — which throws away whatever it had in flight. A file is
# read at every Stop, so arming and disarming become a write and a delete.
# The fast path stays one ``stat``: no arming directory → nothing to look up.
ARM_DIR = Path.home() / "mesh" / "loom-arm"


def pointer_root(agent: str) -> str:
    try:
        return (ARM_DIR / agent).read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def allow_stop():
    sys.exit(0)  # exit 0, no output = let the agent stop


def resolve_agent() -> str:
    """Inside a real tmux pane ($TMUX set), the pane session name wins over a
    possibly-inherited MESH_AGENT (a shared tmux server can leak one agent's
    MESH_AGENT to every pane). Otherwise trust the env. Mirrors the mesh hook."""
    env = os.environ.get("MESH_AGENT", "").strip().lower()
    if os.environ.get("TMUX"):
        try:
            import subprocess
            # Deliberately plain `tmux`, not MESH_TMUX: guarded by $TMUX above,
            # this asks the server we are RUNNING INSIDE which session we are —
            # not which server the deployment drives. Pointing it elsewhere would
            # answer with someone else's session name.
            r = subprocess.run(["tmux", "display-message", "-p", "#S"],
                               capture_output=True, text=True, timeout=2)
            name = r.stdout.strip().lower()
            if name:
                return name
        except Exception:
            pass
    return env


def _usage_level(root: str, run) -> tuple[str, str]:
    """Return ``(level, reason)`` from the fleet usage guard.
    Fail-**closed** (soft): any probe error → ``("unknown", ...)`` so the agent is
    allowed to stop (soft pause) rather than kept burning against a budget we can't
    read. The old fail-open (`→ None`, keep going) is exactly what F2 closes."""
    try:
        from autonomy.usage_guard import UsageLimits, evaluate, fleet_snapshot
        g = run.guardrails()
        limits = UsageLimits(
            window_token_cap=int(g.get("window_token_cap", UsageLimits.window_token_cap)),
            warn_fraction=float(g.get("warn_fraction", UsageLimits.warn_fraction)),
            kill_fraction=float(g.get("kill_fraction", UsageLimits.kill_fraction)),
        )
        snap, coverage = fleet_snapshot(Path(root) / ".loom" / "usage",
                                        expected=g.get("usage_sources"))
        verdict = evaluate(snap, limits, coverage)
        return verdict.level, verdict.reason
    except Exception:
        return "unknown", "usage probe failed"


def main() -> None:
    try:
        # consume stdin (the Stop payload) so the pipe never blocks
        try:
            sys.stdin.read()
        except Exception:
            pass

        root = os.environ.get("LOOM_ROOT", "").strip()
        agent = ""
        if not root:
            if not ARM_DIR.is_dir():
                allow_stop()
            # Resolve the agent the tmux-aware way even though MESH_AGENT is right
            # here: a shared tmux server can leak one agent's MESH_AGENT into every
            # pane, and here that would arm the wrong agent onto someone's board.
            agent = resolve_agent()
            if not agent:
                allow_stop()
            root = pointer_root(agent)
            if not root:
                allow_stop()

        from autonomy.board import Board
        from autonomy.run import Run

        run = Run(root)
        if not run.active():
            allow_stop()

        agent = agent or resolve_agent()
        if not agent or agent not in run.participants():
            allow_stop()

        board = Board(root)
        opn = board.open_for(agent)

        state_dir = Path(root) / ".loom" / "state"
        marker = state_dir / f"work-drain-{agent}.json"

        if not opn:
            try:
                marker.unlink()
            except Exception:
                pass
            allow_stop()

        # Night window: the engine may only keep agents working during the hours
        # the operator opened (see autonomy/night_window.py for why the closing
        # hour is 02:30 and not dawn). Checked before the usage guard because it
        # is cheaper and more absolute. Same shape as the soft pause below: the
        # agent stops, its tasks stay OPEN for the next night — closing time is
        # not a failure, so nothing gets marked blocked.
        try:
            from autonomy.night_window import decide as night_decide
            night = night_decide()
        except Exception:
            night = None  # gate unavailable → behave as before (fail-open)
        if night is not None and night.closed:
            try:
                # Announce the decision: a guard that declines in silence is
                # indistinguishable from an agent that simply had nothing to do.
                board.log("night_window_closed", by=agent, reason=night.reason)
            except Exception:
                pass
            # The marker is deliberately NOT cleared here: closing time is a
            # pause, not a reset. Clearing it would restart the 30-turn stale
            # count every night, so a wedged agent would burn turns forever
            # without ever escalating to the coordinator.
            allow_stop()

        # Kill-switch: the shared rate-limit window is the one thing no single
        # agent can see. The guard runs only when usage monitoring is actually
        # configured (usage_sources set, or the usage dir has ≥1 snapshot file);
        # an unconfigured run leaves the switch inert, as before.
        usage_dir = Path(root) / ".loom" / "usage"
        monitoring = bool(run.guardrails().get("usage_sources")) or (
            usage_dir.is_dir() and any(usage_dir.glob("*.json")))
        if monitoring:
            level, reason = _usage_level(root, run)
            if level == "kill":
                # Hard kill: block the tasks (coordinator must pause the run) and stop.
                for t in opn:
                    try:
                        board.block(t["id"], agent, f"usage kill-switch: {reason}")
                    except Exception:
                        pass
                try:
                    board.log("usage_kill", by=agent, reason=reason)
                except Exception:
                    pass
                try:
                    marker.unlink()
                except Exception:
                    pass
                allow_stop()
            if level == "unknown":
                # Soft pause (fail-closed): let the agent stop, but DON'T block its
                # tasks — they stay open for when data returns. No forced relaunch.
                try:
                    marker.unlink()
                except Exception:
                    pass
                allow_stop()

        # Signature = which tasks + how much progress. While it moves we assume
        # forward motion and keep going with no hard cap; when it stalls, escalate.
        sig = ";".join(
            f"{t['id']}:{t['attempts']}:{t['status']}"
            for t in sorted(opn, key=lambda x: x["id"])
        )

        prev = {}
        try:
            if marker.exists():
                prev = json.loads(marker.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
        stale = prev.get("stale", 0) + 1 if prev.get("sig") == sig else 0

        if stale >= MAX_STALE:
            for t in opn:
                try:
                    board.block(t["id"], agent,
                                f"work-drain backstop: no progress for {MAX_STALE} turns, "
                                f"escalating to coordinator")
                except Exception:
                    pass
            try:
                marker.unlink()
            except Exception:
                pass
            allow_stop()

        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"sig": sig, "stale": stale}), encoding="utf-8")
        except Exception:
            pass

        listing = "; ".join(f"{t['id']} « {t['title']} »" for t in opn)
        reason = (
            f"WORK IN PROGRESS — you own {len(opn)} open task(s): {listing}. "
            f"DO NOT STOP: keep working. When a task advances, record it "
            f"(`loom-task progress <id> -m '...'`); when it is finished and pushed "
            f"to its branch, submit it (`loom-task submit <id> --branch <b>`); if "
            f"you are genuinely blocked, mark it (`loom-task block <id> -m '...'`) "
            f"— otherwise the coordinator cannot move the run forward."
        )
        print(json.dumps({"decision": "block", "reason": reason}))
        sys.exit(0)
    except Exception:
        allow_stop()  # absolute fail-open


if __name__ == "__main__":
    main()
