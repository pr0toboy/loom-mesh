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
import json
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
    assert (sender, to, priority) == ("pilot-matrix", "supervisor", "high")
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
        json.dumps({"id": "a1b2c3d4", "from": "agent-1", "body": "once"}) + "\n"
    )
    seen = []

    def flaky(room_id, text, token=None, txn=None):
        seen.append(txn)
        if len(seen) == 1:
            raise urllib.error.URLError("timed out after the server took it")

    br.send_matrix = flaky
    br.poll_inbox()   # fails, holds
    br.poll_inbox()   # retries
    assert seen == ["a1b2c3d4", "a1b2c3d4"], f"transaction id changed: {seen}"


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
