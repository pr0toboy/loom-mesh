"""`reply_to` must survive the trip from the API to the inbox line.

The API accepted `reply_to`, and for a while it dropped the value before the
bus: the field validated, the response was 200, and the message landed with no
`reply_to` at all — a reply that pointed at nothing. Nothing failed.

The three tests that cover `reply_to` live in `bus/tests/test_bus.py` and drive
`send.py` and `mesh-send-checked.sh` directly, so they stayed green throughout;
no test in this suite mentioned the field. This one exercises the seam they
skip — route, then `mesh_io`, then the real bus script `conftest.py` installs —
and reads the value back out of the inbox file rather than out of the response.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

MESH_HOME = Path(os.environ["MESH_HOME"])  # conftest.py, before any import of mesh_api


def _token_file(tmp_path: Path, token: str = "test-token-abc") -> Path:
    p = tmp_path / "api-tokens.json"
    p.write_text(json.dumps([{"name": "test", "token": token}]))
    os.chmod(p, 0o600)
    return p


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import mesh_api.auth as auth
    monkeypatch.setattr(auth, "TOKENS_PATH", _token_file(tmp_path))
    from mesh_api.main import app
    with TestClient(app) as c:
        yield c


AUTH = {"Authorization": "Bearer test-token-abc"}


def _inbox_lines(agent: str) -> list[dict]:
    path = MESH_HOME / f"inbox-{agent}.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_reply_to_reaches_the_inbox_line(client):
    first = client.post("/send", headers=AUTH, json={
        "from": "alice", "to": "bob", "priority": "normal", "body": "the question"})
    assert first.status_code == 200, first.text
    question_id = first.json()["id"]

    answer = client.post("/send", headers=AUTH, json={
        "from": "bob", "to": "alice", "priority": "normal",
        "body": "the answer", "reply_to": question_id})
    assert answer.status_code == 200, answer.text

    written = [m for m in _inbox_lines("alice") if m.get("id") == answer.json()["id"]]
    assert written, "the API answered 200 but wrote no line to alice's inbox"
    assert written[0].get("reply_to") == question_id, (
        "the API accepted reply_to and did not pass it to the bus: the message "
        f"landed as {written[0]!r}"
    )


def test_a_message_without_reply_to_carries_none(client):
    r = client.post("/send", headers=AUTH, json={
        "from": "alice", "to": "bob", "priority": "normal", "body": "standalone"})
    assert r.status_code == 200, r.text
    written = [m for m in _inbox_lines("bob") if m.get("id") == r.json()["id"]]
    assert written, "no line written to bob's inbox"
    assert not written[0].get("reply_to"), (
        "a message sent without reply_to must not acquire one: "
        f"{written[0]!r}"
    )
