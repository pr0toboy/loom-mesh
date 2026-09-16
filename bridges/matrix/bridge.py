#!/usr/bin/env python3
"""
Matrix <-> mesh bridge.

A *chat facade* on top of the LoomMesh bus, alongside the browser webui: it
does not replace the bus, it adds a new client surface — the conversational one,
with native client push. The human pilot talks to agents from any Matrix client
(Element, ...); each agent gets its own room and posts under its own Matrix
identity.

Two directions, one single-threaded loop:
  1. Matrix -> mesh : text messages from the pilot in a room mapped to an agent
     are relayed via send.py  (from=<mesh_peer>, to=<agent>).
  2. mesh -> Matrix : new lines in inbox-<mesh_peer>.jsonl (the agents' replies)
     are posted into the sending agent's room, under that agent's identity.

Design notes
------------
* No E2EE. Confidentiality is delegated to the network layer (run the
  homeserver behind a private overlay such as Tailscale/WireGuard and never
  expose a public port). This keeps bot accounts simple and reliable.
* Pure standard library — no dependency to install.
* Anti-loop: only the pilot's own messages are relayed to the mesh. Anything
  sent by the bot OR by an agent identity is ignored, otherwise an agent's
  reply (posted under @agent) would be re-injected into the mesh on next sync.
* Read receipts: as soon as a pilot message is seen, a Matrix read receipt is
  sent under the agent's identity — the pilot sees "seen" immediately, before
  the agent has even composed a reply. Best-effort.

Configuration: see config.example.json. Point BRIDGE_CONFIG at your real file
(kept out of version control — it holds access tokens).
"""
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG_PATH = os.environ.get(
    "BRIDGE_CONFIG", str(Path(__file__).resolve().parent / "config.json")
)


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


class Bridge:
    def __init__(self, cfg):
        self.hs = cfg["homeserver"].rstrip("/")
        self.token = cfg["bot_token"]
        self.bot_user = cfg["bot_user"]
        # The human pilot's Matrix ID. Only this sender is relayed to the mesh.
        self.pilot_user = cfg.get("pilot_user") or cfg["owner_user"]
        self.mesh_peer = cfg["mesh_peer"]
        self.mesh_send = cfg["mesh_send"]
        self.inbox = Path(cfg["inbox"])
        self.state_file = Path(cfg["state_file"])
        self.default_priority = cfg.get("default_priority", "normal")
        self.agent_to_room = dict(cfg["rooms"])
        self.room_to_agent = {v: k for k, v in self.agent_to_room.items()}
        # Per-agent token: lets the bridge post under @agent-1/@agent-2/... instead
        # of only @mesh-bot. The bot_token is used for /sync and inbound routing.
        self.agent_tokens = {}
        atf = cfg.get("agent_tokens_file")
        if atf and Path(atf).exists():
            self.agent_tokens = json.loads(Path(atf).read_text())
        self.state = self._load_state()
        # ---- outage escalation ---------------------------------------------
        # A bridge outage cannot be reported *through* the bridge: when /sync is
        # failing, every path to the pilot's client is the path that is down. So
        # the bridge escalates onto the bus instead — a filesystem write that
        # needs no network — towards an agent that is still locally reachable.
        # Without this the loop retries in silence for as long as it takes, and
        # the only detector left is the human wondering why nobody answers.
        self.alert_peer = cfg.get("alert_agent", "supervisor")
        if self.alert_peer not in self.agent_to_room:
            self.alert_peer = None
        # A machine notice must not be signed with the pilot's name. Sending it
        # as the pilot facade is wrong twice over: the recipient reads "the
        # operator wrote to me" at the top of its context, and the bus exempts
        # human facades from the fleet policy, so the notice would wake an agent
        # the operator had deliberately paused. Default to a dedicated `bridge`
        # peer; if the roster does not know it the send is refused, and
        # _escalate falls back to the facade *and says so* rather than losing
        # the alert.
        self.alert_from = cfg.get("alert_from", "bridge")
        # Counted in seconds, not in attempts. A DNS failure returns instantly
        # (~4 s per cycle) but a blackholed TCP peer sits in _req's 60 s timeout,
        # so twenty *attempts* is anywhere between one minute and twenty. The
        # count stays as a floor so a fast-failing loop still has to persist.
        self.sync_alert_seconds = int(cfg.get("sync_alert_seconds", 60))
        self.sync_alert_after = int(cfg.get("sync_alert_after", 3))
        self.sync_fails = 0
        self.sync_down_since = None
        self.sync_down_at = None
        self.sync_alerted = False
        # ---- a held message must not become a silent freeze ------------------
        # Holding the cursor protects one reply; left unbounded it stops every
        # room's traffic instead, and /sync stays healthy so nothing else
        # notices. Escalate once the hold outlives `hold_alert_seconds`, and
        # give up on that single line after `hold_skip_seconds` — giving up is
        # survivable because the inbox file is append-only: the message stays on
        # disk and in the notice, only the bridge stops trying to post it.
        self.hold_alert_seconds = int(cfg.get("hold_alert_seconds", 300))
        self.hold_skip_seconds = int(cfg.get("hold_skip_seconds", 3600))
        self.hold_since = None
        self.hold_ticked_at = None
        self.hold_key = None
        self.hold_alerted = False
        # Rooms already reported as un-backfillable, so one unusable marker
        # does not escalate on every single sync.
        self.gap_reported = set()
        # Backfill: how many pages of history one gap may cost before the
        # bridge stops digging and says the gap is wider than it can close.
        self.backfill_max_pages = int(cfg.get("backfill_max_pages", 20))

    # ---- state -------------------------------------------------------------
    def _load_state(self):
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text())
            except Exception:
                pass
        # Cold start: ignore mesh history already present in the inbox.
        offset = 0
        if self.inbox.exists():
            with self.inbox.open("r", encoding="utf-8") as f:
                offset = sum(1 for _ in f)
        return {"since": None, "inbox_offset": offset}

    def _save_state(self):
        tmp = self.state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state))
        tmp.replace(self.state_file)

    # ---- HTTP Matrix -------------------------------------------------------
    def _req(self, method, path, params=None, body=None, token=None):
        url = self.hs + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token or self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())

    def send_matrix(self, room_id, text, token=None, txn=None):
        # token=None -> posts as @mesh-bot ; otherwise under the agent identity.
        #
        # `txn` is the Matrix transaction id, and it is what makes a retry safe:
        # the homeserver treats a repeated txn as the same event. Passing the
        # bus message id here means a post retried after a timeout is deduped
        # server-side instead of appearing twice — a clock-based id would be
        # fresh on every attempt and turn "the reply was lost" into "the reply
        # was said twice", which is not an improvement.
        txn = urllib.parse.quote(str(txn), safe="") if txn else str(int(time.time() * 1000))
        path = f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/send/m.room.message/{txn}"
        self._req("PUT", path, body={"msgtype": "m.text", "body": text}, token=token)

    def send_read_receipt(self, room_id, event_id, token=None):
        # Mark the pilot's event as read under the agent's identity. Gives the
        # "seen" receipt in the client the moment the bridge picks the message
        # up, before the agent replies. Best-effort.
        path = (f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}"
                f"/receipt/m.read/{urllib.parse.quote(event_id)}")
        self._req("POST", path, body={}, token=token)

    # ---- outage escalation -------------------------------------------------
    def _sync_failed(self, exc):
        self.sync_fails += 1
        if self.sync_fails == 1:
            self.sync_down_since = time.strftime("%Y-%m-%d %H:%M:%S")
            self.sync_down_at = time.monotonic()
        log(f"sync error: {exc}")
        down_for = time.monotonic() - (self.sync_down_at or time.monotonic())
        if self.sync_alerted or self.sync_fails < self.sync_alert_after:
            return
        if down_for < self.sync_alert_seconds:
            return
        self.sync_alerted = True
        self._escalate(
            f"[bridge] Matrix sync failing since {self.sync_down_since} "
            f"({self.sync_fails} consecutive failures, last error: {exc}). "
            "Messages from the pilot are NOT reaching the mesh until this is "
            "fixed, and the pilot has no way to know. This notice travelled "
            "over the bus because the bridge cannot deliver its own alert."
        )

    def _sync_recovered(self):
        if not self.sync_fails:
            return
        fails, since, alerted = self.sync_fails, self.sync_down_since, self.sync_alerted
        self.sync_fails, self.sync_down_since, self.sync_alerted = 0, None, False
        log(f"sync recovered after {fails} failure(s), down since {since}")
        if alerted:
            self._escalate(
                f"[bridge] Matrix sync recovered (was down from {since}, "
                f"{fails} failures). What the pilot sent meanwhile is being "
                "relayed now, as far back as the server still exposes it — a "
                "long outage can exceed the window /sync returns per room, and "
                "any gap the bridge cannot close is reported separately."
            )

    def _escalate(self, text):
        """Put an outage notice on the bus, where the network is not involved."""
        if not self.alert_peer or self.alert_peer == self.alert_from:
            # No reachable confidant: say it in full in the log rather than
            # drop it. A deployment that never set `alert_agent` still has the
            # outage written down somewhere a human can find it — silence here
            # would reproduce the exact defect this code exists to remove.
            log(f"OUTAGE, and no alert_agent configured to tell: {text}")
            return
        senders = [self.alert_from]
        if self.mesh_peer != self.alert_from:
            senders.append(self.mesh_peer)  # last resort, announced when used
        for sender in senders:
            try:
                subprocess.run(
                    [sys.executable, self.mesh_send, sender, self.alert_peer,
                     "high", text],
                    check=True, capture_output=True, text=True, timeout=30,
                )
                if sender != self.alert_from:
                    log(f"outage notice sent as {sender}: the roster does not know "
                        f"{self.alert_from!r}, so this machine notice carries the "
                        "pilot's identity — declare a `bridge` peer to fix it")
                log(f"outage notice -> {self.alert_peer}")
                return
            except Exception as e:
                # Deliberately broad, and never re-raised. This runs inside the
                # failure path of the sync loop: a mistyped `mesh_send` raising
                # FileNotFoundError here would crash the service on every outage
                # — a guard that kills the process it guards.
                detail = getattr(e, "stderr", "") or str(e)
                log(f"outage notice FAILED as {sender} to={self.alert_peer}: "
                    f"{detail.strip()}")
        log(f"OUTAGE, undeliverable on the bus: {text}")

    # ---- direction 1 : Matrix -> mesh -------------------------------------
    def poll_matrix(self):
        params = {"timeout": "2000"}
        if self.state.get("since"):
            params["since"] = self.state["since"]
        else:
            # First sync: do not replay history.
            params["timeout"] = "0"
        try:
            resp = self._req("GET", "/_matrix/client/v3/sync", params=params)
        except Exception as e:
            self._sync_failed(e)
            time.sleep(3)
            return
        self._sync_recovered()
        first_sync = self.state.get("since") is None
        self.state["since"] = resp.get("next_batch")

        # auto-join on invite
        for room_id in (resp.get("rooms", {}).get("invite", {}) or {}):
            try:
                self._req("POST", f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/join")
                log(f"auto-join room {room_id}")
            except Exception as e:
                log(f"join error {room_id}: {e}")

        if first_sync:
            # Record where each room stands right now. Without this seed the
            # backfill has no ground truth to walk back to, so the first gap a
            # deployment ever hits is also the one it cannot close — and the
            # tail is already in this very response, so there is nothing to
            # fetch and nothing to guess.
            for room_id, room in (resp.get("rooms", {}).get("join", {}) or {}).items():
                evs = (room.get("timeline", {}) or {}).get("events") or []
                last_id = evs[-1].get("event_id") if evs else None
                if last_id:
                    self.state.setdefault("last_events", {})[room_id] = last_id
            self._save_state()
            return  # we just captured next_batch

        joined = resp.get("rooms", {}).get("join", {}) or {}
        for room_id, room in joined.items():
            agent = self.room_to_agent.get(room_id)
            if not agent:
                continue
            timeline = room.get("timeline", {}) or {}
            events = list(timeline.get("events", []) or [])
            if timeline.get("limited"):
                events = self._backfill(room_id, timeline.get("prev_batch"), events)
            if events:
                last_id = events[-1].get("event_id")
                if last_id:
                    self.state.setdefault("last_events", {})[room_id] = last_id
            for ev in events:
                if ev.get("type") != "m.room.message":
                    continue
                # Relay to the mesh ONLY the pilot's messages. Any other sender
                # (the bot @mesh-bot OR agent accounts @agent-1/@agent-2/...) is
                # ignored, otherwise agent replies would loop back to the mesh.
                if ev.get("sender") != self.pilot_user:
                    continue
                content = ev.get("content", {}) or {}
                if content.get("msgtype") != "m.text":
                    continue
                body = (content.get("body") or "").strip()
                if not body:
                    continue
                # Instant "seen": read receipt under the agent identity as soon
                # as the message is captured (best-effort, never blocks relay).
                ev_id = ev.get("event_id")
                if ev_id:
                    try:
                        self.send_read_receipt(room_id, ev_id,
                                               token=self.agent_tokens.get(agent))
                    except Exception as e:
                        log(f"read-receipt FAILED to={agent}: {e}")
                self._to_mesh(agent, body, room_id)
        self._save_state()

    def _backfill(self, room_id, prev_batch, tail):
        """Recover what `/sync` summarised away, oldest-first.

        Answering an old `since`, a homeserver returns only the last handful of
        events per room — Conduit stops at ten and ignores a filter asking for
        more — and flags the truncation with `limited` plus a `prev_batch`
        token. Reading that timeline as if it were whole silently loses
        everything before the window: precisely the messages a pilot sent while
        the bridge was down, which is the one moment this matters.

        The backfill runs **only against a known last event**. With no marker
        for this room the bridge cannot tell an outage gap from the room's
        entire history, and guessing in that direction is how a bridge relays a
        year of old messages into a live mesh. No ground truth, no backfill: it
        records the marker and moves on, losing nothing that was not already
        lost before this code existed.
        """
        known = (self.state.get("last_events") or {}).get(room_id)
        if not known or not prev_batch:
            # Silence here was the bug the second review found: every existing
            # deployment starts with no marker, so this is the *normal* path on
            # the first gap, not a corner case — and the recovery notice was
            # promising that any gap would be reported.
            self._report_gap(
                room_id,
                f"[bridge] room {room_id} came back with a gap and no recorded "
                "last event: an outage gap cannot be told apart from the room's "
                "whole history, so NOTHING was backfilled. Messages sent while "
                "the bridge was down may be missing from the mesh — they are "
                "still in the room itself.",
            )
            return tail
        seen = {e.get("event_id") for e in tail}
        recovered, token, pages, closed, failure = [], prev_batch, 0, False, None
        while token and pages < self.backfill_max_pages:
            pages += 1
            try:
                resp = self._req(
                    "GET",
                    f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/messages",
                    params={"dir": "b", "from": token, "limit": "100"},
                )
            except Exception as e:
                log(f"backfill FAILED room={room_id}: {e}")
                failure = str(e)
                break
            chunk = resp.get("chunk") or []
            if not chunk:
                # Ran out of history without meeting the marker. The marker is
                # gone (a homeserver restored from backup, a hand-edited state),
                # so everything walked back so far is of unknown age: relaying it
                # as a recovery is how an archive lands on top of a live
                # conversation. `closed` stays False, which reports it.
                break
            for ev in chunk:  # dir=b walks backwards: newest first
                if ev.get("event_id") == known:
                    closed = True
                    break
                if ev.get("event_id") not in seen:
                    recovered.append(ev)
            if closed:
                break
            token = resp.get("end")
        if recovered:
            recovered.reverse()  # back to chronological order for relaying
            log(f"backfilled {len(recovered)} event(s) in room={room_id} "
                f"over {pages} page(s)")
        if not closed:
            # Never report a partial recovery as a complete one — and name the
            # actual reason: "the server stopped answering" and "the history ran
            # out before the marker" call for different repairs, and one of them
            # means the marker itself is gone.
            if failure:
                why = f"the server stopped answering ({failure})"
            elif pages >= self.backfill_max_pages:
                why = f"the gap is wider than {self.backfill_max_pages} pages"
            else:
                why = ("the room's history ran out before the last event this "
                       "bridge had relayed — that marker no longer exists")
            self._report_gap(
                room_id,
                f"[bridge] could not close the gap in room {room_id}: {why}. "
                f"{len(recovered)} event(s) recovered and relayed; anything "
                "older was NOT, and is only in the room itself.",
            )
            # The marker still advances to the visible tail afterwards: the lost
            # events are unreachable either way, and re-reporting them on every
            # sync would bury the notice that matters.
        return recovered + tail

    def _report_gap(self, room_id, text):
        """Escalate a gap once per room, so one bad marker is not a siren."""
        if room_id in self.gap_reported:
            log(f"gap in room={room_id} (already reported)")
            return
        self.gap_reported.add(room_id)
        self._escalate(text)

    def _to_mesh(self, agent, body, room_id=None):
        try:
            subprocess.run(
                [sys.executable, self.mesh_send, self.mesh_peer, agent,
                 self.default_priority, body],
                check=True, capture_output=True, text=True, timeout=30,
            )
            log(f"Matrix->mesh  to={agent}: {body[:80]!r}")
        except Exception as e:
            detail = (getattr(e, "stderr", "") or str(e)).strip()
            log(f"mesh send FAILED to={agent}: {detail}")
            # The read receipt already told the pilot "seen", and the bus
            # refused the message: without this the pilot is looking at a tick
            # under a message no agent will ever receive. Matrix is up in this
            # branch — it is the bus that said no — so the room is the one place
            # the truth can still be delivered.
            if room_id:
                try:
                    self.send_matrix(
                        room_id,
                        f"[bridge] this message was NOT delivered to {agent}: "
                        f"{detail[:300]}",
                    )
                except Exception as post_err:
                    log(f"could not report the drop in room={room_id}: {post_err}")

    # ---- direction 2 : mesh -> Matrix -------------------------------------
    def poll_inbox(self):
        if not self.inbox.exists():
            return
        with self.inbox.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        offset = self.state.get("inbox_offset", 0)
        if len(lines) <= offset:
            self.state["inbox_offset"] = min(offset, len(lines))
            return
        for idx, line in enumerate(lines[offset:], start=offset):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            sender = msg.get("from", "?")
            room_id = self.agent_to_room.get(sender)
            body = msg.get("body", "")
            if room_id and body:
                pri = msg.get("priority", "normal")
                prefix = "" if pri in ("normal", "low") else f"[{pri}] "
                token = self.agent_tokens.get(sender)  # post under agent identity
                try:
                    self._post_with_fallback(room_id, f"{prefix}{body}", token,
                                             self._txn_for(msg), sender)
                    log(f"mesh->Matrix  from={sender}: {body[:80]!r}")
                    self._release_hold()
                except Exception as e:
                    if self._should_hold(e) and not self._hold_expired(idx, sender, body, e):
                        # Transport failure (no route, no DNS, 5xx, throttling):
                        # hold the cursor on this very line and try again next
                        # cycle. The message stays on disk, so an outage *delays*
                        # delivery instead of eating it — advancing past a failed
                        # post dropped agents' replies for good, and the log line
                        # scrolled away with them.
                        log(f"matrix post FAILED from={sender}: {e} — holding, will retry")
                        self.state["inbox_offset"] = idx
                        self._save_state()
                        return
                    # Either a retry cannot clear this (a payload the server will
                    # never accept, or a defect in this bridge), or the hold has
                    # outlived its deadline. Skip it loudly and keep the queue
                    # moving: one stuck line must not stop every room, and the
                    # message is not destroyed — the inbox file is append-only,
                    # so it stays on disk and in the notice that was escalated.
                    log(f"matrix post GIVEN UP, skipped from={sender}: {e!r}")
                    self._release_hold()
            else:
                log(f"mesh->Matrix  skip (no room for from={sender})")
        self.state["inbox_offset"] = len(lines)
        self._save_state()

    def _post_with_fallback(self, room_id, text, token, txn, sender):
        """Post as the agent; on 401/403 try once as the bot before holding.

        A rotated agent token is the common way this direction dies, and it
        dies per-agent: without the retry the bridge would hold — then give up
        on — every reply from that one agent while the rest of the mesh looks
        fine. Posting as the bot is a visible downgrade (the message shows up
        under the bot identity, and the log says so), which beats not arriving.
        """
        try:
            self.send_matrix(room_id, text, token=token, txn=txn)
        except urllib.error.HTTPError as e:
            if e.code not in (401, 403) or token is None:
                raise
            self.send_matrix(room_id, text, token=None, txn=txn)
            log(f"posted as the bot, not as {sender}: its token was refused ({e.code})")

    @staticmethod
    def _txn_for(msg):
        """A transaction id stable across retries and unlikely to collide.

        The bus id is eight hex characters — 32 bits — and a homeserver that
        never expires transaction ids (Conduit does not) turns a birthday
        collision into a message swallowed with a 200 and no log line at all.
        Prefixing the timestamp costs nothing and removes the class of bug;
        both fields come from the message, so a retry still sends the same id.
        """
        ident, stamp = str(msg.get("id") or ""), str(msg.get("ts") or "")
        return f"{stamp}-{ident}" if (stamp or ident) else None

    def _hold_expired(self, idx, sender, body, exc):
        """Track one held line; escalate, then let it go once it is hopeless.

        Returns True when this line has been held long enough that continuing
        to block every other room costs more than dropping it from the queue.
        """
        key = (idx, sender)
        now = time.monotonic()
        if self.hold_key != key:
            self.hold_key, self.hold_since, self.hold_alerted = key, now, False
            self.hold_ticked_at = now
            return False
        # The deadline exists for a failure specific to THIS message — a dead
        # room, a payload one server hates. While /sync is failing too, the link
        # itself is down: skipping buys nothing (there is nowhere to post
        # anything, and the queue behind is just as stuck) and costs a reply
        # that would have gone out on recovery. Replayed against a real 9h45
        # outage, a deadline that kept running delivered 3 messages out of 12
        # where simply waiting delivered 12. So the clock only runs while the
        # bridge can actually reach the homeserver.
        elapsed, self.hold_ticked_at = now - self.hold_ticked_at, now
        if self.sync_fails:
            self.hold_since += elapsed  # outage time is not held time
            return False
        held = now - self.hold_since
        if not self.hold_alerted and held >= self.hold_alert_seconds:
            self.hold_alerted = True
            self._escalate(
                f"[bridge] mesh->Matrix has been stuck for {int(held)}s on a "
                f"message from {sender} ({exc!r}). Nothing from any agent is "
                f"reaching the pilot's client meanwhile, and /sync is healthy, "
                f"so nothing else will notice. Message text: {body[:200]!r}"
            )
        if held >= self.hold_skip_seconds:
            self._escalate(
                f"[bridge] giving up after {int(held)}s on the message from "
                f"{sender} that was blocking mesh->Matrix ({exc!r}). It was "
                f"NOT posted to the room; it is still in the inbox file. Text: "
                f"{body[:200]!r}"
            )
            return True
        return False

    def _release_hold(self):
        self.hold_key, self.hold_since, self.hold_alerted = None, None, False
        self.hold_ticked_at = None

    @staticmethod
    def _should_hold(exc):
        """True when a retry can plausibly clear this failure.

        Listed positively, on purpose. The first version of this asked the
        opposite question — "is it hopeless?" — and answered no for everything
        that was not a 4xx, which quietly included `TypeError`: a defect in this
        file would then be retried forever and stop the whole mesh→Matrix
        direction, with one line in a log nobody reads. A bug is not a transient
        condition. Only transport failures are.

        401/403 *are* held: a revoked token or a missing invite is the
        operator's to fix, and the message loses nothing by waiting for them.

        `http.client` raises a family of its own — IncompleteRead,
        BadStatusLine — that is **not** a subclass of OSError, so a connection
        dropped while the response was being read looked like a defect and the
        reply was discarded. That is the failure mode of a flaky link, i.e. the
        one this whole branch exists for.
        """
        if isinstance(exc, urllib.error.HTTPError):
            return exc.code in (401, 403, 408, 429) or exc.code >= 500
        return isinstance(exc, (OSError, http.client.HTTPException))

    # ---- loop --------------------------------------------------------------
    def run(self):
        log(f"bridge starting — rooms={self.agent_to_room}")
        while True:
            self.poll_matrix()
            self.poll_inbox()
            time.sleep(1)


def main():
    cfg = json.loads(Path(CONFIG_PATH).read_text())
    Bridge(cfg).run()


if __name__ == "__main__":
    main()
