#!/usr/bin/env python3
"""PreToolUse hook: show "<agent> is typing…" in the agent's Matrix room.

Each time the agent is about to use a tool, refresh a Matrix typing
notification (timeout 30 s) in the agent's room, under the agent's own
identity. Element renders this as "<Agent> is typing…", so the human pilot
can see the agent is actively working between milestone messages.

The 30 s timeout alone lets the indicator lapse during one long tool call (a
multi-minute test run) although the agent is still working. So the first call
of a turn also starts a detached keeper that re-sends the typing every 20 s
until the turn ends:
  - run with `stop` from the Stop hook, it kills the keeper and clears the
    typing at once;
  - otherwise the keeper exits by itself when the agent's claude process is
    gone, when no tool has started for KEEPER_IDLE_S (an interrupted turn
    fires no Stop hook), or after KEEPER_MAX_S.

Costs zero LLM tokens (just HTTP calls from this script). Fully fail-open
and non-blocking: any error, missing config, or unknown agent → exit 0 and
let the tool run. NEVER denies a tool.

Wire it in Claude Code settings.json under PreToolUse with a broad matcher,
and the same script with the `stop` argument under Stop.

Env:
    MESH_AGENT     — agent name; must be a key in the bridge config "rooms".
    BRIDGE_CONFIG  — path to the Matrix bridge config.json (default below).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BRIDGE_CONFIG = os.environ.get("BRIDGE_CONFIG", str(Path.home() / "matrix/bridge/config.json"))
TYPING_TIMEOUT_MS = 30000
KEEPER_PERIOD_S = 20
KEEPER_IDLE_S = 15 * 60
KEEPER_MAX_S = 3 * 3600
STATE_DIR = Path(f"/run/user/{os.getuid()}/matrix-typing")


def resolve(agent: str | None = None):
    """(agent, put) where put(typing) sends the typing state, or None."""
    cfg = json.loads(Path(BRIDGE_CONFIG).read_text())
    rooms = cfg.get("rooms", {})

    if agent is None:
        # Identity: inside a real tmux pane ($TMUX set), the pane's session name
        # wins over a possibly-inherited MESH_AGENT (a shared tmux server can
        # carry one agent's MESH_AGENT globally to every pane). Otherwise trust
        # the env. Only accept a name that maps to a room.
        agent = os.environ.get("MESH_AGENT", "").strip().lower()
        if os.environ.get("TMUX"):
            try:
                r = subprocess.run(["tmux", "display-message", "-p", "#S"],
                                   capture_output=True, text=True, timeout=2)
                name = r.stdout.strip().lower()
                if name in rooms:
                    agent = name
            except Exception:
                pass
    if not agent:
        return None

    room = rooms.get(agent)
    if not room:
        return None

    hs = cfg["homeserver"].rstrip("/")
    domain = cfg["bot_user"].split(":", 1)[1]

    tokens = {}
    atf = cfg.get("agent_tokens_file")
    if atf and Path(atf).exists():
        tokens = json.loads(Path(atf).read_text())
    token = tokens.get(agent)
    if not token:
        return None

    user = f"@{agent}:{domain}"
    path = (f"/_matrix/client/v3/rooms/{urllib.parse.quote(room)}"
            f"/typing/{urllib.parse.quote(user)}")

    def put(typing: bool) -> None:
        payload = {"typing": typing}
        if typing:
            payload["timeout"] = TYPING_TIMEOUT_MS
        req = urllib.request.Request(hs + path, data=json.dumps(payload).encode(), method="PUT")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        urllib.request.urlopen(req, timeout=2)  # homeserver is local (tailnet)

    return agent, put


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def claude_ancestor() -> int | None:
    """PID of the claude process that runs this hook (walks up the parents)."""
    pid = os.getppid()
    for _ in range(8):
        try:
            if Path(f"/proc/{pid}/comm").read_text().strip() == "claude":
                return pid
            stat = Path(f"/proc/{pid}/stat").read_text()
            pid = int(stat.rsplit(")", 1)[1].split()[1])
        except Exception:
            return None
        if pid <= 1:
            return None
    return None


def keeper_pid(agent: str) -> int | None:
    try:
        pid = int((STATE_DIR / f"{agent}.pid").read_text())
        return pid if alive(pid) else None
    except Exception:
        return None


def pretool() -> None:
    r = resolve()
    if r is None:
        return
    agent, put = r
    put(True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"{agent}.touch").touch()
    if keeper_pid(agent) is None:
        claude = claude_ancestor()
        if claude is not None:
            subprocess.Popen([sys.executable, os.path.abspath(__file__), "keep", agent, str(claude)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)


def keep(agent: str, claude: int) -> None:
    pidfile = STATE_DIR / f"{agent}.pid"
    pidfile.write_text(str(os.getpid()))
    # SIGTERM from the Stop hook must still run the finally (pidfile cleanup).
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    touch = STATE_DIR / f"{agent}.touch"
    start = time.time()
    try:
        r = resolve(agent)
        if r is None:
            return
        _, put = r
        while True:
            time.sleep(KEEPER_PERIOD_S)
            if not alive(claude) or time.time() - start > KEEPER_MAX_S:
                return
            try:
                if time.time() - touch.stat().st_mtime > KEEPER_IDLE_S:
                    return
            except OSError:
                return
            try:
                put(True)
            except Exception:
                pass
    finally:
        try:
            if pidfile.read_text() == str(os.getpid()):
                pidfile.unlink()
        except Exception:
            pass


def stop() -> None:
    r = resolve()
    if r is None:
        return
    agent, put = r
    pid = keeper_pid(agent)
    if pid is not None:
        os.kill(pid, signal.SIGTERM)
    put(False)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "pretool"
    if mode == "keep":
        try:
            keep(sys.argv[2], int(sys.argv[3]))
        except Exception:
            pass
        return
    try:
        try:
            sys.stdin.read()  # drain stdin so the pipe never stalls
        except Exception:
            pass
        if mode == "stop":
            stop()
        else:
            pretool()
    except Exception:
        pass
    sys.exit(0)  # always allow the tool


if __name__ == "__main__":
    main()
