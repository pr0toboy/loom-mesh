#!/usr/bin/env python3
"""Reconcile every autonomy run with the night window. Idempotent, run on a timer.

The Stop hook (``hooks/work_drain_stop.py``) can only decline to *continue* an
agent: it is consulted when an agent finishes a turn. Two things it cannot do,
which is why this exists:

  * **close**: a turn already in flight at 02:30 keeps going, and nothing marks
    the run as paused, so the coordinator would keep assigning.
  * **open**: at 23:00 an idle agent stays idle forever — something has to send
    the first nudge.

So this is a *reconciler*, not a pair of one-shot actions: it compares the
window to the state of each run and moves the state, whichever direction it is
out of date. Running it every 10 minutes rather than exactly at the boundaries
means the hours live in **one** place (the night config) instead of being
duplicated into ``OnCalendar=`` lines that then drift, and a machine that was
down at 02:30 still gets closed at its next tick.

Pausing is deliberately distinguishable from finishing: the pause writes
``end_reason = NIGHT_PAUSE`` and the reopen only ever touches runs carrying that
exact marker. A run the coordinator ended because it *converged* is never
resurrected by the night.
"""
from __future__ import annotations


import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Sender of the reconciliation messages, supplied by the deployment through
# MESH_RECONCILE_SENDER: no agent name is hardcoded here. The bus REFUSES a
# sender the roster does not know, so the unit has to set this variable — a
# generic fallback would only fail later, at send time.
_SENDER = os.environ.get("MESH_RECONCILE_SENDER", "")

# Which tmux server, and may we type into it? `_interrupt_pane` sends a key to
# a pane, so the same rule as the bus watcher and the ticket dispatcher applies:
# the server is configurable, and a mesh home that is not the default one may
# not drive the default server — otherwise a bench whose roster holds a real
# agent name reaches that agent's live session.
_TMUX = shlex.split(os.environ.get("MESH_TMUX", "") or "tmux")
_DEFAULT_MESH_HOME = os.path.expanduser("~/mesh")


def _targets_shared_server() -> bool:
    """True when the tmux command names no server of its own (-L / -S).

    A private server is one this deployment created; whatever sessions it holds,
    they are not the operator's agents. The shared default server is the only
    dangerous target.
    """
    if {"-L", "-S"} & set(_TMUX):
        return False
    # Only the tmux binary itself reaches the shared server: pointing MESH_TMUX
    # at a wrapper or a stub is as deliberate as naming a socket.
    return bool(_TMUX) and os.path.basename(_TMUX[0]) == "tmux"


def _tmux_scope_is_consistent() -> bool:
    # `MESH_TMUX=tmux` is a declaration that names the SHARED server, and it used
    # to satisfy this check — the comment above stated the right rule, the code
    # applied a weaker one. Same fix as bus/watcher.sh.
    if not _targets_shared_server():
        return True
    mesh_home = os.environ.get("MESH_HOME", _DEFAULT_MESH_HOME)
    if os.path.realpath(mesh_home) == os.path.realpath(_DEFAULT_MESH_HOME):
        return True
    return os.environ.get("MESH_ALLOW_SHARED_TMUX") == "1"


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from autonomy.board import Board          # noqa: E402
from autonomy.night_window import decide   # noqa: E402
from autonomy.run import Run               # noqa: E402
from autonomy.usage_guard import report_local  # noqa: E402

NIGHT_PAUSE = "night window closed"
DEFAULT_RUNS_DIR = Path.home() / "loom-runs"
MESH_SEND = Path.home() / "mesh" / "mesh-send.sh"

# Waking several agents at once is the fleet's main cost spike (measured
# measured: load 11.7 → 18 on 4 cores after one broadcast), and the operator caps
# concurrent agents at 3. So a pass nudges at most this many and says which ones
# it left for the next pass — a silent cap would read as "everyone was started".
MAX_KICKS = 3

# A pause older than this is not a pause any more, it is a zombie: the reopen
# refuses it. Found the hard way — the June 2026 ``addup`` run was still flagged
# active with 9 todo tasks and participants that no longer exist, so a blind
# reopen would have woken agents at 23:00 onto a two-month-old board. Wide enough
# (a week) that a machine down for a few days still resumes its real work.
MAX_PAUSE_AGE_DAYS = 7

# Host label under which this machine's ccusage snapshot is filed. It is the
# machine, not the agent: one ccusage sees every agent sharing the same user.
USAGE_SOURCE = os.environ.get("LOOM_USAGE_SOURCE", "").strip() or os.uname().nodename


def _pause_age_days(ended: object) -> float | None:
    """Age in days of a pause timestamp. ``None`` when it is missing or
    unparseable — the caller treats that as stale, never as fresh."""
    if not isinstance(ended, str) or not ended:
        return None
    try:
        ts = datetime.fromisoformat(ended)
    except ValueError:
        return None
    now = datetime.now(tz=ts.tzinfo) if ts.tzinfo else datetime.now()
    return (now - ts).total_seconds() / 86400.0


def run_roots(runs_dir: Path) -> list[Path]:
    if not runs_dir.is_dir():
        return []
    return sorted(p for p in runs_dir.iterdir() if (p / ".loom" / "run.json").exists())


def _interrupt_pane(agent: str, log) -> None:
    """Belt for the closing edge: stop a turn that is already in flight.

    Local tmux only. An agent on another machine is not reachable this way; the run
    being paused plus the Stop hook's gate already prevent its *next* turn, so we
    record the gap instead of pretending to have closed it.
    """
    if not _tmux_scope_is_consistent():
        log(f"  {agent}: REFUSED — this mesh home may not drive the default tmux "
            f"server; set MESH_TMUX to a socket of its own")
        return
    try:
        has = subprocess.run([*_TMUX, "has-session", "-t", agent],
                             capture_output=True, timeout=5)
        if has.returncode != 0:
            log(f"  {agent}: no local pane (remote or asleep) — not interrupted")
            return
        subprocess.run([*_TMUX, "send-keys", "-t", agent, "Escape"],
                       capture_output=True, timeout=5)
        log(f"  {agent}: Escape sent (turn in flight interrupted)")
    except Exception as exc:
        log(f"  {agent}: interrupt failed ({exc.__class__.__name__}: {exc})")


def _kick(agent: str, goal: str, log) -> bool:
    body = (
        "Night window open: pick your board back up in full-auto. "
        f"Goal for this run: {goal or '(see the board)'}. "
        "Use loom-task to make progress; the window closes at 02:30 and you will "
        "be stopped cleanly then."
    )
    try:
        r = subprocess.run(["bash", str(MESH_SEND), _SENDER, agent, "normal", body],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            log(f"  {agent}: nudged")
            return True
        log(f"  {agent}: mesh-send failed rc={r.returncode} {r.stderr.strip()[:200]}")
    except Exception as exc:
        log(f"  {agent}: mesh-send raised {exc.__class__.__name__}: {exc}")
    return False


def reconcile(runs_dir: Path | None = None, dry_run: bool = False,
              now: datetime | None = None, log=print) -> dict:
    """Returns a summary dict. ``dry_run`` suppresses **every** observable effect
    (run.json writes, board events, tmux keys, mesh messages) — not just the
    messages, so that rehearsing it can never be the thing that pauses a run."""
    d = decide(now)
    if d.open is None:
        log("no night window configured — nothing to reconcile")
        return {"window": None, "paused": [], "resumed": [], "deferred": [],
                "usage_fed": []}

    roots = run_roots(runs_dir or DEFAULT_RUNS_DIR)
    log(f"window {'OPEN' if d.open else 'CLOSED'} ({d.reason}); {len(roots)} run root(s)")

    paused, resumed, deferred = [], [], []
    kicks = 0

    for root in roots:
        run = Run(root)
        st = run.status()
        active = bool(st.get("active"))
        participants = list(st.get("participants", []))

        if d.closed and active:
            log(f"{root.name}: active outside the window → pausing")
            if not dry_run:
                run.end(reason=NIGHT_PAUSE)
                try:
                    Board(root).log("night_pause", by="night-reconcile", reason=d.reason)
                except Exception:
                    pass
                for a in participants:
                    _interrupt_pane(a, log)
            paused.append(root.name)

        elif d.open and not active and st.get("end_reason") == NIGHT_PAUSE:
            age = _pause_age_days(st.get("ended"))
            if age is None or age > MAX_PAUSE_AGE_DAYS:
                log(f"{root.name}: pause is stale ({st.get('ended')!r}, "
                    f"age={age}) → NOT resuming")
                continue
            log(f"{root.name}: paused by the night → resuming")
            if not dry_run:
                run.resume(reason="night window open")
                try:
                    Board(root).log("night_resume", by="night-reconcile", reason=d.reason)
                except Exception:
                    pass
            resumed.append(root.name)
            for a in participants:
                if kicks >= MAX_KICKS:
                    deferred.append(a)
                    continue
                if dry_run or _kick(a, st.get("goal", ""), log):
                    kicks += 1

    if deferred:
        log(f"deferred to the next pass (cap {MAX_KICKS} concurrent wakes): "
            + ", ".join(deferred))

    # Feed the kill-switch of every run that is (still) active. The hook's usage
    # guard treats a snapshot older than 600 s as a coverage gap and soft-pauses
    # the agent, so the refresh cadence has to stay comfortably under that — this
    # is why the timer ticks every 5 min and not every 10. Nothing runs during the
    # day: no active run, no probe.
    fed = []
    for root in roots:
        if not Run(root).active():
            continue
        if dry_run:
            fed.append(root.name)
            continue
        try:
            snap = report_local(USAGE_SOURCE, root / ".loom" / "usage")
            log(f"{root.name}: usage snapshot refreshed"
                + (f" ({snap.limit_tokens:,} tok this window)" if snap else " (idle marker)"))
            fed.append(root.name)
        except Exception as exc:
            # Deliberately loud: an unfed guard is an unguarded night, and the
            # hook would then soft-pause every turn rather than work.
            log(f"{root.name}: usage refresh FAILED ({exc.__class__.__name__}: {exc})")

    return {"window": d.open, "paused": paused, "resumed": resumed,
            "deferred": deferred, "usage_fed": fed}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs-dir", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, touch nothing at all")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = ap.parse_args()

    lines: list[str] = []
    def log(msg):
        lines.append(str(msg))
        if not args.json:
            print(msg, flush=True)

    summary = reconcile(args.runs_dir, dry_run=args.dry_run, log=log)
    if args.json:
        print(json.dumps({**summary, "log": lines}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
