"""What the bridge does when the homeserver stops answering.

These tests exist because of a real morning: the machine's DNS lost the
homeserver's name, `/sync` raised for nine hours, the loop logged the same line
every three seconds and told nobody. The pilot kept typing into a room whose
messages reached no agent, and the outage was found by a human asking why the
fleet had gone quiet — which is the definition of an unwatched failure.

So the properties under test are not the happy path:
  1. a sustained sync outage escalates **onto the bus**, because an alert about
     the bridge cannot travel through the bridge;
  2. a post that fails does **not** advance the inbox cursor, because the reply
     it carries is the agent's only copy;
  3. the retry carries the same Matrix transaction id, so holding a message
     cannot turn "lost once" into "said twice";
  4. what is held is a *transport* failure — a defect in the bridge is skipped
     loudly instead of wedging every room behind it forever.

Both run against a fake `send.py` and a fake homeserver; nothing here touches a
deployed mesh or the network.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import json
import time
import urllib.error
from pathlib import Path

import pytest

BRIDGE_PY = Path(__file__).resolve().parents[1] / "bridge.py"


def _load_bridge_module():
    spec = importlib.util.spec_from_file_location("loom_bridge", BRIDGE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bridge_env(tmp_path: Path):
    """A Bridge whose send.py is a stub recording every message it is handed."""
    mod = _load_bridge_module()
    sent = tmp_path / "sent.jsonl"
    fake_send = tmp_path / "send.py"
    fake_send.write_text(
        "import json, sys\n"
        f"open({str(sent)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    inbox = tmp_path / "inbox-pilot-matrix.jsonl"
    inbox.write_text("")
    cfg = {
        "homeserver": "https://hs.example.invalid",
        "bot_token": "t", "bot_user": "@bot:example.invalid",
        "pilot_user": "@pilot:example.invalid",
        "mesh_peer": "pilot-matrix",
        "mesh_send": str(fake_send),
        "inbox": str(inbox),
        "state_file": str(tmp_path / "state.json"),
        "rooms": {"agent-1": "!a1:example.invalid",
                  "supervisor": "!sup:example.invalid"},
        "sync_alert_after": 3,
        "sync_alert_seconds": 0,
    }
    br = mod.Bridge(cfg)

    def messages():
        if not sent.exists():
            return []
        return [json.loads(l) for l in sent.read_text().splitlines() if l.strip()]

    return mod, br, inbox, messages


def test_sustained_sync_outage_is_escalated_over_the_bus(bridge_env):
    """The failure that went unnoticed for nine hours now reaches an agent."""
    _mod, br, _inbox, messages = bridge_env
    br._req = lambda *a, **k: (_ for _ in ()).throw(
        urllib.error.URLError("Name or service not known")
    )

    for _ in range(2):  # under the threshold: a blip must stay quiet
        br.poll_matrix()
    assert messages() == [], "alerted on a blip"

    br.poll_matrix()  # third consecutive failure crosses sync_alert_after
    out = messages()
    assert len(out) == 1, "a sustained outage told nobody"
    sender, to, priority, body = out[0]
    assert (sender, to, priority) == ("bridge", "supervisor", "high")
    assert "[bridge]" in body and "Name or service not known" in body

    br.poll_matrix()  # still down: one notice per outage, not one per cycle
    assert len(messages()) == 1

    # ... and the recovery is announced to the same agent, so the thread closes.
    br._req = lambda *a, **k: {"next_batch": "s1", "rooms": {}}
    br.poll_matrix()
    out = messages()
    assert len(out) == 2 and "recovered" in out[1][3]


def test_escalation_is_not_silent_when_no_alert_agent_is_configured(bridge_env, capsys):
    """An unconfigured guard must still leave the outage somewhere findable."""
    _mod, br, _inbox, messages = bridge_env
    br.alert_peer = None
    br._req = lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("down"))

    for _ in range(3):
        br.poll_matrix()

    assert messages() == []
    printed = capsys.readouterr().out
    assert "OUTAGE" in printed and "no alert_agent" in printed


def test_failed_post_keeps_the_message_for_the_next_cycle(bridge_env):
    """A transient post failure must delay a reply, never consume it."""
    _mod, br, inbox, _messages = bridge_env
    inbox.write_text(json.dumps({"from": "agent-1", "body": "the result"}) + "\n")

    attempts = []

    def failing_post(room_id, text, token=None, txn=None):
        attempts.append(text)
        raise urllib.error.URLError("Name or service not known")

    br.send_matrix = failing_post
    br.poll_inbox()
    assert br.state["inbox_offset"] == 0, "cursor moved past an undelivered reply"

    posted = []
    br.send_matrix = lambda room_id, text, token=None, txn=None: posted.append(text)
    br.poll_inbox()
    assert posted == ["the result"], "the held reply was never delivered"
    assert br.state["inbox_offset"] == 1


def test_a_payload_the_server_will_never_accept_is_skipped(bridge_env):
    """One poison message must not wedge every other room behind it."""
    _mod, br, inbox, _messages = bridge_env
    inbox.write_text(
        json.dumps({"from": "agent-1", "body": "malformed"}) + "\n"
        + json.dumps({"from": "supervisor", "body": "next in line"}) + "\n"
    )
    posted = []

    def post(room_id, text, token=None, txn=None):
        if text == "malformed":
            raise urllib.error.HTTPError("u", 400, "Bad Request", {}, None)
        posted.append(text)

    br.send_matrix = post
    br.poll_inbox()
    assert posted == ["next in line"]
    assert br.state["inbox_offset"] == 2


def test_a_retried_post_reuses_its_transaction_id(bridge_env):
    """Holding a message is only an improvement if the retry cannot double-post.

    Matrix dedups on the transaction id, so the retry has to carry the same one.
    A clock-based id would be fresh on each attempt and turn a lost reply into a
    reply said twice.
    """
    _mod, br, inbox, _messages = bridge_env
    inbox.write_text(
        json.dumps({"id": "a1b2c3d4", "ts": "2026-09-16T15:00:00+02:00",
                    "from": "agent-1", "body": "once"}) + "\n"
    )
    seen = []

    def flaky(room_id, text, token=None, txn=None):
        seen.append(txn)
        if len(seen) == 1:
            raise urllib.error.URLError("timed out after the server took it")

    br.send_matrix = flaky
    br.poll_inbox()   # fails, holds
    br.poll_inbox()   # retries
    assert len(seen) == 2 and seen[0] == seen[1], f"transaction id changed: {seen}"
    assert "a1b2c3d4" in seen[0] and "2026-09-16" in seen[0], seen[0]


def test_a_defect_in_the_bridge_does_not_wedge_every_room(bridge_env):
    """A bug is not a transient condition, and must not stop the traffic.

    Classifying failures negatively ("anything that is not a 4xx is transient")
    swept programming errors into the retry-forever branch: one TypeError and
    the whole mesh->Matrix direction stops, silently, behind a single line.
    """
    _mod, br, inbox, _messages = bridge_env
    inbox.write_text(
        json.dumps({"from": "agent-1", "body": "triggers the bug"}) + "\n"
        + json.dumps({"from": "supervisor", "body": "must still go out"}) + "\n"
    )
    posted = []

    def buggy(room_id, text, token=None, txn=None):
        if "bug" in text:
            raise TypeError("unexpected keyword argument")
        posted.append(text)

    br.send_matrix = buggy
    br.poll_inbox()
    assert posted == ["must still go out"]
    assert br.state["inbox_offset"] == 2


def test_auth_failure_holds_rather_than_drops(bridge_env):
    """A revoked token is the operator's to fix; the reply waits for them."""
    _mod, br, inbox, _messages = bridge_env
    inbox.write_text(json.dumps({"from": "agent-1", "body": "keep me"}) + "\n")

    def forbidden(room_id, text, token=None, txn=None):
        raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)

    br.send_matrix = forbidden
    br.poll_inbox()
    assert br.state["inbox_offset"] == 0


# --------------------------------------------------------------------------
# The gate review found these by mutation: each one below kills a mutant that
# survived the first round, or covers a blocker the first round missed.
# --------------------------------------------------------------------------


def test_a_5xx_is_held_not_dropped(bridge_env):
    """The commonest transport failure after DNS, and nothing covered it.

    A mutant turning `>= 500` into `>= 600` survived the first test round: no
    test ever posted against a failing server, only against a missing one.
    """
    _mod, br, inbox, _messages = bridge_env
    inbox.write_text(json.dumps({"from": "agent-1", "body": "held"}) + "\n")
    br.send_matrix = lambda *a, **k: (_ for _ in ()).throw(
        urllib.error.HTTPError("u", 502, "Bad Gateway", {}, None)
    )
    br.poll_inbox()
    assert br.state["inbox_offset"] == 0


def test_a_refused_agent_token_falls_back_to_the_bot(bridge_env):
    """A rotated token kills one agent's replies; the bot can still deliver."""
    _mod, br, inbox, _messages = bridge_env
    br.agent_tokens = {"agent-1": "stale-token"}
    inbox.write_text(json.dumps({"from": "agent-1", "body": "still arrives"}) + "\n")
    tokens_tried = []

    def post(room_id, text, token=None, txn=None):
        tokens_tried.append(token)
        if token is not None:
            raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)

    br.send_matrix = post
    br.poll_inbox()
    assert tokens_tried == ["stale-token", None], "never retried as the bot"
    assert br.state["inbox_offset"] == 1, "delivered, so the cursor must move"


def test_a_second_outage_alerts_again(bridge_env):
    """`sync_alerted` has to be cleared on recovery, or only the first is seen.

    A mutant that never reset the flag survived: every test stopped at one
    outage, and a bridge that warns once per process is a bridge that warns once
    a year.
    """
    _mod, br, _inbox, messages = bridge_env
    down = lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("down"))
    up = lambda *a, **k: {"next_batch": "s", "rooms": {}}

    br._req = down
    for _ in range(3):
        br.poll_matrix()
    br._req = up
    br.poll_matrix()
    br._req = down
    for _ in range(3):
        br.poll_matrix()

    bodies = [m[3] for m in messages()]
    assert sum("failing since" in b for b in bodies) == 2, bodies


def test_a_held_line_persists_its_cursor_to_disk(bridge_env):
    """Holding in memory only would replay the whole inbox after a restart."""
    _mod, br, inbox, _messages = bridge_env
    inbox.write_text(
        json.dumps({"from": "agent-1", "body": "one"}) + "\n"
        + json.dumps({"from": "agent-1", "body": "two"}) + "\n"
    )
    posted = []

    def post(room_id, text, token=None, txn=None):
        if text == "two":
            raise urllib.error.URLError("down")
        posted.append(text)

    br.send_matrix = post
    br.poll_inbox()
    on_disk = json.loads(br.state_file.read_text())
    assert on_disk["inbox_offset"] == 1, "the held position never reached disk"


def test_a_read_interrupted_mid_response_is_held(bridge_env):
    """`http.client` errors are not `OSError`, so the first rule dropped them."""
    import http.client

    _mod, br, inbox, _messages = bridge_env
    inbox.write_text(json.dumps({"from": "agent-1", "body": "keep"}) + "\n")
    br.send_matrix = lambda *a, **k: (_ for _ in ()).throw(
        http.client.IncompleteRead(b"half")
    )
    br.poll_inbox()
    assert br.state["inbox_offset"] == 0


def test_a_stuck_hold_escalates_then_lets_go(bridge_env):
    """Protecting one reply must not silently freeze every room forever.

    This is the regression the review caught: a 500 on one bad room id is held
    by the transport rule, `/sync` stays healthy, and nothing on the bus ever
    says that the whole agent->pilot direction has stopped.
    """
    _mod, br, inbox, messages = bridge_env
    br.hold_alert_seconds, br.hold_skip_seconds = 0, 0.01
    inbox.write_text(
        json.dumps({"from": "agent-1", "body": "the stuck one"}) + "\n"
        + json.dumps({"from": "supervisor", "body": "behind it"}) + "\n"
    )
    posted = []

    def post(room_id, text, token=None, txn=None):
        if "stuck" in text:
            raise urllib.error.HTTPError("u", 500, "M_UNKNOWN", {}, None)
        posted.append(text)

    br.send_matrix = post
    br.poll_inbox()                      # first failure: starts the hold
    assert br.state["inbox_offset"] == 0
    time.sleep(0.05)
    br.poll_inbox()                      # deadline passed: warn, give up, move on
    bodies = [m[3] for m in messages()]
    assert any("stuck for" in b for b in bodies), bodies
    assert any("giving up" in b for b in bodies), bodies
    assert posted == ["behind it"], "the queue stayed frozen behind one message"
    assert br.state["inbox_offset"] == 2


def test_a_gap_after_an_outage_is_backfilled(bridge_env):
    """A long outage exceeds the window /sync returns, and it says so.

    Conduit caps an incremental sync at ten events per room and ignores a
    filter asking for more, so the messages a pilot sent during the outage —
    the ones that matter most — fall outside the timeline entirely.
    """
    _mod, br, _inbox, _messages = bridge_env
    room = "!a1:example.invalid"
    br.state["since"] = "old"
    br.state["last_events"] = {room: "$known"}
    relayed = []
    br._to_mesh = lambda agent, body, room_id=None: relayed.append(body)

    def fake_req(method, path, params=None, body=None, token=None):
        if path.endswith("/sync"):
            return {"next_batch": "s2", "rooms": {"join": {room: {"timeline": {
                "limited": True, "prev_batch": "p1",
                "events": [_pilot_event("$e3", "third")]}}}}}
        assert "/messages" in path and params["dir"] == "b"
        return {"chunk": [_pilot_event("$e2", "second"),
                          _pilot_event("$e1", "first"),
                          {"event_id": "$known", "type": "m.room.message"}],
                "end": "p2"}

    br._req = fake_req
    br.send_read_receipt = lambda *a, **k: None
    br.poll_matrix()
    assert relayed == ["first", "second", "third"], relayed


def test_a_gap_is_not_backfilled_without_a_known_last_event(bridge_env):
    """No ground truth, no backfill — or a first run replays a year of history.

    A bridge that guesses here floods the mesh with old messages; that has
    happened, and it buries the live ones under the archive.
    """
    _mod, br, _inbox, _messages = bridge_env
    room = "!a1:example.invalid"
    br.state["since"] = "old"
    relayed, paged = [], []
    br._to_mesh = lambda agent, body, room_id=None: relayed.append(body)

    def fake_req(method, path, params=None, body=None, token=None):
        if path.endswith("/sync"):
            return {"next_batch": "s2", "rooms": {"join": {room: {"timeline": {
                "limited": True, "prev_batch": "p1",
                "events": [_pilot_event("$e3", "only the visible one")]}}}}}
        paged.append(path)
        return {"chunk": [], "end": None}

    br._req = fake_req
    br.send_read_receipt = lambda *a, **k: None
    br.poll_matrix()
    assert paged == [], "walked back through history with nothing to stop it"
    assert relayed == ["only the visible one"]


def test_a_gap_too_wide_to_close_is_reported(bridge_env):
    """Never announce a partial recovery as a complete one."""
    _mod, br, _inbox, messages = bridge_env
    room = "!a1:example.invalid"
    br.state["since"] = "old"
    br.state["last_events"] = {room: "$unreachable"}
    br.backfill_max_pages = 2
    br._to_mesh = lambda agent, body, room_id=None: None

    def fake_req(method, path, params=None, body=None, token=None):
        if path.endswith("/sync"):
            return {"next_batch": "s2", "rooms": {"join": {room: {"timeline": {
                "limited": True, "prev_batch": "p1",
                "events": [_pilot_event("$e9", "visible")]}}}}}
        return {"chunk": [_pilot_event("$e8", "older")], "end": "more"}

    br._req = fake_req
    br.send_read_receipt = lambda *a, **k: None
    br.poll_matrix()
    bodies = [m[3] for m in messages()]
    assert any("wider than 2 pages" in b for b in bodies), bodies


def _pilot_event(event_id, body):
    return {"event_id": event_id, "type": "m.room.message",
            "sender": "@pilot:example.invalid",
            "content": {"msgtype": "m.text", "body": body}}


# --------------------------------------------------------------------------
# Second gate round. Every test below kills a mutant that survived the first
# one, or covers a blocker that round introduced.
# --------------------------------------------------------------------------


def test_a_total_outage_does_not_burn_the_hold_deadline(bridge_env, monkeypatch):
    """The deadline is for a message the link refuses, not for a dead link.

    Replayed against the real nine-hour outage, a deadline that kept ticking
    delivered three replies out of twelve where plain waiting delivered all
    twelve: it turned a delay into a loss, on the exact incident this work
    exists for. While /sync is failing there is nowhere to post anything, so
    dropping the message buys nothing at all.
    """
    mod, br, inbox, _messages = bridge_env
    clock = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
    br.hold_alert_seconds, br.hold_skip_seconds = 300, 3600
    inbox.write_text(json.dumps({"from": "agent-1", "body": "written mid-outage"}) + "\n")

    br._req = lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("no DNS"))
    br.send_matrix = lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("no DNS"))

    for _ in range(12):           # nine hours of a dead homeserver
        br.poll_matrix()
        br.poll_inbox()
        clock[0] += 2700          # 45 min per cycle
    assert br.state["inbox_offset"] == 0, "gave up on a reply the outage would have delivered"

    posted = []
    br._req = lambda *a, **k: {"next_batch": "s", "rooms": {}}
    br.send_matrix = lambda room_id, text, token=None, txn=None: posted.append(text)
    br.poll_matrix()
    br.poll_inbox()
    assert posted == ["written mid-outage"], "the held reply never went out on recovery"


def test_the_backfill_marker_is_recorded_without_help(bridge_env):
    """The mutant that mattered most: nothing ever wrote the marker.

    The first round's tests seeded `last_events` by hand, so they passed while
    the backfill was dead on every real deployment — it can only walk back to a
    marker that something records. Here the bridge is driven exactly as it runs:
    a first sync, then a gap.
    """
    _mod, br, _inbox, _messages = bridge_env
    room = "!a1:example.invalid"
    relayed = []
    br._to_mesh = lambda agent, body, room_id=None: relayed.append(body)
    br.send_read_receipt = lambda *a, **k: None
    responses = [
        # first sync: establishes where the room stands
        {"next_batch": "s1", "rooms": {"join": {room: {"timeline": {
            "events": [_pilot_event("$e0", "before the outage")]}}}}},
        # after the outage: only the tail, flagged limited
        {"next_batch": "s2", "rooms": {"join": {room: {"timeline": {
            "limited": True, "prev_batch": "p1",
            "events": [_pilot_event("$e2", "second")]}}}}},
    ]

    def fake_req(method, path, params=None, body=None, token=None):
        if path.endswith("/sync"):
            return responses.pop(0)
        return {"chunk": [_pilot_event("$e1", "first"),
                          {"event_id": "$e0", "type": "m.room.message"}], "end": "p2"}

    br._req = fake_req
    br.poll_matrix()   # first sync: seeds the marker, relays nothing
    assert br.state["last_events"][room] == "$e0", "no marker recorded on the first sync"
    br.poll_matrix()   # the gap, closed against that marker
    assert relayed == ["first", "second"], relayed


def test_the_backfill_stops_at_the_marker_inside_a_page(bridge_env):
    """Cutting the chunk at the marker is what stops a re-relay.

    A mutant that found the marker but kept the whole page survived: with a
    hundred events per page that is up to ninety-nine messages sent twice.
    """
    _mod, br, _inbox, _messages = bridge_env
    room = "!a1:example.invalid"
    br.state["since"] = "old"
    br.state["last_events"] = {room: "$known"}
    relayed = []
    br._to_mesh = lambda agent, body, room_id=None: relayed.append(body)
    br.send_read_receipt = lambda *a, **k: None

    def fake_req(method, path, params=None, body=None, token=None):
        if path.endswith("/sync"):
            return {"next_batch": "s2", "rooms": {"join": {room: {"timeline": {
                "limited": True, "prev_batch": "p1",
                "events": [_pilot_event("$new", "new")]}}}}}
        return {"chunk": [_pilot_event("$gap", "in the gap"),
                          {"event_id": "$known", "type": "m.room.message"},
                          _pilot_event("$ancient", "already relayed last week")],
                "end": "p2"}

    br._req = fake_req
    br.poll_matrix()
    assert relayed == ["in the gap", "new"], relayed


def test_the_backfill_follows_the_end_token_to_the_next_page(bridge_env):
    """One page is not a gap. Ignoring `end` silently truncates every long one."""
    _mod, br, _inbox, _messages = bridge_env
    room = "!a1:example.invalid"
    br.state["since"] = "old"
    br.state["last_events"] = {room: "$known"}
    relayed, pages = [], []
    br._to_mesh = lambda agent, body, room_id=None: relayed.append(body)
    br.send_read_receipt = lambda *a, **k: None

    def fake_req(method, path, params=None, body=None, token=None):
        if path.endswith("/sync"):
            return {"next_batch": "s2", "rooms": {"join": {room: {"timeline": {
                "limited": True, "prev_batch": "p1",
                "events": [_pilot_event("$e3", "third")]}}}}}
        pages.append(params["from"])
        if params["from"] == "p1":
            return {"chunk": [_pilot_event("$e2", "second")], "end": "p2"}
        return {"chunk": [_pilot_event("$e1", "first"),
                          {"event_id": "$known", "type": "m.room.message"}], "end": None}

    br._req = fake_req
    br.poll_matrix()
    assert pages == ["p1", "p2"], pages
    assert relayed == ["first", "second", "third"], relayed


def test_history_running_out_before_the_marker_is_reported(bridge_env):
    """An empty page is not a closed gap — it means the marker is gone.

    Treating it as "done" relays everything walked back so far as if it were
    recent. That is how an archive lands on top of a live conversation.
    """
    _mod, br, _inbox, messages = bridge_env
    room = "!a1:example.invalid"
    br.state["since"] = "old"
    br.state["last_events"] = {room: "$vanished"}
    br._to_mesh = lambda agent, body, room_id=None: None
    br.send_read_receipt = lambda *a, **k: None

    def fake_req(method, path, params=None, body=None, token=None):
        if path.endswith("/sync"):
            return {"next_batch": "s2", "rooms": {"join": {room: {"timeline": {
                "limited": True, "prev_batch": "p1",
                "events": [_pilot_event("$e9", "visible")]}}}}}
        if params["from"] == "p1":
            return {"chunk": [_pilot_event("$old", "ancient")], "end": "p2"}
        return {"chunk": [], "end": None}

    br._req = fake_req
    br.poll_matrix()
    bodies = [m[3] for m in messages()]
    assert any("no longer exists" in b for b in bodies), bodies


def test_a_gap_with_no_marker_is_escalated_not_just_logged(bridge_env):
    """Every deployment meets this path first, so silence here is the default.

    The recovery notice promises that a gap it cannot close is reported; a log
    line nobody reads is not that.
    """
    _mod, br, _inbox, messages = bridge_env
    room = "!a1:example.invalid"
    br.state["since"] = "old"
    br._to_mesh = lambda agent, body, room_id=None: None
    br.send_read_receipt = lambda *a, **k: None
    br._req = lambda *a, **k: {"next_batch": "s2", "rooms": {"join": {room: {"timeline": {
        "limited": True, "prev_batch": "p1",
        "events": [_pilot_event("$e1", "only this")]}}}}}

    br.poll_matrix()
    bodies = [m[3] for m in messages()]
    assert any("no recorded last event" in b for b in bodies), bodies
    br.poll_matrix()  # ... but it says it once per room, not once per sync
    assert sum("no recorded last event" in m[3] for m in messages()) == 1


def test_the_sync_alert_waits_for_seconds_not_attempts(bridge_env, monkeypatch):
    """Twenty attempts is a minute on a DNS failure and twenty on a dead peer.

    Every earlier test set the threshold to zero, so the clock it now depends on
    was never exercised.
    """
    mod, br, _inbox, messages = bridge_env
    clock = [500.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
    br.sync_alert_seconds, br.sync_alert_after = 60, 3
    br._req = lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("down"))

    for _ in range(10):      # ten fast failures inside twenty seconds
        br.poll_matrix()
        clock[0] += 2
    assert messages() == [], "alerted on attempts, before the outage was real"

    clock[0] += 60
    br.poll_matrix()
    assert len(messages()) == 1


def test_a_stalled_hold_warns_once_not_every_cycle(bridge_env, monkeypatch):
    """A guard that repeats every second is noise, and noise is not a signal."""
    mod, br, inbox, messages = bridge_env
    clock = [10.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
    br.hold_alert_seconds, br.hold_skip_seconds = 10, 100000
    inbox.write_text(json.dumps({"from": "agent-1", "body": "stuck"}) + "\n")
    br.send_matrix = lambda *a, **k: (_ for _ in ()).throw(
        urllib.error.HTTPError("u", 500, "M_UNKNOWN", {}, None))

    for _ in range(40):
        br.poll_inbox()
        clock[0] += 1
    assert sum("stuck for" in m[3] for m in messages()) == 1, [m[3] for m in messages()]


def test_the_escalation_falls_back_when_the_roster_refuses_bridge(bridge_env, tmp_path):
    """The fallback path had no test, and it is the one a fresh install takes."""
    _mod, br, _inbox, messages = bridge_env
    refusing = tmp_path / "send_refusing.py"
    sent = tmp_path / "sent2.jsonl"
    refusing.write_text(
        "import json, sys\n"
        "if sys.argv[1] == 'bridge':\n"
        "    sys.stderr.write(\"error: unknown from peer 'bridge'\")\n"
        "    sys.exit(2)\n"
        f"open({str(sent)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    br.mesh_send = str(refusing)
    br._escalate("[bridge] something broke")

    lines = [json.loads(l) for l in sent.read_text().splitlines() if l.strip()]
    assert lines and lines[0][0] == "pilot-matrix", lines
    assert messages() == []


def test_a_broken_mesh_send_cannot_kill_the_loop(bridge_env, capsys):
    """The guard must not crash the service it guards.

    A mistyped `mesh_send` raises FileNotFoundError, which is not a
    CalledProcessError: uncaught, it propagates out of the sync failure path and
    restarts the bridge every minute, alerting no one.
    """
    _mod, br, _inbox, _messages = bridge_env
    br.mesh_send = "/nonexistent/send.py"
    br._escalate("[bridge] something broke")   # must not raise
    assert "undeliverable" in capsys.readouterr().out


def test_a_pilot_message_the_bus_refuses_is_reported_in_its_room(bridge_env):
    """The read receipt already said "seen"; the bus then dropped the message."""
    _mod, br, _inbox, _messages = bridge_env
    br.mesh_send = "/nonexistent/send.py"
    posted = []
    br.send_matrix = lambda room_id, text, token=None, txn=None: posted.append(text)

    br._to_mesh("agent-1", "do the thing", "!a1:example.invalid")
    assert posted and "NOT delivered to agent-1" in posted[0], posted


# --------------------------------------------------------------------------
# Third gate round.
# --------------------------------------------------------------------------


def test_a_policy_refusal_is_not_routed_around(bridge_env, tmp_path):
    """The refusal IS the feature; retrying as the pilot walks through it.

    The fleet policy lets the operator put an agent out of play, and exempts
    human facades so they can still reach it. A daemon inheriting that exemption
    wakes a paused agent with a high-priority message signed as the operator —
    which is precisely what the system-peer category exists to prevent.
    """
    _mod, br, _inbox, messages = bridge_env
    sent = tmp_path / "sent_policy.jsonl"
    refusing = tmp_path / "send_policy.py"
    refusing.write_text(
        "import json, sys\n"
        "if sys.argv[1] == 'bridge':\n"
        "    sys.stderr.write(\"error: 'supervisor' is paused — only the operator may write\")\n"
        "    sys.exit(3)\n"
        f"open({str(sent)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    br.mesh_send = str(refusing)
    br._escalate("[bridge] sync is down")

    assert not sent.exists(), "wrote as the pilot past a policy refusal"
    assert messages() == []


def test_an_unknown_sender_still_falls_back(bridge_env, tmp_path):
    """The fallback must survive the fix above: a fresh install needs it."""
    _mod, br, _inbox, _messages = bridge_env
    sent = tmp_path / "sent_unknown.jsonl"
    refusing = tmp_path / "send_unknown.py"
    refusing.write_text(
        "import json, sys\n"
        "if sys.argv[1] == 'bridge':\n"
        "    sys.stderr.write(\"error: unknown from peer 'bridge' (known: ...)\")\n"
        "    sys.exit(2)\n"
        f"open({str(sent)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    br.mesh_send = str(refusing)
    br._escalate("[bridge] sync is down")
    lines = [json.loads(l) for l in sent.read_text().splitlines() if l.strip()]
    assert lines and lines[0][0] == "pilot-matrix", lines


def test_a_later_gap_in_the_same_room_is_reported_again(bridge_env):
    """Deduplicating by room alone silenced every outage after the first."""
    _mod, br, _inbox, messages = bridge_env
    room = "!a1:example.invalid"
    br._to_mesh = lambda agent, body, room_id=None: None
    br.send_read_receipt = lambda *a, **k: None
    br.state["since"] = "old"

    for marker, name in (("$m1", "first outage"), ("$m2", "weeks later")):
        br.state["last_events"] = {room: marker}
        br._req = lambda *a, **k: (
            {"next_batch": "s", "rooms": {"join": {room: {"timeline": {
                "limited": True, "prev_batch": "p1",
                "events": [_pilot_event("$tail" + marker, name)]}}}}}
            if a[1].endswith("/sync") else
            (_ for _ in ()).throw(urllib.error.URLError("messages endpoint down"))
        )
        br.poll_matrix()

    notices = [m[3] for m in messages() if "could not close the gap" in m[3]]
    assert len(notices) == 2, notices


def test_undatable_history_is_reported_but_not_relayed(bridge_env):
    """When the marker is gone, nothing walked back can be dated — so none of
    it may be relayed as if it were recent. The comment said so; the code did
    the opposite, and pushed thirty old messages into an agent's inbox."""
    _mod, br, _inbox, messages = bridge_env
    room = "!a1:example.invalid"
    br.state["since"] = "old"
    br.state["last_events"] = {room: "$vanished"}
    relayed = []
    br._to_mesh = lambda agent, body, room_id=None: relayed.append(body)
    br.send_read_receipt = lambda *a, **k: None

    def fake_req(method, path, params=None, body=None, token=None):
        if path.endswith("/sync"):
            return {"next_batch": "s2", "rooms": {"join": {room: {"timeline": {
                "limited": True, "prev_batch": "p1",
                "events": [_pilot_event("$new", "genuinely new")]}}}}}
        if params["from"] == "p1":
            return {"chunk": [_pilot_event("$old1", "ancient one"),
                              _pilot_event("$old2", "ancient two")], "end": "p2"}
        return {"chunk": [], "end": None}

    br._req = fake_req
    br.poll_matrix()
    assert relayed == ["genuinely new"], relayed
    bodies = [m[3] for m in messages()]
    assert any("were NOT relayed" in b for b in bodies), bodies


def test_the_deadline_resumes_after_recovery_if_the_room_stays_dead(bridge_env, monkeypatch):
    """Freezing the clock must not disable it — two mutants lived in that gap.

    One never pushed the hold forward (giving up seconds after recovery), the
    other never advanced the tick (never giving up at all, the original wedge).
    """
    mod, br, inbox, messages = bridge_env
    clock = [100.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
    br.hold_alert_seconds, br.hold_skip_seconds = 300, 3600
    inbox.write_text(json.dumps({"from": "agent-1", "body": "for a dead room"}) + "\n")
    br.send_matrix = lambda *a, **k: (_ for _ in ()).throw(
        urllib.error.HTTPError("u", 500, "M_UNKNOWN", {}, None))

    br._req = lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("no DNS"))
    for _ in range(8):                       # two hours of total outage
        br.poll_matrix(); br.poll_inbox(); clock[0] += 900
    assert br.state["inbox_offset"] == 0, "gave up during the outage"

    br._req = lambda *a, **k: {"next_batch": "s", "rooms": {}}
    br.poll_matrix(); br.poll_inbox()
    clock[0] += 400                          # past the alert, not the deadline
    br.poll_matrix(); br.poll_inbox()
    assert any("stuck for" in m[3] for m in messages()), "no stall alarm after recovery"
    assert br.state["inbox_offset"] == 0, "gave up too early after recovery"

    clock[0] += 3400                         # past the deadline, sync healthy
    br.poll_matrix(); br.poll_inbox()
    assert br.state["inbox_offset"] == 1, "never gave up, the queue stayed wedged"


def test_the_marker_is_the_most_recent_event_not_just_any(bridge_env):
    """Seeding or recording the *oldest* event replays the whole window.

    Every earlier test used a single event per room, so "first" and "last" were
    the same thing and three mutants lived there comfortably.
    """
    _mod, br, _inbox, _messages = bridge_env
    room = "!a1:example.invalid"
    br._to_mesh = lambda agent, body, room_id=None: None
    br.send_read_receipt = lambda *a, **k: None
    responses = [
        {"next_batch": "s1", "rooms": {"join": {room: {"timeline": {"events": [
            _pilot_event("$a", "older"), _pilot_event("$b", "newer")]}}}}},
        {"next_batch": "s2", "rooms": {"join": {room: {"timeline": {"events": [
            _pilot_event("$c", "third"), _pilot_event("$d", "fourth")]}}}}},
    ]
    br._req = lambda *a, **k: responses.pop(0)

    br.poll_matrix()
    assert br.state["last_events"][room] == "$b", "seeded the oldest event"
    br.poll_matrix()
    assert br.state["last_events"][room] == "$d", "incremental marker not advanced"


def test_a_failing_messages_endpoint_is_reported_as_such(bridge_env):
    """Two mutants: reporting nothing, and blaming the marker for a reset."""
    _mod, br, _inbox, messages = bridge_env
    room = "!a1:example.invalid"
    br.state["since"] = "old"
    br.state["last_events"] = {room: "$known"}
    br._to_mesh = lambda agent, body, room_id=None: None
    br.send_read_receipt = lambda *a, **k: None

    def fake_req(method, path, params=None, body=None, token=None):
        if path.endswith("/sync"):
            return {"next_batch": "s2", "rooms": {"join": {room: {"timeline": {
                "limited": True, "prev_batch": "p1",
                "events": [_pilot_event("$e", "visible")]}}}}}
        raise urllib.error.URLError("connection reset")

    br._req = fake_req
    br.poll_matrix()
    bodies = [m[3] for m in messages() if "could not close the gap" in m[3]]
    assert bodies, "a failing backfill was reported to nobody"
    assert "stopped answering" in bodies[0], bodies[0]
    assert "no longer exists" not in bodies[0], "blamed the marker for a network fault"


def test_an_outage_notice_mentions_the_replies_held_behind_it(bridge_env, monkeypatch):
    """While only /sync is dead the stall alarm is frozen by design, so the
    outage notice is the only place those queued replies can be named."""
    mod, br, inbox, messages = bridge_env
    clock = [10.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
    inbox.write_text(json.dumps({"from": "agent-1", "body": "queued"}) + "\n")
    br.send_matrix = lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("down"))
    br._req = lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("down"))

    br.poll_inbox()          # starts the hold
    for _ in range(4):
        br.poll_matrix()
        clock[0] += 5
    bodies = [m[3] for m in messages()]
    assert any("agent reply is also held" in b for b in bodies), bodies


def test_a_system_peer_is_refused_as_a_recipient(tmp_path):
    """It owns no inbox: accepting it writes a file nobody will ever read."""
    import subprocess as sp

    bus = Path(__file__).resolve().parents[3] / "bus"
    home = tmp_path / "mesh"
    home.mkdir()
    (home / "peers.py").write_text(
        'AGENTS = {"agent-1"}\n'
        'PILOT_PEERS = {"pilot-matrix"}\n'
        'SYSTEM_PEERS = {"bridge"}\n'
    )
    env = {**os.environ, "MESH_HOME": str(home)}
    out = sp.run([sys.executable, str(bus / "send.py"), "agent-1", "bridge",
                  "normal", "hello"], capture_output=True, text=True, env=env)
    assert out.returncode == 2, out
    assert "system sender" in out.stderr, out.stderr
    assert not (home / "inbox-bridge.jsonl").exists()

    ok = sp.run([sys.executable, str(bus / "send.py"), "bridge", "agent-1",
                 "high", "outage"], capture_output=True, text=True, env=env)
    assert ok.returncode == 0, ok.stderr   # ... but it remains a valid sender
