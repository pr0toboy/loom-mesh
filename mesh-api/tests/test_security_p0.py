"""Security regression tests for P0 fixes (v0.30 batch).

Each test reproduces the attack vector described in the Opus audit and asserts
the fixed behaviour. These must pass for a release.
"""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("MESH_TOKENS_PATH", "/tmp/test-mesh-tokens-sec.json")


def _make_token_file(path: str, token: str = "sec-token-123") -> None:
    Path(path).write_text(json.dumps([{"name": "test", "token": token}]))
    os.chmod(path, 0o600)


@pytest.fixture(autouse=True)
def patch_paths(tmp_path, monkeypatch):
    tokens_file = str(tmp_path / "api-tokens.json")
    _make_token_file(tokens_file)
    monkeypatch.setenv("MESH_TOKENS_PATH", tokens_file)

    import mesh_api.lib.tickets_io as tio
    monkeypatch.setattr(tio, "TICKETS_DIR", tmp_path / "tickets")
    monkeypatch.setattr(tio, "AGENTS", frozenset({"alice", "bob", "yara", "dave", "zoe", "auto"}))
    # Recipient validation reads ALL_PEERS from the send module: without this
    # patch the test would depend on the roster of whatever mesh is deployed here,
    # and break the day a name changes. The tests declare their own fleet.
    import mesh_api.routes.send as send_mod
    monkeypatch.setattr(send_mod, "ALL_PEERS", frozenset({"alice", "bob", "yara", "dave", "user-web"}))

    import mesh_api.auth as auth
    monkeypatch.setattr(auth, "TOKENS_PATH", Path(tokens_file))
    monkeypatch.setattr(auth, "_cached_tokens", set())
    monkeypatch.setattr(auth, "_cache_loaded_at", 0.0)


@pytest.fixture()
def client(patch_paths):
    from mesh_api.main import app
    with TestClient(app) as c:
        yield c


AUTH = {"Authorization": "Bearer sec-token-123"}


# ── P0-1 : from field injection via send ──────────────────────────────────────

def test_p0_1_from_injection_rejected(client):
    """POST /send with from=$(touch /tmp/PWNED) must return 422, not execute the command."""
    r = client.post("/send", headers=AUTH, json={
        "from": "$(touch /tmp/PWNED_TEST_P0_1)",
        "to": "alice",
        "priority": "normal",
        "body": "exploit attempt",
    })
    assert r.status_code == 422, f"Expected 422, got {r.status_code}: {r.text}"
    assert not Path("/tmp/PWNED_TEST_P0_1").exists(), "RCE executed — critical failure"


def test_p0_1_from_with_semicolons_rejected(client):
    """Semicolons and shell metacharacters in 'from' must be rejected."""
    r = client.post("/send", headers=AUTH, json={
        "from": "alice; rm -rf /tmp/test",
        "to": "bob",
        "priority": "normal",
        "body": "exploit attempt",
    })
    assert r.status_code == 422


def test_p0_1_valid_from_accepted(client, tmp_path):
    """Valid 'from' peer names must still be accepted."""
    import mesh_api.lib.mesh_io as mio

    class _OkResult:
        returncode = 2  # sent OK, recipient busy
        stdout = "id=cafebabe ts=2026-05-24T12:00+02:00\n"
        stderr = ""

    with patch.object(mio.subprocess, "run", return_value=_OkResult()):
        r = client.post("/send", headers=AUTH, json={
            "from": "user-web",
            "to": "alice",
            "priority": "normal",
            "body": "hello",
        })
    assert r.status_code == 200


# ── P0-2 : wildcard ticket_id glob ────────────────────────────────────────────

def test_p0_2_wildcard_ticket_id_get_rejected(client):
    """GET /tickets/alice/%2A must return 422 (wildcard rejected)."""
    r = client.get("/tickets/alice/%2A", headers=AUTH)
    assert r.status_code == 422, f"Expected 422, got {r.status_code}: {r.text}"


def test_p0_2_wildcard_ticket_id_delete_rejected(client):
    """DELETE /tickets/alice/%2A must return 422."""
    r = client.delete("/tickets/alice/%2A", headers=AUTH)
    assert r.status_code == 422


def test_p0_2_wildcard_ticket_id_patch_rejected(client):
    """PATCH /tickets/alice/%2A must return 422."""
    r = client.patch("/tickets/alice/%2A", headers=AUTH, json={"position": 0})
    assert r.status_code == 422


def test_p0_2_dotdot_ticket_id_rejected(client):
    """Path traversal via ticket_id must return 422."""
    r = client.get("/tickets/alice/../../etc/passwd", headers=AUTH)
    # FastAPI may strip it, but check no 200
    assert r.status_code in (404, 422)


def test_p0_2_valid_ticket_id_format_accepted(client, tmp_path):
    """Valid tk-XXXXXX format ticket IDs must pass format validation."""
    import mesh_api.lib.tickets_io as tio
    # Create a real ticket so the route finds it
    tio.ensure_dirs()
    ticket = tio.create_ticket(to="alice", prompt="test", from_="user-web")
    r = client.get(f"/tickets/alice/{ticket['id']}", headers=AUTH)
    assert r.status_code == 200


# ── P0-3 : from field spoofing ────────────────────────────────────────────────

def test_p0_3_from_must_be_valid_peer_format(client):
    """'from' with spaces rejected (spoofing / injection risk)."""
    r = client.post("/send", headers=AUTH, json={
        "from": "alice dave",
        "to": "bob",
        "priority": "normal",
        "body": "test",
    })
    assert r.status_code == 422


def test_p0_3_from_with_null_byte_rejected(client):
    """Null byte in body must return 422."""
    r = client.post("/send", headers=AUTH, json={
        "from": "alice",
        "to": "bob",
        "priority": "normal",
        "body": "hello\x00world",
    })
    assert r.status_code == 422


def test_p0_3_body_too_long_rejected(client):
    """Body exceeding 65536 chars must return 422."""
    r = client.post("/send", headers=AUTH, json={
        "from": "alice",
        "to": "bob",
        "priority": "normal",
        "body": "x" * 70000,
    })
    assert r.status_code == 422


# ── P1-4 : unknown to peer → 422 not 502 ─────────────────────────────────────

def test_p1_4_unknown_to_returns_422(client):
    """Sending to an unknown peer returns 422 (upfront validation)."""
    r = client.post("/send", headers=AUTH, json={
        "from": "alice",
        "to": "nobody",
        "priority": "normal",
        "body": "test",
    })
    assert r.status_code == 422
    assert "nobody" in r.json()["detail"]


# ── P0-4 : from spoofing — unknown peer rejected even if format valid ─────────

def test_p0_4_from_unknown_peer_rejected(client):
    """POST /send with format-valid but unknown 'from' must return 422 (spoofing)."""
    r = client.post("/send", headers=AUTH, json={
        "from": "totally-unknown",
        "to": "alice",
        "priority": "normal",
        "body": "spoofed message",
    })
    assert r.status_code == 422, f"Expected 422, got {r.status_code}: {r.text}"


def test_p0_4_from_too_long_rejected(client):
    """POST /send with 33-char 'from' (off-by-one) must return 422."""
    r = client.post("/send", headers=AUTH, json={
        "from": "a" * 33,
        "to": "alice",
        "priority": "normal",
        "body": "off-by-one exploit",
    })
    assert r.status_code == 422, f"Expected 422 for 33-char from, got {r.status_code}: {r.text}"


def test_p0_4_from_32_chars_known_peer_accepted(client, tmp_path):
    """POST /send with a known peer in ALL_PEERS must still be accepted."""
    import mesh_api.lib.mesh_io as mio

    class _OkResult:
        returncode = 0
        stdout = "id=cafebabe ts=2026-05-24T12:00+02:00\n"
        stderr = ""

    with patch.object(mio.subprocess, "run", return_value=_OkResult()):
        r = client.post("/send", headers=AUTH, json={
            "from": "user-web",
            "to": "alice",
            "priority": "normal",
            "body": "legit message",
        })
    assert r.status_code == 200


# ── H4 : ticket prompt fields bounded (max_length + null-byte) ────────────────

def test_h4_ticket_prompt_null_byte_rejected(client):
    """Null byte in a ticket prompt must return 422."""
    r = client.post("/tickets", headers=AUTH, json={
        "from": "user-web",
        "to": "alice",
        "prompt": "hello\x00world",
    })
    assert r.status_code == 422, f"Expected 422, got {r.status_code}: {r.text}"


def test_h4_ticket_prompt_too_long_rejected(client):
    """Ticket prompt exceeding 65536 chars must return 422."""
    r = client.post("/tickets", headers=AUTH, json={
        "from": "user-web",
        "to": "alice",
        "prompt": "x" * 70000,
    })
    assert r.status_code == 422, f"Expected 422, got {r.status_code}: {r.text}"


# ── P1-3 : /ack accepts user-web ───────────────────────────────────────

def test_p1_3_ack_facade_accepted(client, tmp_path):
    """POST /ack with agent=user-web must not return 404."""
    import mesh_api.lib.mesh_io as mio

    with patch.object(mio.subprocess, "run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "acked 1 message(s)\n"
        mock_run.return_value.stderr = ""
        r = client.post("/ack", headers=AUTH, json={
            "agent": "user-web",
            "id": "cafebabe",
        })
    assert r.status_code != 404, f"user-web rejected from /ack: {r.text}"
