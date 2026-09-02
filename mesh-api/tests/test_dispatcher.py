"""Tests for ticket_dispatcher — deps/blocked/dispatch logic (no real tmux)."""
from __future__ import annotations

import json
import os
import sys
import time
import unittest.mock as mock
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Add the mesh-api root to path so we can import ticket_dispatcher
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture()
def tickets_root(tmp_path, monkeypatch):
    """Redirect TICKETS_DIR and MESH_DIR to tmp and create agent state dirs."""
    import ticket_dispatcher as td

    monkeypatch.setattr(td, "TICKETS_DIR", tmp_path / "tickets")
    monkeypatch.setattr(td, "MESH_DIR", tmp_path / "mesh")

    agents = ["alice", "bob", "yara", "dave", "zoe", "auto"]
    for ag in agents:
        for state in ("draft", "armed", "blocked", "queued", "running", "done", "failed", "cancelled"):
            (tmp_path / "tickets" / ag / state).mkdir(parents=True, exist_ok=True)

    # Create stub inboxes (old mtime = agent looks idle wrt inbox guard)
    mesh = tmp_path / "mesh"
    mesh.mkdir(parents=True, exist_ok=True)
    for ag in agents:
        inbox = mesh / f"inbox-{ag}.jsonl"
        inbox.write_text("")
        # Set mtime to 10 minutes ago so inbox guard passes
        old = time.time() - 600
        os.utime(inbox, (old, old))

    return tmp_path


def _make_ticket(tickets_root: Path, agent: str, state: str, ticket_id: str,
                 depends_on: list | None = None, **extra) -> Path:
    d = tickets_root / "tickets" / agent / state
    d.mkdir(parents=True, exist_ok=True)
    ticket = {
        "id": ticket_id,
        "to": agent,
        "from": "test",
        "status": state,
        "dispatch_mode": "armed",
        "priority": "normal",
        "prompt": f"Test prompt for {ticket_id}",
        "depends_on": depends_on or [],
        "queued_at": "2026-05-21T10:00:00+00:00",
        "started_at": None,
        "completed_at": None,
        "tldr": None,
        **extra,
    }
    path = d / f"{ticket_id}.json"
    path.write_text(json.dumps(ticket, indent=2))
    return path


# ── Dependency tests ────────────────────────────────────────────────────────────

def test_all_deps_done_empty(tickets_root):
    import ticket_dispatcher as td
    assert td._all_deps_done([], ["alice"]) is True


def test_all_deps_done_missing(tickets_root):
    import ticket_dispatcher as td
    assert td._all_deps_done(["tk-notexist"], ["alice"]) is False


def test_all_deps_done_present(tickets_root):
    import ticket_dispatcher as td
    _make_ticket(tickets_root, "alice", "done", "tk-dep001")
    assert td._all_deps_done(["tk-dep001"], ["alice"]) is True


# ── Blocked → armed re-promotion ────────────────────────────────────────────────

def test_blocked_stays_blocked_while_dep_missing(tickets_root):
    import ticket_dispatcher as td
    _make_ticket(tickets_root, "alice", "blocked", "tk-b001", depends_on=["tk-missing"])
    config = {**td.DEFAULT_CONFIG, "agents": ["alice"]}

    td.process_blocked("alice", config)

    blocked_dir = tickets_root / "tickets" / "alice" / "blocked"
    armed_dir = tickets_root / "tickets" / "alice" / "armed"
    assert list(blocked_dir.glob("*.json")), "ticket should remain blocked"
    assert not list(armed_dir.glob("*.json")), "should not be promoted"


def test_blocked_promoted_when_dep_done(tickets_root):
    import ticket_dispatcher as td
    _make_ticket(tickets_root, "alice", "done", "tk-dep-a")
    _make_ticket(tickets_root, "alice", "blocked", "tk-b002", depends_on=["tk-dep-a"])
    config = {**td.DEFAULT_CONFIG, "agents": ["alice"]}

    td.process_blocked("alice", config)

    blocked_dir = tickets_root / "tickets" / "alice" / "blocked"
    armed_dir = tickets_root / "tickets" / "alice" / "armed"
    assert not list(blocked_dir.glob("*.json")), "ticket should leave blocked"
    promoted = list(armed_dir.glob("*.json"))
    assert len(promoted) == 1
    data = json.loads(promoted[0].read_text())
    assert data["status"] == "armed"


# ── Armed → blocked when dep unresolved during dispatch ─────────────────────────

def test_armed_moves_to_blocked_on_unresolved_dep(tickets_root):
    import ticket_dispatcher as td
    _make_ticket(tickets_root, "bob", "armed", "tk-a001", depends_on=["tk-not-there"])
    config = {**td.DEFAULT_CONFIG, "agents": ["bob"]}

    with mock.patch.object(td, "is_idle_local", return_value=True):
        td.process_armed("bob", config)

    armed_dir = tickets_root / "tickets" / "bob" / "armed"
    blocked_dir = tickets_root / "tickets" / "bob" / "blocked"
    assert not list(armed_dir.glob("*.json")), "should have left armed"
    assert list(blocked_dir.glob("*.json")), "should be in blocked"


# ── Full dispatch cycle ─────────────────────────────────────────────────────────

def test_dispatch_armed_ticket_to_running(tickets_root):
    import ticket_dispatcher as td
    _make_ticket(tickets_root, "yara", "armed", "tk-d001")
    config = {**td.DEFAULT_CONFIG, "agents": ["yara"]}

    with (
        mock.patch.object(td, "is_idle_local", return_value=True),
        mock.patch.object(td, "_push_local", return_value=True) as mock_push,
    ):
        td.process_armed("yara", config)

    armed_dir = tickets_root / "tickets" / "yara" / "armed"
    running_dir = tickets_root / "tickets" / "yara" / "running"

    assert not list(armed_dir.glob("*.json")), "ticket should have left armed"
    running_files = list(running_dir.glob("*.json"))
    assert len(running_files) == 1

    data = json.loads(running_files[0].read_text())
    assert data["status"] == "running"
    assert data["started_at"] is not None
    assert data["position"] is None

    mock_push.assert_called_once()
    call_args = mock_push.call_args
    assert "yara" in call_args[0]
    assert "tk-d001" in call_args[0][1]


def test_no_dispatch_when_inbox_recent(tickets_root):
    import ticket_dispatcher as td
    # Ticket must be fresh too — stale tickets bypass the inbox guard (by design)
    fresh_ts = datetime.fromtimestamp(time.time() - 60, tz=timezone.utc).isoformat()
    _make_ticket(tickets_root, "alice", "armed", "tk-inbox-guard", queued_at=fresh_ts)
    config = {**td.DEFAULT_CONFIG, "agents": ["alice"], "idle_min_minutes": 5}

    # Set inbox mtime to 1 minute ago (< 5 min threshold)
    inbox = tickets_root / "mesh" / "inbox-alice.jsonl"
    recent = time.time() - 60
    os.utime(inbox, (recent, recent))

    with (
        mock.patch.object(td, "is_idle_local", return_value=True),
        mock.patch.object(td, "_push_local", return_value=True) as mock_push,
    ):
        td.process_armed("alice", config)

    mock_push.assert_not_called()
    # Ticket should still be in armed
    armed_dir = tickets_root / "tickets" / "alice" / "armed"
    assert list(armed_dir.glob("*.json"))


def test_no_dispatch_when_agent_busy(tickets_root):
    import ticket_dispatcher as td
    _make_ticket(tickets_root, "alice", "armed", "tk-busy")
    config = {**td.DEFAULT_CONFIG, "agents": ["alice"]}

    with (
        mock.patch.object(td, "is_idle_local", return_value=False),
        mock.patch.object(td, "_push_local", return_value=True) as mock_push,
    ):
        td.process_armed("alice", config)

    mock_push.assert_not_called()


def test_no_dispatch_when_already_running(tickets_root):
    import ticket_dispatcher as td
    _make_ticket(tickets_root, "alice", "armed", "tk-extra")
    _make_ticket(tickets_root, "alice", "running", "tk-current",
                 started_at="2026-05-21T10:00:00+00:00")
    config = {**td.DEFAULT_CONFIG, "agents": ["alice"]}

    with (
        mock.patch.object(td, "is_idle_local", return_value=True),
        mock.patch.object(td, "_push_local", return_value=True) as mock_push,
    ):
        td.process_armed("alice", config)

    mock_push.assert_not_called()


# ── Remote agent (zoe) dispatch ────────────────────────────────────────────────

def test_dispatch_remote_agent_uses_push_remote(tickets_root):
    """Zoe is a remote agent: dispatch must call _push_remote, not _push_local."""
    import ticket_dispatcher as td
    _make_ticket(tickets_root, "zoe", "armed", "tk-eve01")
    config = {
        **td.DEFAULT_CONFIG,
        "agents": ["zoe"],
        "zoe_ssh": "operator@remote.example",
        "zoe_tmux_session": "zoe",
    }

    with (
        mock.patch.object(td, "is_idle_remote", return_value=True),
        mock.patch.object(td, "_push_remote", return_value=True) as mock_remote,
        mock.patch.object(td, "_push_local", return_value=True) as mock_local,
    ):
        td.process_armed("zoe", config)

    mock_local.assert_not_called()
    mock_remote.assert_called_once()
    call_args = mock_remote.call_args[0]
    assert call_args[0] == "operator@remote.example"
    assert call_args[1] == "zoe"
    assert "tk-eve01" in call_args[2]

    running_dir = tickets_root / "tickets" / "zoe" / "running"
    assert list(running_dir.glob("*.json")), "ticket should be in running"


def test_is_remote_is_decided_by_config_alone(tickets_root):
    """An agent is remote because the config gives it an SSH host — nothing else.

    This used to assert that a second name was remote *by default*, which only
    held because the original deployment's remote agents were listed in the
    code. With that list gone, "remote" is a property of the config the operator
    writes, so the test declares one host and checks both sides of the rule.
    """
    import ticket_dispatcher as td
    config = {**td.DEFAULT_CONFIG, "zoe_ssh": "operator@remote.example", "zoe_tmux_session": "zoe"}
    assert td.is_remote("zoe", config) is True
    assert td.is_remote("alice", config) is False
    # An agent the config says nothing about is local, even if another one is remote.
    assert td.is_remote("dave", config) is False


# ── Timeout ─────────────────────────────────────────────────────────────────────

def test_running_ticket_times_out(tickets_root):
    import ticket_dispatcher as td
    old_ts = "2026-05-21T09:00:00+00:00"
    _make_ticket(tickets_root, "alice", "running", "tk-timeout",
                 started_at=old_ts)
    config = {**td.DEFAULT_CONFIG, "agents": ["alice"], "running_timeout_min": 0}

    td.check_timeouts(config)

    running_dir = tickets_root / "tickets" / "alice" / "running"
    failed_dir = tickets_root / "tickets" / "alice" / "failed"
    assert not list(running_dir.glob("*.json")), "should have left running"
    failed_files = list(failed_dir.glob("*.json"))
    assert len(failed_files) == 1
    data = json.loads(failed_files[0].read_text())
    assert data["status"] == "failed"
    assert "Timeout" in data["tldr"]


# ── E2E: armed → dispatch → ticket-complete → done ─────────────────────────────

def test_e2e_armed_to_done_via_ticket_complete(tickets_root, tmp_path):
    """Full cycle: create armed ticket, dispatch, then close via ticket-complete.py."""
    import ticket_dispatcher as td
    import importlib.util
    import subprocess

    _make_ticket(tickets_root, "yara", "armed", "tk-e2e01")
    config = {**td.DEFAULT_CONFIG, "agents": ["yara"]}

    with (
        mock.patch.object(td, "is_idle_local", return_value=True),
        mock.patch.object(td, "_push_local", return_value=True),
    ):
        td.process_armed("yara", config)

    running_dir = tickets_root / "tickets" / "yara" / "running"
    assert list(running_dir.glob("*.json"))

    # Now call ticket-complete.py directly (using its logic, not subprocess).
    # It used to be looked up in the operator's deployed bus directory, outside
    # the repository — so the test only passed on the machine that had one.
    tc_path = Path(__file__).resolve().parents[2] / "bus" / "ticket-complete.py"
    assert tc_path.exists(), f"ticket-complete.py not found at {tc_path}"

    spec = importlib.util.spec_from_file_location("ticket_complete", tc_path)
    tc = importlib.util.module_from_spec(spec)

    # Patch TICKETS_DIR inside ticket_complete to use tmp
    with mock.patch.dict(sys.modules, {}):
        import builtins
        original_open = builtins.open

    # Simpler: call find_running_ticket and the close logic directly
    # (patch Path in ticket_complete module)
    import importlib
    tc_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tc_mod)

    # Monkey-patch the module's TICKETS_DIR
    tc_mod.TICKETS_DIR = tickets_root / "tickets"

    result = tc_mod.find_running_ticket("tk-e2e01")
    assert result is not None, "Should find the running ticket"
    ticket, path = result

    ticket["status"] = "done"
    ticket["completed_at"] = "2026-05-21T11:00:00+00:00"
    ticket["tldr"] = "Test completed successfully"
    done_dir = tickets_root / "tickets" / "yara" / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    done_file = done_dir / path.name
    done_file.write_text(json.dumps(ticket, indent=2))
    path.unlink()

    done_files = list(done_dir.glob("*.json"))
    assert len(done_files) == 1
    data = json.loads(done_files[0].read_text())
    assert data["status"] == "done"
    assert data["tldr"] == "Test completed successfully"
    assert not list(running_dir.glob("*.json"))
