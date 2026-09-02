"""Tests for the bus: send, read, ack, and the guards that refuse a message.

The bus is the part of this system where a mistake is not recoverable. A message
written to an inbox is read by an agent that acts on it, and a message lost in a
rewrite is lost for good — so these tests are mostly about the refusals and the
locking, not about the happy path.

Everything runs against a temporary MESH_HOME with a roster of its own; no test
here touches a deployed mesh.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

#: The shell these tests drive with send-keys. `--norc` skips the operator's
#: .bashrc — it does NOT touch HISTFILE, which stays ~/.bash_history, *theirs*.
#: An interactive bash appends its history on exit, so every line typed into a
#: test pane landed in the operator's own history file, on a private socket, with
#: nothing ever written to their terminal. Measured 2026-09-09, three times over
#: two days, 29 lines; found from their `history` output, not from ours. The
#: channel was a FILE, which is why every tmux measurement came back clean.
HARNESS_SHELL = "env HISTFILE=/dev/null bash --norc -i"

BUS = Path(__file__).resolve().parents[1]
SEND = BUS / "send.py"
READ = BUS / "read.py"
WATCHER = BUS / "watcher.sh"


@pytest.fixture()
def mesh(tmp_path: Path) -> Path:
    (tmp_path / "mesh_roster.py").write_text(
        'INBOX_PEERS = ("alice", "bob")\n'
        'FACADE_PEERS = ("user-web", "pilot-matrix")\n'
    )
    return tmp_path


def run(script: Path, *args: str, mesh: Path, **env_extra: str):
    env = {**os.environ, "MESH_HOME": str(mesh), **env_extra}
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True, text=True, env=env,
    )


def inbox(mesh: Path, agent: str) -> list[dict]:
    path = mesh / f"inbox-{agent}.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


# ── Delivery ──────────────────────────────────────────────────────────────────

def test_send_appends_one_line(mesh):
    r = run(SEND, "alice", "bob", "normal", "hello", mesh=mesh)
    assert r.returncode == 0, r.stderr
    msgs = inbox(mesh, "bob")
    assert len(msgs) == 1
    assert msgs[0]["from"] == "alice" and msgs[0]["to"] == "bob"
    assert msgs[0]["acked"] is False


def test_each_message_gets_a_distinct_id(mesh):
    """Two identical messages in the same second must not share an id.

    The id is what an ack names and what the delivery check greps for. When it
    was derived from sender and minute alone, a burst produced duplicates and an
    ack silently marked the wrong message handled.
    """
    for _ in range(5):
        assert run(SEND, "alice", "bob", "normal", "same body", mesh=mesh).returncode == 0
    ids = [m["id"] for m in inbox(mesh, "bob")]
    assert len(set(ids)) == 5


# ── Refusals: every one of these is a message that must NOT be written ─────────

@pytest.mark.parametrize("args", [
    ("alice", "mallory", "normal", "unknown recipient"),
    ("mallory", "bob", "normal", "unknown sender"),
    ("alice", "alice", "normal", "to self"),
    ("alice", "bob", "screaming", "unknown priority"),
    ("alice", "bob", "normal", "   "),
])
def test_refused_sends_write_nothing(mesh, args):
    r = run(SEND, *args, mesh=mesh)
    assert r.returncode != 0
    assert inbox(mesh, "bob") == []
    assert inbox(mesh, "alice") == []


def test_unknown_sender_is_not_silently_renamed(mesh):
    """An unknown sender is refused, never accepted under a default identity.

    Accepting it under a fallback name is worse than refusing: the recipient
    reads a message attributed to someone who never wrote it, and the bus is
    the only record of who said what.
    """
    r = run(SEND, "mallory", "bob", "normal", "x", mesh=mesh)
    assert r.returncode == 2
    assert "unknown from peer" in r.stderr


def test_topic_namespace_is_accepted_without_a_roster_entry(mesh):
    r = run(SEND, "topic-release", "bob", "normal", "from a topic room", mesh=mesh)
    assert r.returncode == 0, r.stderr
    assert inbox(mesh, "bob")[0]["from"] == "topic-release"


def test_fleet_policy_refuses_at_send_and_writes_nothing(mesh):
    (mesh / "fleet-policy.json").write_text(json.dumps(
        {"agents": {"bob": {"inbound": "human_only", "mode": "paused", "reason": "quota"}}}
    ))
    r = run(SEND, "alice", "bob", "normal", "work for you", mesh=mesh)
    assert r.returncode == 3
    assert inbox(mesh, "bob") == [], "a refused message must not reach the inbox"

    # The operator's own facade still gets through.
    ok = run(SEND, "user-web", "bob", "normal", "still allowed",
             mesh=mesh, MESH_HUMAN_FACADES="user-web")
    assert ok.returncode == 0, ok.stderr
    assert len(inbox(mesh, "bob")) == 1


def test_broken_policy_file_does_not_close_the_bus(mesh):
    """A corrupt policy file must not cut delivery — and must say so.

    The failure direction is chosen: a policy that fails closed strands agents
    mid-task on a JSON typo, while failing open costs at most a message reaching
    a paused agent. Silence would be the real defect, so stderr must carry it.
    """
    (mesh / "fleet-policy.json").write_text("{not json")
    r = run(SEND, "alice", "bob", "normal", "hello", mesh=mesh)
    assert r.returncode == 0
    assert "unreadable" in r.stderr


def test_legacy_ids_are_rewritten_to_the_canonical_name(mesh):
    (mesh / "legacy-ids.json").write_text(json.dumps({"old-alice": "alice"}))
    r = run(SEND, "old-alice", "bob", "normal", "renamed sender", mesh=mesh)
    assert r.returncode == 0, r.stderr
    assert inbox(mesh, "bob")[0]["from"] == "alice"


def test_no_roster_refuses_everything(tmp_path):
    """With no roster deployed, the bus accepts nobody rather than everybody."""
    r = run(SEND, "alice", "bob", "normal", "hello", mesh=tmp_path)
    assert r.returncode != 0
    assert not (tmp_path / "inbox-bob.jsonl").exists()


# ── Reading and acking ────────────────────────────────────────────────────────

def test_read_shows_unread_then_ack_hides_it(mesh):
    run(SEND, "alice", "bob", "normal", "first", mesh=mesh)
    msg_id = inbox(mesh, "bob")[0]["id"]

    r = run(READ, "bob", mesh=mesh)
    assert "1 message(s)" in r.stdout and msg_id in r.stdout

    acked = run(READ, "bob", "--ack", msg_id, mesh=mesh)
    assert "acked 1 message(s)" in acked.stdout
    assert inbox(mesh, "bob")[0]["acked"] is True

    after = run(READ, "bob", mesh=mesh)
    assert "0 message(s)" in after.stdout


def test_displaying_a_message_does_not_mark_it_handled(mesh):
    """Reading is not acknowledging. An interrupted session must keep its queue."""
    run(SEND, "alice", "bob", "normal", "work", mesh=mesh)
    run(READ, "bob", mesh=mesh)
    run(READ, "bob", mesh=mesh)
    assert inbox(mesh, "bob")[0]["acked"] is False


def test_unacked_ignores_the_cursor(mesh):
    """The view an agent woken from sleep depends on.

    Its wake path advances the read cursor at boot, so the cursor-based view
    goes empty while the work is still owed. --unacked answers the other
    question: what have I not dealt with, whatever the cursor says.
    """
    run(SEND, "alice", "bob", "normal", "owed work", mesh=mesh)
    msgs = inbox(mesh, "bob")
    (mesh / "state-bob.json").write_text(json.dumps(
        {"last_read": {"alice": {"ts": msgs[0]["ts"], "ids_at_ts": [msgs[0]["id"]]}}}
    ))
    assert "0 message(s)" in run(READ, "bob", mesh=mesh).stdout
    assert "1 message(s)" in run(READ, "bob", "--unacked", mesh=mesh).stdout


def test_ack_preserves_a_message_that_arrives_during_the_rewrite(mesh):
    """The ack rewrites the whole file; a delivery must survive it.

    Read and rewrite under two separate locks leaves a window where an append
    lands in a snapshot that is then truncated away — the message was delivered
    and is gone. This asserts the file still holds both messages afterwards.
    """
    run(SEND, "alice", "bob", "normal", "first", mesh=mesh)
    first_id = inbox(mesh, "bob")[0]["id"]
    run(SEND, "alice", "bob", "normal", "second", mesh=mesh)

    run(READ, "bob", "--ack", first_id, mesh=mesh)
    msgs = inbox(mesh, "bob")
    assert len(msgs) == 2
    assert msgs[0]["acked"] is True and msgs[1]["acked"] is False


def test_a_corrupt_line_does_not_hide_the_rest_of_the_inbox(mesh):
    run(SEND, "alice", "bob", "normal", "good one", mesh=mesh)
    with (mesh / "inbox-bob.jsonl").open("a") as fh:
        fh.write("{ this is not json\n")
    r = run(READ, "bob", "--all", mesh=mesh)
    assert "good one" in r.stdout
    assert "skipped malformed line" in r.stderr


def test_reply_hint_is_shown_for_a_human_behind_a_chat_facade(mesh):
    """A human reading in a chat client never sees a terminal answer."""
    run(SEND, "user-web", "bob", "normal", "question", mesh=mesh,
        MESH_HUMAN_FACADES="user-web")
    r = run(READ, "bob", mesh=mesh)
    assert "reply through the bus" in r.stdout


# ── Watcher: the predicate that decides whether to type into a session ────────

def _pane_is_idle(pane_text: str) -> bool:
    """Call the watcher's own predicate, without starting its watch loop."""
    r = subprocess.run(
        ["bash", "-c", f'source "{WATCHER}"; _pane_is_idle "$1"', "_", pane_text],
        capture_output=True, text=True,
    )
    return r.returncode == 0


@pytest.mark.parametrize("pane", [
    "bash-5.2$ ",
    "user@host:~/work# ",
    "❯ ",
    "  shift+tab to cycle",
])
def test_idle_panes_are_recognised(pane):
    assert _pane_is_idle(pane) is True


@pytest.mark.parametrize("pane", [
    "thinking… (12s · esc to interrupt)",
    "building (1m 4s)",
    "",
    "   \n  \n",
])
def test_busy_or_unreadable_panes_are_not_pushed_into(pane):
    """Bias is towards busy: a wrong 'idle' interrupts work in flight."""
    assert _pane_is_idle(pane) is False


def test_prompt_far_above_the_blank_bottom_of_a_pane_still_reads_as_idle():
    """Panes are as tall as the terminal, and mostly empty.

    Tailing the capture before dropping blank lines returns nothing but blanks,
    every session then reads as busy, and delivery stops with no error anywhere
    — the failure this asserts against.
    """
    pane = "bash-5.2$ " + "\n" * 40
    assert _pane_is_idle(pane) is True


# ── Watcher: capture, which is where the predicate gets its input ─────────────

def _has_tmux() -> bool:
    return subprocess.run(["which", "tmux"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _has_tmux(), reason="tmux not installed")
def test_capture_of_a_real_idle_pane_reads_as_idle(tmp_path):
    """The predicate above is only as good as what capture feeds it.

    A pane is as tall as its terminal, so an idle session's prompt sits near the
    top with dozens of empty lines under it. Capturing with a plain `tail` hands
    the predicate nothing but blank lines: it answers "busy" for every session,
    the watcher logs endless "skip push", and no message is ever delivered
    although each part looks correct on its own. This exercises capture and
    predicate together, on a real pane, which is the only place that shows up.
    """
    socket = f"loomtest-{os.getpid()}"
    tmux = ["tmux", "-L", socket]
    subprocess.run([*tmux, "new-session", "-d", "-s", "probe", HARNESS_SHELL],
                   capture_output=True, check=True)
    try:
        subprocess.run(["sleep", "1"])
        probe = subprocess.run(
            ["bash", "-c",
             f'export MESH_TMUX="tmux -L {socket}"; source "{WATCHER}"; '
             'text="$(capture_local probe)"; _pane_is_idle "$text"'],
            capture_output=True, text=True,
        )
        assert probe.returncode == 0, "an idle pane must be recognised through capture"
    finally:
        subprocess.run([*tmux, "kill-server"], capture_output=True)


# ── Closing a ticket ──────────────────────────────────────────────────────────

def _ticket(mesh: Path, agent: str, ticket_id: str, state: str = "running") -> Path:
    d = mesh / "tickets" / agent / state
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{ticket_id}.json"
    path.write_text(json.dumps({"id": ticket_id, "to": agent, "status": state,
                                "prompt": "do the thing"}))
    return path


def _complete(mesh: Path, *args: str):
    env = {**os.environ, "MESH_HOME": str(mesh), "MESH_TICKETS_DIR": str(mesh / "tickets")}
    return subprocess.run([sys.executable, str(BUS / "ticket-complete.py"), *args],
                          capture_output=True, text=True, env=env)


def test_completing_a_ticket_moves_it_to_done(mesh):
    _ticket(mesh, "alice", "tk-abc123")
    r = _complete(mesh, "tk-abc123", "--tldr", "finished")
    assert r.returncode == 0, r.stderr
    assert (mesh / "tickets" / "alice" / "done" / "tk-abc123.json").exists()
    assert not (mesh / "tickets" / "alice" / "running" / "tk-abc123.json").exists()


def test_a_ticket_id_never_closes_a_longer_one(mesh):
    """`tk-1` must not close `tk-10`.

    A substring match here closes someone else's ticket and reports success
    naming it, so the mistake is invisible to whoever ran the command.
    """
    _ticket(mesh, "alice", "tk-10")
    r = _complete(mesh, "tk-1", "--tldr", "done")
    assert r.returncode != 0, f"tk-1 closed something: {r.stdout}"
    assert (mesh / "tickets" / "alice" / "running" / "tk-10.json").exists(), \
        "tk-10 was closed by a request naming tk-1"


def test_a_failed_ticket_records_its_reason(mesh):
    _ticket(mesh, "bob", "tk-xyz789")
    r = _complete(mesh, "tk-xyz789", "--tldr", "could not", "--failed", "upstream down")
    assert r.returncode == 0, r.stderr
    data = json.loads((mesh / "tickets" / "bob" / "failed" / "tk-xyz789.json").read_text())
    assert data["status"] == "failed" and data["failed_reason"] == "upstream down"


# ── What a body may contain ───────────────────────────────────────────────────

def test_an_oversized_body_is_refused_with_a_way_out(mesh):
    """A message is not a file transfer.

    Past 64 KiB it is unreadable in a terminal and eats the recipient's context,
    so the refusal names the alternative rather than just saying no.
    """
    r = run(SEND, "alice", "bob", "normal", "x" * (64 * 1024 + 1), mesh=mesh)
    assert r.returncode == 2
    assert "path instead" in r.stderr
    assert inbox(mesh, "bob") == []


def test_a_body_just_under_the_limit_is_accepted(mesh):
    r = run(SEND, "alice", "bob", "normal", "x" * (64 * 1024 - 100), mesh=mesh)
    assert r.returncode == 0, r.stderr


# NUL is absent from this list on purpose: a command line cannot carry one, so
# the OS refuses it before send.py is reached. The check still covers it, for
# the paths that do not go through argv.
@pytest.mark.parametrize("payload", ["before\x1b[2Jafter", "bell\x07", "cr\rin body"])
def test_control_bytes_are_refused_not_stripped(mesh, payload):
    """The body reaches a terminal: escape sequences there rewrite what a human sees.

    Stripping would deliver a message the sender did not write; refusing tells
    them, and keeps the inbox a faithful record of what was sent.
    """
    r = run(SEND, "alice", "bob", "normal", payload, mesh=mesh)
    assert r.returncode == 2
    assert "control bytes" in r.stderr
    assert inbox(mesh, "bob") == []


def test_newlines_and_tabs_remain_ordinary_text(mesh):
    r = run(SEND, "alice", "bob", "normal", "line one\nline\ttwo", mesh=mesh)
    assert r.returncode == 0, r.stderr
    assert inbox(mesh, "bob")[0]["body"] == "line one\nline\ttwo"


def _build_notice(sender: str, to: str, mid: str) -> str:
    r = subprocess.run(
        ["bash", "-c", f'source "{WATCHER}"; build_notice "$1" "$2" "$3"',
         "_", sender, to, mid],
        capture_output=True, text=True,
    )
    return r.stdout


def test_the_typed_notice_is_plain_ascii():
    """It is quoted with printf %q before crossing SSH.

    Under a C locale that turns any non-ASCII byte into a bash-only `$'\\nnn'`
    escape, which a remote shell that is not bash types literally. An em dash in
    a message notice is enough to break delivery to a second machine.
    """
    notice = _build_notice("alice", "bob", "abc12345")
    assert notice.isascii(), f"non-ascii in the notice: {notice!r}"
    assert "abc12345" in notice


def test_the_operator_reaches_a_paused_agent_without_extra_configuration(mesh):
    """The roster already says who the human facades are; no variable needed.

    Reading only MESH_HUMAN_FACADES meant a mesh installed by bootstrap refused
    the operator's own messages to a paused agent — the one sender the pause is
    meant to keep.
    """
    (mesh / "fleet-policy.json").write_text(json.dumps(
        {"agents": {"bob": {"inbound": "human_only", "mode": "paused"}}}))
    r = run(SEND, "user-web", "bob", "normal", "still me", mesh=mesh)   # no env
    assert r.returncode == 0, r.stderr
    assert len(inbox(mesh, "bob")) == 1


# -- Threading --------------------------------------------------------------

def test_a_reply_records_what_it_answers(mesh):
    """`reply_to` was accepted by the API and dropped before the bus, so every
    reply looked like the start of a new thread."""
    run(SEND, "alice", "bob", "normal", "question", mesh=mesh)
    first = inbox(mesh, "bob")[0]["id"]

    r = run(SEND, "bob", "alice", "normal", "--reply-to", first, "answer", mesh=mesh)
    assert r.returncode == 0, r.stderr
    assert inbox(mesh, "alice")[0]["reply_to"] == first


def test_a_plain_message_carries_no_thread_field(mesh):
    run(SEND, "alice", "bob", "normal", "standalone", mesh=mesh)
    assert "reply_to" not in inbox(mesh, "bob")[0]


def test_a_malformed_reply_id_is_refused(mesh):
    r = run(SEND, "bob", "alice", "normal", "--reply-to", "not-an-id", "answer", mesh=mesh)
    assert r.returncode == 2
    assert inbox(mesh, "alice") == []


def test_the_wrapper_forwards_options_instead_of_swallowing_them(mesh):
    """mesh-send.sh joined its arguments into one body, which turned an option
    and its value into message text."""
    run(SEND, "alice", "bob", "normal", "question", mesh=mesh)
    first = inbox(mesh, "bob")[0]["id"]

    env = {**os.environ, "MESH_HOME": str(mesh)}
    r = subprocess.run(["bash", str(BUS / "mesh-send.sh"), "bob", "alice", "normal",
                        "--reply-to", first, "answer through the wrapper"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    msg = inbox(mesh, "alice")[0]
    assert msg["reply_to"] == first
    assert msg["body"] == "answer through the wrapper"


# ── Watcher: what it refuses to type into a pane ──────────────────────────────
#
# The notice the watcher types carries the message id, and that id comes out of
# a file another agent wrote. `handle_event` checks it against ^[0-9a-f]{4,32}$
# before building the notice — a guard no test exercised: replacing the check
# with `if false` left the whole suite green, because the only test that runs
# the watcher sends a well-formed id. What follows drives the guard from both
# sides, so a delivery that stops working and a guard that stops guarding are
# each visible.

_FAKE_TMUX = r'''#!/usr/bin/env bash
# Stands in for tmux: reports an idle pane, and records what would be typed.
case "$1" in
  has-session) exit 0 ;;
  capture-pane) printf '%s\n' "bash-5.2$ " ; exit 0 ;;
  send-keys)   printf '%s\n' "$*" >> "$FAKE_TMUX_LOG" ; exit 0 ;;
esac
exit 0
'''


def _watch_one(mesh: Path, line: dict) -> tuple[str, str]:
    """Append `line` to bob's inbox, run one handle_event, return (log, typed)."""
    (mesh / "inbox-bob.jsonl").write_text(json.dumps(line) + "\n")
    fake = mesh / "fake-tmux.sh"
    fake.write_text(_FAKE_TMUX)
    fake.chmod(0o755)
    typed = mesh / "typed.log"
    typed.write_text("")
    subprocess.run(
        ["bash", "-c", f'source "{WATCHER}"; handle_event inbox-bob.jsonl'],
        capture_output=True, text=True,
        env={**os.environ, "MESH_HOME": str(mesh),
             "MESH_TMUX": f"bash {fake}", "FAKE_TMUX_LOG": str(typed)},
    )
    log = mesh / "logs" / "watcher.log"
    return (log.read_text() if log.exists() else ""), typed.read_text()


def test_a_well_formed_id_is_typed_into_the_pane(mesh):
    """The control: without it, the refusals below could pass on a broken watcher."""
    log, typed = _watch_one(mesh, {"id": "cafe1234", "from": "alice", "to": "bob",
                                   "body": "hello", "ts": "2026-09-03T12:00:00+02:00"})
    assert "push local OK" in log, log
    assert "cafe1234" in typed, f"the notice was never typed: {typed!r}"


@pytest.mark.parametrize("bad_id", [
    "x; touch /tmp/pwn",       # command substitution attempt
    "$(id)",
    "`id`",
    "../../etc/passwd",
    "CAFE1234",                # uppercase is not the id format
    "ab",                      # too short
    "a" * 33,                  # too long
])
def test_a_malformed_id_is_never_typed(mesh, bad_id):
    log, typed = _watch_one(mesh, {"id": bad_id, "from": "alice", "to": "bob",
                                   "body": "hello", "ts": "2026-09-03T12:00:00+02:00"})
    assert "malformed id" in log, (
        f"the watcher accepted {bad_id!r} as a message id: {log!r}")
    assert typed == "", (
        f"{bad_id!r} reached send-keys, which is the injection this guard exists "
        f"to prevent: {typed!r}")


# ── Watcher: the last filter before a keystroke ───────────────────────────────

def _sanitize(raw: str) -> str:
    """As the watcher uses it: the result is captured with $(), like build_notice.

    That capture is part of the behaviour — it is what drops the trailing
    newline `cut` leaves behind — so testing the pipeline without it would
    report a newline the pane never receives.
    """
    r = subprocess.run(
        ["bash", "-c",
         f'source "{WATCHER}"; text="$(printf %s "$1" | _sanitize)"; printf %s "$text"',
         "_", raw],
        capture_output=True, text=True,
    )
    return r.stdout


@pytest.mark.parametrize("raw, banned", [
    ("first line\nrm -rf /", "\n"),      # a newline would submit the first half
    ("carriage\rreturn", "\r"),
    ("bell\x07 and backspace\x08", "\x07"),
    ("escape\x1b[31m sequence", "\x1b"),
])
def test_sanitize_strips_what_would_submit_or_steer_a_pane(raw, banned):
    """Keystrokes are not a data channel.

    `send-keys -l` types the text literally, so anything left in it is typed —
    a newline in the middle submits the first half as a command, and an escape
    sequence steers the terminal. This is the only place that is checked, and
    nothing exercised it.
    """
    out = _sanitize(raw)
    assert banned not in out, f"{banned!r} survived sanitising: {out!r}"
    assert out.strip(), "sanitising must not empty the text either"


def test_sanitize_bounds_the_length():
    out = _sanitize("x" * 5000)
    assert len(out.rstrip("\n")) <= 400, f"typed {len(out)} characters into a pane"


# ── Watcher: whose tmux server is it allowed to type into? ────────────────────
#
# `push_local` ends with `send-keys Enter`, so it RUNS the notice rather than
# just displaying it. What kept a test bench away from a live agent was that no
# session happened to be named like one — and this project's own deployments
# name sessions after their agents, so that separation does not exist there.
# Observed on 2026-09-03: a probe running this watcher from a copied mesh aimed
# at the DEFAULT tmux server, the production one, and only missed because no
# session carried the fixture's names.
#
# The tests below drive the guard from both sides. The negative one proves
# nothing was typed by putting a fake tmux on PATH and checking it was never
# called at all; the positive ones prove the guard did not simply mute delivery.

_FAKE_TMUX_RECORDER = r'''#!/usr/bin/env bash
# Records every invocation, and reports an idle pane so a push would proceed.
printf '%s\n' "$*" >> "$FAKE_TMUX_LOG"
case "$1" in
  has-session) exit 0 ;;
  capture-pane) printf '%s\n' "bash-5.2$ " ; exit 0 ;;
esac
exit 0
'''


def _push_attempt(tmp_path: Path, mesh: Path, home: Path, mesh_tmux: str | None) -> tuple[int, str, str]:
    """Call push_local through the watcher, with a fake tmux that records calls.

    Returns (returncode, what tmux was asked to do, what the watcher logged).
    """
    (mesh / "logs").mkdir(parents=True, exist_ok=True)
    fake_dir = tmp_path / "bin"
    fake_dir.mkdir(exist_ok=True)
    fake = fake_dir / "tmux"
    fake.write_text(_FAKE_TMUX_RECORDER)
    fake.chmod(0o755)
    calls = tmp_path / "tmux-calls.log"
    calls.write_text("")

    env = {**os.environ,
           "HOME": str(home),
           "MESH_HOME": str(mesh),
           "PATH": f"{fake_dir}:{os.environ['PATH']}",
           "FAKE_TMUX_LOG": str(calls)}
    env.pop("MESH_TMUX", None)
    if mesh_tmux is not None:
        env["MESH_TMUX"] = mesh_tmux

    r = subprocess.run(
        ["bash", "-c", f'source "{WATCHER}"; push_local "$PEER" "a notice"'],
        capture_output=True, text=True, env={**env, "PEER": "bob"},
    )
    log = mesh / "logs" / "watcher.log"
    return r.returncode, calls.read_text(), (log.read_text() if log.exists() else "")


def test_a_bench_mesh_may_not_type_into_the_default_tmux_server(tmp_path):
    """The negative: a non-default mesh home with no socket of its own."""
    home = tmp_path / "home"; (home / "mesh").mkdir(parents=True)   # the default, elsewhere
    bench = tmp_path / "bench-mesh"; bench.mkdir()
    rc, tmux_calls, log = _push_attempt(tmp_path, bench, home, mesh_tmux=None)

    assert rc != 0, "the push must fail rather than reach a server this mesh does not own"
    assert tmux_calls == "", (
        "tmux was invoked at all — the guard has to refuse BEFORE has-session, or a "
        f"session merely named like a peer gets keystrokes: {tmux_calls!r}")
    assert "REFUSED push" in log, f"the refusal must be logged, not silent: {log!r}"


def test_the_default_mesh_may_type_into_the_default_server(tmp_path):
    """Positive control: without it, a guard that refuses everything would pass above."""
    home = tmp_path / "home"
    mesh = home / "mesh"; mesh.mkdir(parents=True)     # MESH_HOME *is* $HOME/mesh
    rc, tmux_calls, log = _push_attempt(tmp_path, mesh, home, mesh_tmux=None)

    assert rc == 0, f"delivery on the default mesh must still work: {log!r}"
    assert "send-keys" in tmux_calls, (
        f"nothing was typed on the deployment's own server: {tmux_calls!r}")


def test_a_bench_with_its_own_socket_may_type_into_it(tmp_path):
    """Second positive: an explicit socket is what a bench is supposed to pass."""
    home = tmp_path / "home"; (home / "mesh").mkdir(parents=True)
    bench = tmp_path / "bench-mesh"; bench.mkdir()
    rc, tmux_calls, log = _push_attempt(tmp_path, bench, home, mesh_tmux="tmux -L benchsock")

    assert rc == 0, f"an explicit socket must be accepted: {log!r}"
    assert "send-keys" in tmux_calls, f"nothing typed on the bench socket: {tmux_calls!r}"
