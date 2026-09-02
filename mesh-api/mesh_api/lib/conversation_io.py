"""Aggregate messages + tickets for a single agent into a unified conversation timeline."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..peers import DEFAULT_SENDER
from .message_summarizer import summarize as _summarize_body

MESH_DIR = Path(os.environ.get("MESH_HOME", os.path.expanduser("~/mesh")))
TICKETS_DIR = Path(os.environ.get("MESH_TICKETS_DIR", str(MESH_DIR / "tickets")))
ALL_STATES = ("draft", "armed", "blocked", "queued", "running", "done", "failed", "cancelled")


def _parse_ts(ts_str: str | None) -> datetime | None:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return None


def _ts_ms(dt: datetime | None) -> Optional[int]:
    if dt is None:
        return None
    return int(dt.timestamp() * 1000)


def _messages_for_agent(agent: str) -> list[dict]:
    items: list[dict] = []

    # inbox-{agent}.jsonl → messages where agent is the recipient (incoming)
    inbox_path = MESH_DIR / f"inbox-{agent}.jsonl"
    if inbox_path.exists():
        try:
            with inbox_path.open() as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        m = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    dt = _parse_ts(m.get("ts"))
                    if dt is None:
                        continue
                    items.append({
                        "kind": "message",
                        "id": m.get("id", ""),
                        "ts": m.get("ts", ""),
                        "ts_ms": _ts_ms(dt),
                        "direction": "incoming",
                        "from": m.get("from", ""),
                        "to": m.get("to", agent),
                        "priority": m.get("priority", "normal"),
                        "body": m.get("body", ""),
                        "reply_to": m.get("reply_to"),
                        "acked": bool(m.get("acked", False)),
                        "summary": _summarize_body(m.get("body") or "", m.get("from", "")),
                    })
        except OSError:
            pass

    # All other inboxes → messages where agent is the sender (outgoing)
    try:
        all_inboxes = list(MESH_DIR.glob("inbox-*.jsonl"))
    except OSError:
        all_inboxes = []

    for inbox in all_inboxes:
        if inbox.name == f"inbox-{agent}.jsonl":
            continue
        try:
            with inbox.open() as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        m = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if m.get("from") != agent:
                        continue
                    dt = _parse_ts(m.get("ts"))
                    if dt is None:
                        continue
                    items.append({
                        "kind": "message",
                        "id": m.get("id", ""),
                        "ts": m.get("ts", ""),
                        "ts_ms": _ts_ms(dt),
                        "direction": "outgoing",
                        "from": m.get("from", agent),
                        "to": m.get("to", ""),
                        "priority": m.get("priority", "normal"),
                        "body": m.get("body", ""),
                        "reply_to": m.get("reply_to"),
                        "acked": bool(m.get("acked", False)),
                        "summary": _summarize_body(m.get("body") or "", m.get("from", "")),
                    })
        except OSError:
            continue

    return items


def _tickets_for_agent(agent: str) -> list[dict]:
    items: list[dict] = []
    base = TICKETS_DIR / agent
    if not base.exists():
        return items

    for state in ALL_STATES:
        state_dir = base / state
        if not state_dir.exists():
            continue
        for ticket_file in state_dir.glob("*.json"):
            try:
                data = json.loads(ticket_file.read_text())
            except Exception:
                continue
            queued_at = data.get("queued_at", "")
            dt = _parse_ts(queued_at)
            if dt is None:
                continue
            prompt = data.get("prompt", "")
            items.append({
                "kind": "ticket",
                "id": data.get("id", ""),
                "ts": queued_at,
                "ts_ms": _ts_ms(dt),
                "from": data.get("from", DEFAULT_SENDER),
                "to": data.get("to", agent),
                "status": data.get("status", ""),
                "priority": data.get("priority", "normal"),
                "prompt_preview": prompt[:280],
                "tldr": data.get("tldr"),
                "queued_at": queued_at,
                "started_at": data.get("started_at"),
                "completed_at": data.get("completed_at"),
                "dispatch_mode": data.get("dispatch_mode", "draft"),
                "depends_on": data.get("depends_on", []),
            })

    return items


def get_conversation(agent: str, limit: int = 100, before: Optional[str] = None) -> dict:
    items = _messages_for_agent(agent) + _tickets_for_agent(agent)

    if before:
        before_dt = _parse_ts(before)
        if before_dt is not None:
            # Compare via ts_ms to handle mixed UTC/+02:00 timestamps correctly
            before_ms = _ts_ms(before_dt)
            items = [it for it in items if (it.get("ts_ms") or 0) < before_ms]
        else:
            # Fallback to string comparison if parsing fails
            items = [it for it in items if it["ts"] < before]

    items.sort(key=lambda it: it.get("ts_ms") or 0, reverse=True)

    page = items[:limit]
    next_before = page[-1]["ts"] if len(items) > limit else None

    return {"agent": agent, "items": page, "next_before": next_before}
