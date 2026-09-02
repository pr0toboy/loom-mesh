"""Low-level mesh I/O: read JSONL inboxes and wrap mesh-send-checked.sh."""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path

MESH_DIR = Path(os.environ.get("MESH_HOME", os.path.expanduser("~/mesh")))
MESH_SEND = MESH_DIR / "mesh-send-checked.sh"
READ_PY = MESH_DIR / "read.py"


def _load_last_read(agent: str) -> dict[str, str]:
    """Return {sender: last_read_ts} from state-<agent>.json."""
    state_path = MESH_DIR / f"state-{agent}.json"
    if not state_path.exists():
        return {}
    try:
        data = json.loads(state_path.read_text())
        last_read = data.get("last_read", {})
        return {sender: v.get("ts", "") for sender, v in last_read.items() if isinstance(v, dict)}
    except Exception:
        return {}


def read_inbox(agent: str, unread_only: bool = True, limit: int = 50) -> list[dict]:
    inbox_path = MESH_DIR / f"inbox-{agent}.jsonl"
    if not inbox_path.exists():
        return []

    last_read = _load_last_read(agent) if unread_only else {}

    messages = []
    with inbox_path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if unread_only:
                sender = m.get("from", "")
                read_ts = last_read.get(sender, "")
                if read_ts and m.get("ts", "") <= read_ts:
                    continue
            messages.append(m)

    messages.sort(key=lambda m: m.get("ts", ""), reverse=True)
    return messages[:limit]


def ack_message(agent: str, message_id: str) -> int:
    result = subprocess.run(
        ["python3", str(READ_PY), agent, "--ack", message_id],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return 1


def _run_send(from_: str, to: str, priority: str, body: str,
              reply_to: str | None = None) -> dict:
    import uuid, datetime
    if not MESH_SEND.exists():
        raise RuntimeError(
            f"mesh-send-checked.sh not found at {MESH_SEND} — run bootstrap.sh so it "
            f"installs the bus into MESH_HOME")
    # The flag goes before the body, which is positional and may itself start
    # with a dash.
    argv = ["bash", str(MESH_SEND), from_, to, priority]
    if reply_to:
        argv += ["--reply-to", reply_to]
    argv.append(body)
    result = subprocess.run(argv, capture_output=True, text=True)
    # exit 0: sent + pushed live; exit 2: sent, recipient busy; exit 3: sent, watcher may be down; exit 1: failure
    if result.returncode == 1:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    m = re.search(r"id=([a-f0-9]+)", result.stdout)
    real_id = m.group(1) if m else uuid.uuid4().hex[:8]
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {"id": real_id, "queued_at": ts, "delivered_live": result.returncode == 0}


def send_message(from_: str, to: str, priority: str, body: str, reply_to: str | None = None) -> dict:
    return _run_send(from_, to, priority, body, reply_to)


async def send_message_async(from_: str, to: str, priority: str, body: str, reply_to: str | None = None) -> dict:
    return await asyncio.to_thread(_run_send, from_, to, priority, body, reply_to)
