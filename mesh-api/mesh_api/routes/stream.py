"""WebSocket /stream endpoint with inotify-driven push events."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from ..auth import verify_ws_token
from ..lib.status import get_agent_status

router = APIRouter()

MESH_DIR = Path(os.environ.get("MESH_HOME", os.path.expanduser("~/mesh")))
TICKETS_DIR = Path(os.environ.get("MESH_TICKETS_DIR", str(MESH_DIR / "tickets")))
from ..peers import AGENTS

_STATUS_POLL_INTERVAL = 5  # seconds between server-side status polls


class _ConnectionManager:
    """Tracks active WS connections and their subscribed topics for broadcast."""

    def __init__(self) -> None:
        self._clients: dict[WebSocket, set[str]] = {}

    def add(self, ws: WebSocket, topics: set[str]) -> None:
        self._clients[ws] = topics

    def remove(self, ws: WebSocket) -> None:
        self._clients.pop(ws, None)

    async def broadcast_to_topic(self, topic: str, data: dict) -> None:
        dead: list[WebSocket] = []
        for ws, topics in list(self._clients.items()):
            if topic in topics:
                try:
                    await ws.send_json(data)
                except Exception:
                    dead.append(ws)
        for ws in dead:
            self.remove(ws)


manager = _ConnectionManager()
_last_status_snapshot: dict[str, dict] = {}


async def _status_watcher_loop() -> None:
    """Server-side task: poll agent status every 5s, broadcast diffs to WS clients."""
    while True:
        await asyncio.sleep(_STATUS_POLL_INTERVAL)
        for agent in AGENTS:
            try:
                current = get_agent_status(agent)
            except Exception:
                continue
            prev = _last_status_snapshot.get(agent)
            _last_status_snapshot[agent] = current
            if prev is None:
                continue
            if (prev.get("alive") != current.get("alive")
                    or prev.get("idle") != current.get("idle")
                    or prev.get("tickets_running") != current.get("tickets_running")):
                await manager.broadcast_to_topic("status", {
                    "topic": "status",
                    "event": "agent_changed",
                    "payload": {
                        "agent": agent,
                        "before": prev,
                        "after": current,
                    },
                })

# watchfiles is optional — fall back to polling if not available
try:
    from watchfiles import awatch
    HAS_WATCHFILES = True
except ImportError:
    HAS_WATCHFILES = False


async def _poll_inbox_loop(ws: WebSocket, topics: set[str]) -> None:
    """Fallback polling loop (30s) when watchfiles is unavailable."""
    while True:
        await asyncio.sleep(30)
        ts = datetime.now(timezone.utc).isoformat()
        try:
            await ws.send_json({"topic": "status", "event": "heartbeat", "ts": ts})
        except Exception:
            break


async def _heartbeat_loop(ws: WebSocket) -> None:
    """Emit a heartbeat every 30s so the Android watchdog doesn't close the WS."""
    while True:
        await asyncio.sleep(30)
        try:
            await ws.send_json({"topic": "status", "event": "heartbeat",
                                "ts": datetime.now(timezone.utc).isoformat()})
        except Exception:
            return


async def _watch_loop(ws: WebSocket, topics: set[str]) -> None:
    """inotify-driven event loop via watchfiles, with parallel heartbeat task."""
    watch_paths: set[Path] = set()
    for topic in topics:
        if topic == "status":
            continue
        if topic.startswith("inbox:"):
            watch_paths.add(MESH_DIR)
        elif topic.startswith("tickets:"):
            agent = topic.split(":", 1)[1]
            watch_paths.add(TICKETS_DIR / agent)

    if not watch_paths:
        await _poll_inbox_loop(ws, topics)
        return

    hb_task = asyncio.create_task(_heartbeat_loop(ws))
    try:
        async for changes in awatch(*watch_paths):
            for change_type, path_str in changes:
                path = Path(path_str)
                ts = datetime.now(timezone.utc).isoformat()
                if path.name.startswith("inbox-"):
                    agent = path.name.removeprefix("inbox-").removesuffix(".jsonl")
                    topic = f"inbox:{agent}"
                    if topic not in topics:
                        continue
                    try:
                        await ws.send_json({"topic": topic, "event": "new_message", "ts": ts})
                    except Exception:
                        return
                elif "tickets" in path.parts:
                    for ag in AGENTS:
                        if f"/{ag}/" in path_str or path_str.endswith(f"/{ag}"):
                            topic = f"tickets:{ag}"
                            if topic not in topics:
                                break
                            state = path.parent.name
                            if state == "running":
                                event = "started"
                            elif state in ("done", "failed", "cancelled"):
                                event = state
                            elif state in ("blocked", "armed", "queued"):
                                event = state
                            else:
                                event = "updated"
                            try:
                                await ws.send_json({"topic": topic, "event": event,
                                                    "file": path.name, "ts": ts})
                            except Exception:
                                return
                            break
    finally:
        hb_task.cancel()


@router.websocket("/stream")
async def stream(ws: WebSocket, token: str = Query(None)):
    # Prefer Authorization: Bearer <token> header over query param
    auth_header = ws.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    if not verify_ws_token(token):
        await ws.close(code=1008)
        return

    await ws.accept()

    try:
        # Wait for subscription message
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
        msg = json.loads(raw)
        topics: set[str] = set(msg.get("subscribe", []))
    except (asyncio.TimeoutError, json.JSONDecodeError, WebSocketDisconnect):
        await ws.close(code=1003)
        return

    manager.add(ws, topics)
    try:
        if HAS_WATCHFILES and topics - {"status"}:
            await _watch_loop(ws, topics)
        else:
            await _poll_inbox_loop(ws, topics)
    except WebSocketDisconnect:
        pass
    finally:
        manager.remove(ws)
