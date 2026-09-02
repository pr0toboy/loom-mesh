"""Tests for mesh_api.lib.message_summarizer."""
from __future__ import annotations

import pytest
from mesh_api.lib import message_summarizer
from mesh_api.lib.message_summarizer import summarize


@pytest.fixture(autouse=True)
def sender_roles(monkeypatch):
    """Declare which senders play the human and supervisor roles.

    The module reads these from the environment at import time and defaults to
    *empty* — no agent name is baked into the code any more. The consequence for
    the suite is not neutral: with both tuples empty, a message from the human
    facade or from the supervisor silently takes the generic branch, and the
    tests below still pass because the generic branch also truncates to 100
    chars. They would then assert nothing about the branch they are named after.
    So the roles are declared here, explicitly, per test session.
    """
    monkeypatch.setattr(message_summarizer, "_HUMAN_SENDERS", ("user-web",))
    monkeypatch.setattr(message_summarizer, "_SUPERVISOR_SENDERS", ("auto",))


# ── Spec cases (required by Alice's ticket) ───────────────────────────────────

def test_brief_extracted():
    body = "# Brief v0.30.6 for Dave\n\n## Context\nSomething..."
    assert summarize(body, "alice") == "Brief v0.30.6 for Dave"


def test_brief_versioned_h1():
    """H1 starting with # vX.Y.Z must be treated as a brief, not a delivery."""
    body = (
        "# v0.30.7 — Consume the summary field in the app\n\n"
        "**Context**: v0.30.6 backend delivered by yara exposes 'summary'.\n"
        "Dave must now show it in the bubbles.\n"
    )
    result = summarize(body, "alice")
    assert result.startswith("v0.30.7"), f"Expected versioned brief title, got: {result!r}"
    assert "delivered" not in result, f"Should not look like a delivery: {result!r}"


def test_brief_wins_over_delivery_mention_in_body():
    """A brief that quotes a past delivery must summarize as Brief, not as a delivery."""
    body = (
        "# Brief v0.30.6 backend — summary transformer\n\n"
        "**Context**: v0.30.5-readability delivered by dave introduced the collapse\n"
        "but without real semantic hierarchy.\n"
    )
    result = summarize(body, "alice")
    assert result.startswith("Brief"), f"Expected Brief, got: {result!r}"


def test_brief_heading_below_first_line():
    """The brief branch must find an H1 that is not the first line.

    Every other brief case here opens with the heading, so the fallback
    ("first line, stripped of markdown") returns the same string as the brief
    branch and the test passes either way. Here the first line is prose, so
    only a working _RE_BRIEF scan can reach the heading.
    """
    body = (
        "Quick note before the details.\n\n"
        "# Brief v0.31.0 — ticket dependencies\n\n"
        "Body follows...\n"
    )
    assert summarize(body, "alice") == "Brief v0.31.0 — ticket dependencies"


def test_mandate_extracted():
    body = "# Mandate 3 — Working dirs per ticket\n\nBlah blah blah..."
    assert summarize(body, "alice") == "Mandate 3 — Working dirs per ticket"


def test_delivery_prefix():
    body = "Delivery v0.30.5-readability by Dave\n\n## Features\n- Stuff"
    result = summarize(body, "dave")
    assert result == "v0.30.5-readability delivered by Dave"


def test_delivery_infix():
    body = "v0.30.2 delivered — DELETE working dir cleanup. 82/82 tests."
    result = summarize(body, "yara")
    # Exact, not startswith: the fallback ("first line, truncated") also starts
    # with "v0.30.2 delivered", so a startswith assertion stays green even when
    # the infix pattern never fires. Only the " by <sender>" suffix, which just
    # the delivery branch adds, tells the two apart.
    assert result == "v0.30.2 delivered by yara"


def test_delivery_agent_ver_first_line():
    body = "dave v0.30.5 delivered\n\nChangelog..."
    result = summarize(body, "dave")
    assert result == "v0.30.5 delivered by dave"


def test_ack_no_context():
    body = "ACK 7c256b9c"
    assert summarize(body, "alice") == "ACK 7c256b9c"


def test_ack_with_context():
    body = "ACK af77f316 — read receipts fix validated live: GET /conversation/alice now exposes acked"
    result = summarize(body, "alice")
    assert result.startswith("ACK af77f316 — ")
    assert len(result) <= 100


def test_plain_short_body_unchanged():
    body = "Hi, can you take a look at this?"
    assert summarize(body, "alice") == body


def test_plain_long_body_first_sentence():
    body = "The dispatcher had a bug. It did not scan armed tickets at startup. Here is why..."
    result = summarize(body, "alice")
    assert len(result) <= 100
    assert "dispatcher" in result or "bug" in result


# ── Pattern: Phase N ──────────────────────────────────────────────────────────

def test_phase_delivered():
    body = "Phase 2 delivered. Everything works fine."
    assert summarize(body, "alice") == "Phase 2 delivered"


def test_phase_started():
    body = "Here is the report — Phase 3 started on Dave's side."
    assert summarize(body, "alice") == "Phase 3 started"


# ── Pattern: Health check ─────────────────────────────────────────────────────

def test_health_check_from_auto():
    body = "health check 2026-05-24 P0=0 P1=1 P2=3 total=15 ok=true"
    result = summarize(body, "auto")
    assert "P0=0" in result
    assert "P1=1" in result
    assert "ok=true" in result


def test_health_check_from_agent():
    body = "Backend Pi P0=0 P1=0 total=82 ok=true"
    result = summarize(body, "alice")
    assert "P0=0" in result


# ── Pattern: auto ─────────────────────────────────────────────────────────────

def test_auto_ctx():
    body = "[ctx] alice 45% — session active"
    result = summarize(body, "auto")
    assert "ctx" not in result.lower() or "alice" in result


def test_auto_mesh_alert():
    body = "[mesh-alert] your msg id=abc has been waiting for a reply for 205s"
    result = summarize(body, "auto")
    assert "[mesh-alert]" not in result
    assert "abc" in result or "waiting" in result


# ── Pattern: Commit ───────────────────────────────────────────────────────────

def test_commit_with_heading():
    body = "## Fix auth\n\ncommit a1b2c3d — fix auth token expiry\n\nDetails..."
    result = summarize(body, "alice")
    assert "a1b2c3" in result
    assert "Fix auth" in result


def test_commit_without_heading():
    body = "Applied commit deadbee1 to staging."
    result = summarize(body, "alice")
    assert "deadbee" in result


# ── Special: user-web ───────────────────────────────────────────────────

def test_human_short_message_unchanged():
    body = "Can you look at the mesh-watcher log?"
    assert summarize(body, "user-web") == body


def test_human_long_message_truncated():
    body = "a" * 200
    result = summarize(body, "user-web")
    assert len(result) <= 100


def test_human_long_message_with_words_truncated():
    body = ("word " * 50).strip()  # 249 chars, spaces → _first_sentence returns first 80
    result = summarize(body, "user-web")
    assert len(result) <= 100


def test_human_medium_message_fits():
    body = "x" * 115
    result = summarize(body, "user-web")
    assert len(result) <= 100


# ── Edge cases ────────────────────────────────────────────────────────────────

def test_empty_body():
    assert summarize("", "alice") == "(empty message)"


def test_none_body():
    assert summarize(None, "alice") == "(empty message)"


def test_whitespace_only():
    assert summarize("   \n  ", "alice") == "(empty message)"


def test_emoji_only():
    body = "👍"
    assert summarize(body, "alice") == "👍"


def test_summary_max_length():
    """No summary should exceed 100 chars."""
    bodies = [
        "x" * 300,
        "ACK abcdef12 — " + "z" * 200,
        "# Brief v0.30.6 for " + "W" * 100,
        "Delivery v0.30.1-some-long-tag by " + "a" * 80,
    ]
    for body in bodies:
        result = summarize(body, "alice")
        assert len(result) <= 100, f"Too long ({len(result)}): {result!r}"


def test_exception_fallback():
    """summarize() must never raise — bad input returns truncated body."""
    # Passing non-string from_ shouldn't crash
    result = summarize("Hello world", None)
    assert isinstance(result, str)


# ── Integration: summary appears in /conversation ─────────────────────────────

def test_conversation_items_have_summary(tmp_path):
    """GET /conversation → each message item has a 'summary' field."""
    import json, os
    from pathlib import Path as P

    # Minimal in-memory test without full FastAPI client
    from mesh_api.lib import conversation_io as cio
    from unittest.mock import patch

    mesh = tmp_path / "mesh"
    mesh.mkdir()
    tickets = tmp_path / "tickets"
    tickets.mkdir()
    inbox = mesh / "inbox-alice.jsonl"
    inbox.write_text(json.dumps({
        "id": "msg-summary-test",
        "from": "user-web",
        "to": "alice",
        "priority": "normal",
        "body": "Did you see this?",
        "ts": "2026-05-24T10:00+02:00",
    }) + "\n")

    with patch.object(cio, "MESH_DIR", mesh), patch.object(cio, "TICKETS_DIR", tickets):
        result = cio.get_conversation("alice")

    items = result["items"]
    assert len(items) == 1
    assert "summary" in items[0]
    assert items[0]["summary"] == "Did you see this?"
