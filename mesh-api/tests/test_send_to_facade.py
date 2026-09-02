"""Tests for user-web as a message destination (return channel)."""
import json
import os
from pathlib import Path
from unittest.mock import patch

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
    # Every route imports ALL_PEERS for itself, so patching the send module alone
    # left GET /inbox/<agent> and /conversation/<agent> answering 422. Without all
    # three patches the test would depend on the roster of whatever mesh is
    # deployed here and break the day a name changes — the tests declare their
    # own fleet.
    _PEERS = frozenset({"alice", "bob", "yara", "dave", "user-web"})
    import mesh_api.routes.send as send_mod
    import mesh_api.routes.inbox as inbox_mod
    import mesh_api.routes.conversation as conv_mod
    monkeypatch.setattr(send_mod, "ALL_PEERS", _PEERS)
    monkeypatch.setattr(inbox_mod, "ALL_PEERS", _PEERS)
    monkeypatch.setattr(conv_mod, "ALL_PEERS", _PEERS)

    import mesh_api.lib.conversation_io as cio
    monkeypatch.setattr(cio, "MESH_DIR", tmp_path / "mesh")
    monkeypatch.setattr(cio, "TICKETS_DIR", tmp_path / "tickets")

    import mesh_api.auth as auth
    monkeypatch.setattr(auth, "TOKENS_PATH", Path(tokens_file))

    (tmp_path / "mesh").mkdir()
    (tmp_path / "tickets").mkdir()


@pytest.fixture()
def client(patch_paths):
    from mesh_api.main import app
    with TestClient(app) as c:
        yield c


TOKEN = "test-token-abc"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

FAKE_SEND_OUTPUT = "appended to inbox-user-web.jsonl: id=cafebabe ts=2026-05-24T02:30+02:00\n"


class _FakeSendResult:
    returncode = 0
    stdout = FAKE_SEND_OUTPUT
    stderr = ""


def test_send_to_facade_accepted(client):
    """POST /send with to=user-web must return 200."""
    import mesh_api.lib.mesh_io as mio
    with patch.object(mio.subprocess, "run", return_value=_FakeSendResult()):
        r = client.post("/send", headers=AUTH, json={
            "from": "alice",
            "to": "user-web",
            "priority": "normal",
            "body": "Test of the agent -> app return channel",
        })
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == "cafebabe"
    assert data["delivered_live"] is True


def test_send_to_facade_inbox_readable(tmp_path, client):
    """GET /inbox/user-web must return 200 (not 404)."""
    # Create empty inbox
    (tmp_path / "mesh" / "inbox-user-web.jsonl").touch()

    r = client.get("/inbox/user-web", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["agent"] == "user-web"


def test_send_to_facade_appears_in_conversation(tmp_path, client):
    """Message from alice to user-web shows up as outgoing in GET /conversation/alice."""
    # Simulate what send.py writes: a message in inbox-user-web.jsonl from alice
    inbox = tmp_path / "mesh" / "inbox-user-web.jsonl"
    msg = {
        "id": "deadbeef",
        "ts": "2026-05-24T02:30+02:00",
        "from": "alice",
        "to": "user-web",
        "priority": "normal",
        "body": "Result of ticket tk-abc: all good.",
        "acked": False,
    }
    inbox.write_text(json.dumps(msg) + "\n")

    r = client.get("/conversation/alice", headers=AUTH)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    it = items[0]
    assert it["kind"] == "message"
    assert it["id"] == "deadbeef"
    assert it["direction"] == "outgoing"
    assert it["to"] == "user-web"
    assert it["from"] == "alice"


def test_conversation_facade_inbox(tmp_path, client):
    """GET /conversation/user-web returns messages sent to the app."""
    inbox = tmp_path / "mesh" / "inbox-user-web.jsonl"
    msg = {
        "id": "aabbccdd",
        "ts": "2026-05-24T02:31+02:00",
        "from": "bob",
        "to": "user-web",
        "priority": "normal",
        "body": "Task done.",
        "acked": False,
    }
    inbox.write_text(json.dumps(msg) + "\n")

    r = client.get("/conversation/user-web", headers=AUTH)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == "aabbccdd"
    assert items[0]["direction"] == "incoming"


def test_send_to_unknown_still_rejected(client):
    """Sending to an unknown peer returns 422 (upfront validation, P1-4)."""
    r = client.post("/send", headers=AUTH, json={
        "from": "alice",
        "to": "nobody",
        "priority": "normal",
        "body": "test",
    })
    assert r.status_code == 422
