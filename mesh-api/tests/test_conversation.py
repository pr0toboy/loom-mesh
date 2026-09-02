"""Tests for GET /conversation/{agent}."""
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("MESH_TOKENS_PATH", "/tmp/test-mesh-tokens.json")


def _make_token_file(path: str, token: str = "test-token-abc") -> None:
    Path(path).write_text(json.dumps([{"name": "test", "token": token}]))
    os.chmod(path, 0o600)


@pytest.fixture(autouse=True)
def patch_paths(tmp_path, monkeypatch):
    tokens_file = str(tmp_path / "api-tokens.json")
    _make_token_file(tokens_file)
    monkeypatch.setenv("MESH_TOKENS_PATH", tokens_file)

    import mesh_api.lib.tickets_io as tio
    monkeypatch.setattr(tio, "TICKETS_DIR", tmp_path / "tickets")
    monkeypatch.setattr(tio, "AGENTS", ("alice", "bob", "yara", "dave"))

    import mesh_api.lib.conversation_io as cio
    monkeypatch.setattr(cio, "MESH_DIR", tmp_path / "mesh")
    monkeypatch.setattr(cio, "TICKETS_DIR", tmp_path / "tickets")

    import mesh_api.auth as auth
    from pathlib import Path as P
    monkeypatch.setattr(auth, "TOKENS_PATH", P(tokens_file))

    # Create mesh dir so glob doesn't fail
    (tmp_path / "mesh").mkdir()
    (tmp_path / "tickets").mkdir()


@pytest.fixture()
def client(patch_paths):
    from mesh_api.main import app
    with TestClient(app) as c:
        yield c


TOKEN = "test-token-abc"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _write_inbox(tmp_path: Path, agent: str, messages: list[dict]) -> None:
    inbox = tmp_path / "mesh" / f"inbox-{agent}.jsonl"
    with inbox.open("a") as fh:
        for m in messages:
            fh.write(json.dumps(m) + "\n")


def _write_ticket(tmp_path: Path, agent: str, status: str, ticket: dict) -> None:
    d = tmp_path / "tickets" / agent / status
    d.mkdir(parents=True, exist_ok=True)
    tid = ticket["id"]
    (d / f"{tid}.json").write_text(json.dumps(ticket))


def test_conversation_unknown_agent(client):
    r = client.get("/conversation/nobody", headers=AUTH)
    assert r.status_code == 404


def test_conversation_requires_auth(client):
    r = client.get("/conversation/alice")
    assert r.status_code == 401


def test_conversation_empty(client):
    """Agent with no messages and no tickets → empty items list."""
    r = client.get("/conversation/alice", headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert data["agent"] == "alice"
    assert data["items"] == []
    assert data["next_before"] is None


def test_conversation_mixed_items_sort(tmp_path, client):
    """Messages and tickets are merged and sorted by ts descending."""
    _write_inbox(tmp_path, "alice", [
        {
            "id": "msg-001",
            "from": "bob", "to": "alice",
            "priority": "normal",
            "body": "Hello",
            "ts": "2026-05-24T10:00+02:00",
        },
        {
            "id": "msg-002",
            "from": "bob", "to": "alice",
            "priority": "normal",
            "body": "World",
            "ts": "2026-05-24T12:00+02:00",
        },
    ])
    _write_ticket(tmp_path, "alice", "draft", {
        "id": "tk-aaaaaa",
        "to": "alice",
        "from": "user-web",
        "queued_at": "2026-05-24T11:00+02:00",
        "started_at": None,
        "completed_at": None,
        "status": "draft",
        "dispatch_mode": "draft",
        "priority": "normal",
        "prompt": "Faire quelque chose",
        "tldr": None,
        "depends_on": [],
        "parent_ticket_id": None,
    })

    r = client.get("/conversation/alice", headers=AUTH)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 3

    # Most recent first
    assert items[0]["ts"] == "2026-05-24T12:00+02:00"   # msg-002
    assert items[1]["ts"] == "2026-05-24T11:00+02:00"   # ticket
    assert items[2]["ts"] == "2026-05-24T10:00+02:00"   # msg-001

    kinds = [it["kind"] for it in items]
    assert kinds == ["message", "ticket", "message"]


def test_conversation_direction(tmp_path, client):
    """Incoming = agent is to, outgoing = agent is from."""
    # Message TO alice (incoming)
    _write_inbox(tmp_path, "alice", [{
        "id": "msg-in",
        "from": "bob", "to": "alice",
        "priority": "normal",
        "body": "Hi Alice",
        "ts": "2026-05-24T10:00+02:00",
    }])
    # Message FROM alice to bob → goes in inbox-bob (outgoing for alice)
    _write_inbox(tmp_path, "bob", [{
        "id": "msg-out",
        "from": "alice", "to": "bob",
        "priority": "normal",
        "body": "Hi Bob",
        "ts": "2026-05-24T09:00+02:00",
    }])

    r = client.get("/conversation/alice", headers=AUTH)
    assert r.status_code == 200
    items = r.json()["items"]
    by_id = {it["id"]: it for it in items}

    assert by_id["msg-in"]["direction"] == "incoming"
    assert by_id["msg-out"]["direction"] == "outgoing"


def test_conversation_pagination_before(tmp_path, client):
    """before=<ts> filters out items at or after that timestamp."""
    _write_inbox(tmp_path, "alice", [
        {"id": "msg-old", "from": "dave", "to": "alice", "priority": "normal",
         "body": "old", "ts": "2026-05-24T08:00+02:00"},
        {"id": "msg-new", "from": "dave", "to": "alice", "priority": "normal",
         "body": "new", "ts": "2026-05-24T10:00+02:00"},
    ])

    # before=09:00 should exclude msg-new (10:00 >= 09:00), keep msg-old (08:00 < 09:00)
    r = client.get("/conversation/alice?before=2026-05-24T09:00+02:00", headers=AUTH)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == "msg-old"


def test_conversation_pagination_next_before(tmp_path, client):
    """next_before is set when there are more items than limit."""
    for i in range(5):
        _write_inbox(tmp_path, "alice", [{
            "id": f"msg-{i:02d}",
            "from": "bob", "to": "alice",
            "priority": "normal",
            "body": f"msg {i}",
            "ts": f"2026-05-24T{10 + i:02d}:00+02:00",
        }])

    r = client.get("/conversation/alice?limit=3", headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert len(data["items"]) == 3
    # Most recent first: 14:00, 13:00, 12:00 → next_before = 12:00
    assert data["next_before"] == "2026-05-24T12:00+02:00"
    assert data["items"][0]["ts"] == "2026-05-24T14:00+02:00"


def test_conversation_ticket_fields(tmp_path, client):
    """Ticket items expose all required fields."""
    _write_ticket(tmp_path, "alice", "done", {
        "id": "tk-bbbbbb",
        "to": "alice",
        "from": "user-web",
        "queued_at": "2026-05-24T10:00+02:00",
        "started_at": "2026-05-24T10:01+02:00",
        "completed_at": "2026-05-24T10:30+02:00",
        "status": "done",
        "dispatch_mode": "armed",
        "priority": "high",
        "prompt": "A" * 300,
        "tldr": "Done successfully",
        "depends_on": ["tk-cccccc"],
        "parent_ticket_id": None,
    })

    r = client.get("/conversation/alice", headers=AUTH)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    t = items[0]
    assert t["kind"] == "ticket"
    assert t["id"] == "tk-bbbbbb"
    assert t["status"] == "done"
    assert t["tldr"] == "Done successfully"
    assert len(t["prompt_preview"]) == 280   # truncated at 280
    assert t["depends_on"] == ["tk-cccccc"]
    assert t["ts_ms"] is not None
    assert isinstance(t["ts_ms"], int)


def test_conversation_message_acked_false_by_default(tmp_path, client):
    """Message without acked field → acked=false in conversation response."""
    _write_inbox(tmp_path, "alice", [{
        "id": "msg-unacked",
        "from": "user-web", "to": "alice",
        "priority": "normal",
        "body": "Did you get it?",
        "ts": "2026-05-24T10:00+02:00",
    }])
    r = client.get("/conversation/alice", headers=AUTH)
    assert r.status_code == 200
    items = r.json()["items"]
    msg = next(it for it in items if it["id"] == "msg-unacked")
    assert msg["acked"] is False


def test_conversation_message_acked_true_after_ack(tmp_path, client):
    """Message with acked=true in JSONL → acked=true in conversation response."""
    _write_inbox(tmp_path, "alice", [{
        "id": "msg-acked",
        "from": "user-web", "to": "alice",
        "priority": "normal",
        "body": "Message lu",
        "ts": "2026-05-24T10:00+02:00",
        "acked": True,
    }])
    r = client.get("/conversation/alice", headers=AUTH)
    assert r.status_code == 200
    items = r.json()["items"]
    msg = next(it for it in items if it["id"] == "msg-acked")
    assert msg["acked"] is True


def test_conversation_acked_outgoing(tmp_path, client):
    """Outgoing message acked by recipient → acked=true in sender's conversation."""
    # alice sent to bob; bob acked it
    _write_inbox(tmp_path, "bob", [{
        "id": "msg-out-acked",
        "from": "alice", "to": "bob",
        "priority": "normal",
        "body": "Salut Bob",
        "ts": "2026-05-24T10:00+02:00",
        "acked": True,
    }])
    r = client.get("/conversation/alice", headers=AUTH)
    assert r.status_code == 200
    items = r.json()["items"]
    msg = next(it for it in items if it["id"] == "msg-out-acked")
    assert msg["direction"] == "outgoing"
    assert msg["acked"] is True
