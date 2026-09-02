"""Tests for Chain mode placeholder resolution in POST /tickets/bulk."""
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


def _get_ticket_deps(client, agent: str, ticket_id: str) -> list[str]:
    r = client.get(f"/tickets/{agent}/{ticket_id}", headers=AUTH)
    assert r.status_code == 200
    return r.json()["depends_on"]


def test_bulk_chain_with_placeholders(client):
    """Chain of 3 tickets: each depends on the previous via placeholder."""
    r = client.post("/tickets/bulk", headers=AUTH, json={
        "to": "alice",
        "tickets": [
            {"prompt": "Step 0", "depends_on": []},
            {"prompt": "Step 1", "depends_on": ["_step0"]},
            {"prompt": "Step 2", "depends_on": ["_step1"]},
        ],
        "dispatch_mode": "draft",
    })
    assert r.status_code == 200
    ids = r.json()["ids"]
    assert len(ids) == 3

    # Step 0 has no deps
    assert _get_ticket_deps(client, "alice", ids[0]) == []
    # Step 1 depends on the real ID of step 0
    assert _get_ticket_deps(client, "alice", ids[1]) == [ids[0]]
    # Step 2 depends on the real ID of step 1
    assert _get_ticket_deps(client, "alice", ids[2]) == [ids[1]]


def test_bulk_placeholder_out_of_range(client):
    """Forward reference (_step1 at index 0) must return 422."""
    r = client.post("/tickets/bulk", headers=AUTH, json={
        "to": "alice",
        "tickets": [
            {"prompt": "Step 0", "depends_on": ["_step1"]},
            {"prompt": "Step 1", "depends_on": []},
        ],
    })
    assert r.status_code == 422
    assert "_step1" in r.json()["detail"]


def test_bulk_placeholder_self_reference(client):
    """Self-reference (_step0 at index 0) must return 422."""
    r = client.post("/tickets/bulk", headers=AUTH, json={
        "to": "alice",
        "tickets": [
            {"prompt": "Step 0", "depends_on": ["_step0"]},
        ],
    })
    assert r.status_code == 422


def test_bulk_placeholder_malformed_no_number(client):
    """'_step' without a number is malformed → 422."""
    r = client.post("/tickets/bulk", headers=AUTH, json={
        "to": "alice",
        "tickets": [
            {"prompt": "A", "depends_on": []},
            {"prompt": "B", "depends_on": ["_step"]},
        ],
    })
    assert r.status_code == 422
    assert "malformed" in r.json()["detail"]


def test_bulk_placeholder_malformed_non_integer(client):
    """'_stepX' (non-integer) is malformed → 422."""
    r = client.post("/tickets/bulk", headers=AUTH, json={
        "to": "alice",
        "tickets": [
            {"prompt": "A", "depends_on": []},
            {"prompt": "B", "depends_on": ["_stepX"]},
        ],
    })
    assert r.status_code == 422
    assert "malformed" in r.json()["detail"]


def test_bulk_mixed_placeholder_and_external(client):
    """Mix of placeholder and external ticket ID resolves correctly."""
    external_id = "tk-abc123"
    r = client.post("/tickets/bulk", headers=AUTH, json={
        "to": "alice",
        "tickets": [
            {"prompt": "Step 0", "depends_on": []},
            {"prompt": "Step 1", "depends_on": ["_step0", external_id]},
        ],
    })
    assert r.status_code == 200
    ids = r.json()["ids"]
    deps = _get_ticket_deps(client, "alice", ids[1])
    assert ids[0] in deps
    assert external_id in deps
    assert len(deps) == 2


def test_bulk_no_placeholder_backwards_compat(client):
    """Bulk without any placeholders behaves identically to before."""
    r = client.post("/tickets/bulk", headers=AUTH, json={
        "to": "alice",
        "tickets": [
            {"prompt": "Task A", "depends_on": []},
            {"prompt": "Task B", "depends_on": []},
        ],
    })
    assert r.status_code == 200
    data = r.json()
    assert data["created_count"] == 2
    assert len(data["ids"]) == 2
    # Neither ticket has any deps
    for tid in data["ids"]:
        assert _get_ticket_deps(client, "alice", tid) == []
