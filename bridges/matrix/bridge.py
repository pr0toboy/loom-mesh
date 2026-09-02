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
import json
import os
import subprocess
import sys
import time
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

    def send_matrix(self, room_id, text, token=None):
        # token=None -> posts as @mesh-bot ; otherwise under the agent identity.
        txn = str(int(time.time() * 1000))
        path = f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/send/m.room.message/{txn}"
        self._req("PUT", path, body={"msgtype": "m.text", "body": text}, token=token)

    def send_read_receipt(self, room_id, event_id, token=None):
        # Mark the pilot's event as read under the agent's identity. Gives the
        # "seen" receipt in the client the moment the bridge picks the message
        # up, before the agent replies. Best-effort.
        path = (f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}"
                f"/receipt/m.read/{urllib.parse.quote(event_id)}")
        self._req("POST", path, body={}, token=token)

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
            log(f"sync error: {e}")
            time.sleep(3)
            return
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
            self._save_state()
            return  # we just captured next_batch

        joined = resp.get("rooms", {}).get("join", {}) or {}
        for room_id, room in joined.items():
            agent = self.room_to_agent.get(room_id)
            if not agent:
                continue
            for ev in room.get("timeline", {}).get("events", []) or []:
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
                self._to_mesh(agent, body)
        self._save_state()

    def _to_mesh(self, agent, body):
        try:
            subprocess.run(
                [sys.executable, self.mesh_send, self.mesh_peer, agent,
                 self.default_priority, body],
                check=True, capture_output=True, text=True,
            )
            log(f"Matrix->mesh  to={agent}: {body[:80]!r}")
        except subprocess.CalledProcessError as e:
            log(f"mesh send FAILED to={agent}: {e.stderr.strip()}")

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
        for line in lines[offset:]:
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
                    self.send_matrix(room_id, f"{prefix}{body}", token=token)
                    log(f"mesh->Matrix  from={sender}: {body[:80]!r}")
                except Exception as e:
                    log(f"matrix post FAILED from={sender}: {e}")
            else:
                log(f"mesh->Matrix  skip (no room for from={sender})")
        self.state["inbox_offset"] = len(lines)
        self._save_state()

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
