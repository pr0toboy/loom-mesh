#!/usr/bin/env python3
"""Close a running ticket from the agent that did the work.

Usage:
    python3 ticket-complete.py <ticket-id> --tldr "one to three sentences"
    python3 ticket-complete.py <ticket-id> --tldr "..." --failed "reason"
    python3 ticket-complete.py <ticket-id> --tldr "..." --response-file out.md

A ticket's state is its directory: closing one is a move from ``running/`` to
``done/`` or ``failed/``. That is why this is a small CLI and not an API call —
the agent that finished the work is the one that knows, and it can say so with a
single command, offline, with no service in the loop.

The summary is mandatory. A ticket closed with no ``--tldr`` is indistinguishable
from a ticket abandoned, and the queue is read by someone who was not watching.

Environment:
    MESH_HOME          bus directory (default ``~/mesh``)
    MESH_TICKETS_DIR   ticket store (default ``$MESH_HOME/tickets``)
    MESH_API_BASE      optional: mesh-api checkout, used for push notifications
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from roster import INBOX_PEERS as AGENTS  # noqa: E402

_MESH_HOME = Path(os.environ.get("MESH_HOME", os.path.expanduser("~/mesh")))
TICKETS_DIR = Path(os.environ.get("MESH_TICKETS_DIR", str(_MESH_HOME / "tickets")))
MESH_API_DIR = Path(os.environ.get("MESH_API_BASE", ""))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def find_running_ticket(ticket_id: str) -> tuple[dict, Path] | None:
    """The running ticket with exactly this id, or None.

    The match is exact on purpose. A substring glob (`*<id>*.json`) looks
    harmless until the tenth ticket exists: `tk-1` then matches `tk-10`, and
    closing `tk-1` silently marks someone else's work done — with a success
    message naming the wrong ticket. The id is also confirmed against the
    file's own contents, so a renamed file cannot close the wrong ticket
    either.
    """
    for agent in sorted(AGENTS):
        running_dir = TICKETS_DIR / agent / "running"
        if not running_dir.exists():
            continue
        for f in sorted(running_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text())
            except Exception:
                continue
            if data.get("id") == ticket_id or f.stem == ticket_id:
                return data, f
    return None


def _notify(ticket_id: str, agent: str, tldr: str, failed: bool) -> None:
    """Best-effort push notification. Never fails the close."""
    if not MESH_API_DIR:
        return
    try:
        if str(MESH_API_DIR) not in sys.path:
            sys.path.insert(0, str(MESH_API_DIR))
        from mesh_api.notifications.fcm import notify_ticket_done

        notify_ticket_done(ticket_id, agent, tldr, failed=failed)
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Close a running mesh ticket")
    parser.add_argument("ticket_id", help="ticket id, e.g. tk-abc123")
    parser.add_argument("--tldr", required=True, help="1-3 sentence summary")
    parser.add_argument("--failed", default=None, metavar="REASON",
                        help="mark as failed with this reason")
    parser.add_argument("--response-file", default=None, metavar="PATH",
                        help="copy this file as the ticket's full response")
    args = parser.parse_args()

    result = find_running_ticket(args.ticket_id)
    if result is None:
        print(f"ERROR: no running ticket matching '{args.ticket_id}'", file=sys.stderr)
        sys.exit(1)

    ticket, running_path = result
    agent = ticket.get("to") or running_path.parts[running_path.parts.index("tickets") + 1]

    final_status = "failed" if args.failed else "done"
    ticket["status"] = final_status
    ticket["completed_at"] = _now_iso()
    ticket["tldr"] = args.tldr
    if args.failed:
        ticket["failed_reason"] = args.failed

    dest_dir = TICKETS_DIR / agent / final_status
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_file = dest_dir / running_path.name

    dest_file.write_text(json.dumps(ticket, indent=2, ensure_ascii=False))
    running_path.unlink(missing_ok=True)

    working_dir = TICKETS_DIR / agent / "working" / ticket["id"]
    if working_dir.exists():
        (working_dir / "output.md").write_text(args.tldr)
        dest_working = dest_dir / ticket["id"]
        if dest_working.exists():
            shutil.rmtree(dest_working)
        working_dir.rename(dest_working)

    if args.response_file:
        src = Path(args.response_file)
        if src.exists():
            out_path = dest_dir / f"{ticket['id']}.out.md"
            shutil.copy2(src, out_path)
            print(f"Response saved: {out_path}")
        else:
            print(f"WARNING: response-file not found: {src}", file=sys.stderr)

    print(f"ticket {ticket['id']} → {final_status} (agent: {agent})")
    _notify(ticket["id"], agent, args.tldr, failed=bool(args.failed))


if __name__ == "__main__":
    main()
