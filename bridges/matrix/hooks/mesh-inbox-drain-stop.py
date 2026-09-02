#!/usr/bin/env python3
"""Stop hook: never finish a turn with an unprocessed mesh message.

When the agent is about to go idle, this checks its mesh inbox for messages
that arrived while it was busy and were never acked. If any recent ones are
pending, it BLOCKS the stop and feeds the agent a prompt to drain its inbox
first — so a message can no longer sit unprocessed until the next watcher
sweep, or be silently skipped.

Why: when an agent is busy, the watcher skips the send-keys push (can't type
into a mid-response pane). The message waits in the inbox and is only
re-pushed by the periodic sweep some tens of seconds later. This closes that
gap structurally — the agent itself refuses to stop with mail pending.

Anti-loop guards:
  - Only messages with acked=false AND ts within RECENCY_WINDOW count
    (ancient un-acked stragglers never block).
  - Blocks the SAME pending id-set at most MAX_BLOCKS times, then gives up
    (lets the agent stop) so a message it genuinely cannot ack cannot create
    an infinite block loop.
  - Fail-open everywhere: any error → allow stop.

Wire it in Claude Code settings.json under Stop (alongside any existing Stop
hook). Output on block: {"decision": "block", "reason": "..."}.

Env:
    MESH_AGENT  — agent name (recipient inbox is inbox-<agent>.jsonl).
    MESH_HOME   — mesh root dir (default below).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

MESH_HOME = Path(os.environ.get("MESH_HOME", str(Path.home() / "mesh")))
RECENCY_WINDOW = 6 * 3600     # ignore un-acked messages older than 6 h
MAX_BLOCKS = 2               # block the same pending set at most this many times


def _allow_stop():
    sys.exit(0)  # exit 0, no output = let the agent stop


def _parse_ts(ts: str) -> float:
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return 0.0


def pending_messages(agent: str, now: float) -> list[dict]:
    inbox = MESH_HOME / f"inbox-{agent}.jsonl"
    if not inbox.exists():
        return []
    out = []
    try:
        with inbox.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                if m.get("acked"):
                    continue
                if m.get("from") == agent:        # ignore our own echoes
                    continue
                ts = _parse_ts(m.get("ts", ""))
                if ts and (now - ts) > RECENCY_WINDOW:
                    continue                       # too old → never block
                out.append(m)
    except Exception:
        return []
    return out


def resolve_agent() -> str:
    # Inside a real tmux pane ($TMUX set), the pane's session name wins over a
    # possibly-inherited MESH_AGENT (a shared tmux server can carry one agent's
    # MESH_AGENT globally to every pane). Otherwise trust the env.
    env = os.environ.get("MESH_AGENT", "").strip().lower()
    if os.environ.get("TMUX"):
        try:
            import subprocess
            r = subprocess.run(["tmux", "display-message", "-p", "#S"],
                               capture_output=True, text=True, timeout=2)
            name = r.stdout.strip().lower()
            if name:
                return name
        except Exception:
            pass
    return env


def main() -> None:
    try:
        agent = resolve_agent()
        if not agent:
            _allow_stop()

        now = time.time()
        pend = pending_messages(agent, now)
        marker = MESH_HOME / "state" / f"stop-drain-{agent}.json"

        if not pend:
            try:
                if marker.exists():
                    marker.unlink()
            except Exception:
                pass
            _allow_stop()

        ids = sorted(m["id"] for m in pend)
        id_key = ",".join(ids)

        prev = {}
        try:
            if marker.exists():
                prev = json.loads(marker.read_text())
        except Exception:
            prev = {}
        count = prev.get("count", 0) if prev.get("id_key") == id_key else 0

        if count >= MAX_BLOCKS:
            _allow_stop()  # give up to avoid an infinite block loop

        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"id_key": id_key, "count": count + 1}))
        except Exception:
            pass

        froms = ", ".join(f"{m['from']}(id={m['id']})" for m in pend)
        reason = (
            f"{len(pend)} unprocessed mesh message(s) arrived while you were busy: "
            f"{froms}. DO NOT STOP — read and handle them first: "
            f"`python3 {MESH_HOME}/read.py {agent}` (ack each with `--ack <id>` "
            f"after reading, and reply if needed). This is the anti-skip net: you "
            f"must not end a turn with a message still pending."
        )
        print(json.dumps({"decision": "block", "reason": reason}))
        sys.exit(0)
    except Exception:
        _allow_stop()  # absolute fail-open


if __name__ == "__main__":
    main()
