"""Smoke tests — run with pytest from mesh-api/."""
import json
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Point to a temp dir so tests don't touch real mesh data
os.environ.setdefault("MESH_TOKENS_PATH", "/tmp/test-mesh-tokens.json")


def _make_token_file(path: str, token: str = "test-token-abc") -> None:
    Path(path).write_text(json.dumps([{"name": "test", "token": token}]))
    os.chmod(path, 0o600)


@pytest.fixture(autouse=True)
def patch_paths(tmp_path, monkeypatch):
    tokens_file = str(tmp_path / "api-tokens.json")
    _make_token_file(tokens_file)
    monkeypatch.setenv("MESH_TOKENS_PATH", tokens_file)

    # Redirect tickets dir to tmp
    import mesh_api.lib.tickets_io as tio
    monkeypatch.setattr(tio, "TICKETS_DIR", tmp_path / "tickets")
    monkeypatch.setattr(tio, "AGENTS", ("alice", "bob", "yara", "dave"))

    import mesh_api.auth as auth
    from pathlib import Path as P
    monkeypatch.setattr(auth, "TOKENS_PATH", P(tokens_file))


@pytest.fixture()
def client(patch_paths):
    from mesh_api.main import app
    with TestClient(app) as c:
        yield c


TOKEN = "test-token-abc"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_status_requires_auth(client):
    r = client.get("/status")
    assert r.status_code == 401


def test_status_with_auth(client):
    r = client.get("/status", headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert "agents" in data
    assert "host" in data


def test_inbox_all_agents(client, tmp_path, monkeypatch):
    """All 6 agents should return 200 from GET /inbox/<agent>."""
    import mesh_api.lib.mesh_io as mio
    # Patch read_inbox to return empty list for any agent (avoids real FS)
    monkeypatch.setattr(mio, "read_inbox", lambda *a, **kw: [])

    for agent in ("alice", "bob", "yara", "dave", "zoe", "auto"):
        r = client.get(f"/inbox/{agent}", headers=AUTH)
        assert r.status_code == 200, f"/inbox/{agent} returned {r.status_code}"
        assert r.json()["agent"] == agent

    r = client.get("/inbox/unknown-agent", headers=AUTH)
    assert r.status_code == 404


def test_send_real_id(client, monkeypatch):
    """ID returned by /send must match what mesh-send-checked.sh printed."""
    import mesh_api.lib.mesh_io as mio

    fake_output = "appended to inbox-alice.jsonl: id=ab12cd34 ts=2026-05-22T14:00+02:00\n"

    class FakeResult:
        returncode = 0
        stdout = fake_output
        stderr = ""

    monkeypatch.setattr(mio.subprocess, "run", lambda *a, **kw: FakeResult())

    r = client.post("/send", headers=AUTH, json={
        "to": "alice", "body": "test", "from": "user-web", "priority": "normal"
    })
    assert r.status_code == 200
    assert r.json()["id"] == "ab12cd34"
    assert r.json()["delivered_live"] is True


def test_ticket_crud(client):
    # Create draft ticket
    r = client.post("/tickets", headers=AUTH, json={
        "to": "alice", "prompt": "Test the draft ticket", "dispatch_mode": "draft"
    })
    assert r.status_code == 200
    ticket_id = r.json()["id"]
    assert r.json()["status"] == "draft"

    # List tickets
    r = client.get("/tickets/alice?status=draft", headers=AUTH)
    assert r.status_code == 200
    assert any(t["id"] == ticket_id for t in r.json()["tickets"])

    # Get detail
    r = client.get(f"/tickets/alice/{ticket_id}", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["id"] == ticket_id

    # Patch: arm the ticket
    r = client.patch(f"/tickets/alice/{ticket_id}", headers=AUTH,
                     json={"dispatch_mode": "armed"})
    assert r.status_code == 200
    assert r.json()["status"] == "armed"

    # Delete
    r = client.delete(f"/tickets/alice/{ticket_id}", headers=AUTH)
    assert r.status_code == 204

    # Verify gone
    r = client.get(f"/tickets/alice/{ticket_id}", headers=AUTH)
    assert r.status_code == 404
