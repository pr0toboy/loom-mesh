"""Tests for the server-side status watcher and WS ConnectionManager."""
from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("MESH_TOKENS_PATH", "/tmp/test-mesh-tokens.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_status(alive=True, idle=True, tickets_running=0):
    return {"alive": alive, "idle": idle, "tickets_running": tickets_running,
            "tickets_pending": 0, "last_activity": None}


def _make_ws(*, subscribed_topics: set[str] | None = None):
    ws = AsyncMock()
    ws.send_json = AsyncMock()
    return ws


# ---------------------------------------------------------------------------
# ConnectionManager unit tests
# ---------------------------------------------------------------------------

def test_manager_add_remove():
    from mesh_api.routes.stream import _ConnectionManager
    m = _ConnectionManager()
    ws = _make_ws()
    m.add(ws, {"status", "inbox:alice"})
    assert ws in m._clients
    m.remove(ws)
    assert ws not in m._clients


def test_manager_broadcast_reaches_subscribed():
    from mesh_api.routes.stream import _ConnectionManager

    m = _ConnectionManager()
    ws_status = _make_ws()
    ws_other = _make_ws()
    m.add(ws_status, {"status"})
    m.add(ws_other, {"inbox:bob"})

    payload = {"topic": "status", "event": "agent_changed", "payload": {}}
    asyncio.run(m.broadcast_to_topic("status", payload))

    ws_status.send_json.assert_awaited_once_with(payload)
    ws_other.send_json.assert_not_awaited()


def test_manager_broadcast_multiple_clients():
    from mesh_api.routes.stream import _ConnectionManager

    m = _ConnectionManager()
    clients = [_make_ws() for _ in range(3)]
    for ws in clients:
        m.add(ws, {"status"})

    payload = {"topic": "status", "event": "agent_changed", "payload": {}}
    asyncio.run(m.broadcast_to_topic("status", payload))

    for ws in clients:
        ws.send_json.assert_awaited_once_with(payload)


def test_manager_broadcast_removes_dead_client():
    from mesh_api.routes.stream import _ConnectionManager

    m = _ConnectionManager()
    ws_dead = _make_ws()
    ws_live = _make_ws()
    ws_dead.send_json = AsyncMock(side_effect=RuntimeError("disconnected"))
    m.add(ws_dead, {"status"})
    m.add(ws_live, {"status"})

    asyncio.run(m.broadcast_to_topic("status", {}))

    assert ws_dead not in m._clients
    assert ws_live in m._clients


# ---------------------------------------------------------------------------
# _status_watcher_loop unit tests
# ---------------------------------------------------------------------------

def _run_watcher_one_tick(
    agent_statuses: dict[str, dict],
    initial_snapshot: dict[str, dict] | None = None,
    *,
    broadcast_calls: list | None = None,
):
    """Run the watcher loop for exactly one tick (one sleep period).

    Patches asyncio.sleep to return immediately on first call and raise
    CancelledError on the second (so the loop exits cleanly).
    """
    import mesh_api.routes.stream as stream_mod

    # Reset snapshot
    stream_mod._last_status_snapshot.clear()
    if initial_snapshot:
        stream_mod._last_status_snapshot.update(initial_snapshot)

    captured: list[dict] = [] if broadcast_calls is None else broadcast_calls

    sleep_call_count = 0

    async def fake_sleep(_):
        nonlocal sleep_call_count
        sleep_call_count += 1
        if sleep_call_count > 1:
            raise asyncio.CancelledError

    async def fake_broadcast(topic, data):
        if topic == "status":
            captured.append(data)

    with (
        patch("mesh_api.routes.stream.asyncio.sleep", side_effect=fake_sleep),
        patch("mesh_api.routes.stream.get_agent_status",
              side_effect=lambda agent: agent_statuses.get(agent, _make_status())),
        patch.object(stream_mod.manager, "broadcast_to_topic",
                     side_effect=fake_broadcast),
    ):
        try:
            asyncio.run(stream_mod._status_watcher_loop())
        except asyncio.CancelledError:
            pass

    return captured


def test_watcher_no_event_on_first_tick():
    """First tick populates snapshot but emits no event (no previous state)."""
    events = _run_watcher_one_tick({"alice": _make_status(alive=True, idle=True)})
    assert events == []


def test_watcher_no_event_when_nothing_changed():
    s = _make_status(alive=True, idle=True, tickets_running=0)
    events = _run_watcher_one_tick(
        {"alice": s},
        initial_snapshot={"alice": dict(s)},
    )
    assert events == []


def test_watcher_emits_event_on_alive_change():
    prev = _make_status(alive=True, idle=True)
    curr = _make_status(alive=False, idle=False)
    events = _run_watcher_one_tick(
        {"alice": curr},
        initial_snapshot={"alice": prev},
    )
    assert len(events) == 1
    ev = events[0]
    assert ev["topic"] == "status"
    assert ev["event"] == "agent_changed"
    assert ev["payload"]["agent"] == "alice"
    assert ev["payload"]["before"]["alive"] is True
    assert ev["payload"]["after"]["alive"] is False


def test_watcher_emits_event_on_idle_change():
    prev = _make_status(alive=True, idle=True)
    curr = _make_status(alive=True, idle=False)
    events = _run_watcher_one_tick(
        {"alice": curr},
        initial_snapshot={"alice": prev},
    )
    assert len(events) == 1
    assert events[0]["payload"]["agent"] == "alice"


def test_watcher_emits_event_on_tickets_running_change():
    prev = _make_status(alive=True, idle=True, tickets_running=0)
    curr = _make_status(alive=True, idle=False, tickets_running=1)
    events = _run_watcher_one_tick(
        {"alice": curr},
        initial_snapshot={"alice": prev},
    )
    assert len(events) == 1


def test_watcher_one_event_per_changed_agent():
    """Only the agent that changed triggers an event."""
    snapshot = {
        "alice": _make_status(alive=True, idle=True),
        "bob": _make_status(alive=True, idle=True),
    }
    current = {
        "alice": _make_status(alive=True, idle=False),  # changed
        "bob": _make_status(alive=True, idle=True),  # unchanged
    }
    events = _run_watcher_one_tick(current, initial_snapshot=snapshot)
    assert len(events) == 1
    assert events[0]["payload"]["agent"] == "alice"
