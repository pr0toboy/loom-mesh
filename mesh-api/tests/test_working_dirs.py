"""Tests for ticket working directory scaffold (Octogent-inspired tentacle pattern).

Verifies:
- Ticket creation scaffolds working/<tk-id>/ with 4 files
- brief.md is written at dispatch time (prompt finalised)
- ticket-complete archives working/<tk-id>/ to done/<tk-id>/ with tldr in output.md
- No cross-contamination between tickets
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ticket_dispatcher lives at mesh-api root (not in the package)
sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("MESH_TOKENS_PATH", "/tmp/test-mesh-tokens-wd.json")


def _make_token_file(path: str, token: str = "wd-token-xyz") -> None:
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

    import mesh_api.auth as auth
    monkeypatch.setattr(auth, "TOKENS_PATH", Path(tokens_file))
    monkeypatch.setattr(auth, "_cached_tokens", set())
    monkeypatch.setattr(auth, "_cache_loaded_at", 0.0)


@pytest.fixture()
def client(patch_paths):
    from mesh_api.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


AUTH = {"Authorization": "Bearer wd-token-xyz"}


# ── Scaffold at creation ───────────────────────────────────────────────────────

def test_scaffold_created_on_ticket_creation(client):
    """POST /tickets → working/<tk-id>/ exists with 4 expected files."""
    import mesh_api.lib.tickets_io as tio

    r = client.post("/tickets", headers=AUTH, json={
        "to": "alice",
        "prompt": "Test scaffold ticket",
        "dispatch_mode": "draft",
    })
    assert r.status_code == 200
    ticket_id = r.json()["id"]

    working = tio.TICKETS_DIR / "alice" / "working" / ticket_id
    assert working.is_dir(), f"working dir not created: {working}"
    for fname in ("brief.md", "todo.md", "notes.md", "output.md"):
        assert (working / fname).exists(), f"Missing file: {fname}"


def test_todo_md_has_template_sections(client):
    """todo.md must contain the 3 expected section headers."""
    import mesh_api.lib.tickets_io as tio

    r = client.post("/tickets", headers=AUTH, json={
        "to": "alice",
        "prompt": "Todo template check",
        "dispatch_mode": "draft",
    })
    assert r.status_code == 200
    ticket_id = r.json()["id"]

    todo_text = (tio.TICKETS_DIR / "alice" / "working" / ticket_id / "todo.md").read_text()
    assert "## To do" in todo_text
    assert "## In progress" in todo_text
    assert "## Done" in todo_text
    assert ticket_id in todo_text


def test_brief_md_initially_empty(client):
    """brief.md must be empty at creation (filled at dispatch time)."""
    import mesh_api.lib.tickets_io as tio

    r = client.post("/tickets", headers=AUTH, json={
        "to": "alice",
        "prompt": "Some prompt here",
        "dispatch_mode": "draft",
    })
    assert r.status_code == 200
    ticket_id = r.json()["id"]

    brief = (tio.TICKETS_DIR / "alice" / "working" / ticket_id / "brief.md").read_text()
    assert brief.strip() == "", f"brief.md should be empty at creation, got: {brief!r}"


def test_scaffold_idempotent(tmp_path, monkeypatch):
    """scaffold_working_dir called twice must not overwrite existing content."""
    import mesh_api.lib.tickets_io as tio
    monkeypatch.setattr(tio, "TICKETS_DIR", tmp_path / "tickets")
    monkeypatch.setattr(tio, "AGENTS", frozenset({"alice"}))

    tio.scaffold_working_dir("tk-aabbcc", "alice")
    (tmp_path / "tickets" / "alice" / "working" / "tk-aabbcc" / "notes.md").write_text("my notes")

    tio.scaffold_working_dir("tk-aabbcc", "alice")
    assert (tmp_path / "tickets" / "alice" / "working" / "tk-aabbcc" / "notes.md").read_text() == "my notes"


# ── Brief written at dispatch ──────────────────────────────────────────────────

def test_extract_brief_to_working(tmp_path):
    """_extract_brief_to_working writes prompt into brief.md."""
    import ticket_dispatcher as td
    import importlib, sys

    # Patch TICKETS_DIR inside the module
    original = td.TICKETS_DIR
    td.TICKETS_DIR = tmp_path / "tickets"

    try:
        working = tmp_path / "tickets" / "alice" / "working" / "tk-123abc"
        working.mkdir(parents=True)
        (working / "brief.md").write_text("")

        td._extract_brief_to_working("tk-123abc", "alice", "My dispatch prompt")
        assert (working / "brief.md").read_text() == "My dispatch prompt"
    finally:
        td.TICKETS_DIR = original


def test_extract_brief_skips_if_already_filled(tmp_path):
    """_extract_brief_to_working must not overwrite an already non-empty brief.md."""
    import ticket_dispatcher as td

    original = td.TICKETS_DIR
    td.TICKETS_DIR = tmp_path / "tickets"
    try:
        working = tmp_path / "tickets" / "alice" / "working" / "tk-deadbe"
        working.mkdir(parents=True)
        (working / "brief.md").write_text("existing content")

        td._extract_brief_to_working("tk-deadbe", "alice", "new prompt")
        assert (working / "brief.md").read_text() == "existing content"
    finally:
        td.TICKETS_DIR = original


def test_extract_brief_no_working_dir_is_noop(tmp_path):
    """_extract_brief_to_working must not crash if working dir doesn't exist."""
    import ticket_dispatcher as td

    original = td.TICKETS_DIR
    td.TICKETS_DIR = tmp_path / "tickets"
    try:
        td._extract_brief_to_working("tk-ffffff", "alice", "some prompt")
        # no exception = pass
    finally:
        td.TICKETS_DIR = original


# ── Archive at completion ──────────────────────────────────────────────────────

def test_archive_working_dir_on_done(tmp_path, monkeypatch):
    """archive_working_dir moves working/<id>/ to done/<id>/ and writes tldr to output.md."""
    import mesh_api.lib.tickets_io as tio
    monkeypatch.setattr(tio, "TICKETS_DIR", tmp_path / "tickets")
    monkeypatch.setattr(tio, "AGENTS", frozenset({"alice"}))

    tio.scaffold_working_dir("tk-done01", "alice")
    (tmp_path / "tickets" / "alice" / "working" / "tk-done01" / "notes.md").write_text("some notes")

    (tmp_path / "tickets" / "alice" / "done").mkdir(parents=True, exist_ok=True)
    tio.archive_working_dir("tk-done01", "alice", "done", tldr="Task completed successfully.")

    done_dir = tmp_path / "tickets" / "alice" / "done" / "tk-done01"
    assert done_dir.is_dir(), "done/<tk-id>/ not created"
    assert (done_dir / "output.md").read_text() == "Task completed successfully."
    assert (done_dir / "notes.md").read_text() == "some notes"
    assert not (tmp_path / "tickets" / "alice" / "working" / "tk-done01").exists()


def test_archive_working_dir_on_failed(tmp_path, monkeypatch):
    """archive_working_dir moves to failed/<id>/ on failure."""
    import mesh_api.lib.tickets_io as tio
    monkeypatch.setattr(tio, "TICKETS_DIR", tmp_path / "tickets")
    monkeypatch.setattr(tio, "AGENTS", frozenset({"alice"}))

    tio.scaffold_working_dir("tk-fail01", "alice")
    (tmp_path / "tickets" / "alice" / "failed").mkdir(parents=True, exist_ok=True)
    tio.archive_working_dir("tk-fail01", "alice", "failed", tldr="Timed out.")

    failed_dir = tmp_path / "tickets" / "alice" / "failed" / "tk-fail01"
    assert failed_dir.is_dir()
    assert (failed_dir / "output.md").read_text() == "Timed out."


def test_archive_noop_if_no_working_dir(tmp_path, monkeypatch):
    """archive_working_dir must not crash if working dir was never created."""
    import mesh_api.lib.tickets_io as tio
    monkeypatch.setattr(tio, "TICKETS_DIR", tmp_path / "tickets")
    monkeypatch.setattr(tio, "AGENTS", frozenset({"alice"}))

    (tmp_path / "tickets" / "alice" / "done").mkdir(parents=True, exist_ok=True)
    tio.archive_working_dir("tk-ghost1", "alice", "done", tldr="Done.")  # no exception


# ── No cross-contamination ─────────────────────────────────────────────────────

def test_no_cross_contamination_between_tickets(client):
    """Working dirs for different tickets must be isolated."""
    import mesh_api.lib.tickets_io as tio

    ids = []
    for i in range(3):
        r = client.post("/tickets", headers=AUTH, json={
            "to": "alice",
            "prompt": f"Ticket {i}",
            "dispatch_mode": "draft",
        })
        assert r.status_code == 200
        ids.append(r.json()["id"])

    # All 3 dirs must exist and be separate
    for tid in ids:
        working = tio.TICKETS_DIR / "alice" / "working" / tid
        assert working.is_dir()

    # Writing to one must not affect others
    (tio.TICKETS_DIR / "alice" / "working" / ids[0] / "notes.md").write_text("private note")
    for tid in ids[1:]:
        assert (tio.TICKETS_DIR / "alice" / "working" / tid / "notes.md").read_text() == ""


# ── DELETE cleans up working dir ──────────────────────────────────────────────

def test_delete_draft_ticket_removes_working_dir(client):
    """DELETE /tickets/<agent>/<id> on a draft ticket must remove working/<id>/."""
    import mesh_api.lib.tickets_io as tio

    r = client.post("/tickets", headers=AUTH, json={
        "to": "alice", "prompt": "to be deleted", "dispatch_mode": "draft",
    })
    assert r.status_code == 200
    tid = r.json()["id"]
    assert (tio.TICKETS_DIR / "alice" / "working" / tid).is_dir()

    r = client.delete(f"/tickets/alice/{tid}", headers=AUTH)
    assert r.status_code == 204
    assert not (tio.TICKETS_DIR / "alice" / "working" / tid).exists(), \
        "working dir orphaned after DELETE"


def test_delete_running_ticket_archives_working_to_cancelled(client):
    """DELETE /tickets on a running ticket → JSON goes to cancelled/, working dir archived too."""
    import mesh_api.lib.tickets_io as tio

    # Manually plant a running ticket
    tio.ensure_dirs()
    running_dir = tio.TICKETS_DIR / "alice" / "running"
    running_dir.mkdir(parents=True, exist_ok=True)
    ticket_id = "tk-aabbcc"
    ticket = {"id": ticket_id, "to": "alice", "from": "user-web",
               "status": "running", "priority": "normal", "prompt": "running task",
               "queued_at": "2026-05-24T12:00:00+00:00", "started_at": "2026-05-24T12:01:00+00:00",
               "completed_at": None, "dispatch_mode": "armed", "tldr": None,
               "depends_on": [], "parent_ticket_id": None}
    (running_dir / f"{ticket_id}.json").write_text(__import__("json").dumps(ticket))
    tio.scaffold_working_dir(ticket_id, "alice")
    (tio.TICKETS_DIR / "alice" / "working" / ticket_id / "notes.md").write_text("work in progress")

    r = client.delete(f"/tickets/alice/{ticket_id}", headers=AUTH)
    assert r.status_code == 204

    cancelled_working = tio.TICKETS_DIR / "alice" / "cancelled" / ticket_id
    assert cancelled_working.is_dir(), "working dir not archived to cancelled/"
    assert (cancelled_working / "notes.md").read_text() == "work in progress"
    assert not (tio.TICKETS_DIR / "alice" / "working" / ticket_id).exists()


def test_bulk_create_scaffolds_all_tickets(client):
    """POST /tickets/bulk must scaffold a working dir for each created ticket."""
    import mesh_api.lib.tickets_io as tio

    r = client.post("/tickets/bulk", headers=AUTH, json={
        "to": "alice",
        "tickets": [
            {"prompt": "Step 1", "depends_on": []},
            {"prompt": "Step 2", "depends_on": ["_step0"]},
        ],
        "dispatch_mode": "armed",
    })
    assert r.status_code == 200
    for tid in r.json()["ids"]:
        working = tio.TICKETS_DIR / "alice" / "working" / tid
        assert working.is_dir(), f"working dir missing for {tid}"
        assert (working / "brief.md").exists()
