#!/usr/bin/env python3
"""Append one message to a recipient's inbox.

Usage:
    python3 send.py <from> <to> <priority> <body...>
    python3 send.py alice bob normal "your build finished"
    python3 send.py alice bob normal --reply-to a1b2c3d4 "answering yours"

What it does, and nothing more:
  * validates both endpoints against the deployment's roster;
  * refuses a recipient the fleet policy has put out of play;
  * appends one JSON line to ``$MESH_HOME/inbox-<to>.jsonl`` under an exclusive
    lock, flushed and fsynced *before* the lock is released;
  * prints the id it wrote, so the caller can check delivery.

It does **not** push anything to the recipient: that is the watcher's job. The
split is deliberate — a message is durable on disk before anyone tries to wake
its reader, so a watcher that is down delays delivery instead of losing it.

Security note, because it is easy to miss: an inbox is read by an agent running
with tool permissions. **Writing to an inbox is close to executing code on the
recipient's behalf.** Every check here fails closed for that reason — an unknown
sender is refused rather than accepted under a default name.

Environment:
    MESH_HOME          bus directory (default ``~/mesh``)
    MESH_TZ            IANA timezone for timestamps (default: system local time)
    MESH_NO_REPLY=1    mark the message as one-way (no ack expected)
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from roster import FACADE_PEERS, SEND_PEERS as AGENTS, SYSTEM_PEERS  # noqa: E402

MESH_DIR = Path(os.environ.get("MESH_HOME", os.path.expanduser("~/mesh")))
#: Endpoints a fleet policy may reserve for the operator.
#:
#: Taken from the roster first — those are the deployment's declared human-facing
#: peers — and extended by MESH_HUMAN_FACADES for anything the roster does not
#: know. Reading the environment *alone* was a quiet trap: a mesh installed by
#: bootstrap declares its facades in the roster and sets no variable, so the
#: operator's own message to a paused agent was refused as if it came from
#: another agent, with an error telling them only the operator may write.
HUMAN_FACADES = frozenset(FACADE_PEERS) | frozenset(
    x.strip() for x in os.environ.get("MESH_HUMAN_FACADES", "").split(",") if x.strip()
)
FLEET_POLICY = MESH_DIR / "fleet-policy.json"
PRIORITIES = {"urgent", "high", "normal", "low"}
#: Largest body accepted. A message is typed into a terminal and read by an
#: agent whose context is finite: past a certain size it is not a message any
#: more, it is a file, and it should be written as one with a path in the body.
MAX_BODY_BYTES = 64 * 1024
#: Control bytes are refused rather than stripped. A body travels to a terminal
#: (`read.py` prints it, the watcher's notice is typed into a pane): an escape
#: sequence there can rewrite what a human sees, and NUL truncates the line for
#: whoever reads the file with C tooling. Tab and newline are ordinary text.
_CONTROL_BYTES = {c for c in range(0x00, 0x20) if c not in (0x09, 0x0A)} | {0x7F}
_PEER_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
# Topic rooms are created on the fly by the chat facade, so they cannot be in a
# roster that is generated at deploy time. They are namespaced instead, and the
# namespace is all the authority they get.
_TOPIC_RE = re.compile(r"^topic-[a-z0-9][a-z0-9-]{0,31}$")


def _load_legacy() -> dict[str, str]:
    """Old peer ids → current ones, from ``$MESH_HOME/legacy-ids.json``.

    Renaming an agent does not rename it in every cron, remote script and habit
    at once. Mapping the old id here keeps those senders working *and* writes
    the canonical id to disk, so the history does not fork. Fail-open: an
    unreadable map means no aliases, never a refused send.
    """
    try:
        with (MESH_DIR / "legacy-ids.json").open(encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str) and not k.startswith("_")}


LEGACY = _load_legacy()


def load_fleet_policy() -> dict:
    """Which recipients are currently out of play → ``{agent: {...}}``.

    Failure direction is chosen, not accidental: a missing **or broken** policy
    file applies *no* restriction. A policy that fails closed would cut the bus
    on a JSON typo and strand agents mid-task; the worst case in this direction
    is extra work reaching a paused agent — visible and reversible. An
    unreadable file is announced on stderr, because a guard that dissolves in
    silence is indistinguishable from a fleet with no policy at all.
    """
    try:
        with FLEET_POLICY.open(encoding="utf-8") as fh:
            agents = json.load(fh).get("agents") or {}
        return agents if isinstance(agents, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(
            f"warning: {FLEET_POLICY.name} unreadable ({exc}) — no fleet restriction "
            "applied to this message",
            file=sys.stderr,
        )
        return {}


def _timestamp() -> str:
    tzname = os.environ.get("MESH_TZ", "").strip()
    now = datetime.now()
    if tzname:
        try:
            from zoneinfo import ZoneInfo

            now = datetime.now(ZoneInfo(tzname))
        except Exception:
            now = datetime.now().astimezone()
    else:
        now = now.astimezone()
    ts = now.strftime("%Y-%m-%dT%H:%M:%S%z")
    return ts[:-2] + ":" + ts[-2:]  # +0200 → +02:00


def usage() -> None:
    print(__doc__.strip(), file=sys.stderr)
    sys.exit(2)


def main() -> None:
    if len(sys.argv) < 5:
        usage()
    _, from_a, to_a, priority, *rest = sys.argv

    # --reply-to <id>: the message this one answers. The API has always accepted
    # the field and it was dropped on the way to the bus, so every reply looked
    # like the start of a new thread. Parsed here rather than with argparse to
    # keep the body positional and free of flag syntax.
    reply_to = None
    if rest and rest[0] == "--reply-to":
        if len(rest) < 2:
            print("error: --reply-to needs a message id", file=sys.stderr)
            sys.exit(2)
        reply_to = rest[1]
        rest = rest[2:]
        if not re.fullmatch(r"[0-9a-f]{4,32}", reply_to or ""):
            print(f"error: invalid --reply-to id {reply_to!r}", file=sys.stderr)
            sys.exit(2)

    body = " ".join(rest).strip()

    from_a = LEGACY.get(from_a, from_a)
    to_a = LEGACY.get(to_a, to_a)

    if not _PEER_RE.match(from_a):
        print(f"error: invalid from peer '{from_a}' — must match ^[a-z][a-z0-9-]{{0,31}}$",
              file=sys.stderr)
        sys.exit(2)
    if from_a not in AGENTS and not _TOPIC_RE.match(from_a):
        print(f"error: unknown from peer '{from_a}' (known: {sorted(AGENTS)} or topic-<slug>)",
              file=sys.stderr)
        sys.exit(2)
    if to_a not in AGENTS and not _TOPIC_RE.match(to_a):
        print(f"error: unknown to peer '{to_a}' (known: {sorted(AGENTS)} or topic-<slug>)",
              file=sys.stderr)
        sys.exit(2)
    if to_a in SYSTEM_PEERS:
        # A system peer sends and is never read: it owns no inbox and cannot run
        # read.py. Accepting it as a recipient wrote a file nobody would ever
        # open while telling the sender "appended", which is the worst of both —
        # the message looks delivered and is simply gone.
        print(f"error: '{to_a}' is a system sender, not a recipient — it has no inbox",
              file=sys.stderr)
        sys.exit(2)
    if from_a == to_a:
        print("error: from and to are the same peer", file=sys.stderr)
        sys.exit(2)

    # A recipient under policy is refused AT SEND: nothing is written. Delivering
    # it silently would leave the sender believing it had dispatched work and
    # waiting for an answer that cannot come — and, on a mesh where arrival wakes
    # a sleeping agent, it would also wake the very agent that was put to rest.
    pol = load_fleet_policy().get(to_a) or {}
    if pol.get("inbound") == "human_only" and from_a not in HUMAN_FACADES:
        mode = pol.get("mode", "paused")
        print(
            f"error: '{to_a}' is {mode} — only the operator may write to it. "
            f"Reason: {pol.get('reason', 'fleet policy')}. "
            f"REFUSED, nothing was written to its inbox: do not dispatch to it — do the "
            f"work yourself, or remove its entry from {FLEET_POLICY.name}.",
            file=sys.stderr,
        )
        sys.exit(3)

    if priority not in PRIORITIES:
        print(f"error: unknown priority '{priority}' (allowed: {sorted(PRIORITIES)})",
              file=sys.stderr)
        sys.exit(2)
    if not body:
        print("error: empty body", file=sys.stderr)
        sys.exit(2)

    encoded = body.encode("utf-8")
    if len(encoded) > MAX_BODY_BYTES:
        print(f"error: body is {len(encoded)} bytes, over the {MAX_BODY_BYTES} limit. "
              f"Write it to a file and send the path instead — a body this size is "
              f"unreadable in a terminal and eats the recipient's context.",
              file=sys.stderr)
        sys.exit(2)
    offending = sorted({b for b in encoded if b in _CONTROL_BYTES})
    if offending:
        print(f"error: body contains control bytes {[hex(b) for b in offending]}. "
              f"They are refused, not stripped: the body is printed to terminals, "
              f"where an escape sequence can rewrite what a human reads.",
              file=sys.stderr)
        sys.exit(2)

    ts = _timestamp()
    # Seconds plus a random suffix: two messages from the same sender inside one
    # minute used to collide on the id.
    msg_id = hashlib.sha1(
        f"{from_a}|{ts}|{body[:200]}|{secrets.token_hex(2)}".encode("utf-8")
    ).hexdigest()[:8]

    entry = {
        "id": msg_id,
        "ts": ts,
        "from": from_a,
        "to": to_a,
        "priority": priority,
        "body": body,
        "acked": False,
    }
    if reply_to:
        entry["reply_to"] = reply_to
    # One-way notification (health checks, alerts): the recipient acts on it but
    # never answers, so the no-ack tracker must not nag about it forever.
    if os.environ.get("MESH_NO_REPLY") == "1":
        entry["no_reply"] = True

    MESH_DIR.mkdir(parents=True, exist_ok=True)
    inbox = MESH_DIR / f"inbox-{to_a}.jsonl"
    with inbox.open("a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
            # Flush UNDER the lock. `with` flushes on block exit — after the
            # unlock below — so the deferred append could land inside a
            # concurrent ack rewrite (after its truncate, before its write) and
            # be clobbered: delivered, then lost.
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

    print(f"appended to {inbox.name}: id={msg_id} ts={ts}")

    render = MESH_DIR / "render.py"
    if render.exists():
        subprocess.run([sys.executable, str(render)], check=False, capture_output=True)


if __name__ == "__main__":
    main()
