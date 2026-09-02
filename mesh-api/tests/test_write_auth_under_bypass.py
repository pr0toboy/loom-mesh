"""Writes stay authenticated even when the read bypass is on.

``MESH_API_NO_AUTH=1`` exists for a deliberate reason: a dashboard on a trusted
network, consulted without a token. That trade was made about *reading*. It
silently covered writing too — and on this system a write is not a row in a
table:

* ``POST /send`` appends to an agent's inbox, and an agent reads its inbox as
  instructions it acts on. Writing there is close to running code as that agent.
* ``POST /tickets`` queues work that a dispatcher will hand to an agent.

So the bypass applies to reads only, and these tests pin that: with the bypass
on, a read answers without a token and a write refuses.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def bypass_client(tmp_path: Path, monkeypatch):
    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps([{"name": "test", "token": "valid-token"}]))
    monkeypatch.setenv("MESH_TOKENS_PATH", str(tokens))
    monkeypatch.setenv("MESH_API_NO_AUTH", "1")

    import mesh_api.auth as auth
    monkeypatch.setattr(auth, "TOKENS_PATH", tokens)
    monkeypatch.setattr(auth, "_cache_loaded_at", 0.0)

    import mesh_api.lib.tickets_io as tio
    monkeypatch.setattr(tio, "TICKETS_DIR", tmp_path / "tickets")

    from mesh_api.main import app
    with TestClient(app) as client:
        yield client


def test_read_is_open_under_the_bypass(bypass_client):
    r = bypass_client.get("/status")
    assert r.status_code == 200, "the bypass exists precisely to allow this"


@pytest.mark.parametrize("method,path,body", [
    ("post", "/send", {"to": "alice", "body": "run this", "from": "bob", "priority": "normal"}),
    ("post", "/ack", {"agent": "alice", "id": "abc12345"}),
    ("post", "/tickets", {"to": "alice", "title": "t", "body": "b", "priority": "normal"}),
    ("delete", "/tickets/alice/tk-abc123", None),
    ("post", "/push", {"title": "t", "body": "b"}),
])
def test_writes_are_refused_without_a_token_under_the_bypass(bypass_client, method, path, body):
    call = getattr(bypass_client, method)
    r = call(path, json=body) if body is not None else call(path)
    assert r.status_code == 401, (
        f"{method.upper()} {path} accepted an unauthenticated write under the read bypass"
    )


def test_a_valid_token_still_writes(bypass_client):
    r = bypass_client.post("/tickets",
                           headers={"Authorization": "Bearer valid-token"},
                           json={"to": "alice", "title": "t", "body": "b",
                                 "priority": "normal"})
    assert r.status_code in (200, 201, 422), r.text
    assert r.status_code != 401, "a valid token must still be accepted"
