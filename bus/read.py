#!/usr/bin/env python3
"""Read your own inbox.

Usage:
    python3 read.py <agent>              # unread: newer than your cursor, not acked
    python3 read.py <agent> --all        # everything in the file
    python3 read.py <agent> --unacked    # everything still unacked, cursor IGNORED
    python3 read.py <agent> --ack <id> [<id> ...]

Three views, because "have I dealt with this?" and "have I seen this?" are not
the same question:

* **unread** is cursor-based and is what a session opens with;
* **--unacked** ignores the cursor entirely. It exists for agents that sleep and
  are woken by a message: the wake path advances the cursor at boot, so the
  normal view goes empty while the work is still owed. This view shows exactly
  what is still owed;
* **--ack** is the only thing that marks a message handled. Displaying a message
  deliberately does *not*, so an interrupted session does not lose its queue.

Environment:
    MESH_HOME   bus directory (default ``~/mesh``)
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from roster import FACADE_PEERS as HUMAN_FACADE_PEERS, INBOX_PEERS as AGENTS  # noqa: E402

MESH_DIR = Path(os.environ.get("MESH_HOME", os.path.expanduser("~/mesh")))


def _is_newer(candidate_ts: str, baseline_ts: str) -> bool:
    return str(candidate_ts) > str(baseline_ts)


def _is_after_cursor(msg_ts: str, msg_id: str | None, cursor: object) -> bool:
    """Is this message past the cursor?

    The cursor stores a timestamp *and* the ids seen at that exact timestamp.
    A plain ``ts >`` comparison hid same-second siblings of the cursor message:
    they were newer than nothing and older than nothing, so they never showed up
    in the normal view and only ``--unacked`` revealed them.
    """
    if not isinstance(cursor, dict):
        return True
    base_ts = cursor.get("ts", "")
    if _is_newer(msg_ts, base_ts):
        return True
    if _is_newer(base_ts, msg_ts):
        return False
    seen = cursor.get("ids_at_ts")
    if not isinstance(seen, list):
        base_id = cursor.get("id")
        seen = [base_id] if base_id is not None else []
    return msg_id not in seen


def load_inbox(agent: str) -> list[dict]:
    inbox = MESH_DIR / f"inbox-{agent}.jsonl"
    if not inbox.exists():
        return []
    msgs = []
    with inbox.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # One corrupt line must not blind an agent to its whole inbox.
            try:
                rec = json.loads(line)
            except Exception:
                print(f"[read] skipped malformed line in {inbox.name}", file=sys.stderr)
                continue
            if isinstance(rec, dict):
                msgs.append(rec)
    return msgs


def load_state(agent: str) -> dict:
    state = MESH_DIR / f"state-{agent}.json"
    if not state.exists():
        return {"last_read": {}}
    try:
        return json.loads(state.read_text(encoding="utf-8"))
    except Exception:
        return {"last_read": {}}


def save_state(agent: str, state: dict) -> None:
    path = MESH_DIR / f"state-{agent}.json"
    data = (json.dumps(state, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    mode = "r+b" if path.exists() else "w+b"
    with path.open(mode) as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            f.truncate()
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def ack_inbox(agent: str, ids_to_ack: set[str]) -> int:
    """Mark ids acked, holding ONE exclusive lock across read → mutate → rewrite.

    Reading and rewriting under two separate locks leaves a window: a delivery
    that lands between them is inside the snapshot we then truncate away, so a
    message that was successfully delivered disappears. Holding the lock
    end-to-end serialises the append either fully before (we preserve it) or
    fully after (it appends to the rewritten file).
    """
    inbox = MESH_DIR / f"inbox-{agent}.jsonl"
    if not inbox.exists():
        return 0
    with inbox.open("r+b") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            msgs = []
            for line in f.read().decode("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    print(f"[read] skipped malformed line in {inbox.name}", file=sys.stderr)
                    continue
                if isinstance(rec, dict):
                    msgs.append(rec)
            n = 0
            for m in msgs:
                if m.get("id") in ids_to_ack and not m.get("acked"):
                    m["acked"] = True
                    n += 1
            f.seek(0)
            f.truncate()
            for m in msgs:
                f.write((json.dumps(m, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            # Flush before unlocking, same reason as the write path in send.py.
            f.flush()
            os.fsync(f.fileno())
            return n
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def show(msgs: list[dict], full: bool = False, agent: str | None = None) -> None:
    if not msgs:
        print("(no messages)")
        return
    for m in msgs:
        ack = " ✓" if m.get("acked") else ""
        print(f"[{m.get('ts')}] {m.get('from')} → {m.get('to')} "
              f"[{m.get('priority')}] id={m.get('id')}{ack}")
        body = (m.get("body") or "").strip()
        if full:
            print(body)
        else:
            preview = body.replace("\n", " ")[:120]
            if len(body) > 120:
                preview += "…"
            print(f"  {preview}")
        # A human behind a chat facade is not reading this terminal. Without this
        # line an agent answers locally, and looks mute to the person waiting.
        if agent and m.get("from") in HUMAN_FACADE_PEERS and not m.get("acked"):
            peer = m["from"]
            print(f"  ↳ reply through the bus — '{peer}' reads in a chat client, not here: "
                  f"bash {MESH_DIR}/mesh-send.sh {agent} {peer} <priority> \"...\"")
        print()


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in AGENTS:
        known = "|".join(sorted(AGENTS)) or "<no roster: run bootstrap.sh>"
        print(f"usage: {sys.argv[0]} <{known}> [--all|--unacked|--ack <id> ...]",
              file=sys.stderr)
        sys.exit(2)

    agent = sys.argv[1]
    args = sys.argv[2:]
    msgs = load_inbox(agent)
    state = load_state(agent)

    if args and args[0] == "--ack":
        ids_to_ack = set(args[1:])
        if not ids_to_ack:
            print("error: --ack requires at least one id", file=sys.stderr)
            sys.exit(2)
        n = ack_inbox(agent, ids_to_ack)
        render = MESH_DIR / "render.py"
        if render.exists():
            import subprocess

            subprocess.run([sys.executable, str(render)], check=False, capture_output=True)
        print(f"acked {n} message(s)")
        return

    if "--all" in args:
        print(f"=== inbox-{agent}.jsonl — {len(msgs)} message(s) total ===\n")
        show(msgs, full=True, agent=agent)
        return

    if "--unacked" in args:
        unacked = [m for m in msgs if not m.get("acked")]
        print(f"=== unacked in inbox-{agent} ({len(unacked)} message(s), cursor ignored) ===\n")
        show(unacked, full=True, agent=agent)
        return

    last_read = state.get("last_read", {})
    unread = [
        m for m in msgs
        if not m.get("acked")
        and _is_after_cursor(m.get("ts", ""), m.get("id"), last_read.get(m.get("from")))
    ]
    print(f"=== unread in inbox-{agent} ({len(unread)} message(s)) ===\n")
    show(unread, full=True, agent=agent)
    if unread:
        print("(cursor NOT advanced; use --ack <id> to mark messages handled)")


if __name__ == "__main__":
    main()
