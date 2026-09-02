#!/usr/bin/env python3
"""PreToolUse hook: show "<agent> is typing…" in the agent's Matrix room.

Each time the agent is about to use a tool, refresh a Matrix typing
notification (timeout 30 s) in the agent's room, under the agent's own
identity. Element renders this as "<Agent> is typing…", so the human pilot
can see the agent is actively working between milestone messages.

Costs zero LLM tokens (just an HTTP POST from this script). Fully fail-open
and non-blocking: any error, missing config, or unknown agent → exit 0 and
let the tool run. NEVER denies a tool.

Wire it in Claude Code settings.json under PreToolUse with a broad matcher.

Env:
    MESH_AGENT     — agent name; must be a key in the bridge config "rooms".
    BRIDGE_CONFIG  — path to the Matrix bridge config.json (default below).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BRIDGE_CONFIG = os.environ.get("BRIDGE_CONFIG", str(Path.home() / "matrix/bridge/config.json"))
TYPING_TIMEOUT_MS = 30000


def main() -> None:
    try:
        try:
            sys.stdin.read()  # drain stdin so the pipe never stalls
        except Exception:
            pass

        cfg = json.loads(Path(BRIDGE_CONFIG).read_text())
        rooms = cfg.get("rooms", {})

        # Identity: inside a real tmux pane ($TMUX set), the pane's session name
        # wins over a possibly-inherited MESH_AGENT (a shared tmux server can
        # carry one agent's MESH_AGENT globally to every pane). Otherwise trust
        # the env. Only accept a name that maps to a room.
        agent = os.environ.get("MESH_AGENT", "").strip().lower()
        if os.environ.get("TMUX"):
            try:
                import subprocess
                r = subprocess.run(["tmux", "display-message", "-p", "#S"],
                                   capture_output=True, text=True, timeout=2)
                name = r.stdout.strip().lower()
                if name in rooms:
                    agent = name
            except Exception:
                pass
        if not agent:
            sys.exit(0)

        room = rooms.get(agent)
        if not room:
            sys.exit(0)

        hs = cfg["homeserver"].rstrip("/")
        domain = cfg["bot_user"].split(":", 1)[1]

        tokens = {}
        atf = cfg.get("agent_tokens_file")
        if atf and Path(atf).exists():
            tokens = json.loads(Path(atf).read_text())
        token = tokens.get(agent)
        if not token:
            sys.exit(0)

        user = f"@{agent}:{domain}"
        path = (f"/_matrix/client/v3/rooms/{urllib.parse.quote(room)}"
                f"/typing/{urllib.parse.quote(user)}")
        body = json.dumps({"typing": True, "timeout": TYPING_TIMEOUT_MS}).encode()
        req = urllib.request.Request(hs + path, data=body, method="PUT")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        urllib.request.urlopen(req, timeout=2)  # homeserver is local (tailnet)
    except Exception:
        pass
    sys.exit(0)  # always allow the tool


if __name__ == "__main__":
    main()
